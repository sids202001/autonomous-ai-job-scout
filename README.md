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

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                 AUTONOMOUS AI JOB APPLICATION ENGINE                        │
└─────────────────────────────────────────────────────────────────────────────┘

                            ┌───────────────────────────┐
                            │   Candidate Profile       │
                            │   & Resume File (PDF)     │
                            └─────────────┬─────────────┘
                                          │
                                          ▼
                            ┌───────────────────────────┐
                            │  Dynamic Document Parser  │
                            │     (PyPDF2 + NLP)        │
                            └─────────────┬─────────────┘
                                          │ Core Skills, Experience,
                                          │ Work Auth & Compensation
                                          ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │                      Multi-Board Job Ingestion                        │
  │   ┌────────────┐    ┌────────────┐    ┌────────────┐    ┌─────────┐   │
  │   │  LinkedIn  │    │ Greenhouse │    │   Lever    │    │  Ashby  │   │
  │   └─────┬──────┘    └─────┬──────┘    └─────┬──────┘    └────┬────┘   │
  └─────────┼─────────────────┼─────────────────┼────────────────┼────────┘
            └─────────────────┼─────────────────┼────────────────┘
                              │ Live Job Listings
                              ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │                     Match & Qualification Filter                      │
  │  • Visa Sponsorship Requirements    • Years of Experience Filter      │
  │  • Keyword & Skill Overlap          • Quantitative Match Score (0–100)│
  └───────────────────────────────────┬───────────────────────────────────┘
                                      │ Qualified Target Roles
                                      ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │                  Asynchronous Playwright Agent                        │
  │  • Launches Real Chromium Browser Sessions                            │
  │  • Navigates Multi-Step ATS Portals & Uploads Resume                  │
  │  • Fills Standard Profile Fields (Contact, Education, Work History)   │
  └──────────────────┬─────────────────────────────────▲──────────────────┘
                     │                                 │
                     │ Unpredictable Screening Prompts │ Synthesized
                     │ & Custom Questionnaires         │ Answers
                     ▼                                 │
  ┌────────────────────────────────────────────────────┴──────────────────┐
  │                Integrated AI Reasoning Engine (LLM)                   │
  │  • Contextual synthesis referencing candidate's actual background     │
  │  • Deterministic Guardrails: Zero hallucinations on work auth/salary  │
  └──────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     │ Final Submission Confirmed
                                     ▼
  ┌──────────────────────────────────┴────────────────────────────────────┐
  │                      Audit & Notification Layer                       │
  │   ┌────────────────────────────────┐ ┌────────────────────────────┐   │
  │   │        SQLite Database         │ │      Telegram Bot API      │   │
  │   │  • Application Audit Trail     │ │  • Real-Time Status Alerts │   │
  │   │  • Role, Company, Timestamp    │ │  • Confirmation Receipts   │   │
  │   └────────────────────────────────┘ └────────────────────────────┘   │
  └───────────────────────────────────────────────────────────────────────┘
```
