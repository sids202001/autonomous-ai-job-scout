# 🤖 Automated Job Application Engine

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Playwright-Browser_Agent-2EAD33?style=for-the-badge&logo=playwright&logoColor=white" alt="Playwright" />
  <img src="https://img.shields.io/badge/FastAPI-REST_Backend-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/SQLite-Local_Storage-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/Telegram-Live_Alerts-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram" />
</p>

Job hunting in tech has become an exhausting, repetitive numbers game. Spending hours every day scrolling job boards, opening dozens of browser tabs, copy-pasting the same resume details into clunky ATS portals, and re-answering the exact same screening questions over and over again drains time that should be spent preparing for interviews. I built this autonomous AI job application engine to take over the entire application lifecycle on autopilot.

You give the system your resume, and it handles the rest: it automatically scrapes live software engineering openings across LinkedIn, Greenhouse, Lever, and Ashby, uses dynamic document parsing to extract your core technical skills, and filters out jobs that demand excessive experience or lack visa sponsorship. From there, an asynchronous browser agent launches real Chromium sessions to navigate the application pages, upload your resume, and fill out multi-step forms. When hit with unpredictable, open-ended screening prompts ("Why are you a fit for this role?", technical experience summaries, or custom questionnaire dropdowns), an integrated AI reasoning engine references your actual candidate background and dynamically synthesizes tailored, professional answers in real time—enforcing strict guardrails on work authorization and compensation to prevent any hallucinated answers. The system submits applications autonomously, records every entry in a local SQLite database, and broadcasts real-time status alerts to a Telegram channel, turning a grueling 20-minute manual application chore into a seamless background workflow that applies to targeted roles while you sleep.

<p align="left">
  <img src="https://img.shields.io/badge/Autonomous_AI_Agents-LLM_Guardrails-7952B3?style=flat-square" alt="AI Agents" />
  <img src="https://img.shields.io/badge/Playwright-Automated_Chromium-2EAD33?style=flat-square&logo=playwright&logoColor=white" alt="Playwright" />
  <img src="https://img.shields.io/badge/PyPDF2-Resume_Extraction-FF6F00?style=flat-square" alt="PyPDF2" />
  <img src="https://img.shields.io/badge/AsyncIO-Event_Loop-007ACC?style=flat-square" alt="AsyncIO" />
  <img src="https://img.shields.io/badge/SQLite-Persistence-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/Telegram_Bot_API-Instant_Notifications-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="Telegram API" />
</p>

---

## Architecture

```
  Browser (index.html SPA)
       │
       │  REST API (FastAPI)
       ▼
  Python Backend (uvicorn + FastAPI)
       │
       ├─ /api/profile        → Profile CRUD + resume upload/parse
       ├─ /api/filters        → Search filter management
       ├─ /api/jobs           → Job search + scoring (0–100)
       ├─ /api/applications   → Application tracking
       │
       ├─ Services
       │   ├─ job_scraper.py   → Finds jobs from configured sources
       │   ├─ job_matcher.py   → Scores jobs against your profile
       │   ├─ auto_filler.py   → Opens a browser and fills forms
       │   └─ resume_parser.py → Extracts text from PDF resumes
       │
       └─ SQLite (data/autoapply.db)
```

---

## Getting Started

### Prerequisites
- Python 3.10+
- pip

### Installation

```bash
# 1. Clone the repo
cd autonomous-ai-job-scout

# 2. Set up a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers (needed for auto-fill)
playwright install chromium
```

### Run

```bash
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** and you're good to go.

---

## Walkthrough

### 1. Create Your Profile
Go to the Profile tab, fill in your details, and upload your resume as a PDF. The system extracts the text and pulls out your skills automatically.

### 2. Set Up Filters
Head to the Filters tab. Add your search criteria — job title keywords, preferred location, salary range, experience level, job type (remote, hybrid, etc). You can save multiple filter sets.

### 3. Search for Jobs
Back on the Dashboard, click "Search New Jobs." The scraper fetches listings and the matcher scores every one of them against your profile.

### 4. Review Your Matches
Jobs show up sorted by match score. Each card has the salary, location, required skills, and how well you fit. You can filter by application status too — Saved, Applying, Applied, Interview, Offer.

### 5. Auto-Fill & Apply
Click "Auto-Fill & Apply" on any job. A visible Chromium browser opens, goes to the application page, and fills in your info. It **does not submit** — it waits for you to look everything over and hit submit yourself.

---

## Project Structure

```
sid_job_scrip/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI entry point
│   ├── database.py           # SQLite + SQLAlchemy async setup
│   ├── models.py             # ORM models (7 tables)
│   ├── schemas.py            # Pydantic request/response schemas
│   ├── routers/
│   │   ├── profile.py        # Profile CRUD + resume upload
│   │   ├── jobs.py           # Job search + listing
│   │   ├── applications.py   # Application tracking
│   │   └── filters.py        # Search filter management
│   ├── services/
│   │   ├── job_scraper.py    # Finds and fetches job listings
│   │   ├── job_matcher.py    # Multi-factor scoring
│   │   ├── auto_filler.py    # Playwright form filler
│   │   └── resume_parser.py  # PDF text extraction
│   └── static/
│       ├── index.html         # Dashboard SPA
│       ├── styles.css         # Dark-mode styles
│       └── app.js             # Frontend logic
├── data/
│   └── resumes/               # Where uploaded resumes go
├── config.py                  # App settings
├── requirements.txt
└── README.md
```

---

## Plugging In Real Job APIs

Right now the scraper generates realistic simulated listings so you can test the full flow without any API setup. When you're ready to connect to real job boards, edit `app/services/job_scraper.py`:

- **SerpAPI** (Google Jobs) — aggregates results from most major boards
- **Indeed API** — direct Indeed integration
- **LinkedIn API** — needs OAuth, bit more involved to set up

There are commented-out code examples in `job_scraper.py` showing how to wire each one up.

---

## Things to Keep in Mind

- Auto-fill **never** submits for you. It fills the form and stops.
- Some job sites use CAPTCHAs or have unusual form layouts that can trip up the auto-filler.
- Check the terms of service for any job board before connecting a real API.
- All your data stays on your machine — SQLite database at `data/autoapply.db`.

---

## License

MIT
