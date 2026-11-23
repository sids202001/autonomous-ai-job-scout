"""
Submission-Readiness Proof Runner (Dry-Run, NO submit click)
Picks 3 reachable postings (Greenhouse, Lever, Ashby) with no challenge and no active lockout.
Runs full pipeline to READY_TO_SUBMIT with complete field audit (0 unfilled required fields)
and 4-evidence bundle staged (tracker entry, screenshot, photo receipt, ledger entry).
"""
import os
import sys
import time
import json
import asyncio
import logging
from datetime import datetime, timezone

# Add sid_job_scrip to sys.path
sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")

from form_filler import (
    launch_and_fill_job,
    load_profile,
    enumerate_required_fields,
    ensure_required_fields_filled,
    CaptchaManager,
)
from app.services.launch_tracker import launch_tracker
from app.services.intelligence_loop import outcome_ledger
from app.services.audit_logger import audit_logger
from app.services.company_cap import company_cap_tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("readiness_proof")


async def find_submit_button(page):
    submit_selectors = [
        'button[type="submit"]:visible',
        'button:has-text("Submit application"):visible',
        'button:has-text("Submit Application"):visible',
        'button:has-text("Submit profile"):visible',
        'button:has-text("Submit Profile"):visible',
        'button:has-text("Submit"):visible',
        'button:has-text("Apply"):visible',
        'input[type="submit"]:visible',
        'button[id*="submit" i]:visible',
        'button[class*="submit" i]:visible',
        'button[data-qa*="submit" i]:visible',
        'button[data-testid*="submit" i]:visible',
        'div[role="button"]:has-text("Submit"):visible',
        'button[type="submit"]',
        'button:has-text("Submit application")',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
    ]
    for sel in submit_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return el
        except Exception:
            pass

    try:
        await page.mouse.wheel(0, 1000)
        await asyncio.sleep(0.5)
        for sel in submit_selectors:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return el
    except Exception:
        pass

    return None


TARGET_ROLES = [
    {
        "company": "Snowflake",
        "role": "Software Engineer - Database Engineering",
        "url": "https://jobs.ashbyhq.com/snowflake/db1375f0-ea5d-404a-b640-259f94dbc995/application",
        "portal": "ashby",
    },
    {
        "company": "Anyscale",
        "role": "Software Engineer, Ray Core",
        "url": "https://jobs.ashbyhq.com/anyscale/8b29c5e5-d56f-4c52-8887-a6a1827cf042/application",
        "portal": "ashby",
    },
    {
        "company": "Braintrust",
        "role": "Software Engineer, Systems",
        "url": "https://jobs.ashbyhq.com/braintrust/8b9cfa26-627f-442c-a358-783b0e4ef930/application",
        "portal": "ashby",
    },
]


async def prove_role_readiness(target: dict) -> dict:
    company = target["company"]
    role = target["role"]
    url = target["url"]
    portal = target["portal"]

    logger.info(f"=== PROVING READINESS: {company} - {role} ({portal}) ===")

    # 1. Lockout check
    lockout = company_cap_tracker.get_employer_lockout(company)
    assert lockout is None, f"{company} has active employer lockout: {lockout}"

    # 2. Register launch in launch_tracker
    launch_id = launch_tracker.register_launch(
        company=company,
        role=role,
        url=url,
        portal=portal
    )
    logger.info(f"1. Tracker Entry registered: launch_id={launch_id}")

    # 3. Launch and fill form
    logger.info(f"Launching browser and executing form filling for {company}...")
    pw, browser = await launch_and_fill_job(url, source="readiness_proof", headless=True)
    page = browser.contexts[0].pages[0] if (browser.contexts and browser.contexts[0].pages) else None
    assert page is not None, "Failed to get active browser page"

    await asyncio.sleep(2.0)

    try:
        # 4. Challenge check (clean detect)
        is_chal = await CaptchaManager.is_captcha_present(page)
        assert not is_chal, f"Unexpected security challenge detected on {company}"

        # 5. Field completeness audit
        profile = load_profile()
        all_req_filled, unfilled_fields = await ensure_required_fields_filled(page, profile)
        logger.info(f"Field audit result: all_req_filled={all_req_filled}, unfilled count={len(unfilled_fields)}")

        if not all_req_filled:
            # Attempt deterministic resolution pass
            from form_filler import DeterministicEngine
            for uf in unfilled_fields:
                lbl = uf.get("label", "")
                choices = uf.get("choices", [])
                ans = DeterministicEngine.solve(lbl, choices if choices else None)
                if ans:
                    from Desktop.resume_bot import apply_answer_to_page
                    await apply_answer_to_page(page, lbl, str(ans))
            await asyncio.sleep(1.0)
            all_req_filled, unfilled_fields = await ensure_required_fields_filled(page, profile)

        assert all_req_filled, f"Unfilled required fields remaining: {unfilled_fields}"
        assert len(unfilled_fields) == 0, f"Unfilled count must be 0, got {len(unfilled_fields)}"

        # 6. Verify submit button exists
        submit_btn = await find_submit_button(page)
        assert submit_btn is not None, f"Submit button not found for {company}"
        logger.info(f"Submit button found and ready: {submit_btn} (DRY RUN: NO CLICK)")

        # 7. Take screenshot of completed form
        screenshots_dir = os.path.expanduser("~/Desktop/screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_c = company.lower().replace(" ", "_")
        screenshot_path = os.path.join(screenshots_dir, f"{clean_c}_readiness_proof_{ts_str}.png")
        await page.screenshot(path=screenshot_path, full_page=True)
        assert os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0, "Screenshot capture failed"
        logger.info(f"2. Screenshot saved: {screenshot_path} ({os.path.getsize(screenshot_path)} bytes)")

        # 8. Stage photo receipt
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
        receipt_id = f"rcpt_staged_{launch_id}"
        receipt_text = (
            f"✅ *READY_TO_SUBMIT (Dry-Run)*\n"
            f"🏢 *Company:* {company}\n"
            f"💼 *Role:* {role}\n"
            f"🌐 *Portal:* {portal.title()}\n"
            f"🔗 [Direct Link]({url})\n"
            f"🕒 *Time:* {time_str}\n"
            f"📸 *Photo Receipt Staged:* {screenshot_path}\n"
            f"0 unfilled required fields. Submit button ready (dry-run, no submit click)."
        )
        receipt_file = os.path.join(screenshots_dir, f"{clean_c}_photo_receipt_{ts_str}.json")
        with open(receipt_file, "w", encoding="utf-8") as rf:
            json.dump({
                "receipt_id": receipt_id,
                "launch_id": launch_id,
                "company": company,
                "role": role,
                "portal": portal,
                "url": url,
                "screenshot_path": screenshot_path,
                "text": receipt_text,
                "timestamp": time_str
            }, rf, indent=2)
        logger.info(f"3. Photo receipt staged: {receipt_file} (msg_id={receipt_id})")

        # 9. Record in outcome ledger
        ledger_entry = outcome_ledger.record_outcome(
            company=company,
            role=role,
            portal=portal,
            url=url,
            outcome="ready_to_submit",
            reason="0 unfilled required fields, complete field audit passed (dry run)",
            screenshot_path=screenshot_path,
            receipt_msg_id=receipt_id,
            terminal_status="READY_TO_SUBMIT"
        )
        logger.info(f"4. Outcome ledger recorded: status=READY_TO_SUBMIT")

        # 10. Resolve in launch tracker with all 4 evidence items
        resolved_entry = launch_tracker.resolve_launch(
            launch_id=launch_id,
            status="READY_TO_SUBMIT",
            reason="0 unfilled required fields; ready to submit (dry run, no submit click)",
            screenshot_path=screenshot_path,
            receipt_msg_id=receipt_id
        )
        logger.info(f"Launch tracker resolved: {resolved_entry['status']} (evidence: {resolved_entry['evidence']})")

        # Log into audit logger
        audit_logger.log_submission(
            company=company,
            role_title=role,
            job_url=url,
            portal=portal,
            status="ready_to_submit",
            screenshot_path=screenshot_path,
            receipt_msg_id=receipt_id,
            terminal_status="READY_TO_SUBMIT"
        )

        return {
            "company": company,
            "role": role,
            "url": url,
            "portal": portal,
            "launch_id": launch_id,
            "status": "READY_TO_SUBMIT",
            "unfilled_required_fields": len(unfilled_fields),
            "screenshot_path": screenshot_path,
            "photo_receipt": receipt_file,
            "ledger_recorded": True,
            "evidence": resolved_entry.get("evidence", {})
        }

    finally:
        try:
            await browser.close()
            await pw.stop()
        except Exception:
            pass


async def main():
    results = []
    print("\n=======================================================")
    print("SUBMISSION-READINESS PROOF RUN (DRY RUN, NO SUBMIT CLICK)")
    print("=======================================================\n")

    for target in TARGET_ROLES:
        res = await prove_role_readiness(target)
        results.append(res)
        print(f"\n[PASS] {res['company']} ({res['portal']}):")
        print(f"  • Role: {res['role']}")
        print(f"  • Status: {res['status']}")
        print(f"  • Unfilled Required Fields: {res['unfilled_required_fields']}")
        print(f"  • Tracker ID: {res['launch_id']}")
        print(f"  • Screenshot: {res['screenshot_path']}")
        print(f"  • Photo Receipt: {res['photo_receipt']}")
        print(f"  • Evidence: {res['evidence']}")
        await asyncio.sleep(2.0)

    print("\n=======================================================")
    print(f"ALL {len(results)} ROLES PROVEN READY_TO_SUBMIT (0 UNFILLED FIELDS)")
    print("=======================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
