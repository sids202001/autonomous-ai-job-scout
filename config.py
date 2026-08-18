"""
AutoApply - Configuration Module
Loads settings from .env file using python-dotenv.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment variables."""
    APP_NAME: str = "AutoApply - Job Automation Dashboard"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/autoapply.db")
    STATIC_DIR: str = "app/static"
    DATA_DIR: str = "data"
    UPLOAD_DIR: str = "data/resumes"
    SCREENSHOT_DIR: str = "data/screenshots"
    BROWSER_STATE_DIR: str = "data/browser_state"
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
    RESUME_PATH: str = os.getenv("RESUME_PATH", "./data/resumes/SiddheshSDResume.pdf")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    def __post_init__(self):
        os.makedirs(self.DATA_DIR, exist_ok=True)
        os.makedirs(self.UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.SCREENSHOT_DIR, exist_ok=True)
        os.makedirs(self.BROWSER_STATE_DIR, exist_ok=True)


settings = Settings()
