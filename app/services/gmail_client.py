"""
AutoApply - Gmail OAuth Client
Handles Google OAuth 2.0 flow for desktop applications with auto-refresh.

Security:
- Scope is strictly restricted to 'gmail.readonly' (no send, delete, or modify permissions).
- Tokens stored locally in 'token.json' (gitignored).
- Passwords are never handled or stored.
"""
import os
import logging
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# STRICT SCOPE: Read-only access to messages only
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


class GmailClient:
    """Manages Gmail OAuth 2.0 authentication and API access."""

    def __init__(self, credentials_path: str = CREDENTIALS_FILE, token_path: str = TOKEN_FILE):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None

    def get_credentials(self) -> Optional[Credentials]:
        """
        Load existing credentials from token.json or execute OAuth flow using credentials.json.
        Returns valid Credentials object, or None if credentials.json is not configured.
        """
        creds = None

        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            except Exception as e:
                logger.warning(f"Failed to load token from {self.token_path}: {e}")

        # If there are no valid credentials available, refresh or authenticate
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired Gmail OAuth token...")
                    creds.refresh(Request())
                    with open(self.token_path, "w") as token_file:
                        token_file.write(creds.to_json())
                    logger.info("Gmail OAuth token successfully refreshed.")
                except Exception as e:
                    logger.error(f"Failed to refresh Gmail OAuth token: {e}")
                    creds = None

            if not creds:
                if not os.path.exists(self.credentials_path):
                    logger.warning(
                        f"Gmail OAuth '{self.credentials_path}' not found. "
                        "Verification code auto-fetch will fall back to manual entry/Telegram alert. "
                        "Download OAuth Client JSON from Google Cloud Console to enable."
                    )
                    return None

                try:
                    logger.info("Initiating browser OAuth 2.0 flow for Gmail API...")
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                    creds = flow.run_local_server(port=0)
                    with open(self.token_path, "w") as token_file:
                        token_file.write(creds.to_json())
                    logger.info("Gmail OAuth authentication successful, saved token.json")
                except Exception as e:
                    logger.error(f"Gmail OAuth flow failed: {e}")
                    return None

        return creds

    def get_service(self):
        """Build and cache the authenticated Gmail API service resource."""
        if self._service is None:
            creds = self.get_credentials()
            if creds:
                try:
                    self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
                except Exception as e:
                    logger.error(f"Failed to build Gmail service: {e}")
                    self._service = None
        return self._service

    def is_configured(self) -> bool:
        """Returns True if credentials.json or token.json exists."""
        return os.path.exists(self.credentials_path) or os.path.exists(self.token_path)


# Singleton instance
gmail_client = GmailClient()
