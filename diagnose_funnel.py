#!/usr/bin/env python3
"""
Funnel Diagnostic Script — Measure where the zero-match collapse happens.

Calls the same Greenhouse/Lever APIs that the scraper uses, but instruments
every filtering stage so we can report exact counts:

  companies_scanned
    → raw_postings_discovered
      → after_title_seniority_filter (EXCLUDED_KEYWORDS / SENIOR_TITLE_PATTERNS)
      → after_experience_gate  (split: pass / seniority_exceeded / exp_exceeded / unparseable)
      → after_relevance_gate   (split: pass / forbidden_domain / non_target_role / low_relevance)
      → after_sponsorship_gate (split: confirmed_no_sponsorship / unknown / pass)
      → after_dedupe
      → eligible
"""

import asyncio
import sys
import os
import re
import json
from datetime import datetime, timezone
from collections import defaultdict, Counter

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from app.services.job_scraper import REAL_COMPANY_BOARDS, EXCLUDED_KEYWORDS
from app.services.experience_gate import ExperienceGate, SENIOR_TITLE_PATTERNS as EXP_SENIOR_PATTERNS
from app.services.relevance_gate import RelevanceGate
from app.services.job_matcher import NO_SPONSORSHIP_PATTERNS

# ─── Counters ───────────────────────────────────────────────────────────
companies_scanned = 0
companies_reachable = 0
companies_unreachable = []
raw_postings = 0

# Experience gate breakdown
exp_pass = 0
exp_seniority_title = 0
exp_exceeded = 0
exp_unparseable = 0

# Relevance gate breakdown
rel_pass = 0
rel_forbidden = 0
rel_non_target = 0
rel_low_relevance = 0

# Sponsorship gate breakdown
sponsor_pass = 0
sponsor_explicit_no = 0

# Dedupe
dedupe_removed = 0
eligible = 0

# Score distribution for relevance
relevance_scores = []

# Detailed per-stage sample tracking
exp_unparseable_samples = []
rel_low_samples = []
exp_exceeded_samples = []


def strip_html(html_text: str) -> str:
    from bs4 import BeautifulSoup
    if not html_text:
        return ""
    try:
        return BeautifulSoup(html_text, "html.parser").get_text(separator="\n").strip()
    except Exception:
        return html_text


async def fetch_greenhouse(client, company, handle):
    url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20.0)
        if r.status_code != 200:
            return None, r.status_code
        data = r.json()
        jobs = []
        for item in data.get("jobs", []):
            title = item.get("title", "").strip()
            job_url = item.get("absolute_url", "").strip()
            if not title or not job_url:
                continue
            content = strip_html(item.get("content", ""))
            jobs.append({"title": title, "url": job_url, "description": content, "company": company})
        return jobs, 200
    except Exception as e:
        return None, str(e)


async def fetch_lever(client, company, handle):
    url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20.0)
        if r.status_code != 200:
            return None, r.status_code
        jobs = []
        for item in r.json():
            title = item.get("text", "").strip()
            job_url = item.get("hostedUrl", "").strip()
            if not title or not job_url:
                continue
            desc = item.get("descriptionPlain", "")
            jobs.append({"title": title, "url": job_url, "description": desc or "", "company": company})
        return jobs, 200
    except Exception as e:
        return None, str(e)


def check_sponsorship(desc: str) -> str:
    """Returns 'explicit_no' if no-sponsorship pattern matched, else 'pass'."""
    desc_lower = desc.lower()
    for pat in NO_SPONSORSHIP_PATTERNS:
        if re.search(pat, desc_lower):
            return "explicit_no"
    return "pass"


async def main():
    global companies_scanned, companies_reachable, raw_postings
    global exp_pass, exp_seniority_title, exp_exceeded, exp_unparseable
    global rel_pass, rel_forbidden, rel_non_target, rel_low_relevance
    global sponsor_pass, sponsor_explicit_no
    global dedupe_removed, eligible

    seen_urls = set()
    per_company_stats = {}

    print(f"{'='*80}")
    print(f"FUNNEL DIAGNOSTIC — {datetime.now(timezone.utc).isoformat()}")
    print(f"Company boards in list: {len(REAL_COMPANY_BOARDS)}")
    print(f"{'='*80}\n")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for company_name, board_type, handle in REAL_COMPANY_BOARDS:
            companies_scanned += 1
            co_stats = {"raw": 0, "exp_pass": 0, "exp_seniority": 0, "exp_exceeded": 0,
                        "exp_unparseable": 0, "rel_pass": 0, "rel_fail": 0,
                        "sponsor_pass": 0, "sponsor_no": 0, "eligible": 0}

            if board_type == "greenhouse":
                result, status = await fetch_greenhouse(client, company_name, handle)
            else:
                result, status = await fetch_lever(client, company_name, handle)

            if result is None:
                companies_unreachable.append((company_name, board_type, handle, status))
                per_company_stats[company_name] = co_stats
                print(f"  ✗ {company_name} ({board_type}/{handle}): UNREACHABLE ({status})")
                continue

            companies_reachable += 1
            co_raw = len(result)
            raw_postings += co_raw
            co_stats["raw"] = co_raw

            for job in result:
                title = job["title"]
                desc = job["description"]
                url = job["url"]

                # ── Experience Gate ──
                exp_ok, exp_reason, exp_meta = ExperienceGate.evaluate(title, desc)
                if not exp_ok:
                    if "SENIORITY_EXCEEDED" in exp_reason:
                        exp_seniority_title += 1
                        co_stats["exp_seniority"] += 1
                    elif "UNPARSEABLE" in exp_reason:
                        exp_unparseable += 1
                        co_stats["exp_unparseable"] += 1
                        if len(exp_unparseable_samples) < 15:
                            exp_unparseable_samples.append({"company": company_name, "title": title, "url": url})
                    else:
                        exp_exceeded += 1
                        co_stats["exp_exceeded"] += 1
                        if len(exp_exceeded_samples) < 10:
                            exp_exceeded_samples.append({"company": company_name, "title": title, "reason": exp_reason})
                    continue
                exp_pass += 1
                co_stats["exp_pass"] += 1

                # ── Relevance Gate ──
                rel_ok, rel_reason, rel_meta = RelevanceGate.evaluate(title, desc)
                relevance_scores.append(rel_meta.get("relevance_score", 0.0))
                if not rel_ok:
                    if "FORBIDDEN_DOMAIN" in rel_reason:
                        rel_forbidden += 1
                        co_stats["rel_fail"] += 1
                    elif "NON_TARGET_ROLE" in rel_reason:
                        rel_non_target += 1
                        co_stats["rel_fail"] += 1
                    else:
                        rel_low_relevance += 1
                        co_stats["rel_fail"] += 1
                        if len(rel_low_samples) < 10:
                            rel_low_samples.append({
                                "company": company_name, "title": title,
                                "score": rel_meta.get("relevance_score", 0),
                                "matched_skills": rel_meta.get("matched_skills", []),
                            })
                    continue
                rel_pass += 1
                co_stats["rel_pass"] += 1

                # ── Sponsorship Gate ──
                s_result = check_sponsorship(desc)
                if s_result == "explicit_no":
                    sponsor_explicit_no += 1
                    co_stats["sponsor_no"] += 1
                    continue
                sponsor_pass += 1
                co_stats["sponsor_pass"] += 1

                # ── Dedupe ──
                if url in seen_urls:
                    dedupe_removed += 1
                    continue
                seen_urls.add(url)

                eligible += 1
                co_stats["eligible"] += 1

            per_company_stats[company_name] = co_stats
            status_icon = "✓" if co_stats["eligible"] > 0 else "·"
            print(f"  {status_icon} {company_name}: raw={co_raw}, exp_pass={co_stats['exp_pass']}, "
                  f"rel_pass={co_stats['rel_pass']}, eligible={co_stats['eligible']}")

            # Small delay to avoid rate-limiting
            await asyncio.sleep(0.3)

    # ─── Print Full Funnel Table ─────────────────────────────────────────
    print(f"\n{'='*80}")
    print("FUNNEL BREAKDOWN TABLE")
    print(f"{'='*80}")
    print(f"{'Stage':<45} {'Count':>8} {'% of Raw':>10}")
    print(f"{'-'*45} {'-'*8} {'-'*10}")
    pct = lambda n: f"{n/raw_postings*100:.1f}%" if raw_postings > 0 else "N/A"
    print(f"{'Companies in list':<45} {len(REAL_COMPANY_BOARDS):>8}")
    print(f"{'Companies reachable':<45} {companies_reachable:>8}")
    print(f"{'Companies unreachable':<45} {len(companies_unreachable):>8}")
    print(f"{'Raw postings discovered':<45} {raw_postings:>8} {'100.0%':>10}")
    print()
    exp_total_reject = exp_seniority_title + exp_exceeded + exp_unparseable
    print(f"{'  ✗ Exp: Seniority title reject':<45} {exp_seniority_title:>8} {pct(exp_seniority_title):>10}")
    print(f"{'  ✗ Exp: Numeric exceeded (>3yr)':<45} {exp_exceeded:>8} {pct(exp_exceeded):>10}")
    print(f"{'  ✗ Exp: UNPARSEABLE (hard skip)':<45} {exp_unparseable:>8} {pct(exp_unparseable):>10}")
    print(f"{'  ── Exp gate total rejected':<45} {exp_total_reject:>8} {pct(exp_total_reject):>10}")
    print(f"{'After experience gate (pass)':<45} {exp_pass:>8} {pct(exp_pass):>10}")
    print()
    rel_total_reject = rel_forbidden + rel_non_target + rel_low_relevance
    print(f"{'  ✗ Rel: Forbidden domain':<45} {rel_forbidden:>8} {pct(rel_forbidden):>10}")
    print(f"{'  ✗ Rel: Non-target role title':<45} {rel_non_target:>8} {pct(rel_non_target):>10}")
    print(f"{'  ✗ Rel: Low relevance score (<0.60)':<45} {rel_low_relevance:>8} {pct(rel_low_relevance):>10}")
    print(f"{'  ── Rel gate total rejected':<45} {rel_total_reject:>8} {pct(rel_total_reject):>10}")
    print(f"{'After relevance gate (pass)':<45} {rel_pass:>8} {pct(rel_pass):>10}")
    print()
    print(f"{'  ✗ Sponsorship: explicit no':<45} {sponsor_explicit_no:>8} {pct(sponsor_explicit_no):>10}")
    print(f"{'After sponsorship gate (pass)':<45} {sponsor_pass:>8} {pct(sponsor_pass):>10}")
    print()
    print(f"{'  ✗ Dedupe removed':<45} {dedupe_removed:>8}")
    print(f"{'ELIGIBLE (final)':<45} {eligible:>8} {pct(eligible):>10}")

    # ─── Score Distribution ──────────────────────────────────────────────
    if relevance_scores:
        print(f"\n{'='*80}")
        print("RELEVANCE SCORE DISTRIBUTION (all postings that reached the relevance gate)")
        print(f"{'='*80}")
        buckets = Counter()
        for s in relevance_scores:
            if s >= 0.8: buckets["0.80-1.00"] += 1
            elif s >= 0.6: buckets["0.60-0.79 (PASS)"] += 1
            elif s >= 0.4: buckets["0.40-0.59 (NEAR MISS)"] += 1
            elif s >= 0.2: buckets["0.20-0.39"] += 1
            else: buckets["0.00-0.19"] += 1
        for bucket in ["0.00-0.19", "0.20-0.39", "0.40-0.59 (NEAR MISS)", "0.60-0.79 (PASS)", "0.80-1.00"]:
            count = buckets.get(bucket, 0)
            bar = "█" * (count // max(1, len(relevance_scores) // 40 or 1))
            print(f"  {bucket:<30} {count:>6}  {bar}")

    # ─── Unparseable Experience Samples ──────────────────────────────────
    if exp_unparseable_samples:
        print(f"\n{'='*80}")
        print(f"UNPARSEABLE EXPERIENCE SAMPLES (first {len(exp_unparseable_samples)})")
        print(f"{'='*80}")
        for s in exp_unparseable_samples:
            print(f"  • {s['company']}: {s['title']}")
            print(f"    {s['url']}")

    # ─── Low Relevance Samples ───────────────────────────────────────────
    if rel_low_samples:
        print(f"\n{'='*80}")
        print(f"LOW RELEVANCE SAMPLES (first {len(rel_low_samples)})")
        print(f"{'='*80}")
        for s in rel_low_samples:
            print(f"  • {s['company']}: {s['title']} (score={s['score']:.2f}, skills={s['matched_skills'][:5]})")

    # ─── Experience Exceeded Samples ─────────────────────────────────────
    if exp_exceeded_samples:
        print(f"\n{'='*80}")
        print(f"EXPERIENCE EXCEEDED SAMPLES (first {len(exp_exceeded_samples)})")
        print(f"{'='*80}")
        for s in exp_exceeded_samples:
            print(f"  • {s['company']}: {s['title']}")
            print(f"    {s['reason']}")

    # ─── Unreachable Companies ───────────────────────────────────────────
    if companies_unreachable:
        print(f"\n{'='*80}")
        print(f"UNREACHABLE COMPANIES ({len(companies_unreachable)})")
        print(f"{'='*80}")
        for name, btype, handle, status in companies_unreachable:
            print(f"  ✗ {name} ({btype}/{handle}): {status}")

    # ─── BOTTLENECK DIAGNOSIS ────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("BOTTLENECK DIAGNOSIS")
    print(f"{'='*80}")

    stages = [
        ("Raw postings", raw_postings),
        ("After experience gate", exp_pass),
        ("After relevance gate", rel_pass),
        ("After sponsorship gate", sponsor_pass),
        ("Eligible (after dedupe)", eligible),
    ]
    worst_stage = None
    worst_drop = 0
    for i in range(1, len(stages)):
        prev_name, prev_count = stages[i-1]
        curr_name, curr_count = stages[i]
        if prev_count > 0:
            drop_pct = (prev_count - curr_count) / prev_count * 100
            if drop_pct > worst_drop:
                worst_drop = drop_pct
                worst_stage = (prev_name, curr_name, prev_count, curr_count, drop_pct)

    if worst_stage:
        pn, cn, pc, cc, dp = worst_stage
        print(f"  WORST DROP: {pn} ({pc}) → {cn} ({cc}) = {dp:.1f}% loss")

    if exp_unparseable > 0 and exp_unparseable > exp_pass:
        print(f"\n  ⚠ UNPARSEABLE EXPERIENCE is the dominant reject reason ({exp_unparseable}/{raw_postings} raw postings).")
        print(f"    Most postings don't state explicit year requirements.")
        print(f"    PROPOSED FIX: If no experience mentioned AND no seniority title → PASS (assume entry-level compatible).")
        print(f"    PROJECTED IMPACT: ~{exp_unparseable} additional postings would reach the relevance gate.")

    if exp_seniority_title > raw_postings * 0.4:
        print(f"\n  ⚠ SENIORITY TITLE filtering is very aggressive ({exp_seniority_title}/{raw_postings} = {exp_seniority_title/max(1,raw_postings)*100:.0f}%).")
        print(f"    Check if patterns like 'lead', 'architect', 'iii' are over-matching.")

    if rel_non_target + rel_forbidden > rel_pass:
        print(f"\n  ⚠ RELEVANCE GATE non-target/forbidden is rejecting heavily.")
        print(f"    Many postings have titles outside target role families.")

    print(f"\n{'='*80}")
    print("DONE")
    print(f"{'='*80}")

    # Save raw data for analysis
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "companies_in_list": len(REAL_COMPANY_BOARDS),
        "companies_reachable": companies_reachable,
        "companies_unreachable": len(companies_unreachable),
        "raw_postings": raw_postings,
        "exp_pass": exp_pass,
        "exp_seniority_title": exp_seniority_title,
        "exp_exceeded": exp_exceeded,
        "exp_unparseable": exp_unparseable,
        "rel_pass": rel_pass,
        "rel_forbidden": rel_forbidden,
        "rel_non_target": rel_non_target,
        "rel_low_relevance": rel_low_relevance,
        "sponsor_pass": sponsor_pass,
        "sponsor_explicit_no": sponsor_explicit_no,
        "dedupe_removed": dedupe_removed,
        "eligible": eligible,
        "per_company": per_company_stats,
        "unparseable_samples": exp_unparseable_samples,
        "low_relevance_samples": rel_low_samples,
    }
    with open(os.path.join(os.path.dirname(__file__), "data", "funnel_diagnostic.json"), "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nRaw data saved to data/funnel_diagnostic.json")


if __name__ == "__main__":
    asyncio.run(main())
