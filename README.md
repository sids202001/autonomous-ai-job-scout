# 🤖 Autonomous AI Job Scout (AutoApply)

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Playwright-Browser_Agent-2EAD33?style=for-the-badge&logo=playwright&logoColor=white" alt="Playwright" />
  <img src="https://img.shields.io/badge/FastAPI-REST_Backend-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/SQLite-Local_Storage-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/Telegram-Live_Alerts-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram" />
</p>

An autonomous, guardrailed job discovery and application engine built exclusively for **Greenhouse**, **Ashby**, and **Lever** ATS portals.

Instead of scraping third-party aggregators or firing unverified LLM guesses into forms, AutoApply enumerates live openings directly from official ATS board APIs, filters roles before opening a browser (`jd_precheck`), fills forms deterministically from a single canonical source of truth (`profile.json`), writes strictly fact-grounded open-text answers (`grounded_essay.py`), and runs a judge-only **Pre-Submit Review Gate** (`presubmit_review.py`) before clicking submit. Every safeguard defaults to halting cleanly rather than guessing.

<p align="left">
  <img src="https://img.shields.io/badge/ATS_Only-Greenhouse_%7C_Ashby_%7C_Lever-2EAD33?style=flat-square" alt="ATS Only" />
  <img src="https://img.shields.io/badge/Pre--Submit_Gate-Judge--Only_Review-7952B3?style=flat-square" alt="Pre-Submit Gate" />
  <img src="https://img.shields.io/badge/Open--Text-Fact--Grounded_Writer-FF6F00?style=flat-square" alt="Grounded Writer" />
  <img src="https://img.shields.io/badge/Retry_Hygiene-Max_2_Attempts-007ACC?style=flat-square" alt="Retry Hygiene" />
  <img src="https://img.shields.io/badge/Tests-369_Passing-28A745?style=flat-square" alt="369 Tests" />
</p>

---

## 🏗️ System Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                    AUTONOMOUS AI JOB SCOUT (AUTOAPPLY)                      │
└─────────────────────────────────────────────────────────────────────────────┘

                      ┌───────────────────────────────────┐
                      │   Single Source of Truth          │
                      │   • profile.json (gitignored)     │
                      │   • Verified Resume PDF & Facts   │
                      └─────────────────┬─────────────────┘
                                        │
                                        ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │            Official ATS Discovery & Board API Enumeration                 │
  │    ┌───────────────┐      ┌───────────────┐      ┌───────────────┐        │
  │    │  Greenhouse   │      │     Ashby     │      │     Lever     │        │
  │    │   Board API   │      │  Posting API  │      │  Postings API │        │
  │    └───────┬───────┘      └───────┬───────┘      └───────┬───────┘        │
  │        (Official ATS APIs only, no third-party aggregators, <= 10 req/min) │
  └────────────┼──────────────────────┼──────────────────────┼────────────────┘
               └──────────────────────┼──────────────────────┘
                                      ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                 Pre-Browser Eligibility & Qualification                   │
  │  • jd_precheck.py      — Sponsorship bars, language reqs, grad-year match │
  │  • experience_gate.py  — Strict years-of-experience filter                │
  │  • location_gate.py    — US location & remote/hybrid eligibility          │
  │  • dedup_gate.py       — Canonical ATS job-ID deduplication               │
  │  • company_cap.py      — Max 2 apps / 180 days & 60-min company cooldown  │
  └───────────────────────────────────┬───────────────────────────────────────┘
                                      │ Qualified Roles
                                      ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │             Playwright Form Engine + Deterministic Guards                 │
  │  • form_filler.py + form_guards.py                                        │
  │    - Canonical-answer solver backed by profile.json                       │
  │    - Clear-before-fill & read-back verification                           │
  │    - Least-permissive cookie banner dismissal                             │
  │    - Visa support / sponsorship mapping ("Yes" -> "H-1B")                 │
  │    - Safe-halt on ambiguous/ungrounded prompts (never guesses)            │
  │  • grounded_essay.py                                                      │
  │    - Grounded open-text writer (Why company, fit, skills, projects)       │
  │    - Traceable to verified candidate facts + JD / company about page      │
  └───────────────────────────────────┬───────────────────────────────────────┘
                                      │ Filled Form DOM
                                      ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                 PRESUBMIT_REVIEW Gate (Judge-Only)                        │
  │  • presubmit_review.py + presubmit_gate.py                                │
  │    - Deterministic checklist + optional judge-only LLM verifier           │
  │    - NEVER writes, mutates, or invents form values                        │
  │    - Blocks raw HTML 'on', blank required fields, EEO leakage, bad prose  │
  │    - Any FAIL or UNCERTAIN -> immediate PRESUBMIT_REVIEW_FAILED halt      │
  └───────────────────────────────────┬───────────────────────────────────────┘
                                      │ 100% PASS Only
                                      ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │             Submission, Retry Hygiene & Audit Layer                       │
  │  • retry_queue.py      — Max 2 attempts/role (4h cooldown), 24h park      │
  │  • code_fetcher.py     — Automated Gmail 2FA verification code pickup     │
  │  • SQLite + Audit Logs — Structured outcome ledger & submission receipts  │
  │  • Telegram Notifier   — Verified receipts & company-cap notices only     │
  └───────────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 Supported Portals (Greenhouse / Ashby / Lever Only)

Discovery and enumeration (`app/services/discovery_engine.py`, `app/services/job_scraper.py`, `app/services/company_registry.py`) strictly target official ATS board APIs:

| ATS | Official API Endpoint | Rate Limit |
|---|---|---|
| **Greenhouse** | `boards-api.greenhouse.io` / `boards.greenhouse.io/<slug>` | 10 req/min |
| **Ashby** | `api.ashbyhq.com/posting-api/job-board/<slug>` | 10 req/min |
| **Lever** | `api.lever.co/v0/postings/<slug>` | 10 req/min |

- Only official **Greenhouse**, **Ashby**, and **Lever** ATS APIs are used for discovery, scraping, and application.
- Non-Greenhouse/Ashby/Lever portals are classified unsupported once and skipped.

---

## 🧭 Canonical-Answer Rules & Form Guards (`form_guards.py`)

`profile.json` is the **single source of truth** for all deterministic answers. The repo includes a sanitized template ([`profile.example.json`](profile.example.json)) while keeping your real `profile.json` gitignored.

Core rules enforced by `form_filler.py` and `app/services/form_guards.py`:

- **Clear-Before-Fill & Read-Back Verification**: Every input is cleared before filling and read back to confirm exact match; mismatches raise `FILL_VERIFICATION_FAILED`.
- **Visa Support & Sponsorship Mapping**: Any phrasing of sponsorship, visa support, visa assistance, or immigration sponsorship resolves canonically (`Yes` -> `H-1B`, with `F-1 OPT (STEM Eligible)` for status details). Missing visa options halt with `VISA_OPTION_MISSING`.
- **Referral Source ("How did you hear?")**: Always prefers `Company Website` / careers page. If no valid source exists, halts with `SOURCE_OPTION_MISSING`.
- **Relocation & Proximity**: Answers current-location proximity strictly from the candidate's canonical location and relocation willingness from `profile.json`, halting on ambiguous relocation timing or assistance prompts.
- **Start Date & Compensation**: Computes start dates dynamically (`submit_date + 1 month`) and pulls salary/hourly expectations strictly from `profile.json`.
- **Screening Canonical Answers**: Explicit `screening_canonical` entries in `profile.json` govern recurring company/domain questions (prior employer history, travel willingness, authorization expiration, spoken languages, EEO, etc.).
- **Safe-Halt Over Guessing**: Contradictory premises (`PREMISE_MISMATCH`), missing required profile fields (`ADDRESS_REQUIRED`), fine-grained consent ambiguity (`CONSENT_AMBIGUOUS`), or ungrounded questions halt cleanly and park the role rather than fabricating answers.
- **Cookie Banners**: Dismissed least-permissive first (reject/necessary-only before accept).

---

## ✍️ Grounded Open-Text Answer Writer (`grounded_essay.py`)

`app/services/grounded_essay.py` handles open-text and descriptive form questions (e.g., *"Why [company]?"*, *"Why are you a good fit for this role?"*, *"Describe your experience with Python/AWS/AI"*, *"Tell us about a challenging project or bug you fixed"*):

1. **Strict Grounding**: Every factual claim must trace directly to the candidate's verified record (`profile.json`, resume, GitHub, portfolio) and, for company/role questions, the job description and company about page.
2. **Achievement & Metric Verification**: Specific achievements or metrics (e.g., latency/MTTD improvements, systems built) must match the verified candidate record — mentioning a tool or skill name alone never grounds an achievement claim.
3. **Zero Generic Filler**: Generic praise or unverified claims are rejected.
4. **Hard Limits (Always Halt)**:
   - *"Is there anything else?"* (left blank if optional, halts if required)
   - Skill/language self-ratings (e.g., 1–10 scales, unrated languages)
   - Sensitive personal facts not in `profile.json` (DOB, SSN, age)
   - Personal commitments (working weekends, night shifts, pay cuts)
   - Non-standard legal attestations beyond standard application consent

---

## ✅ Pre-Submit Review Gate (`presubmit_review.py` & `presubmit_gate.py`)

Before any submit button is clicked, the engine extracts every visible form field from the DOM and runs a two-layer **judge-only** review gate:

1. **Deterministic Checklist (`run_presubmit_review`)**:
   - Rejects raw HTML default `"on"` values (forces visible label read-back).
   - Rejects blank/placeholder required fields and unselected Yes/No toggles.
   - Verifies profile/GitHub/portfolio URLs, home address, location, work authorization, visa sponsorship/type, referral source, salary, start date, education months & years, EEO consistency (preventing cross-field leakage), and banned boilerplate prose.
   - Verifies open-text answers against the grounded fact bank.
2. **Judge-Only LLM Verifier (`llm_judge_fields_against_record`)**:
   - Audits every filled field against `profile.json`, resume text, and essay grounding sources.
   - **Never writes into the form, never mutates values, and never invents answers.**

Submission proceeds **only** if every field is `PASS`. Any `FAIL` or `UNCERTAIN` verdict halts immediately with `PRESUBMIT_REVIEW_FAILED`, naming the exact field, filled value, and reason.

---

## 🚦 Pre-Browser Eligibility (`jd_precheck.py`) & Retry Hygiene (`retry_queue.py`)

### JD Pre-Check (`app/services/jd_precheck.py`)
Evaluates role title and description **before** launching Playwright:
- **Sponsorship / Citizenship / Clearance Bars**: Skips roles explicitly barring sponsorship or requiring U.S. citizenship / active clearance.
- **Language Requirements**: Skips roles requiring spoken/written fluency in languages not on the candidate's record.
- **Graduation-Year Cohort Windows**: Skips campus/new-grad postings restricted to future graduation cohorts that do not match the candidate's degree dates.

### Retry Queue & Per-Company State Isolation (`app/services/retry_queue.py`)
- **Max 2 Attempts per Role**: 1 initial attempt + at most 1 automatic retry after a **4-hour cooldown** (`RETRY_COOLDOWN_SECONDS = 14400`).
- **Spam-Flag Exclusion**: Spam-flagged roles are never auto-retried; they route to manual review to prevent duplicate submissions.
- **Terminal Parking**: Terminal halts park the role for at least 24 hours (`TERMINAL_PARK_SECONDS = 86400`), deduplicated by canonical ATS job ID.
- **Browser Hygiene**: Clears cookies, cache, and site data between companies.

---

## 🛡️ Politeness, Rate Limiting & Zero-Bypass Security Policy

### Politeness & Pacing (`app/services/politeness.py`)

| Setting | Default | Purpose |
|---|---|---|
| `MAX_APPLICATIONS_PER_DAY` | `50` | Daily submission cap |
| `MIN_DELAY_BETWEEN_APPS` / `MAX_DELAY_BETWEEN_APPS` | `150s` | Fixed deterministic pacing between submit clicks |
| `ACTIVE_HOURS_START` / `ACTIVE_HOURS_END` | `09:00`–`23:00` | Active application window |
| `MAX_APPLICATIONS_PER_COMPANY` | `2` / `180d` | Per-company cap with 1-hour cooldown |
| Per-ATS API limit | `10 req/min` | Separate rate limiter per ATS domain + exponential backoff on 429/5xx |

### Universal Zero-Bypass Policy
AutoApply strictly honors portal security boundaries:
- **Zero CAPTCHA/WAF Evasion**: Never attempts to solve, click through, or bypass reCAPTCHA, hCaptcha, Cloudflare Turnstile, Arkose Labs, DataDome, PerimeterX, Kasada, or AWS WAF.
- **Immediate Clean Halt**: If a security challenge is detected at pre-fill, mid-fill, or post-submit (~15s interstitial polling window), the browser context closes immediately and logs `SECURITY_CHALLENGE` for manual candidate submission in the end-of-run digest.

### Telegram Notifications & Gmail 2FA
- **Telegram Allowlist**: Individual alerts are sent only for **verified submission receipts** and **per-company cap/cooldown notices**; all skips, halts, and challenges are collated into the end-of-run digest.
- **Gmail Verification Codes (`app/services/code_fetcher.py`, `app/services/gmail_client.py`)**: Automatically retrieves 4–8 digit ATS verification PINs via Gmail App Password (IMAP SSL) or read-only OAuth 2.0 (`gmail.readonly`).

---

## ⚙️ Local Setup

1. **Install dependencies**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Create your local profile** (gitignored):
   ```bash
   cp profile.example.json profile.json
   # Fill in your real personal info, education, experience, and screening_canonical answers
   ```

3. **Configure `.env`** (gitignored):
   ```env
   APPLICANT_EMAIL=your-email@gmail.com
   APPLICANT_PHONE=+15550100000
   RESUME_PATH=/absolute/path/to/your_resume.pdf

   # Optional: LLM judge / verification
   GEMINI_API_KEY=your_gemini_api_key

   # Optional: Gmail 2FA pickup (App Password)
   GMAIL_USER=your-email@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

   # Optional: Telegram alerts
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id

   # Execution controls
   AUTO_SUBMIT=true
   DRY_RUN=false
   ```

4. **Run the test suite** (369 tests):
   ```bash
   ./venv/bin/pytest -q
   ```

5. **Run the FastAPI dashboard / scheduler**:
   ```bash
   ./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
