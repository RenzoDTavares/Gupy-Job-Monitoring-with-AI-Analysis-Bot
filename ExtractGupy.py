import sqlite3
import requests
import google.genai as genai
from google.genai import Client
from datetime import datetime
import time
import html
from requests.exceptions import ConnectionError, Timeout

# --- INITIAL CONFIGURATION ---
API_URL = "https://portal.api.gupy.io/api/v1/jobs"
client = Client(api_key="INSERT_KEY")
DB_JOBS_NAME = "vagas.db"
WAIT_TIME = 300
TELEGRAM_TOKEN = "INSERT_TOKEN"
TELEGRAM_CHAT_ID = "INSERT_CHAT_ID"

system_prompt = (
    "You are a recruitment assistant. Your task is to analyze an job vacancy description "
    "and provide a concise, high-quality summary (maximum 4 topics). "
    "The summary must highlight key responsibilities, mandatory requirements (hard skills) "
    "and benefits/differentials that the candidate must know before applying. "

    "**The output must be RAW TEXT. Do not use Markdown formatting characters like * (asterisk), ** (bold), or # (header). **EACH TOPIC MUST BE SEPARATED BY TWO NEWLINE CHARACTERS (\\n\\n).**"
)

# --- TELEGRAM FUNCTION ---

def send_telegram_message(message):
    """
    Sends a formatted message to Telegram.

    Uses HTML parse mode for robust formatting, especially for the
    Gemini summary content.
    """
    if not message:
        return
    url = f"https://api.telegram.com/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        print("   [TELEGRAM] Message sent successfully!")
    except requests.exceptions.RequestException as e:
        print(f"❌ [TELEGRAM] Error sending message to Telegram: {e}")


# --- AI ANALYSIS FUNCTION ---

def analyze_job_with_ai(client, full_description):
    """
    Uses Gemini to summarize the job description with up to 3 retries (Exponential Backoff).

    This function implements a retry mechanism to handle transient API errors (like 503 UNAVAILABLE),
    ensuring resilience in the job monitoring process.
    """
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[system_prompt, full_description],
            )
            return response.text

        except Exception as e:
            error_message = str(e)

            if "503 UNAVAILABLE" in error_message or attempt == MAX_RETRIES - 1:
                print(f"           [IA ERROR]: Failure on attempt {attempt + 1}. Error: {error_message}")
                if attempt == MAX_RETRIES - 1:
                    return f"\n[Gemini Analysis - CRITICAL FAILURE after {MAX_RETRIES} attempts. Service unavailable: {error_message}]"

            sleep_time = 2 ** attempt
            print(f"           [IA RETRY]: Model overloaded (503). Waiting {sleep_time}s before retrying...")
            time.sleep(sleep_time)

    return "\n[Gemini Analysis - UNEXPECTED FAILURE. Try again later.]"

# --- DB FUNCTIONS ---

def check_network_connection(timeout_s=10):
    """
    Checks network connectivity with a reliable endpoint (Google).

    Crucial for QA and maintaining the system's operational resilience.
    """
    try:
        requests.get("https://www.google.com", timeout=timeout_s)
        return True
    except (ConnectionError, Timeout):
        print("❌ [NETWORK] No network connection. System waiting.")
        return False

def clear_jobs_db():
    """Performs a Hard Reset, deleting all content from the found_jobs table."""
    try:
        con = sqlite3.connect(DB_JOBS_NAME)
        cur = con.cursor()
        cur.execute("DELETE FROM found_jobs")
        con.commit()
        con.close()
        print("✅ [DB - HARD RESET] Table 'found_jobs' successfully cleared.")
    except sqlite3.Error as e:
        print(f"❌ [DB FATAL] Error clearing the database: {e}")

def initialize_jobs_db():
    """Initializes the SQLite database, creating the 'found_jobs' table with a composite primary key."""
    con = sqlite3.connect(DB_JOBS_NAME)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS found_jobs (
            gupy_id INTEGER NOT NULL,
            search_title TEXT NOT NULL,
            job_name TEXT NOT NULL,
            work_model TEXT,
            publish_date TEXT,
            job_url TEXT,
            ia_summary TEXT,
            extraction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (gupy_id, search_title)
        )
    """)
    con.commit()
    con.close()

def has_search_term_data(search_title):
    """Checks if there is any data saved for a specific search term."""
    con = sqlite3.connect(DB_JOBS_NAME)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM found_jobs WHERE search_title = ? LIMIT 1", (search_title,))
    exists = cur.fetchone() is not None
    con.close()
    return exists

def check_job_exists(gupy_id, search_title):
    """
    Checks if the job has already been analyzed and saved IN THE DB FOR THIS SPECIFIC TERM.

    Uses the composite key (gupy_id, search_title) to prevent re-notifying for the same job under the same search.
    """
    con = sqlite3.connect(DB_JOBS_NAME)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM found_jobs WHERE gupy_id = ? AND search_title = ?", (gupy_id, search_title))
    exists = cur.fetchone() is not None
    con.close()
    return exists

def save_job_to_db(data):
    """Saves or updates a job record in the database using REPLACE INTO."""
    con = sqlite3.connect(DB_JOBS_NAME)
    cur = con.cursor()
    try:
        cur.execute("""
            REPLACE INTO found_jobs (
                gupy_id, search_title, job_name, work_model,
                publish_date, job_url, ia_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, data)
        con.commit()
        print(f"   [DB] Job {data[0]} saved/updated successfully.")
    except sqlite3.Error as e:
        print(f"❌ [DB] Error saving job: {e}")
    finally:
        con.close()

# --- CLIENT EXTRACTION ---
con = sqlite3.connect("clientes.db")
cur = con.cursor()
cur.execute("SELECT id, role FROM cliente")
result = cur.fetchall()
con.close()

# --- SEARCH_JOB (QA FLOW AND EXECUTION CONTROL) ---

def search_job(search_id, search_title):
    """
    Fetches job vacancies from the Gupy API based on the search title.

    Manages two main execution modes:
    1. Initial population (saves the first page, no notification).
    2. Continuous monitoring (checks all pages until a duplicate is found, sends notifications).
    """
    offset = 0
    limit = 10
    jobs_per_page = 10

    is_first_run = not has_search_term_data(search_title)

    execution_mode = "Initial Run (1st Page Only - No Notification)" if is_first_run else "Continuous Monitoring (Until Duplication - With Notification)"
    print(f"[Mode] {execution_mode}")

    while True:

        print(f"\n🔄 Consulting page: {int(offset/limit) + 1} for '{search_title}'...")

        try:
            r = requests.get(
                API_URL,
                params={
                    "jobName": search_title,
                    "limit": limit,
                    "offset": offset
                },
                timeout=15
            )

            if r.status_code != 200:
                print(f"❌ Search failed. Status Code: {r.status_code}. Waiting {WAIT_TIME} seconds.")
                time.sleep(WAIT_TIME)
                return

            data = r.json()
            job_list = data.get('data', [])

        except requests.exceptions.RequestException as e:
            print(f"❌ HTTP request failed: {e}. Waiting {WAIT_TIME} seconds.")
            time.sleep(WAIT_TIME)
            return


        if not job_list:
            print(f"✅ End of jobs for '{search_title}'. Waiting {WAIT_TIME} seconds.")
            time.sleep(WAIT_TIME)
            return

        new_jobs_found = 0

        for job_data in job_list:
            gupy_id = job_data.get('id')

            # --- DUPLICATE STOP CRITERION (Continuous Monitoring Mode) ---
            if not is_first_run and check_job_exists(gupy_id, search_title):
                print(f"⛔ Job ID {gupy_id} ('{job_data.get('name')}') already exists for the term '{search_title}'. Immediate stop...")
                return

            # --- PROCESSING AND SAVING ---
            new_jobs_found += 1

            workplace_type = job_data.get('workplaceType')
            if workplace_type == 'remote':
                work_model = "Remote"
            else:
                city = job_data.get("city", "CITY_NOT_INFORMED")
                state = job_data.get("state", "STATE_NOT_INFORMED")

                if workplace_type == 'hybrid':
                    work_model = f'Hybrid - {city} - {state}'
                else:
                    work_model = f'Onsite - {city} - {state}'

            description = job_data.get('description', 'Description not provided.')

            # --- CONDITIONAL AI CALL (QA ENHANCEMENT) ---
            if is_first_run:
                print("    [AI]: INITIAL POPULATION MODE ACTIVE. AI analysis IGNORED.")
                ia_summary = "[AI analysis ignored in Initial Population mode]"
            else:
                print("    [AI]: MONITORING MODE ACTIVE. Sending for AI analysis...")
                ia_summary = analyze_job_with_ai(client, description)
            # ----------------------------------------------------

            date_raw = job_data.get('publishedDate')
            try:
                date_obj = datetime.fromisoformat(date_raw.replace('Z', '+00:00'))
                formatted_date = date_obj.strftime("%d/%m/%Y")
            except Exception:
                formatted_date = date_raw

            db_record = (
                gupy_id,
                search_title,
                job_data.get('name'),
                work_model,
                formatted_date,
                job_data.get('jobUrl'),
                ia_summary
            )

            save_job_to_db(db_record)

            # --- MESSAGE CONSTRUCTION AND SENDING ---

            if not is_first_run:

                link_vaga = job_data.get('jobUrl')

                # 1. Prepare AI summary content (clean up Gemini's possible greeting and format bullets)
                lines = ia_summary.split('\n')
                content_lines = []
                is_content_started = False

                for line in lines:
                    line_stripped = line.lstrip().lstrip('*').lstrip('•').strip()

                    if any(phrase in line_stripped for phrase in ["Here is the concise summary", "Aqui está o resumo conciso"]):
                        continue

                    if line_stripped:
                        is_content_started = True

                    if is_content_started and line_stripped:
                        # Replace Markdown bold (**) with HTML <b>
                        line_formatted = line_stripped.replace('**', '<b>').replace('<b><b>', '')

                        content_lines.append(f"• {line_formatted}")

                # 2. Join lines, escape HTML special characters, and convert newlines to <br>
                final_resumo_formatado = "\n".join(content_lines)
                final_resumo_formatado = html.escape(final_resumo_formatado).replace('\n', '<br>')
                final_resumo_formatado = final_resumo_formatado.replace('&lt;br&gt;', '<br>').replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')

                # 3. Construct the final message using HTML tags
                message = (
                    f"🚨 <b>ALERT: NEW JOB FOUND (GUPY)!</b> 🚨\n\n"
                    f"<b>Search:</b> {html.escape(search_title)}\n\n"
                    f"<b>Job:</b> <a href='{link_vaga}'>{html.escape(job_data.get('name'))}</a>\n"
                    f"<b>Company:</b> {html.escape(job_data.get('careerPageName'))}\n"
                    f"<b>Location:</b> {html.escape(work_model)}\n\n"
                    f"<b>Summary:</b><br>{final_resumo_formatado}"
                )

                send_telegram_message(message)

            # Console output
            print(f" - Job ID: {gupy_id}, Title: {job_data.get('name')}")
            print(f" - Work Model: {work_model}, Published: **{formatted_date}**")
            print(f" - Job URL: {job_data.get('jobUrl')}")
            print(f" - **Quality Summary (Gemini):**{ia_summary}\n")
            print("--------------------------------------------------")

            # --- STOP CRITERION IN 1ST RUN AFTER 1ST PAGE ---
            if is_first_run and new_jobs_found >= jobs_per_page:
                print(f"✅ Initial run for '{search_title}' complete. 1st page saved ({new_jobs_found} jobs).")
                return

        # --- PAGINATION LOGIC (ONLY CONTINUOUS MODE) ---
        if not is_first_run and new_jobs_found < jobs_per_page:
            print(f"✅ Page {int(offset/limit) + 1} processing finished. All recent jobs were saved.")
            return

        offset += limit

# --- MAIN EXECUTION (CONTROL LOOP WITH NETWORK RESILIENCE) ---
if __name__ == "__main__":
    """
    The main control loop for the job monitor.

    It initializes the DB and runs an infinite loop that includes:
    1. Network QA check and resilience loop.
    2. Conditional Hard Reset (contingency) if a network failure was detected.
    3. Iteration over all search terms, calling search_job for each.
    4. A wait period defined by WAIT_TIME.
    """
    initialize_jobs_db()

    while True:
        network_failure = False

        # STEP 1: NETWORK CHECK AND WAITING LOOP (RESILIENCE)
        if not check_network_connection():
            print(f"❌ [NETWORK QA]: Connection lost. Starting wait loop ({time.strftime('%H:%M:%S')}).")
            network_failure = True

            while not check_network_connection(timeout_s=30):
                print("    ... Network unavailable. Waiting 60 seconds before rechecking.")
                time.sleep(60)

            print("✅ [NETWORK QA]: Connection RESTORED. Applying contingency.")

        # STEP 2: CONDITIONAL DB CLEANUP (CONTINGENCY)
        if network_failure:
            print("💣 [CONTINGENCY]: Clearing DB (Hard Reset) to force re-extraction and repopulation.")
            clear_jobs_db()

        print(f"\n==================================================")
        print(f"🚀 STARTING EXTRACTION CYCLE: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        print(f"==================================================")

        for query in result:
            search_id = query[0]
            search_title = query[1]
            print(f"\n[CLIENT ID: {search_id}] Searching for: **{search_title}**")
            search_job(search_id, search_title)

        print(f"\n💤 All clients processed. System waiting for {WAIT_TIME} seconds...")

        time.sleep(WAIT_TIME)
