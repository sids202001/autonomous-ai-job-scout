"""
Profile Verification & Self-Check Service (Rule 1)
Enforces profile.json as the single source of truth across all platforms.
Runs startup self-checks to verify all canonical profile URLs before submission.
"""
import os
import json
import logging
import subprocess
from pathlib import Path
from typing import Tuple, Dict, Any, Optional
import httpx
import certifi

logger = logging.getLogger(__name__)

PROFILE_PATH = Path("/Users/siddheshsawant/sid_job_scrip/profile.json")
CANONICAL_LINKEDIN = "https://www.linkedin.com/in/sss20/"
CANONICAL_GITHUB = "https://github.com/sids202001"
CANONICAL_PORTFOLIO = "https://siddhesh202001-portfolio.netlify.app"
CANONICAL_LOCATION = "Washington, District of Columbia, United States"


def get_build_hash() -> str:
    """Return the git commit hash of the build, or 'unknown'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True,
            text=True,
            timeout=5
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def load_canonical_profile() -> Dict[str, Any]:
    """Load canonical profile directly from profile.json."""
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(f"profile.json not found at {PROFILE_PATH}")
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def run_startup_self_check() -> Tuple[bool, str]:
    """
    Startup Self-Check (Rule 1):
    Runs on every boot and fails loud:
    1. LinkedIn URL verified by exact string match against canonical URL.
    2. GitHub URL fetched with HTTP 200 AND body must contain candidate's name.
    3. Portfolio URL fetched with HTTP 200 AND body must contain candidate's name.
    If any check fails, logs PROFILE_URL_CHECK_FAILED with URL + status and returns False.
    """
    logger.info("[STARTUP SELF-CHECK] Verifying canonical profile credentials from profile.json...")
    try:
        profile = load_canonical_profile()
    except Exception as e:
        err = f"PROFILE_URL_CHECK_FAILED: Could not load profile.json: {e}"
        logger.error(err)
        return False, err

    pi = profile.get("personal_info", {})
    gh_url = pi.get("github") or pi.get("github_url") or ""
    pf_url = pi.get("portfolio") or pi.get("portfolio_url") or ""
    li_url = pi.get("linkedin") or pi.get("linkedin_url") or ""

    # 1. LinkedIn exact-string verification (LinkedIn blocks automated fetches, exact string match required)
    if li_url.strip().rstrip("/") != CANONICAL_LINKEDIN.rstrip("/"):
        err = f"PROFILE_URL_CHECK_FAILED: LinkedIn URL mismatch. Expected '{CANONICAL_LINKEDIN}', got '{li_url}'"
        logger.error(err)
        return False, err

    # 2. Live HTTP check of GitHub and Portfolio
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    }
    client = httpx.Client(verify=certifi.where(), headers=headers, timeout=12.0, follow_redirects=True)

    try:
        # Check GitHub URL
        try:
            r_gh = client.get(gh_url)
            if r_gh.status_code != 200:
                err = f"PROFILE_URL_CHECK_FAILED: GitHub URL '{gh_url}' returned HTTP {r_gh.status_code}"
                logger.error(err)
                return False, err
            gh_body = r_gh.text
            if not ("Siddhesh" in gh_body or "Sawant" in gh_body):
                err = f"PROFILE_URL_CHECK_FAILED: GitHub profile at '{gh_url}' does not contain candidate name"
                logger.error(err)
                return False, err
            logger.info(f"[STARTUP SELF-CHECK] GitHub URL verified: {gh_url} (HTTP 200, name present)")
        except Exception as ge:
            err = f"PROFILE_URL_CHECK_FAILED: GitHub URL probe failed: {ge}"
            logger.error(err)
            return False, err

        # Check Portfolio URL
        try:
            r_pf = client.get(pf_url)
            if r_pf.status_code != 200:
                err = f"PROFILE_URL_CHECK_FAILED: Portfolio URL '{pf_url}' returned HTTP {r_pf.status_code}"
                logger.error(err)
                return False, err
            pf_body = r_pf.text
            if not ("Siddhesh" in pf_body or "Sawant" in pf_body):
                err = f"PROFILE_URL_CHECK_FAILED: Portfolio at '{pf_url}' does not contain candidate name"
                logger.error(err)
                return False, err
            logger.info(f"[STARTUP SELF-CHECK] Portfolio URL verified: {pf_url} (HTTP 200, name present)")
        except Exception as pe:
            err = f"PROFILE_URL_CHECK_FAILED: Portfolio URL probe failed: {pe}"
            logger.error(err)
            return False, err

    finally:
        client.close()

    success_msg = f"PROFILE_URL_CHECK_PASSED: All URLs verified (GitHub: {gh_url}, Portfolio: {pf_url}, LinkedIn: {li_url})"
    logger.info(f"[STARTUP SELF-CHECK] {success_msg}")
    return True, success_msg
