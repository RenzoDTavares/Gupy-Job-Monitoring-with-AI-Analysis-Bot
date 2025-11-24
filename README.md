# 🤖 Gupy Job Monitoring and AI Analysis Bot

## Overview

This project implements a Python application designed for the continuous extraction and analysis of job vacancies from the Gupy public API. It operates on a fixed schedule, employing Gemini AI for structured summarization of new job descriptions and utilizing the Telegram API for real-time notification alerts. 

## ✨ Key Features

* **Continuous Monitoring Loop:** Executes extraction cycles periodically based on `WAIT_TIME`.
* **Database Synchronization (SQLite):** Maintains state using a local SQLite database (`vagas.db`) to track previously processed jobs.
* **Duplication Control:** Prevents repeat notifications by checking the composite primary key (`gupy_id` and `search_title`) before processing any vacancy.
* **AI-Driven Summarization:** Integrates the Gemini model to condense long job descriptions into structured points (responsibilities, mandatory skills, benefits).
* **Network Resilience and Contingency:** Implements a network check (`check_network_connection`) and a **conditional hard reset** strategy to ensure recovery and prevent data loss following a network outage.
* **API Error Handling:** Uses **Exponential Backoff** logic within the `analyze_job_with_ai` function to manage transient API overload (503) errors.

---

## ⚙️ How It Works (Technical Flow)

The system executes a cycle controlled by the main loop (`if __name__ == "__main__"`):

### 1. Pre-Cycle Validation (QA)
Before starting extraction, the system verifies network connectivity. If a recent network failure occurred, the primary job database (`vagas.db`) is completely cleared (`clear_jobs_db`). This contingency forces a **re-extraction and repopulation** cycle, ensuring all data missed during downtime is captured and reprocessed.

### 2. Extraction and Mode Selection
The script iterates through configured search terms (fetched from `clientes.db`). For each term, it operates in one of two modes, determined by the presence of historical data:

| Mode | Criteria | Action |
| :--- | :--- | :--- |
| **Initial Population** | No previous data for the search term. | Fetches and saves only the first page. **AI Analysis and Notification are skipped.** |
| **Continuous Monitoring** | Historical data exists. | Fetches pages sequentially. **AI Analysis and Notification are enabled.** |

### 3. Duplication Check and Termination
In Continuous Monitoring mode, the pagination continues until a job's composite key is found in the database. This signals the end of new vacancies for that search term, and the process terminates for the current term.

### 4. Analysis and Notification
For new jobs found during Continuous Monitoring:
1.  The job description is sent to the `analyze_job_with_ai` function.
2.  The raw AI summary is cleaned (removing intros/greetings) and formatted using **HTML**.
3.  The complete job alert (with the AI summary) is sent to the configured **Telegram Chat ID**.

---

## 🚀 Setup and Execution

### Prerequisites

* **Python 3.x**
* **Gemini API Key**
* **Telegram Bot Token** and **Chat ID**
* **Local SQLite Database** named `clientes.db` containing a `cliente` table with the `role` (search term) column.

### Installation

```bash
pip install requests google-genai
```

### Configuration (Sensitive Data)
All sensitive keys must be configured in the INITIAL CONFIGURATION section of the script or, preferably, loaded via environment variables (e.g., using a .env file).

```bash
client = Client(api_key="INSERT_KEY")
TELEGRAM_TOKEN = "INSERT_TOKEN"
TELEGRAM_CHAT_ID = "INSERT_CHAT_ID"
```

### Execution

```bash
python ExtractGupyEn.py
```
