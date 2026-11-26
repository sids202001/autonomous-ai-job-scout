"""
AutoApply - Configuration Module
Loads settings from .env file using python-dotenv.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment variables."""
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_NAME: str = "AutoApply - Job Automation Dashboard"
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{os.path.join(_BASE_DIR, 'data', 'autoapply.db')}")
    STATIC_DIR: str = os.path.join(_BASE_DIR, "app/static")
    DATA_DIR: str = os.getenv("DATA_DIR", os.path.join(_BASE_DIR, "data"))
    UPLOAD_DIR: str = os.path.join(_BASE_DIR, "data/resumes")
    SCREENSHOT_DIR: str = os.path.join(_BASE_DIR, "data/screenshots")
    BROWSER_STATE_DIR: str = os.path.join(_BASE_DIR, "data/browser_state")
    HOST: str = "0.0.0.0"
    PORT: int = int(os.getenv("APP_PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
    
    # Applicant info from env vars
    APPLICANT_EMAIL: str = os.getenv("APPLICANT_EMAIL", "")
    APPLICANT_PHONE: str = os.getenv("APPLICANT_PHONE", "")
    APPLICANT_FIRST_NAME: str = os.getenv("APPLICANT_FIRST_NAME", "")
    APPLICANT_MIDDLE_NAME: str = os.getenv("APPLICANT_MIDDLE_NAME", "")
    APPLICANT_LAST_NAME: str = os.getenv("APPLICANT_LAST_NAME", "")
    RESUME_PATH: str = os.getenv("RESUME_PATH", "/Users/siddheshsawant/Downloads/SiddheshSresume.pdf")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # Gmail App Password (IMAP) authentication
    GMAIL_USER: str = os.getenv("GMAIL_USER", os.getenv("APPLICANT_EMAIL", "siddheshs202001@gmail.com"))
    GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "")
    
    # Compliance & knockout preferences
    WORK_AUTH_STATUS: str = os.getenv("WORK_AUTH_STATUS", "Authorized / Require Sponsorship")
    US_PERSON_STATUS: str = os.getenv("US_PERSON_STATUS", "None of the above; I am a citizen of a different country")
    AUDITOR_INDEPENDENCE: str = os.getenv("AUDITOR_INDEPENDENCE", "No - I have never been employed by PwC")
    HYBRID_OFFICE_WILLINGNESS: str = os.getenv("HYBRID_OFFICE_WILLINGNESS", "")
    PRIVACY_POLICY_CONSENT: str = os.getenv("PRIVACY_POLICY_CONSENT", "Yes")
    RELOCATION_WILLINGNESS: str = os.getenv("RELOCATION_WILLINGNESS", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # Auto-submit configuration
    AUTO_SUBMIT: bool = os.getenv('AUTO_SUBMIT', 'true').lower() == 'true'

    # Deterministic inter-field pacing (ms)
    FIELD_FILL_GAP_MS: int = int(os.getenv('FIELD_FILL_GAP_MS', '600'))

    # Headless browser configuration (headless by default, headed only via DEBUG_HEADED=true)
    DEBUG_HEADED: bool = os.getenv('DEBUG_HEADED', 'false').lower() == 'true'
    HEADLESS: bool = not (os.getenv('DEBUG_HEADED', 'false').lower() == 'true')

    # Per-Company Application Cap Configuration
    MAX_APPLICATIONS_PER_COMPANY: int = int(os.getenv("MAX_APPLICATIONS_PER_COMPANY", "2"))
    COMPANY_CAP_WINDOW_DAYS: int = int(os.getenv("COMPANY_CAP_WINDOW_DAYS", "180"))
    COMPANY_COOLDOWN_HOURS: int = int(os.getenv("COMPANY_COOLDOWN_HOURS", "1"))

    # Anti-Spam Application Limits & Human Pacing Configuration
    MAX_DAILY_APPLICATIONS: int = int(os.getenv("MAX_DAILY_APPLICATIONS", "50"))
    MIN_APPLY_DELAY_SECONDS: int = int(os.getenv("MIN_APPLY_DELAY_SECONDS", "150"))  # FIXED 150s between submit clicks — no randomization
    MAX_DELAY_BETWEEN_APPLICATIONS: int = int(os.getenv("MAX_APPLY_DELAY_SECONDS", "150"))  # FIXED 150s — no jitter
    MIN_FORM_FILL_DURATION_SECONDS: int = int(os.getenv("MIN_FORM_FILL_DURATION_SECONDS", "25")) # Human interaction duration

    # Manual Approval Gate
    REQUIRE_MANUAL_APPROVAL: bool = os.getenv("REQUIRE_MANUAL_APPROVAL", "false").lower() == "true"

    # Universal Common Profile Keys
    YEARS_OF_EXPERIENCE: str = os.getenv("YEARS_OF_EXPERIENCE", "2")
    NOTICE_PERIOD: str = os.getenv("NOTICE_PERIOD", "submit_date + 1 month")
    EXPECTED_SALARY: str = os.getenv("EXPECTED_SALARY", "100000")
    LOCATION: str = os.getenv("LOCATION", "Washington, District of Columbia, United States")
    REMOTE_WILLINGNESS: str = os.getenv("REMOTE_WILLINGNESS", "Yes")
    LINKEDIN_URL: str = os.getenv("LINKEDIN_URL", "https://www.linkedin.com/in/sss20/")
    GITHUB_URL: str = os.getenv("GITHUB_URL", "https://github.com/sids202001")
    PORTFOLIO_URL: str = os.getenv("PORTFOLIO_URL", "https://siddhesh202001-portfolio.netlify.app")
    FULL_NAME: str = os.getenv("FULL_NAME", "Siddhesh Santosh Sawant")
    FIRST_NAME: str = os.getenv("FIRST_NAME", "Siddhesh Santosh")
    LAST_NAME: str = os.getenv("LAST_NAME", "Sawant")
    PHONE: str = os.getenv("PHONE", "(445) 208-2299")
    EMAIL: str = os.getenv("EMAIL", "siddheshs202001@gmail.com")

    # Politeness / rate-limiting configuration
    MAX_APPLICATIONS_PER_DAY: int = int(os.getenv('MAX_APPLICATIONS_PER_DAY', '50'))
    MIN_DELAY_BETWEEN_APPS: int = int(os.getenv('MIN_DELAY_BETWEEN_APPS', '150'))  # FIXED 150s between submit clicks — never random
    MAX_DELAY_BETWEEN_APPS: int = int(os.getenv('MAX_DELAY_BETWEEN_APPS', '150'))  # FIXED 150s — no jitter, no randomization
    ACTIVE_HOURS_START: str = os.getenv('ACTIVE_HOURS_START', '09:00')
    ACTIVE_HOURS_END: str = os.getenv('ACTIVE_HOURS_END', '23:00')
    DRY_RUN: bool = os.getenv('DRY_RUN', 'false').lower() == 'true'
    TELEGRAM_SUCCESS_ONLY: bool = os.getenv("TELEGRAM_SUCCESS_ONLY", "true").lower() == "true"
    AUTO_MODE_LOOKBACK_HOURS: int = int(os.getenv("AUTO_MODE_LOOKBACK_HOURS", "720"))

    # ── Gate Relaxation Toggles (approved 2026-09-18) ──────────────────
    # Experience gate: postings with NO parseable experience requirement
    # pass instead of hard-skip, provided no seniority signal in title.
    ALLOW_UNSTATED_EXPERIENCE: bool = os.getenv('ALLOW_UNSTATED_EXPERIENCE', 'true').lower() == 'true'

    # Relevance gate: direct target-role title matches (Software Engineer,
    # Backend Engineer, Data Engineer, SDE, AI/ML Engineer, Full Stack
    # Engineer) pass even with zero skill-keyword mentions in description.
    TITLE_MATCH_GUARANTEES_RELEVANCE: bool = os.getenv('TITLE_MATCH_GUARANTEES_RELEVANCE', 'true').lower() == 'true'

    # Sponsorship gate: postings where sponsorship status is UNKNOWN
    # (neither "no sponsorship" nor confirmed sponsorship language found)
    # proceed with a flag.  Explicit "no sponsorship" always hard-skips.
    SPONSORSHIP_UNKNOWN_PROCEEDS: bool = os.getenv('SPONSORSHIP_UNKNOWN_PROCEEDS', 'true').lower() == 'true'

    def __post_init__(self):
        self.MAX_DAILY_APPLICATIONS = int(os.getenv("MAX_DAILY_APPLICATIONS", str(self.MAX_DAILY_APPLICATIONS)))
        self.MAX_APPLICATIONS_PER_DAY = int(os.getenv("MAX_APPLICATIONS_PER_DAY", str(self.MAX_APPLICATIONS_PER_DAY)))
        self.MAX_APPLICATIONS_PER_COMPANY = int(os.getenv("MAX_APPLICATIONS_PER_COMPANY", str(self.MAX_APPLICATIONS_PER_COMPANY)))
        self.COMPANY_CAP_WINDOW_DAYS = int(os.getenv("COMPANY_CAP_WINDOW_DAYS", str(self.COMPANY_CAP_WINDOW_DAYS)))
        self.COMPANY_COOLDOWN_HOURS = int(os.getenv("COMPANY_COOLDOWN_HOURS", str(self.COMPANY_COOLDOWN_HOURS)))
        self.REQUIRE_MANUAL_APPROVAL = os.getenv("REQUIRE_MANUAL_APPROVAL", str(self.REQUIRE_MANUAL_APPROVAL)).lower() == "true"
        self.FIELD_FILL_GAP_MS = int(os.getenv('FIELD_FILL_GAP_MS', str(self.FIELD_FILL_GAP_MS)))
        self.DEBUG_HEADED = os.getenv('DEBUG_HEADED', 'false').lower() == 'true'
        self.HEADLESS = not self.DEBUG_HEADED
        os.makedirs(self.DATA_DIR, exist_ok=True)
        os.makedirs(self.UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.SCREENSHOT_DIR, exist_ok=True)
        os.makedirs(self.BROWSER_STATE_DIR, exist_ok=True)


settings = Settings()

# Universal COMMON_FIELDS Registry mapping concepts across Lever, Greenhouse, Ashby
COMMON_FIELDS = {
    "years_of_experience": {
        "patterns": [
            r"years?\s*(of\s*)?.*experience",
            r"how\s*many\s*years",
            r"years?\s*in\s*software",
            r"total\s*(years\s*of\s*)?experience",
            r"relevant\s*professional\s*experience",
            r"relevant\s*experience",
            r"professional\s*experience",
        ],
        "config_attr": "YEARS_OF_EXPERIENCE",
        "default": "2",
        "category": "numeric_years"
    },
    "notice_period": {
        "patterns": [
            r"notice\s*period",
            r"how\s*soon\s*can\s*you\s*start",
            r"availability\s*to\s*start",
            r"earliest\s*start\s*date",
            r"when\s*can\s*you\s*start",
            r"start\s*date",
        ],
        "config_attr": "NOTICE_PERIOD",
        "default": "submit_date + 1 month",
        "category": "text"
    },
    "expected_salary": {
        "patterns": [
            r"expected\s*(salary|compensation|pay|rate)",
            r"desired\s*(salary|compensation|pay|rate)",
            r"compensation\s*expectation",
            r"salary\s*expectation",
            r"target\s*compensation",
            r"target\s*salary",
            r"hourly\s*rate",
            r"salary\s*requirements?",
        ],
        "config_attr": "EXPECTED_SALARY",
        "default": "100000",
        "category": "salary"
    },
    "location": {
        "patterns": [
            r"(your\s*)?(current\s*)?location",
            r"city[\s,]+state",
            r"where\s*are\s*you\s*(currently\s*)?(based|located|living)",
            r"current\s*city",
            r"current\s*residence",
            r"candidate\s*location",
            r"opportunity\s*location.*",
            r"opportunitylocationid",
            r"role\s*location",
            r"office\s*location",
            r"where\s*do\s*you\s*plan\s*on\s*working",
            r"plan\s*on\s*working\s*from",
            r"working\s*from.*payroll",
            r"payroll\s*tax",
            r"payroll.*location",
            r"tax\s*purposes?",
        ],
        "config_attr": "LOCATION",
        "default": "Washington, District of Columbia, United States",
        "category": "location"
    },
    "text_messaging_consent": {
        "patterns": [
            r"text\s*message(s)?\s*(updates?|agreement)",
            r"communication\s*consent",
            r"consent\s*to\s*receiving\s*text",
            r"sms\s*(consent|updates?)",
        ],
        "config_attr": "PRIVACY_POLICY_CONSENT",
        "default": "Yes - I consent to receiving text messages",
        "category": "boolean"
    },
    "remote_willingness": {
        "patterns": [
            r"(open\s*to|comfortable\s*with)\s*remote",
            r"remote\s*(or|and)?\s*hybrid",
            r"work\s*remotely",
            r"willing\s*to\s*work\s*remotely",
            r"remote\s*work\s*preference",
        ],
        "config_attr": "REMOTE_WILLINGNESS",
        "default": "Yes",
        "category": "boolean"
    },
    "hybrid_office_willingness": {
        "patterns": [
            r"hybrid",
            r"in-?person",
            r"work\s*from\s*(our\s*)?office",
            r"days?\s*(per|\/|a)\s*week\s*(in|from)\s*(the\s*)?office",
            r"on-?site",
            r"willingness\s*to\s*work\s*in\s*office",
        ],
        "config_attr": "HYBRID_OFFICE_WILLINGNESS",
        "default": "Yes",
        "category": "boolean"
    },
    "relocation_willingness": {
        "patterns": [
            r"willing\s*to\s*relocate",
            r"relocation",
            r"open\s*to\s*relocat",
        ],
        "config_attr": "RELOCATION_WILLINGNESS",
        "default": "Yes",
        "category": "boolean"
    },
    "work_auth_status": {
        "patterns": [
            r"work\s*authorization(\s*status)?",
            r"authorized\s*to\s*work\s*in\s*the\s*(us|united\s*states|u\.s\.)",
            r"require\s*(visa\s*)?sponsorship",
            r"sponsorship\s*now\s*or\s*in\s*the\s*future",
            r"visa\s*sponsorship",
            r"legal\s*right\s*to\s*work",
        ],
        "config_attr": "WORK_AUTH_STATUS",
        "default": "Authorized / Require Sponsorship",
        "category": "work_auth"
    },
    "us_person_status": {
        "patterns": [
            r"u\.?s\.?\s*person",
            r"us\s*person",
            r"which.*best\s*describes\s*your.*u\.?s\.?\s*person",
            r"\bitar\b",
            r"\bear\b",
            r"export\s*controls?",
            r"subject\s*to\s*export\s*controls",
        ],
        "config_attr": "US_PERSON_STATUS",
        "default": "Other",
        "category": "us_person"
    },
    "military_defense_contribution": {
        "patterns": [
            r"reasons?\s*why\s*you\s*would\s*not\s*be\s*able\s*to\s*fully\s*contribute.*military",
            r"military.*allied\s*forces.*reasons",
            r"contribute\s*in\s*support\s*of\s*the\s*u\.?s\.?\s*military",
            r"support\s*the\s*u\.?s\.?\s*military\s*and\s*allied\s*forces",
            r"objections?\s*to\s*working\s*on.*(defense|military)",
        ],
        "config_attr": "MILITARY_CONTRIBUTION_OBJECTIONS",
        "default": "No",
        "category": "boolean_negative"
    },
    "auditor_independence": {
        "patterns": [
            r"auditor\s*independence",
            r"pricewaterhousecoopers",
            r"\bpwc\b",
            r"independent\s*auditor",
            r"previously\s*worked\s*at.*auditor",
            r"ernst\s*&\s*young|\bey\b|deloitte|kpmg|accounting\s*firm",
        ],
        "config_attr": "AUDITOR_INDEPENDENCE",
        "default": "No - I have never been employed by PwC",
        "category": "boolean_negative"
    },
    "privacy_policy_consent": {
        "patterns": [
            r"privacy\s*(policy|notice)",
            r"consent\s*to\s*(processing|data)",
            r"terms\s*and\s*conditions",
            r"acknowledge\s*and\s*agree",
            r"data\s*retention",
        ],
        "config_attr": "PRIVACY_POLICY_CONSENT",
        "default": "Yes",
        "category": "boolean"
    },
    "linkedin_url": {
        "patterns": [r"linkedin", r"linked\s*in"],
        "config_attr": "LINKEDIN_URL",
        "default": "https://www.linkedin.com/in/sss20/",
        "category": "url"
    },
    "github_url": {
        "patterns": [r"github", r"git\s*hub"],
        "config_attr": "GITHUB_URL",
        "default": "https://github.com/sids202001",
        "category": "url"
    },
    "portfolio_url": {
        "patterns": [r"portfolio", r"personal\s*site", r"personal\s*website", r"personal\s*url", r"\bwebsite\b"],
        "config_attr": "PORTFOLIO_URL",
        "default": "https://siddhesh202001-portfolio.netlify.app",
        "category": "url"
    },
    "full_name": {
        "patterns": [r"full\s*name", r"legal\s*name"],
        "config_attr": "FULL_NAME",
        "default": "Siddhesh Santosh Sawant",
        "category": "text"
    },
    "first_name": {
        "patterns": [r"first\s*name", r"given\s*name"],
        "config_attr": "FIRST_NAME",
        "default": "Siddhesh Santosh",
        "category": "text"
    },
    "last_name": {
        "patterns": [r"last\s*name", r"family\s*name", r"surname"],
        "config_attr": "LAST_NAME",
        "default": "Sawant",
        "category": "text"
    },
    "email": {
        "patterns": [r"email(\s*address)?"],
        "config_attr": "EMAIL",
        "default": "siddheshs202001@gmail.com",
        "category": "text"
    },
    "phone": {
        "patterns": [r"phone(\s*number)?", r"mobile(\s*number)?"],
        "config_attr": "PHONE",
        "default": "(445) 208-2299",
        "category": "text"
    },
    "government_employment": {
        "patterns": [
            r"government\s*(employ|service|job|role|official)",
            r"prior.*government",
            r"worked.*government",
            r"civil\s*service",
            r"public\s*official",
            r"us\s*government"
        ],
        "config_attr": "GOVERNMENT_EMPLOYMENT",
        "default": "No",
        "category": "boolean"
    },
}

# Cross-module compatibility bridge with direct_apply_bot
try:
    from app.services.company_registry import (
        REAL_COMPANY_BOARDS,
        UNSUPPORTED_COMPANIES,
        get_coverage_summary,
        format_coverage_line,
        is_searchable,
        get_board_for_company,
        get_unsupported_reason,
    )
except ImportError:
    REAL_COMPANY_BOARDS = []
    UNSUPPORTED_COMPANIES = {}

FULL_NAME = getattr(settings, "FULL_NAME", os.getenv("FULL_NAME", "Siddhesh Santosh Sawant"))
FIRST_NAME = getattr(settings, "FIRST_NAME", os.getenv("FIRST_NAME", "Siddhesh Santosh"))
LAST_NAME = getattr(settings, "LAST_NAME", os.getenv("LAST_NAME", "Sawant"))
PREFERRED_NAME = os.getenv("PREFERRED_NAME", "Siddhesh")
EMAIL = getattr(settings, "EMAIL", os.getenv("EMAIL", "siddheshs202001@gmail.com"))
PHONE = getattr(settings, "PHONE", os.getenv("PHONE", "+1 445-208-2299"))
LINKEDIN_URL = getattr(settings, "LINKEDIN_URL", os.getenv("LINKEDIN_URL", "https://www.linkedin.com/in/sss20/"))
GITHUB_URL = getattr(settings, "GITHUB_URL", os.getenv("GITHUB_URL", "https://github.com/sids202001"))
PORTFOLIO_URL = getattr(settings, "PORTFOLIO_URL", os.getenv("PORTFOLIO_URL", "https://siddhesh202001-portfolio.netlify.app"))
LOCATION = getattr(settings, "LOCATION", os.getenv("LOCATION", "Washington, District of Columbia, United States"))
UNIVERSITY = os.getenv("UNIVERSITY", "George Mason University")
DEGREE = os.getenv("DEGREE", "Master of Science")
MAJOR = os.getenv("MAJOR", "Computer Science")



