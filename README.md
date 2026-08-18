# Autonomous AI Job Scout

A job hunting tool that searches for openings, scores them against your resume, and auto-fills application forms using Playwright — so you just review and hit submit.

### How It Works
> **"Search once, match automatically, apply with one click."**
> 
> Job hunting is tedious. You scroll through listings, copy-paste the same info into a dozen forms, and lose track of where you applied. I built this to skip all that:
> 1. **Set up your profile** — upload your resume, add your skills and experience, set your filters (keywords, location, salary range). Everything gets saved in a local SQLite database.
> 2. **Smart matching** — hit "Search New Jobs" and the engine pulls listings, then scores each one from 0 to 100 based on how well it fits your skills, salary expectations, location, and experience level. Best matches float to the top.
> 3. **Auto-fill and review** — pick a job, click "Auto-Fill & Apply." A real Chromium browser window opens up, navigates to the application, fills in your info, and then stops. It doesn't submit anything — you review first, then submit yourself.

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
