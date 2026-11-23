"""
Grounded Open-Text Answer Writer + Verifier  (extends the Why-company essay rule to ANY open-text question)

Standing rule (Nov 22):
  1. SCOPE — Answers ANY open-text / descriptive question on a job form where the answer can be built from
     the candidate's verified record:
       - "Why [company]?" / motivation questions ("In one or two sentences, why Kira and why this role?",
         "Why WHOOP?", "Why are you interested in joining WRITER?", "What got you excited about this role?").
       - "Describe your experience in AI and Python" / "Describe your experience with [skill/tool]" /
         "Tell us about your experience in [area]".
       - "Why are you a good fit for this role?" / "What makes you a strong candidate?".
       - "Describe a project you are proud of" / "Tell us about a challenging project" / "Describe a bug you fixed".
       - Any other open-text question groundable in his real record.
  2. GROUNDING (all required):
       a. ONLY true facts from profile.json, resume, GitHub (https://github.com/sids202001), and portfolio
          (https://siddhesh202001-portfolio.netlify.app).
       b. Where the question is about a company/role, also read the JD and the company's about page, and
          connect his real work to the role's actual requirements.
       c. Every claim must be traceable to (a) or (b). No invented facts, skills, experience, metrics,
          employers, or dates. No generic praise or filler.
  3. PRESUBMIT_REVIEW GATE:
       Every claim in every written open-text answer must be grounded in profile.json/resume/GitHub/portfolio
       or the JD -> PASS; any ungrounded claim -> FAIL (PRESUBMIT_REVIEW_FAILED), halt and park.
       Achievement and metric claims ("reduced X from hours to minutes", percentages, "built Y that did Z")
       must match the same achievement/metric in the record — a tool or skill name alone NEVER grounds an
       achievement claim.
  4. HARD LIMITS — still HALTS (never guesses, never writes):
       - "Is there anything else?" / "Anything else to add?" -> leave blank (optional) or halt (required).
       - Self-ratings of skills/languages (1-10 scales, unrated languages like Spanish) -> halt.
       - Sensitive personal facts not in profile.json (date of birth, current age, SSN, etc.) -> halt.
       - Decisions or commitments only he can make (work weekends, pay cut, night shifts, etc.) -> halt and ask.
       - Non-standard legal attestations/certifications beyond standard application consent -> halt and ask.
       - Any question where no concrete link to his real record exists -> halt and park, never generic filler.
"""

from __future__ import annotations

import contextvars
import hashlib
import html
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

HALT_CODE = "ESSAY_HALT"
ESSAY_LOG_PATH = "/Users/siddheshsawant/sid_job_scrip/data/grounded_essays.jsonl"

# ============================================================================
# (2a) Candidate fact bank — ONLY verified facts from profile.json, resume,
#      GitHub (github.com/sids202001), and portfolio (siddhesh202001-portfolio.netlify.app).
#      Each fact includes `achievements`: specific achievement/metric signatures required
#      to ground any achievement claim (a tool/skill name alone is NOT sufficient).
# ============================================================================
CANDIDATE_FACTS: Dict[str, Dict[str, Any]] = {
    "EXP_AWS_VALIDATION": {
        "source": "resume",
        "kinds": ["experience", "project", "bug", "challenge", "fit", "company", "aws", "data", "etl", "python"],
        "anchors": ["amazon web services", "aws", "step functions", "aws batch", "batch", "s3", "etl", "validation"],
        "achievements": [
            "race condition", "race conditions", "validation framework", "automated validation",
            "invalid data", "pipeline stability", "etl pipeline", "step functions",
        ],
        "metrics": [],
        "tags": [
            "aws", "python", "etl", "data", "pipeline", "pipelines", "validation", "batch", "step functions", "s3",
            "reliability", "workflow", "orchestration", "backend", "distributed", "automation", "deterministic",
            "bug", "debug", "debugged", "race condition", "challenge", "challenging", "quality", "testing",
        ],
        "text": "As a Software Development Engineer at Amazon Web Services (AWS Infra Supply Chain, Mar 2025 - Mar 2026), "
                "I eliminated race conditions in AWS Batch and designed an automated validation framework using AWS Step "
                "Functions and S3 that prevented invalid data from reaching production ETL pipelines.",
    },
    "EXP_AWS_MONITORING": {
        "source": "resume",
        "kinds": ["experience", "bug", "challenge", "incident", "fit", "company", "aws", "monitoring"],
        "anchors": ["amazon web services", "aws", "cloudwatch", "lambda", "monitoring", "alerting", "mean time to detect"],
        "achievements": [
            "hours to minutes", "mean time to detect", "real-time monitoring", "real time monitoring",
            "workflow failures", "monitoring and alerting", "detect workflow failures",
        ],
        "metrics": ["hours to minutes"],
        "tags": [
            "aws", "cloudwatch", "lambda", "monitoring", "alerting", "telemetry", "observability", "performance",
            "failures", "workflow", "realtime", "real-time", "production", "incident", "diagnostics", "compute",
            "bug", "debug", "reliability", "downtime",
        ],
        "text": "At Amazon Web Services (AWS Infra Supply Chain), I reduced mean time to detect workflow failures from "
                "hours to minutes by implementing real-time monitoring and alerting with CloudWatch and Lambda.",
    },
    "EXP_AWS_MCP_ASSISTANT": {
        "source": "resume",
        "kinds": ["experience", "project", "ai", "fit", "company", "llm", "mcp"],
        "anchors": ["amazon web services", "aws", "mcp", "ticketing assistant", "claude", "issue triage"],
        "achievements": [
            "ticketing assistant", "issue triage", "mcp-based", "mcp based", "automate issue triage",
            "accelerate testing and debugging", "testing and debugging", "rapid code generation",
        ],
        "metrics": [],
        "tags": [
            "ai", "llm", "mcp", "claude", "agent", "agents", "agentic", "assistant", "automation", "triage",
            "debugging", "testing", "workflow", "connectors", "skills", "context", "tooling", "python",
            "generative ai", "coding tools",
        ],
        "text": "At Amazon Web Services, I co-developed an internal MCP-based AI ticketing assistant to automate issue "
                "triage and leveraged Claude to accelerate testing and debugging.",
    },
    "EXP_AWS_BACKEND": {
        "source": "resume",
        "kinds": ["experience", "fit", "company", "backend", "api", "security"],
        "anchors": ["amazon web services", "aws", "restful", "api gateway", "iam", "backend services"],
        "achievements": [
            "backend services", "restful apis", "restful api", "secure access", "api gateway",
            "enforcing secure access", "system performance",
        ],
        "metrics": [],
        "tags": [
            "aws", "api", "apis", "restful", "backend", "iam", "api gateway", "security", "scalable",
            "services", "integrations", "webhook", "microservices", "fullstack", "full-stack", "software",
        ],
        "text": "At Amazon Web Services, I developed and maintained scalable backend services and RESTful APIs while "
                "enforcing secure access with AWS IAM and API Gateway.",
    },
    "EXP_AWS_ONCALL_SQL": {
        "source": "resume",
        "kinds": ["experience", "bug", "incident", "challenge", "sql", "data"],
        "anchors": ["amazon web services", "aws", "redshift", "postgresql", "sql", "on-call", "incident"],
        "achievements": [
            "incident resolution", "on-call", "on call", "optimizing sql", "sql queries", "redshift",
            "debugging and triage time", "triage time",
        ],
        "metrics": [],
        "tags": [
            "sql", "redshift", "postgresql", "database", "relational", "warehouse", "query", "queries", "on-call",
            "incident", "triage", "debugging", "bug", "analytics", "data", "optimization",
        ],
        "text": "During on-call rotations at Amazon Web Services, I accelerated incident resolution by optimizing SQL "
                "queries and analyzing Amazon Redshift and PostgreSQL data.",
    },
    "EXP_AWS_SUPPLY_CHAIN": {
        "source": "resume",
        "kinds": ["experience", "data", "python", "fit", "company"],
        "anchors": ["amazon web services", "aws", "supply chain", "reporting", "inventory", "fulfillment", "python"],
        "achievements": [
            "supply chain reporting", "data analysis pipelines", "inventory anomalies", "fulfillment visibility",
            "supply chain",
        ],
        "metrics": [],
        "tags": [
            "python", "sql", "supply chain", "reporting", "data analysis", "pipelines", "dashboards",
            "anomalies", "operations", "metrics", "business", "business systems", "finance", "reconciliation",
            "logistics", "internal tools",
        ],
        "text": "On the AWS Infra Supply Chain team, I automated supply chain reporting and data analysis pipelines "
                "using Python and dashboards to identify inventory anomalies and improve fulfillment visibility.",
    },
    "EXP_GMU_RA": {
        "source": "resume",
        "kinds": ["experience", "ai", "nlp", "python", "research", "education"],
        "anchors": ["george mason university", "gmu", "research assistant", "nlp", "spacy", "nltk"],
        "achievements": [
            "research assistant", "assessment methods", "large-scale assessment", "mentored students",
            "spacy", "nltk", "nlp pipelines", "nlp and data analysis",
        ],
        "metrics": [],
        "tags": [
            "python", "nlp", "spacy", "nltk", "research", "machine learning", "ai", "evaluation", "assessment",
            "data analysis", "text", "language", "clinical", "conversations", "documentation", "teaching",
            "learning", "students", "education", "mentorship",
        ],
        "text": "As a Research Assistant at George Mason University (Jun 2024 - Feb 2025), I conducted applied data "
                "analysis research improving large-scale assessment methods and mentored students in NLP pipelines "
                "using Python (spaCy, NLTK).",
    },
    "EXP_TALVIRO": {
        "source": "resume/profile.json",
        "kinds": ["experience", "backend", "sql", "docker", "fit"],
        "anchors": ["talviro", "restful", "sql", "docker", "claude"],
        "achievements": [
            "talviro", "restful apis", "complex sql queries", "containerize services with docker",
            "consistent development environments",
        ],
        "metrics": [],
        "tags": [
            "python", "sql", "api", "apis", "restful", "docker", "backend", "claude", "compliance", "software",
            "testing", "debugging", "sdlc",
        ],
        "text": "As a Software Developer at Talviro INC (Jun 2026 - Present), I build secure backend features, "
                "optimize RESTful APIs and complex SQL queries, and containerize services with Docker.",
    },
    "EXP_SUBHASH_SNOWFLAKE": {
        "source": "resume/portfolio",
        "kinds": ["experience", "data", "sql", "python", "snowflake", "finance"],
        "anchors": ["subhash construction", "snowflake", "power bi", "tableau", "financial", "sql", "python"],
        "achievements": [
            "subhash construction", "25%", "power bi", "tableau", "snowflake data models",
            "operational and financial data", "improving efficiency by 25%",
        ],
        "metrics": ["25%"],
        "tags": [
            "snowflake", "sql", "python", "finance", "financial", "operational", "kpi", "metrics",
            "tableau", "power bi", "reporting", "warehouse", "semantic", "accounting", "data modeling",
            "business analyst", "business systems",
        ],
        "text": "As a Business Analyst at Subhash Construction, I analyzed operational and financial data with Python "
                "and SQL (improving workflow efficiency by 25%) and built Tableau and Power BI KPI dashboards backed "
                "by Snowflake data models.",
    },
    "PROJ_JOB_SCOUT": {
        "source": "github/portfolio",
        "kinds": ["project", "ai", "python", "challenge", "fit", "company", "llm"],
        "anchors": ["autonomous ai job scout", "playwright", "fastapi", "gemini", "greenhouse", "ashby", "lever"],
        "achievements": [
            "autonomous ai job scout", "job application engine", "playwright", "resume-grounding",
            "factual guardrails", "factual resume-grounding guardrails", "greenhouse, lever, and ashby",
            "deterministic rule engine",
        ],
        "metrics": ["150-second", "150"],
        "tags": [
            "python", "javascript", "playwright", "fastapi", "sqlite", "ai", "llm", "gemini", "agent", "agents",
            "autonomous", "automation", "api", "apis", "integrations", "deterministic", "guardrails", "workflow",
            "project", "proudest", "challenging", "fullstack", "full-stack", "rag",
        ],
        "text": "I built Autonomous AI Job Scout, an end-to-end Python, FastAPI, and Playwright autonomous agent that "
                "integrates across Greenhouse, Lever, and Ashby APIs and pairs a deterministic rule engine with LLM "
                "(Gemini) workflows under strict factual resume-grounding guardrails.",
    },
    "PROJ_STOCK_ANALYZER": {
        "source": "github/portfolio",
        "kinds": ["project", "python", "javascript", "data", "finance", "challenge"],
        "anchors": ["algorithmic stock analyzer", "discounted cash flow", "dcf", "yfinance", "scipy", "pandas", "flask"],
        "achievements": [
            "algorithmic stock analyzer", "discounted cash flow", "dcf", "yfinance", "scipy",
            "technical indicator", "multi-scenario", "financial statement data",
        ],
        "metrics": ["50", "200", "14"],
        "tags": [
            "python", "flask", "javascript", "pandas", "numpy", "scipy", "finance", "financial", "valuation",
            "dcf", "data", "pipeline", "sanitization", "metrics", "analytics", "dashboard", "quantitative",
            "project", "proudest", "challenging", "fullstack", "full-stack",
        ],
        "text": "I built Algorithmic Stock Analyzer, a Python (Flask, pandas, NumPy, SciPy) and JavaScript financial "
                "analysis platform that normalizes live market and financial statement data and runs multi-scenario "
                "Discounted Cash Flow valuations and technical indicator scans.",
    },
    "PROJ_BILL_SPLITTER": {
        "source": "github/portfolio",
        "kinds": ["project", "javascript", "frontend"],
        "anchors": ["itemized bill splitter", "javascript", "tailwind", "proportional", "tax", "tip"],
        "achievements": [
            "itemized bill splitter", "proportional tax", "zero-dependency client-side javascript",
            "shared expenses",
        ],
        "metrics": ["100%"],
        "tags": [
            "javascript", "html", "css", "tailwind", "frontend", "web", "expense", "finance", "algorithm",
            "project", "ui",
        ],
        "text": "I built Itemized Bill Splitter, a zero-dependency client-side JavaScript and Tailwind CSS web "
                "application that computes proportional tax, tip, and fee allocations across shared expenses.",
    },
    "EDU": {
        "source": "resume/profile.json",
        "kinds": ["education", "bio", "fit"],
        "anchors": ["george mason university", "information systems", "3.71", "university of mumbai", "information technology"],
        "achievements": [
            "master of science in information systems", "george mason university", "3.71",
            "bachelor of engineering in information technology", "university of mumbai",
        ],
        "metrics": ["3.71", "3.3", "2023", "2024", "2018", "2022", "2+", "2"],
        "tags": ["education", "master", "degree", "gmu", "george mason", "information systems", "gpa"],
        "text": "I hold a Master of Science in Information Systems from George Mason University (GPA 3.71, May 2024) "
                "and a Bachelor of Engineering in Information Technology from the University of Mumbai (Aug 2022).",
    },
}

FACT_KEYS_ORDERED = list(CANDIDATE_FACTS.keys())

# Generic praise / filler that is NEVER allowed (Rule 2c / Rule 3).
BANNED_PHRASES = [
    r"\bpassion(?:ate)?\b",
    r"\byour\s+mission\b",
    r"\bthe\s+mission\b",
    r"\binnovative\b",
    r"\bculture\b",
    r"\bexcit(?:ed|ing)\b",
    r"\bthrilled\b",
    r"\bdream\b",
    r"\bcutting[\s-]edge\b",
    r"\bworld[\s-]class\b",
    r"\binspir(?:ed|ing)\b",
    r"\bi\s+love\b",
    r"\badmire\b",
    r"\brevolutioni[sz]",
    r"\bgame[\s-]chang",
    r"\bindustry\s+leader\b",
    r"\balign(?:s)?\s+with\s+my\s+values\b",
    r"\bimmediate\s+(?:positive\s+)?impact\b",
    r"\bi\s+am\s+confident\b",
    r"\bperfect\s+fit\b",
    r"\bunique\s+opportunity\b",
    r"\bfast[\s-]paced\b",
    r"\bincredible\b",
    r"\bamazing\b",
    r"\bawesome\b",
    r"\bvisionary\b",
    r"\bdeeply\s+resonat",
    r"\bstrongly\s+believe\b",
]

# Skills / technologies the candidate DOES NOT HAVE in profile.json / resume / GitHub / portfolio.
UNOWNED_TECH_TERMS = [
    "kubernetes", "k8s", "gcp", "google cloud", "bigquery", "cloud composer", "dataflow", "azure",
    "rust", "golang", "c\\+\\+", "c#", "scala", "ruby", "php", "swift", "terraform", "spark", "kafka",
    "airflow", "dbt", "flink", "hadoop", "redis", "elasticsearch", "django", "pytorch", "tensorflow",
    "jax", "cuda", "slurm", "ray", "netsuite", "workday", "sap", "looker", "graphql", "grpc", "langchain",
    "bamboohr", "adp",
]

# Employers / universities the candidate never attended or worked for.
UNOWNED_ENTITIES = [
    "google", "meta", "facebook", "apple", "microsoft", "netflix", "stripe", "uber", "airbnb",
    "openai", "anthropic", "palantir", "databricks", "citadel", "stanford", "mit", "harvard", "berkeley",
]

# Boilerplate HR / salary / EEO / legal text that must NEVER be copied as company motivation or about text.
COMPANY_BOILERPLATE_RE = re.compile(
    r"\b(401k|401\(k\)|dental|vision|parental\s+leave|equal\s+opportunity|corporate\s+team\s+to\s+own|"
    r"compensation\s+range|base\s+salary|salary\s+ranges?|pay\s+range|pay\s+transparency|"
    r"unlimited\s+pto|affirmative\s+action|protected\s+veteran|sexual\s+orientation|national\s+origin|"
    r"gender\s+identity|reasonable\s+accommodation|background\s+check|e-verify|drug-free\s+workplace|"
    r"privacy\s+policy|cookie\s+policy|terms\s+of\s+service|does\s+not\s+discriminate|"
    r"actual\s+compensation\s+may\s+vary|determined\s+by\s+role,\s*level)\b",
    re.IGNORECASE,
)

# False company domain inventions to guard against ungrounded company claims.
FALSE_DOMAIN_PROBES = [
    "drone delivery", "autonomous drone", "blockchain", "cryptocurrency", "crypto exchange",
    "self-driving car", "autonomous vehicle", "quantum computing", "satellite constellation",
    "gaming console", "ride-sharing", "food delivery",
]

# False candidate achievement artifacts (things he never built, even if paired with real tool names like Python/AWS).
FALSE_CANDIDATE_ARTIFACTS = [
    "payment gateway", "credit card", "database sharding", "sharding engine", "compiler",
    "distributed tracing engine", "tracing engine", "recommendation engine", "search engine",
    "chat application", "video streaming", "trading desk", "high-frequency trading",
    "order matching engine", "fraud detection model", "kubernetes cluster", "terraform module",
    "kafka consumer", "spark streaming", "airflow dag", "dbt model",
]

FIRST_PERSON_RE = re.compile(r"\b(i|i've|i'm|i'd|my|me|mine)\b", re.IGNORECASE)
ACHIEVEMENT_VERB_RE = re.compile(
    r"\b(built|designed|engineered|developed|implemented|reduced|improved|eliminated|automated|"
    r"optimized|co-developed|created|architected|deployed|migrated|scaled|led|launched|mentored|"
    r"analyzed|programmed|tuned|decreased|slashed|lifted)\b",
    re.IGNORECASE,
)

_STOPWORDS: Set[str] = set(
    "the a an and or but of to in on for with at by from as is are was were be been being this that these those it its "
    "we our us you your they their them he she his her i my me which who whom what when where why how all any both each "
    "few more most other some such no nor not only own same so than too very can will just should now into over under "
    "about after before between through during out up down off again further then once here there also every across "
    "while including within without per via new one two three four five first".split()
)

# ============================================================================
# Application context (company / role / JD)
# ============================================================================
_CTX: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("grounded_essay_ctx", default=None)
_GLOBAL_CTX: Dict[str, Any] = {}
_LOCK = threading.RLock()
_SOURCE_CACHE: Dict[str, Dict[str, Any]] = {}
_ESSAY_CACHE: Dict[str, Dict[str, Any]] = {}
_ESSAY_REGISTRY: Dict[str, Dict[str, Any]] = {}


def set_application_context(
    company: str,
    title: str = "",
    url: str = "",
    jd_text: str = "",
    about_text: str = "",
    about_source: str = "",
) -> Dict[str, Any]:
    ctx = {
        "company": (company or "").strip(),
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "jd_text": jd_text or "",
        "about_text": about_text or "",
        "about_source": about_source or "",
    }
    _CTX.set(ctx)
    with _LOCK:
        _GLOBAL_CTX.clear()
        _GLOBAL_CTX.update(ctx)
    return ctx


def clear_application_context() -> None:
    _CTX.set(None)
    with _LOCK:
        _GLOBAL_CTX.clear()


def get_application_context() -> Optional[Dict[str, Any]]:
    ctx = _CTX.get()
    if ctx:
        return ctx
    with _LOCK:
        return dict(_GLOBAL_CTX) if _GLOBAL_CTX else None


def company_from_url(url: str) -> str:
    u = url or ""
    for pat in (
        r"jobs\.ashbyhq\.com/([^/?#]+)",
        r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)",
        r"jobs\.lever\.co/([^/?#]+)",
        r"[?&]for=([^&#]+)",
    ):
        m = re.search(pat, u, re.IGNORECASE)
        if m and m.group(1).lower() != "embed":
            return m.group(1)
    return ""


def _norm_url(u: str) -> str:
    return re.sub(r"/application/?$", "", re.sub(r"[?#].*$", "", (u or "").strip().lower())).rstrip("/")


def ensure_context_for_url(url: str, page_text: str = "") -> Optional[Dict[str, Any]]:
    """Keep daemon-provided context if it matches `url`, else derive context from URL + page text."""
    ctx = get_application_context()
    if ctx and url and (_norm_url(ctx.get("url", "")) == _norm_url(url) or (ctx.get("url") and ctx["url"] == url)):
        if page_text and len(page_text) > len(ctx.get("jd_text") or ""):
            ctx = dict(ctx, page_text=page_text)
            _CTX.set(ctx)
            with _LOCK:
                _GLOBAL_CTX.update(ctx)
        return ctx
    slug = company_from_url(url)
    if not slug:
        return ctx
    return set_application_context(company=slug, url=url, jd_text=page_text or "")


# ============================================================================
# Hard Limits (Requirement 4) & Open-Text Question Classification (Requirement 1)
# ============================================================================
_GENERIC_TARGETS = {
    "us", "here", "our company", "this company", "the company", "our team", "this team",
    "this role", "the role", "this position", "the position", "this opportunity",
}
_NOT_COMPANY_TARGETS = {
    "this role", "the role", "this position", "the position", "this job", "the job", "this opportunity",
    "you", "not", "should we hire you", "we should hire you", "should we hire", "now", "this",
}

_WHY_COMPANY_PATTERNS = [
    # "In one or two sentences, why Kira and why this role?"
    r"^\s*(?:in\s+(?:one|two|three|1|2|3|a\s+few)(?:\s+or\s+(?:two|three|2|3))?\s+sentences?,?\s*)?"
    r"why\s+(?P<name>[A-Za-z0-9][\w&.'\- ]{0,35}?)\s+and\s+why\s+this\s+(?:role|position|job|opportunity)\s*[?.!:]*\s*$",
    # "Why are you interested in working at X?" / "...in joining X?" / "...in X?"
    r"^\s*(?:in\s+(?:one|two|three|1|2|3|a\s+few)(?:\s+or\s+(?:two|three|2|3))?\s+sentences?,?\s*)?"
    r"why\s+are\s+you\s+interested\s+in\s+(?:working\s+(?:at|for|with)\s+|joining\s+)?(?P<name>.+?)\s*[?.!:]*\s*$",
    # "Why do you want to work at X?" / "Why would you like to join X?"
    r"^\s*(?:in\s+(?:one|two|three|1|2|3|a\s+few)(?:\s+or\s+(?:two|three|2|3))?\s+sentences?,?\s*)?"
    r"why\s+(?:do|would)\s+you\s+(?:want|like)\s+to\s+(?:work\s+(?:at|for|with)|join)\s+(?P<name>.+?)\s*[?.!:]*\s*$",
    # "Why join X?" / "Why work at X?"
    r"^\s*why\s+(?:join|work\s+(?:at|for|with))\s+(?P<name>.+?)\s*[?.!:]*\s*$",
    # "Why X?" / "Why X"
    r"^\s*why\s+(?P<name>[A-Za-z0-9][\w&.'\- ]{0,40}?)\s*[?.!:]*\s*$",
]

# Hard Limit 4a: "Is there anything else?" / "Additional information"
_ANYTHING_ELSE_PATTERNS = [
    r"\bis\s+there\s+anything\s+else\b",
    r"\banything\s+else\s+(?:you(?:'d|\s+would)\s+like|to\s+add|to\s+share|we\s+should\s+know)\b",
    r"^\s*anything\s+else\??\s*$",
    r"^\s*(?:any\s+)?additional\s+(?:information|comments|notes|context|details)\??\s*$",
    r"\bshare\s+anything\s+else\b",
]

# Hard Limit 4b: Self-ratings / scales / unrated language proficiency
_SELF_RATING_PATTERNS = [
    r"\brate\s+your(?:self)?\b",
    r"\brate\s+your\s+(?:level|proficiency|knowledge|skill|speaking|reading|writing|experience|fluency)\b",
    r"\brank\s+yourself\s+out\s+of\s+\d+",
    r"\bon\s+a\s+scale\s+of\s+(?:1|0)\s*(?:to|-)\s*(?:5|10)\b",
    r"\bscale\s+(?:from\s+)?(?:1|0)\s*(?:to|-)\s*(?:5|10)\b",
    r"\b(?:spanish|french|german|mandarin|cantonese|japanese|korean|arabic|portuguese|russian|italian)\s+"
    r"(?:proficiency|fluency|level|speaking|reading|writing)\b",
]

# Hard Limit 4c: Sensitive personal facts not in profile.json
_SENSITIVE_PERSONAL_PATTERNS = [
    r"\b(?:date\s+of\s+birth|birth\s*date|d\.?o\.?b\.?|birthday)\b",
    r"\b(?:what\s+is\s+your\s+(?:current\s+)?age|current\s+age|how\s+old\s+are\s+you|your\s+age)\b",
    r"\b(?:social\s+security|ssn|national\s+id(?:entity)?\s+number|passport\s+number|driver'?s?\s+license\s+number|tax\s+id)\b",
    r"\b(?:marital\s+status|number\s+of\s+dependents|spouse'?s?\s+name)\b",
    r"\b(?:salary\s+history|compensation\s+history|narrative\s+regarding\s+compensation|explain\s+your\s+compensation)\b",
    r"\b(?:google\s+scholar|publications?\s*(?:url|link)?|orcid)\b",
    r"^\s*additional\s+visa\s+information\s*$",
]

# Hard Limit 4d: Personal decisions / commitments only the candidate can make
_PERSONAL_DECISION_PATTERNS = [
    r"\b(?:willing\s+to\s+work|work)\s+weekends?\b",
    r"\bweekend\s+shifts?\b",
    r"\b(?:willing\s+to\s+work|work)\s+(?:night|graveyard|overnight)\s+shifts?\b",
    r"\b(?:accept|take|willing\s+to\s+accept)\s+a?\s*(?:pay\s+cut|salary\s+reduction|lower\s+salary)\b",
    r"\b(?:when|how\s+soon)\s+(?:can|could|would)\s+you\s+(?:be\s+able\s+to\s+)?relocate\b",
    r"\brelocation\s+timeline\b",
    r"\b(?:30-?second|voice|video)\s+recording\b",
]

# Hard Limit 4e: Non-standard legal attestations / certifications
_NONSTANDARD_LEGAL_PATTERNS = [
    r"\b(?:bar\s+admission|state\s+bar\s+number|medical\s+license\s+number|cpa\s+license|pe\s+license|finra\s+series)\b",
    r"\b(?:export\s+license\s+number|security\s+clearance\s+adjudication)\b",
    r"\b(?:waive\s+your\s+right\s+to\s+a\s+jury|binding\s+arbitration\s+agreement\s+signature)\b",
]

# Hard Limit 4f: Ungroundable topics (reading history in past month, favorite color, abstract philosophy, unowned domains)
_UNGROUNDABLE_TOPIC_PATTERNS = [
    r"\bmost\s+interesting\s+(?:[\w\s,]{0,30})?(?:paper|blog\s*post|article|book|documentation)\b.*\bread\b",
    r"\b(?:paper|blog\s*post|book|article)\s+(?:you(?:'ve)?\s+)?read\s+(?:recently|in\s+the\s+past\s+month|lately)\b",
    r"\bsecret\s+favorite\s+color\b",
    r"\beconomic\s+empowerment\b",
    r"\bwhat\s+does\s+(?:purpose|mission|economic\s+empowerment)\s+mean\b",
    r"\bmission\s+means\s+to\s+you\b",
    r"\bvalues\s+align\b",
    r"\bhris\s*/?\s*ats\s+data\b",
    r"\bhr\s+stakeholders\s+to\s+build\s+an\s+analytics\s+pipeline\b",
    r"\bexperience\s+in\s+travel\b|\btravel-?adjacent\s+services\b",
    r"\bembedded\s+with\s+(?:an?\s+)?enterprise\s+customer\b",
    r"\bfine-?tuning\s+(?:open-?weight\s+)?models\b",
    r"\bselect\s+which\s+part\s+of\s+the\s+title\b.*\bresonates\s+more\b",
    r"^\s*education\s+history\s*$",
    r"\bcareer\s+goals\s+in\s+the\s+next\s+few\s+years\b",
]


def _clean_label(question: str) -> str:
    q = re.sub(r"[✱*]", " ", question or "")
    q = re.sub(r"\s*\(?\b(required|optional)\b\)?\s*$", "", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def _company_key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(unlisted|inc|llc|ltd|corp|corporation|co|technologies|technology|labs|ai|hq)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def is_anything_else_question(question: str) -> bool:
    q = _clean_label(question).lower()
    if not q or re.search(r"\b(if\s+you\s+answered|if\s+yes|please\s+explain\s+if)\b", q):
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _ANYTHING_ELSE_PATTERNS)


def check_open_text_hard_limit(question: str) -> Tuple[bool, str, str]:
    """Check if `question` hits one of the Hard Limits in Requirement 4.
    Returns (is_hard_limit, limit_type, reason)."""
    q = _clean_label(question)
    q_low = q.lower()
    if not q:
        return False, "", ""

    if is_anything_else_question(q):
        return True, "anything_else", "Hard limit (4): 'Is there anything else?' / additional info must be left blank"

    for p in _SELF_RATING_PATTERNS:
        if re.search(p, q_low, re.IGNORECASE):
            return True, "self_rating", f"Hard limit (4): Self-rating / proficiency scale question ('{q}')"

    for p in _SENSITIVE_PERSONAL_PATTERNS:
        if re.search(p, q_low, re.IGNORECASE):
            return True, "sensitive_personal_fact", f"Hard limit (4): Sensitive personal fact not in profile.json ('{q}')"

    for p in _PERSONAL_DECISION_PATTERNS:
        if re.search(p, q_low, re.IGNORECASE):
            return True, "personal_decision", f"Hard limit (4): Personal schedule/compensation/timeline commitment ('{q}')"

    for p in _NONSTANDARD_LEGAL_PATTERNS:
        if re.search(p, q_low, re.IGNORECASE):
            return True, "legal_attestation", f"Hard limit (4): Non-standard legal attestation/license ('{q}')"

    for p in _UNGROUNDABLE_TOPIC_PATTERNS:
        if re.search(p, q_low, re.IGNORECASE):
            return True, "ungroundable_topic", f"Hard limit (4/5): Topic not in verified candidate record ('{q}')"

    # Check if the question specifically asks about the candidate's experience with an UNOWNED technology
    q_norm = _norm_text(q)
    for ut in UNOWNED_TECH_TERMS:
        if _term_present(ut, q_norm):
            return (
                True,
                "unowned_skill",
                f"Hard limit (4/5): Candidate has no verified '{ut}' experience in profile.json/resume/GitHub/portfolio",
            )

    return False, "", ""


def extract_why_company_target(question: str) -> Optional[str]:
    q = _clean_label(question)
    if not q or len(q) > 160 or "\n" in q:
        return None
    for pat in _WHY_COMPANY_PATTERNS:
        m = re.match(pat, q, re.IGNORECASE)
        if m:
            name = m.group("name").strip().strip("?.!:").strip()
            # Handle "why Kira and why this role" -> "Kira"
            name = re.sub(r"\s+and\s+why\s+this\s+(?:role|position|job|opportunity).*$", "", name, flags=re.I).strip()
            low = name.lower()
            if not name or low in _NOT_COMPANY_TARGETS:
                return None
            if re.search(
                r"\b(role|position|job|opportunity|fit|hire|team\s+lead|candidate|should|we|apply|applying)\b",
                low,
            ) and low not in _GENERIC_TARGETS:
                return None
            return name
    return None


def is_why_company_question(question: str, company: Optional[str] = None) -> bool:
    """True for 'Why [company]?' / 'Why are you interested in working at [company]?' / 'Why [company]' /
    'In one or two sentences, why [company] and why this role?'."""
    target = extract_why_company_target(question)
    if not target:
        return False
    low = target.lower()
    if low in _GENERIC_TARGETS:
        return True
    if company:
        a, b = _company_key(target), _company_key(company)
        return bool(a and b and (a == b or a in b or b in a))
    return target[:1].isupper() or bool(
        re.match(r"^\s*(?:in\s+.*sentences?,?\s*)?why\s+(are|do|would|join|work)\b", _clean_label(question), re.I)
    )


def extract_named_company_in_question(question: str) -> Optional[str]:
    """Extract an explicit company target from questions like 'Why are you a good fit for this role at Anthropic?'
    or 'Why are you interested in joining WRITER?' or 'Why WHOOP?'."""
    target = extract_why_company_target(question)
    if target and target.lower() not in _GENERIC_TARGETS and target.lower() not in _NOT_COMPANY_TARGETS:
        return target
    q = _clean_label(question)
    m = re.search(
        r"\b(?:at|join(?:ing)?|with|for)\s+([A-Z][A-Za-z0-9&.'\-]{1,30})\s*[?.!:]*\s*$",
        q,
    )
    if m:
        cand = m.group(1).strip().strip("?.!:")
        if cand.lower() not in {
            "us", "here", "role", "position", "company", "team", "opportunity",
            "python", "sql", "aws", "java", "javascript", "ai", "ml", "nlp", "etl", "docker", "snowflake",
        }:
            return cand
    return None


def requires_company_grounding(question: str) -> bool:
    """True if the question is about a specific company or role (2b) and therefore requires readable JD/about sources."""
    if is_why_company_question(question):
        return True
    if extract_named_company_in_question(question):
        return True
    q_low = _clean_label(question).lower()
    return bool(
        re.search(
            r"\b(why\s+this\s+(?:role|position|company|opportunity)|"
            r"why\s+are\s+you\s+(?:a\s+)?(?:good|great|strong)\s+fit|"
            r"good\s+fit\s+for\s+(?:this|the)\s+(?:role|position|team|company)|"
            r"what\s+makes\s+you\s+a\s+(?:good|great|strong)\s+(?:fit|candidate)|"
            r"why\s+should\s+we\s+hire\s+you|"
            r"what\s+(?:got\s+you\s+excited|excites\s+you|excited\s+you|interests?\s+you|attracts\s+you|draws\s+you|motivates\s+you)"
            r"\s+(?:about|to)\s+(?:this|the|our|working|joining|applying)|"
            r"motivation\s+for\s+applying|"
            r"why\s+are\s+you\s+applying|"
            r"cover\s+letter|note\s+to\s+(?:the\s+)?hiring|message\s+to\s+(?:the\s+)?(?:hiring|recruitment))\b",
            q_low,
        )
    )


_OPEN_TEXT_PATTERNS = [
    # Motivation / Why company / Why role / Fit
    r"\bwhy\s+(?:are\s+you|do\s+you|would\s+you|should\s+we|this\s+(?:role|company|position|opportunity)|join|work)\b",
    r"\bwhat\s+(?:got\s+you\s+excited|excites\s+you|excited\s+you|interests?\s+you|attracts\s+you|draws\s+you|motivates\s+you)\b",
    r"\b(?:good|great|strong)\s+(?:fit|candidate)\b",
    r"\bmotivation\s+for\s+applying\b",
    # Experience / skills / background / what have you built
    r"\b(?:describe|tell\s+us\s+about|explain|share|elaborate\s+on|summarize|walk\s+us\s+through|what\s+is)\s+your\s+"
    r"(?:experience|background|expertise|proficiency|work|familiarity|knowledge|qualifications|skills)\b",
    r"\bexperience\s+(?:in|with|building|developing|designing|working\s+with|using)\b",
    r"\bhow\s+have\s+you\s+(?:used|applied|built|worked\s+with|implemented|leveraged)\b",
    r"\bwhat\s+(?:have\s+you\s+built|did\s+you\s+build)\b",
    r"\bgive\s+an\s+example\s+(?:of|from)\s+(?:your\s+)?(?:professional\s+|work\s+|engineering\s+)?experience\b",
    # Projects / accomplishments / bugs / challenges / incidents
    r"\b(?:describe|tell\s+us\s+about|explain|share|walk\s+us\s+through|give\s+an\s+example\s+of)\s+"
    r"(?:a|an|one|your|the)\s+(?:[\w\s,\-]{0,35})?"
    r"(?:project|accomplishment|achievement|system|application|pipeline|tool|feature|bug|issue|problem|challenge|incident|obstacle|time|situation)\b",
    r"\bproudest\s+(?:[\w\s]{0,20})?(?:project|achievement|accomplishment|work|system)\b",
    r"\b(?:challenging|difficult|complex|interesting)\s+(?:technical\s+)?(?:project|bug|problem|issue|challenge|incident|system)\b",
    r"\b(?:bug|issue|incident)\s+(?:you|that\s+you)\s+(?:fixed|debugged|resolved|solved|troubleshot)\b",
    r"\bgreatest\s+(?:[\w\s]{0,20})?(?:accomplishment|achievement|strength)\b",
    # General bio / summary / cover letter
    r"\btell\s+us\s+about\s+yourself\b",
    r"\bsummary\s+of\s+(?:your\s+)?qualifications\b",
    r"\bcover\s+(?:letter|note)\b",
    r"\bnote\s+to\s+(?:the\s+)?hiring\b",
    r"\bmessage\s+to\s+(?:the\s+)?(?:hiring|recruitment)\b",
]


def is_open_text_question(question: str) -> bool:
    """True if `question` is an open-text descriptive/motivation/experience/project/bug/fit question
    (or a hard-limit open-text question)."""
    if not question:
        return False
    is_hl, _, _ = check_open_text_hard_limit(question)
    if is_hl:
        return True
    if is_why_company_question(question) or requires_company_grounding(question):
        return True
    q = _clean_label(question)
    return any(re.search(p, q, re.IGNORECASE) for p in _OPEN_TEXT_PATTERNS)


def _context_matches_question(ctx: Optional[Dict[str, Any]], question: str) -> bool:
    """Ensure the active application context belongs to the company asked in `question` (when a company is named)."""
    named = extract_named_company_in_question(question)
    if not named:
        return bool(ctx and ctx.get("company"))
    if not ctx or not ctx.get("company"):
        return False
    a, b = _company_key(named), _company_key(ctx.get("company", ""))
    if a and b and (a == b or a in b or b in a):
        return True
    url_low = (ctx.get("url") or "").lower()
    if a and a in re.sub(r"[^a-z0-9]", "", url_low):
        return True
    return False


def _requested_sentence_bounds(question: str) -> Tuple[int, int]:
    """Respect explicit length instructions like 'In one or two sentences' -> (1, 2); default -> (2, 4)."""
    q_low = (question or "").lower()
    if re.search(r"\b(?:in\s+)?one\s+sentence\b|\b1\s+sentence\b", q_low) and "two" not in q_low and "2" not in q_low:
        return 1, 2
    if re.search(r"\b(?:one\s+or\s+two|1\s+or\s+2|1\s*[-–to]+\s*2|two)\s+sentences?\b", q_low):
        return 1, 2
    return 2, 4


# ============================================================================
# (2b) Company sources: job description + about page
# ============================================================================
def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw or "")
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", raw).strip()


def _http_get(url: str, timeout: float = 4.0) -> Tuple[int, str]:
    try:
        import httpx
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/133.0 Safari/537.36"
            },
        ) as c:
            r = c.get(url)
            return r.status_code, r.text
    except Exception as e:
        logger.debug(f"grounded_essay GET {url} failed: {e}")
        return 0, ""


def fetch_job_description(url: str, jd_text: str = "") -> Tuple[str, str]:
    """Return (jd_text, source). Uses ATS public posting APIs; falls back to provided text / page HTML."""
    best, src = (jd_text or "").strip(), ("provided" if (jd_text or "").strip() else "")
    u = url or ""
    try:
        m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", u, re.IGNORECASE)
        if m:
            code, body = _http_get(
                f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}?includeCompensation=false",
                timeout=8.0,
            )
            if code == 200:
                for j in (json.loads(body).get("jobs") or []):
                    if m.group(2).lower() in (j.get("jobUrl", "") + j.get("applyUrl", "") + j.get("id", "")).lower():
                        txt = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
                        if len(txt) > len(best):
                            best, src = txt, "ashby_posting_api"
                        break
        m = re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", u, re.IGNORECASE) or (
            re.search(r"[?&]for=([^&]+).*?[?&]token=(\d+)", u) if "greenhouse" in u else None
        )
        if m:
            code, body = _http_get(
                f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}",
                timeout=8.0,
            )
            if code == 200:
                txt = _strip_html(html.unescape(json.loads(body).get("content", "")))
                if len(txt) > len(best):
                    best, src = txt, "greenhouse_board_api"
        m = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", u, re.IGNORECASE)
        if m:
            code, body = _http_get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", timeout=8.0)
            if code == 200:
                d = json.loads(body)
                parts = (
                    [d.get("descriptionPlain", "")]
                    + [f"{l.get('text','')}\n{_strip_html(l.get('content',''))}" for l in d.get("lists", [])]
                    + [d.get("additionalPlain", "")]
                )
                txt = "\n".join(p for p in parts if p)
                if len(txt) > len(best):
                    best, src = txt, "lever_posting_api"
    except Exception as e:
        logger.debug(f"grounded_essay JD API fetch error: {e}")
    if len(best) < 400 and u and u.startswith("http"):
        code, body = _http_get(re.sub(r"/application/?$", "", u), timeout=6.0)
        if code == 200:
            txt = _strip_html(body)
            if len(txt) > len(best):
                best, src = txt, "posting_page_html"
    return best, (src or "provided")


_ABOUT_HEAD_RE = r"(?:about\s+us|about\s+{name}|who\s+we\s+are|our\s+company|the\s+company|company\s+overview)"
_NEXT_HEAD_RE = (
    r"\n\s*(?:[^\w\s]\s*)?(?:about\s+the\s+(?:role|team|job|position)|the\s+role|role\s+overview|"
    r"what\s+you(?:'|’)?ll\s+do|what\s+you\s+will\s+do|responsibilities|requirements|qualifications|"
    r"who\s+you\s+are|what\s+we(?:'|’)?re\s+looking\s+for|you\s+will|in\s+this\s+role|the\s+opportunity|benefits)\b"
)


def extract_jd_about_section(jd_text: str, company: str) -> str:
    if not jd_text:
        return ""
    clean_comp = re.sub(r"unlisted$", "", (company or "").strip(), flags=re.IGNORECASE).strip()
    name = re.escape(clean_comp or company or "")
    head = _ABOUT_HEAD_RE.replace("{name}", name or "zzzz")
    m = (
        re.search(r"(?im)^\s*(?:[^\w\s]\s*)*" + head + r"\s*:?\s*$", jd_text)
        or re.search(r"(?i)\b" + head + r"\b\s*:?\s*\n?", jd_text)
    )
    if m:
        rest = jd_text[m.end() :]
        n = re.search(r"(?i)" + _NEXT_HEAD_RE, rest)
        section = rest[: n.start()] if n else rest[:2500]
        if len(section.strip()) >= 80:
            return section.strip()[:3000]

    # Many postings (e.g. Kira, Whoop, Netic) open directly with the company overview paragraphs before
    # RESPONSIBILITIES / WHAT YOU'LL DO / THE ROLE without an explicit "ABOUT US" header line.
    n = re.search(r"(?i)" + _NEXT_HEAD_RE, jd_text)
    intro = (jd_text[: n.start()] if n else jd_text[:1800]).strip()
    comp_probe = (clean_comp or company or "").lower()
    if len(intro) >= 80 and (not comp_probe or comp_probe in intro.lower()):
        return intro[:3000]
    return ""


def _distinct_tokens(text: str) -> Set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9\-]{4,}", (text or "").lower()) if t not in _STOPWORDS}


def fetch_about_page(company: str, jd_text: str, url: str = "") -> Tuple[str, str]:
    """Return (about_text, source). Checks company website (/about, /) and verifies it belongs to the same
    company as the JD; falls back to the posting's own 'About us / company intro' section."""
    section = extract_jd_about_section(jd_text, company)
    if not url or not url.startswith("http"):
        if len(section) >= 80:
            return section, "job_posting_about_section"
        return "", ""

    jd_tokens = _distinct_tokens(jd_text)
    clean_comp = re.sub(r"unlisted$", "", (company or "").strip(), flags=re.IGNORECASE).strip()
    slug = re.sub(r"[^a-z0-9]", "", (clean_comp or company or "").lower())
    domains: List[str] = []
    for m in re.finditer(r"https?://(?:www\.)?([a-z0-9\-]+\.[a-z.]{2,10})", jd_text or "", re.IGNORECASE):
        d = m.group(1).lower()
        if slug and slug in d.replace("-", "") and not any(
            x in d for x in ("ashbyhq", "greenhouse", "lever.co", "linkedin")
        ):
            domains.append(d)
    if slug:
        domains += [f"{slug}.com", f"{slug}.ai"]
    seen = set()
    for d in domains:
        if d in seen:
            continue
        seen.add(d)
        for path in ("/about", ""):
            target_url = f"https://www.{d}{path}" if not d.startswith("www.") else f"https://{d}{path}"
            code, body = _http_get(target_url, timeout=3.5)
            if code != 200 or not body:
                continue
            txt = _strip_html(body)
            if len(txt) < 250 or (clean_comp or company or "").lower() not in txt.lower():
                continue
            overlap = _distinct_tokens(txt) & jd_tokens
            if len(overlap) < 8:
                logger.info(
                    f"[GROUNDED_ESSAY] Rejected about page {target_url}: only {len(overlap)} shared terms "
                    f"with the posting (different company)."
                )
                continue
            combined = (txt[:4500] + ("\n" + section if section and section not in txt else "")).strip()
            return combined[:6000], target_url
    if len(section) >= 80:
        return section, "job_posting_about_section"
    return "", ""


def gather_company_sources(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not ctx:
        return {"company": "", "title": "", "url": "", "jd_text": "", "jd_source": "", "about_text": "", "about_source": ""}
    jd_seed = max([ctx.get("jd_text") or "", ctx.get("page_text") or ""], key=len)
    cache_key = (
        f"{_company_key(ctx.get('company',''))}|{_norm_url(ctx.get('url',''))}|"
        f"{hashlib.sha256((jd_seed[:500] + (ctx.get('about_text') or '')[:200]).encode()).hexdigest()[:10]}"
    )
    with _LOCK:
        if cache_key in _SOURCE_CACHE:
            return _SOURCE_CACHE[cache_key]
    jd, jd_src = fetch_job_description(ctx.get("url", ""), jd_seed)
    if ctx.get("about_text"):
        about, about_src = ctx["about_text"], (ctx.get("about_source") or "provided_about_page")
    elif jd:
        about, about_src = fetch_about_page(ctx.get("company", ""), jd, ctx.get("url", ""))
    else:
        about, about_src = "", ""
    clean_comp = re.sub(r"unlisted$", "", (ctx.get("company") or "").strip(), flags=re.IGNORECASE).strip()
    res = {
        "company": clean_comp or ctx.get("company", ""),
        "title": ctx.get("title", ""),
        "url": ctx.get("url", ""),
        "jd_text": jd[:9000],
        "jd_source": jd_src,
        "about_text": about,
        "about_source": about_src,
    }
    with _LOCK:
        _SOURCE_CACHE[cache_key] = res
    return res


# ============================================================================
# Deterministic Grounding Scorer & Verifier (with Achievement & Metric Verification)
# ============================================================================
def _norm_text(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[\u2013\u2014\-]+", " ", s)
    s = re.sub(r"[^a-z0-9%$+#.' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_sentences(text: str) -> List[str]:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])[\"\'\)]*\s+(?=[A-Z0-9\"\'])", t)
    return [p.strip() for p in parts if p.strip()]


def _term_present(term: str, corpus_norm: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + term + r"(?![a-z0-9])", corpus_norm) is not None


def match_sentence_to_candidate_facts(sentence: str) -> List[str]:
    """Identify which CANDIDATE_FACTS ground a sentence.
    Requirement 3: Achievement and metric claims ('reduced X from hours to minutes', percentages, 'built Y that did Z')
    MUST match the same achievement/metric in the record — a tool or skill name alone NEVER grounds an achievement claim."""
    s_low = _norm_text(sentence)
    has_achievement_claim = bool(
        ACHIEVEMENT_VERB_RE.search(sentence)
        or re.search(r"\bfrom\s+\w+\s+to\s+\w+\b|\b\d+(?:\.\d+)?%\b", s_low)
    )
    matched: List[str] = []
    for fid, f in CANDIDATE_FACTS.items():
        anchors = [a for a in f["anchors"] if _term_present(_norm_text(a), s_low)]
        ach_hits = [a for a in f.get("achievements", []) if _norm_text(a) in s_low]
        tags = [t for t in f["tags"] if _term_present(_norm_text(t), s_low)]
        if has_achievement_claim:
            # Must match at least one specific achievement signature of this fact AND an anchor/tag
            if ach_hits and (anchors or len(tags) >= 2):
                matched.append(fid)
        else:
            if (ach_hits and anchors) or len(anchors) >= 2 or (len(anchors) >= 1 and len(tags) >= 2):
                matched.append(fid)
    return matched


def verify_sentence_achievements_and_metrics(
    sentence: str,
    verified_fids: List[str],
    company_corpus_norm: str,
) -> List[str]:
    """Requirement 3: Enforce that every achievement claim, time-reduction claim, percentage, and built artifact
    matches the verified candidate facts (or company JD/about text when describing the company)."""
    errs: List[str] = []
    s_norm = _norm_text(sentence)
    first_person = bool(FIRST_PERSON_RE.search(sentence))
    has_ach_verb = bool(ACHIEVEMENT_VERB_RE.search(sentence))

    # 1. False artifact guard (e.g. "payment gateway", "database sharding", "compiler", "distributed tracing engine")
    for bad_art in FALSE_CANDIDATE_ARTIFACTS:
        bn = _norm_text(bad_art)
        if bn in s_norm and bn not in company_corpus_norm:
            errs.append(f"Ungrounded achievement artifact '{bad_art}' not in resume/GitHub/portfolio: '{sentence[:80]}'")

    # 2. If sentence makes a first-person achievement claim, it MUST match a specific achievement signature
    if first_person and has_ach_verb:
        if not verified_fids:
            errs.append(
                f"Achievement claim not grounded in any specific resume/GitHub/portfolio achievement "
                f"(tool/skill names alone do not ground achievements): '{sentence[:90]}'"
            )
        else:
            # Verify at least one matched fact has an achievement signature in this sentence
            has_sig = any(
                any(_norm_text(sig) in s_norm for sig in CANDIDATE_FACTS[fid].get("achievements", []))
                for fid in verified_fids
                if fid in CANDIDATE_FACTS
            )
            if not has_sig:
                errs.append(
                    f"Achievement claim lacks matching achievement in resume/GitHub/portfolio: '{sentence[:90]}'"
                )

    # 3. Time-unit reduction claims ("from <unit> to <unit>", e.g. "from hours to minutes" vs "from days to seconds")
    time_units = r"(?:days|hours|minutes|seconds|milliseconds|weeks|months)"
    for m in re.finditer(rf"\bfrom\s+{time_units}\s+to\s+{time_units}\b", s_norm):
        phrase = m.group(0)
        allowed_in_facts = any(
            phrase in _norm_text(CANDIDATE_FACTS[fid]["text"])
            for fid in (verified_fids or FACT_KEYS_ORDERED)
            if fid in CANDIDATE_FACTS
        )
        if not (allowed_in_facts or (company_corpus_norm and phrase in company_corpus_norm)):
            errs.append(f"Ungrounded metric/reduction '{phrase}' not in candidate record: '{sentence[:80]}'")

    # 4. Percentage and multiplier claims ("80%", "10x")
    allowed_fact_corpus = _norm_text(
        " ".join(CANDIDATE_FACTS[fid]["text"] for fid in (verified_fids or FACT_KEYS_ORDERED) if fid in CANDIDATE_FACTS)
    )
    for m in re.finditer(r"\b\d+(?:\.\d+)?%|\b\d+x\b", s_norm):
        tok = m.group(0)
        if not (tok in allowed_fact_corpus or (company_corpus_norm and tok in company_corpus_norm)):
            errs.append(f"Ungrounded percentage/multiplier metric '{tok}' in: '{sentence[:80]}'")

    return errs


def extract_verbatim_company_matches(sentence: str, company_corpus_norm: str, company: str = "") -> List[str]:
    """Find verbatim phrases (3+ words) or distinctive domain terms in `sentence` that appear in `company_corpus_norm`."""
    if not company_corpus_norm:
        return []
    s_norm = _norm_text(sentence)
    words = [w.strip(".'\"") for w in s_norm.split() if w.strip(".'\"")]
    matches: List[str] = []
    n = len(words)
    for k in (6, 5, 4, 3):
        for i in range(0, max(0, n - k + 1)):
            span_words = words[i : i + k]
            non_stop = [w for w in span_words if w not in _STOPWORDS and len(w) >= 3]
            if len(non_stop) < 2:
                continue
            phrase = " ".join(span_words)
            if phrase in company_corpus_norm:
                if not any(phrase in m for m in matches):
                    matches.append(phrase)
    if matches:
        return matches[:4]
    cand_only = {"siddhesh", "sawant", "talviro", "mason", "subhash", "mumbai", "job", "scout"}
    comp_key = _company_key(company)
    shared_terms = [
        w for w in dict.fromkeys(words)
        if len(w) >= 4 and w not in _STOPWORDS and w not in cand_only and w != comp_key
        and _term_present(w, company_corpus_norm)
    ]
    if len(shared_terms) >= 2:
        return [" ".join(shared_terms[:4])]
    return []


def rank_candidate_facts_for_sources(sources: Dict[str, Any]) -> List[Tuple[str, int, List[str]]]:
    """Rank CANDIDATE_FACTS by concrete technical/domain overlap with the company's JD and about page."""
    corpus = _norm_text((sources.get("jd_text") or "") + "\n" + (sources.get("about_text") or ""))
    if not corpus:
        return []
    ranked: List[Tuple[str, int, List[str]]] = []
    high_signal = {
        "snowflake", "redshift", "postgresql", "step functions", "cloudwatch", "lambda", "telemetry",
        "observability", "mcp", "claude", "agent", "agents", "agentic", "playwright", "fastapi", "nlp", "spacy",
        "etl", "pipeline", "pipelines", "validation", "monitoring", "alerting", "restful", "docker",
        "valuation", "dcf", "supply chain", "reconciliation", "deterministic", "guardrails", "business systems",
    }
    for fid, f in CANDIDATE_FACTS.items():
        if fid == "EDU":
            continue
        shared = []
        for t in f["tags"]:
            tn = _norm_text(t)
            if _term_present(tn, corpus):
                shared.append(t.lower())
        shared_uniq = list(dict.fromkeys(shared))
        score = len(shared_uniq) + sum(2 for s in shared_uniq if s in high_signal)
        if score >= 2:
            ranked.append((fid, score, shared_uniq))
    ranked.sort(key=lambda x: (-x[1], FACT_KEYS_ORDERED.index(x[0])))
    return ranked


def rank_candidate_facts_for_question(
    question: str,
    sources: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, int, List[str]]]:
    """Rank CANDIDATE_FACTS for any open-text question (skill/experience, project, bug/incident, fit, or why-company)."""
    q_norm = _norm_text(question)
    jd_corpus = _norm_text(((sources or {}).get("jd_text") or "") + "\n" + ((sources or {}).get("about_text") or ""))

    is_bug_q = bool(re.search(r"\b(bug|debug|debugged|debugging|incident|race\s+condition|failure|outage|troubleshoot|fix|fixed)\b", q_norm))
    is_proj_q = bool(re.search(r"\b(project|proudest|proud|accomplishment|achievement|system\s+you\s+built|application\s+you\s+built|challenging\s+project)\b", q_norm))
    is_fit_q = bool(re.search(r"\b(good\s+fit|strong\s+candidate|hire\s+you|about\s+yourself|qualifications|background)\b", q_norm))

    # Extract explicit topic tokens from the question
    q_ignore = _STOPWORDS | {
        "describe", "tell", "explain", "share", "walk", "give", "example", "experience", "background",
        "expertise", "working", "worked", "proficient", "proficiency", "knowledge", "familiarity",
        "project", "proud", "proudest", "challenging", "difficult", "complex", "technical", "accomplishment",
        "achievement", "bug", "fixed", "debugged", "solved", "resolved", "time", "situation", "role",
        "position", "company", "good", "strong", "fit", "candidate", "interested", "joining", "why",
        "sentences", "sentence", "briefly", "please", "area", "skill", "tool",
    }
    q_topics = [w for w in re.findall(r"[a-z0-9+#.]{2,}", q_norm) if w not in q_ignore]

    ranked: List[Tuple[str, int, List[str]]] = []
    for fid, f in CANDIDATE_FACTS.items():
        fact_blob = _norm_text(f["text"] + " " + " ".join(f["tags"]) + " " + " ".join(f["anchors"]))
        matched_q_topics = [t for t in q_topics if _term_present(t, fact_blob)]
        matched_jd_tags = [
            t.lower() for t in f["tags"]
            if jd_corpus and _term_present(_norm_text(t), jd_corpus)
        ]

        score = len(set(matched_q_topics)) * 6 + len(set(matched_jd_tags))
        kinds = set(f.get("kinds", []))
        if is_bug_q and "bug" in kinds:
            score += 8
        if is_proj_q and "project" in kinds:
            score += 8
        if is_fit_q and "experience" in kinds:
            score += 4

        # If the question asks about specific technical topics (e.g. "AI and Python", "SQL", "NLP", "AWS")
        # and neither bug/project/fit/company is the sole driver, require topic overlap!
        shared_labels = list(dict.fromkeys(matched_q_topics + matched_jd_tags))
        if score >= 3:
            ranked.append((fid, score, shared_labels or f["tags"][:3]))

    # If the question asked for a specific topic (not a general bug/project/fit/company prompt)
    # verify that at least one topic word from the question actually matched a fact!
    if q_topics and not (is_bug_q or is_proj_q or is_fit_q or requires_company_grounding(question)):
        any_topic_hit = any(
            any(_term_present(t, _norm_text(f["text"] + " " + " ".join(f["tags"]))) for f in CANDIDATE_FACTS.values())
            for t in q_topics
        )
        if not any_topic_hit:
            return []

    ranked.sort(key=lambda x: (-x[1], FACT_KEYS_ORDERED.index(x[0])))
    return ranked


def verify_grounded_essay(
    answer: str,
    claims: Optional[List[Dict[str, Any]]],
    sources: Optional[Dict[str, Any]] = None,
    question: str = "",
) -> Tuple[bool, List[str]]:
    """Deterministically verify every sentence of an open-text answer against (2a), (2b), (2c), (3), and (4)."""
    errors: List[str] = []
    sources = sources or {}
    company = (sources.get("company") or "").strip()
    jd = (sources.get("jd_text") or "").strip()
    about = (sources.get("about_text") or "").strip()

    if question:
        is_hl, _, hl_reason = check_open_text_hard_limit(question)
        if is_hl:
            return False, [hl_reason]

    need_company = requires_company_grounding(question) if question else bool(company or jd)
    if need_company:
        if not jd or len(jd) < 150:
            errors.append("Job description not readable (2b)")
        if is_why_company_question(question) and (not about or len(about) < 80):
            errors.append("Company about page not readable (2b)")
        if errors:
            return False, errors

    company_corpus = _norm_text(jd + "\n" + about) if (jd or about) else ""
    fact_corpus = _norm_text(" ".join(f["text"] for f in CANDIDATE_FACTS.values()))

    if re.search(r"\[(EXP_|PROJ_|SKILLS_|EDU|ABOUT|JD|ashby|greenhouse|lever|\d+\])", answer or ""):
        errors.append("Contains raw citation brackets in prose")

    sentences = split_sentences(answer)
    min_s, max_s = _requested_sentence_bounds(question)
    if not (min_s <= len(sentences) <= max_s):
        errors.append(f"Answer must be {min_s}-{max_s} sentences (got {len(sentences)})")
    if len(answer or "") < 45 or len(answer or "") > 1100:
        errors.append(f"Answer length ({len(answer or '')} chars) out of bounds [45, 1100]")

    for pat in BANNED_PHRASES:
        m = re.search(pat, answer or "", re.IGNORECASE)
        if m:
            errors.append(f"Generic praise / banned phrase: '{m.group(0)}'")

    m_bp = COMPANY_BOILERPLATE_RE.search(answer or "")
    if m_bp:
        errors.append(f"HR/salary/EEO boilerplate copied into answer: '{m_bp.group(0)}'")

    for false_probe in FALSE_DOMAIN_PROBES:
        if false_probe in _norm_text(answer) and (not company_corpus or false_probe not in company_corpus):
            errors.append(f"Ungrounded company domain claim not in JD/about page: '{false_probe}'")

    claim_by_sent: Dict[str, Dict[str, Any]] = {}
    for c in claims or []:
        if isinstance(c, dict) and c.get("sentence"):
            claim_by_sent[_norm_text(c["sentence"])] = c

    total_cand_sentences = 0
    total_comp_sentences = 0
    has_concrete_link = False
    name_re = re.escape(company) if company else "zzzz"

    for s in sentences:
        s_norm = _norm_text(s)
        c = claim_by_sent.get(s_norm, {})

        # 1. Candidate fact grounding + strict Achievement & Metric verification (Requirement 3)
        raw_fids = c.get("candidate_fact_ids") or []
        norm_fids: List[str] = []
        for fid in raw_fids:
            if isinstance(fid, int) and 1 <= fid <= len(FACT_KEYS_ORDERED):
                norm_fids.append(FACT_KEYS_ORDERED[fid - 1])
            elif isinstance(fid, str) and fid in CANDIDATE_FACTS:
                norm_fids.append(fid)
            elif isinstance(fid, str) and fid not in CANDIDATE_FACTS:
                errors.append(f"Unknown candidate fact id '{fid}' in: '{s[:80]}'")
        inferred_fids = match_sentence_to_candidate_facts(s)
        verified_fids = list(dict.fromkeys(
            [f for f in norm_fids if f in inferred_fids] + inferred_fids
        ))

        ach_errors = verify_sentence_achievements_and_metrics(s, verified_fids, company_corpus)
        errors.extend(ach_errors)

        # 2. Company JD/about grounding
        raw_quotes = [q for q in (c.get("company_quotes") or []) if isinstance(q, str) and q.strip()]
        verified_quotes: List[str] = []
        for q in raw_quotes:
            qn = _norm_text(q)
            if company_corpus and len(qn.split()) >= 3 and qn in company_corpus:
                verified_quotes.append(q)
            elif company_corpus:
                errors.append(f"Company quote not found in JD/about page: '{q[:80]}'")
        auto_quotes = extract_verbatim_company_matches(s, company_corpus, company=company) if company_corpus else []
        all_company_matches = list(dict.fromkeys(verified_quotes + auto_quotes))

        first_person = bool(FIRST_PERSON_RE.search(s))
        mentions_company = bool(
            company and re.search(r"\b(you|your|yours|" + name_re + r")\b", s, re.IGNORECASE)
        )

        if not verified_fids and not all_company_matches:
            errors.append(f"Sentence not grounded in candidate facts or JD/about page: '{s[:90]}'")
        if first_person and not verified_fids:
            errors.append(f"First-person claim not grounded in profile.json/resume/GitHub/portfolio: '{s[:90]}'")
        if mentions_company and not all_company_matches:
            errors.append(f"Claim about {company or 'company'} not grounded in JD/about page: '{s[:90]}'")

        # 3. Guard against unowned skills/technologies & fake employers
        for term in UNOWNED_TECH_TERMS:
            if _term_present(term, s_norm):
                if first_person or not (company_corpus and _term_present(term, company_corpus)):
                    errors.append(f"Unowned skill/technology '{term}' claimed in: '{s[:80]}'")
        for ent in UNOWNED_ENTITIES:
            if _term_present(ent, s_norm):
                if re.search(r"\b(?:at|for|with|from)\s+" + ent + r"\b", s_norm) and first_person:
                    errors.append(f"Unverified employer/school '{ent}' claimed in: '{s[:80]}'")
                elif not (_term_present(ent, fact_corpus) or (company_corpus and _term_present(ent, company_corpus))):
                    errors.append(f"Entity '{ent}' not in candidate record or JD/about page: '{s[:80]}'")

        # 4. Guard numbers: every number in the sentence must appear in fact_corpus or company_corpus
        for num in re.findall(r"\d[\d,.%+]*", s):
            n = num.rstrip(".,")
            if not (n in fact_corpus or (company_corpus and n in company_corpus)):
                errors.append(f"Unverified number '{n}' in: '{s[:80]}'")

        if verified_fids:
            total_cand_sentences += 1
        if all_company_matches:
            total_comp_sentences += 1
        if verified_fids and all_company_matches:
            has_concrete_link = True

    if total_cand_sentences == 0:
        errors.append("Answer does not reference any real candidate project, skill, or experience (2a)")
    if need_company:
        if total_comp_sentences == 0:
            errors.append("Answer does not reference the company's actual work/product or role requirements (2b)")
        ranked_links = rank_candidate_facts_for_sources(sources)
        if not ranked_links:
            errors.append("No concrete link exists between candidate's real work and the company's JD/about page (2b)")
        elif not has_concrete_link and not (total_cand_sentences >= 1 and total_comp_sentences >= 1):
            errors.append("No concrete link between a real candidate project/skill and the company's actual work (2b)")

    return (len(errors) == 0), errors


# ============================================================================
# Deterministic Grounded Composer + Fast LLM Writer for ANY Open-Text Question
# ============================================================================
def _extract_best_company_clauses(sources: Dict[str, Any], shared_tags: List[str]) -> Tuple[str, str, List[str]]:
    """Extract (about_clause, role_clause, verbatim_quotes) directly from `about_text` and `jd_text`."""
    company = (sources.get("company") or "").strip()
    about_text = (sources.get("about_text") or "").strip()
    jd_text = (sources.get("jd_text") or "").strip()

    def _clean_sent(line: str) -> str:
        line = re.sub(r"^[\s•\-\*→\d.)🚀🦸🏻‍♀️]+", "", line).strip()
        return re.sub(r"\s+", " ", line)

    _BOILERPLATE_RE = COMPANY_BOILERPLATE_RE

    about_sents: List[str] = []
    for line in (about_text or jd_text).splitlines():
        for s in split_sentences(_clean_sent(line)):
            if (
                35 <= len(s) <= 320
                and s[:1].isupper()
                and not _BOILERPLATE_RE.search(s)
                and not any(re.search(p, s, re.I) for p in BANNED_PHRASES)
            ):
                about_sents.append(s)

    jd_sents: List[str] = []
    for line in jd_text.splitlines():
        for s in split_sentences(_clean_sent(line)):
            if (
                35 <= len(s) <= 320
                and s[:1].isupper()
                and not _BOILERPLATE_RE.search(s)
                and not any(re.search(p, s, re.I) for p in BANNED_PHRASES)
            ):
                jd_sents.append(s)

    def _score_about(s: str) -> int:
        sl = s.lower()
        sc = 0
        if company and company.lower() in sl:
            sc += 5
        if any(k in sl for k in ["platform", "layer", "builds", "built", "designed to", "watches", "connects", "supports", "system", "agents", "engine"]):
            sc += 4
        sc += sum(2 for t in shared_tags if _term_present(_norm_text(t), _norm_text(s)))
        return sc

    about_sents_sorted = sorted(about_sents, key=_score_about, reverse=True)
    best_about = about_sents_sorted[0] if about_sents_sorted else ""

    def _score_jd(s: str) -> int:
        if s == best_about:
            return -10
        sl = s.lower()
        sc = sum(3 for t in shared_tags if _term_present(_norm_text(t), _norm_text(s)))
        if any(k in sl for k in ["role", "you'll", "you will", "build", "own", "architect", "bridge", "pipeline", "deploy", "snowflake", "telemetry", "gpu", "workflows", "integrations", "apis"]):
            sc += 3
        return sc

    jd_sents_sorted = sorted(jd_sents, key=_score_jd, reverse=True)
    best_jd = jd_sents_sorted[0] if jd_sents_sorted else best_about

    quotes: List[str] = []
    for src_sent in (best_about, best_jd):
        words = src_sent.split()
        if len(words) >= 5:
            q = " ".join(words[: min(10, len(words))]).strip(".,;: ")
            if len(_norm_text(q).split()) >= 3:
                quotes.append(q)
    return best_about, best_jd, list(dict.fromkeys(quotes))


def compose_deterministic_grounded_essay(
    sources: Dict[str, Any],
    ranked_facts: List[Tuple[str, int, List[str]]],
    question: str = "",
) -> Optional[Dict[str, Any]]:
    """Deterministically compose a 2-3 sentence grounded answer for ANY open-text question."""
    if not ranked_facts:
        return None
    need_company = requires_company_grounding(question) if question else bool(sources.get("jd_text"))
    min_s, max_s = _requested_sentence_bounds(question)

    top1_id, _, top1_tags = ranked_facts[0]
    top2_id, _, top2_tags = (ranked_facts[1] if len(ranked_facts) > 1 else ranked_facts[0])
    fact1_text = CANDIDATE_FACTS[top1_id]["text"].rstrip(".")
    fact2_text = CANDIDATE_FACTS[top2_id]["text"].rstrip(".")

    # Case 1: Candidate experience / skill / project / bug / challenge question (does not require company quotes)
    if not need_company:
        s1 = fact1_text + "."
        if top2_id != top1_id:
            s2 = fact2_text + "."
            claims = [
                {"sentence": s1, "candidate_fact_ids": [top1_id], "company_quotes": []},
                {"sentence": s2, "candidate_fact_ids": [top2_id], "company_quotes": []},
            ]
            if max_s >= 3 and len(ranked_facts) > 2:
                top3_id = ranked_facts[2][0]
                s3 = CANDIDATE_FACTS[top3_id]["text"].rstrip(".") + "."
                claims.append({"sentence": s3, "candidate_fact_ids": [top3_id], "company_quotes": []})
        else:
            s2 = CANDIDATE_FACTS["EDU"]["text"].rstrip(".") + "."
            claims = [
                {"sentence": s1, "candidate_fact_ids": [top1_id], "company_quotes": []},
                {"sentence": s2, "candidate_fact_ids": ["EDU"], "company_quotes": []},
            ]
        answer = " ".join(c["sentence"] for c in claims[:max_s])
        ok, errs = verify_grounded_essay(answer, claims[:max_s], sources, question=question)
        if ok:
            return {"answer": answer, "claims": claims[:max_s]}
        logger.debug(f"[GROUNDED_ESSAY] Deterministic candidate-only draft verify errors: {errs}")
        return None

    # Case 2: Company / Role / Motivation / Fit question
    company = (sources.get("company") or "").strip()
    all_shared = list(dict.fromkeys(top1_tags + top2_tags))
    about_sent, jd_sent, _ = _extract_best_company_clauses(sources, all_shared)
    if not about_sent and not jd_sent:
        return None

    corpus_norm = _norm_text((sources.get("jd_text") or "") + "\n" + (sources.get("about_text") or ""))
    comp_quotes_s1 = extract_verbatim_company_matches(about_sent, corpus_norm, company=company)
    comp_quotes_s3 = extract_verbatim_company_matches(jd_sent, corpus_norm, company=company)
    if not comp_quotes_s1 and not comp_quotes_s3:
        return None

    q1 = (comp_quotes_s1 or comp_quotes_s3)[0]
    q2 = (comp_quotes_s3 or comp_quotes_s1)[0]
    shared_str = ", ".join(all_shared[:3]) if all_shared else "Python and backend data systems"
    jd_clean = (jd_sent or about_sent).rstrip(".!?").strip()
    about_clean = (about_sent or jd_sent).rstrip(".!?").strip()

    if max_s <= 2:
        # 2-sentence answer (e.g., "In one or two sentences, why Kira and why this role?")
        s1 = f"{about_clean}."
        if top2_id != top1_id:
            s2 = f"{fact1_text}, and {fact2_text[0].lower() + fact2_text[1:]}, which directly applies to {company}'s work ({jd_clean})."
            fids = [top1_id, top2_id]
        else:
            s2 = f"{fact1_text}, which directly applies to {company}'s work ({jd_clean})."
            fids = [top1_id]
        claims = [
            {"sentence": s1, "candidate_fact_ids": [], "company_quotes": [q1]},
            {"sentence": s2, "candidate_fact_ids": fids, "company_quotes": [q2]},
        ]
    else:
        s1 = f"{about_clean}."
        s2 = f"{fact1_text}, which directly supports {company}'s work across {shared_str}."
        if top2_id != top1_id:
            s3 = f"{fact2_text}, and I can apply that background in {shared_str} to {company}'s role ({jd_clean})."
            claims = [
                {"sentence": s1, "candidate_fact_ids": [], "company_quotes": [q1]},
                {"sentence": s2, "candidate_fact_ids": [top1_id], "company_quotes": []},
                {"sentence": s3, "candidate_fact_ids": [top2_id], "company_quotes": [q2]},
            ]
        else:
            claims = [
                {"sentence": s1, "candidate_fact_ids": [], "company_quotes": [q1]},
                {"sentence": f"{fact1_text}, which directly applies to {company}'s role ({jd_clean}).",
                 "candidate_fact_ids": [top1_id], "company_quotes": [q2]},
            ]

    answer = " ".join(c["sentence"] for c in claims)
    ok, errs = verify_grounded_essay(answer, claims, sources, question=question)
    if ok:
        return {"answer": answer, "claims": claims}
    logger.debug(f"[GROUNDED_ESSAY] Deterministic company draft verify errors: {errs}")
    return None


_WRITER_SYSTEM = (
    "You write a concise, factual answer to an open-text job application question for Siddhesh Sawant.\n"
    "HARD RULES:\n"
    "1. Write 2 to 3 sentences (or 2 sentences if the question asks for 'one or two sentences'). Clean first-person "
    "prose only — NO greeting, NO sign-off, NO bullet points, and NEVER put bracketed tags like [EXP_AWS_BACKEND] in "
    "the prose.\n"
    "2. Use ONLY the provided CANDIDATE FACTS (and COMPANY SOURCES when provided). Every achievement or metric you "
    "state MUST be an exact achievement/metric from CANDIDATE FACTS (e.g. eliminated race conditions in AWS Batch and "
    "designed an automated validation framework using AWS Step Functions and S3; reduced mean time to detect workflow "
    "failures from hours to minutes with CloudWatch and Lambda; co-developed an internal MCP-based AI ticketing "
    "assistant at AWS and used Claude for testing/debugging; built Autonomous AI Job Scout in Python, FastAPI, "
    "Playwright, and Gemini LLM under strict factual resume-grounding guardrails; built Algorithmic Stock Analyzer in "
    "Python, Flask, pandas, NumPy, SciPy for multi-scenario Discounted Cash Flow valuations; built Itemized Bill "
    "Splitter in JavaScript and Tailwind CSS; Research Assistant at George Mason University in NLP using Python, "
    "spaCy, NLTK).\n"
    "3. NEVER invent achievements, percentages, time reductions, employers, or tools.\n"
    "4. NEVER use generic praise: no 'passionate', 'mission', 'innovative', 'culture', 'excited', 'cutting-edge', "
    "'world-class', 'dream', 'confident', 'impact', 'thrilled', 'admire'.\n"
    "5. Output ONLY JSON: {\"sentences\": [{\"sentence\": str, \"candidate_fact_ids\": [str], "
    "\"company_quotes\": [str]}]}"
)


def _load_keys() -> Tuple[Optional[str], Optional[str]]:
    gem = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    nim = os.getenv("NVIDIA_API_KEY")
    for p in [
        "/Users/siddheshsawant/sid_job_scrip/.env",
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/.hermes/.env"),
    ]:
        if os.path.exists(p):
            try:
                for line in open(p, encoding="utf-8"):
                    if (line.startswith("GEMINI_API_KEY=") or line.startswith("GOOGLE_API_KEY=")) and not gem:
                        gem = line.split("=", 1)[1].strip().strip('"').strip("'") or None
                    elif line.startswith("NVIDIA_API_KEY=") and not nim:
                        nim = line.split("=", 1)[1].strip().strip('"').strip("'") or None
            except Exception:
                pass
    return gem, nim


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = re.sub(r"(?is)<think>.*?</think>", "", raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _clean_prose_sentence(s: str) -> str:
    s = re.sub(r"\s*\[(?:EXP_|PROJ_|SKILLS_|EDU|ABOUT|JD|ashby|greenhouse|lever)[^\]]*\]", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def _llm_call(system: str, user: str) -> Optional[Dict[str, Any]]:
    gem, nim = _load_keys()
    if gem:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=gem)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            d = _parse_json(resp.text or "")
            if d:
                return d
        except Exception as e:
            logger.debug(f"grounded_essay Gemini error: {e}")
    if nim:
        try:
            import httpx
            r = httpx.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {nim}"},
                json={
                    "model": "nvidia/nemotron-3.5-bolt-e4b",
                    "temperature": 0.1,
                    "max_tokens": 1100,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=15.0,
                verify=False,
            )
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                d = _parse_json(msg.get("content") or "")
                if d:
                    return d
        except Exception as e:
            logger.debug(f"grounded_essay NIM error: {e}")
    return None


def _halt(question: str, reason: str) -> None:
    from app.services.form_guards import FormHaltException
    raise FormHaltException(
        HALT_CODE,
        f"ESSAY_UNGROUNDED: Cannot write a grounded answer for '{question}': {reason}. "
        f"Bot never writes generic filler — halting and parking.",
        question=question,
    )


def write_grounded_open_answer(
    question: str,
    is_required: bool = True,
    ctx: Optional[Dict[str, Any]] = None,
    max_attempts: int = 1,
) -> str:
    """Write a grounded answer (2-4 sentences, or 1-2 if requested) for ANY open-text question,
    or halt (required) / return '' (optional) if it hits a Hard Limit (4) or cannot be grounded (5)."""
    ctx = ctx if ctx is not None else get_application_context()

    def _fail(reason: str) -> str:
        logger.warning(f"[GROUNDED_ESSAY] Ungrounded/halted for '{question}': {reason}")
        if is_required:
            _halt(question, reason)
        return ""

    # 1. Hard limits check (Requirement 4)
    is_hl, hl_type, hl_reason = check_open_text_hard_limit(question)
    if is_hl:
        if hl_type == "anything_else" and not is_required:
            return ""
        return _fail(hl_reason)

    need_company = requires_company_grounding(question)
    if need_company and not _context_matches_question(ctx, question):
        return _fail("no matching application context (company / job description) available for this question")

    sources = gather_company_sources(ctx) if (ctx and _context_matches_question(ctx, question)) else {
        "company": "", "title": "", "url": "", "jd_text": "", "jd_source": "", "about_text": "", "about_source": ""
    }

    if need_company:
        if len(sources.get("jd_text", "")) < 150:
            return _fail("job description not readable")
        if is_why_company_question(question) and len(sources.get("about_text", "")) < 80:
            return _fail("company about page not readable or not verifiably the same company")
        ranked_facts = rank_candidate_facts_for_sources(sources)
        q_ranked = rank_candidate_facts_for_question(question, sources)
        if q_ranked:
            # Merge question-specific ranking with JD ranking
            seen_f = set()
            merged = []
            for item in q_ranked + ranked_facts:
                if item[0] not in seen_f:
                    seen_f.add(item[0])
                    merged.append(item)
            ranked_facts = merged if ranked_facts else []
    else:
        ranked_facts = rank_candidate_facts_for_question(question, sources)

    if not ranked_facts:
        return _fail("no concrete link between candidate's verified record and this question")

    q_hash = hashlib.sha256(_norm_text(question).encode()).hexdigest()[:10]
    cache_key = (
        f"{q_hash}|{_company_key(sources.get('company',''))}|{_norm_url(sources.get('url',''))}|"
        f"{hashlib.sha256((sources.get('jd_text') or '')[:300].encode()).hexdigest()[:8]}"
    )
    with _LOCK:
        cached = _ESSAY_CACHE.get(cache_key)
    if cached:
        _register(question, cached)
        return cached["answer"]

    min_s, max_s = _requested_sentence_bounds(question)
    top_fact_ids = [fid for fid, _, _ in ranked_facts[:6]]
    facts_block = "\n".join(f"[{fid}] {CANDIDATE_FACTS[fid]['text']}" for fid in top_fact_ids)
    user = (
        f"QUESTION: {question}\n"
        f"SENTENCE COUNT REQUIRED: {min_s} to {max_s} sentences\n"
        + (f"COMPANY: {sources['company']}\nROLE: {sources.get('title','')}\n" if sources.get("company") else "")
        + f"\nTOP MATCHING CANDIDATE FACTS:\n{facts_block}\n"
        + (
            f"\nCOMPANY SOURCES — ABOUT PAGE ({sources['about_source']}):\n{sources['about_text'][:2500]}\n"
            f"\nCOMPANY SOURCES — JOB DESCRIPTION ({sources['jd_source']}):\n{sources['jd_text'][:4000]}\n"
            if need_company
            else ""
        )
    )

    # Only call external LLM when an live HTTP URL is in context (keeps unit tests instant & deterministic)
    use_live_llm = bool(sources.get("url", "").startswith("http"))
    last_errors: List[str] = []
    if use_live_llm:
        for attempt in range(1, max_attempts + 1):
            d = _llm_call(_WRITER_SYSTEM, user)
            if not d or d.get("cannot_ground"):
                break
            raw_items = d.get("sentences") or d.get("claims") or []
            claims: List[Dict[str, Any]] = []
            if isinstance(raw_items, list) and raw_items:
                for item in raw_items[:max_s]:
                    if isinstance(item, dict) and item.get("sentence"):
                        s_clean = _clean_prose_sentence(str(item["sentence"]))
                        claims.append({
                            "sentence": s_clean,
                            "candidate_fact_ids": item.get("candidate_fact_ids") or [],
                            "company_quotes": item.get("company_quotes") or [],
                        })
                answer = " ".join(c["sentence"] for c in claims)
            else:
                answer = _clean_prose_sentence(str(d.get("answer") or ""))
                claims = [{"sentence": s} for s in split_sentences(answer)[:max_s]]
                answer = " ".join(c["sentence"] for c in claims)

            ok, errs = verify_grounded_essay(answer, claims, sources, question=question)
            if ok:
                rec = {
                    "answer": answer,
                    "claims": claims,
                    "question": question,
                    "company": sources.get("company", ""),
                    "url": sources.get("url", ""),
                    "title": sources.get("title", ""),
                    "jd_source": sources.get("jd_source", ""),
                    "about_source": sources.get("about_source", ""),
                    "jd_text": sources.get("jd_text", ""),
                    "about_text": sources.get("about_text", ""),
                    "created_at": datetime.now().isoformat(),
                }
                with _LOCK:
                    _ESSAY_CACHE[cache_key] = rec
                _register(question, rec)
                logger.info(f"[GROUNDED_ESSAY] Wrote grounded answer (LLM) for '{question}': {answer}")
                return answer
            last_errors = errs
            logger.info(f"[GROUNDED_ESSAY] LLM draft rejected for '{question}': {errs[:3]}")

    # Deterministic grounded synthesis (guaranteed 100% grounded in CANDIDATE_FACTS + JD/about, verified by same gate)
    det = compose_deterministic_grounded_essay(sources, ranked_facts, question=question)
    if det:
        rec = {
            "answer": det["answer"],
            "claims": det["claims"],
            "question": question,
            "company": sources.get("company", ""),
            "url": sources.get("url", ""),
            "title": sources.get("title", ""),
            "jd_source": sources.get("jd_source", ""),
            "about_source": sources.get("about_source", ""),
            "jd_text": sources.get("jd_text", ""),
            "about_text": sources.get("about_text", ""),
            "created_at": datetime.now().isoformat(),
        }
        with _LOCK:
            _ESSAY_CACHE[cache_key] = rec
        _register(question, rec)
        logger.info(f"[GROUNDED_ESSAY] Wrote verified grounded answer for '{question}': {det['answer']}")
        return det["answer"]

    return _fail("drafts failed grounding verification: " + "; ".join(last_errors[:3]))


# Backwards-compatible alias for Why-company calls
write_why_company_answer = write_grounded_open_answer


def _register(question: str, rec: Dict[str, Any]) -> None:
    key = hashlib.sha256(_norm_text(rec["answer"]).encode()).hexdigest()
    with _LOCK:
        _ESSAY_REGISTRY[key] = dict(rec, question=question)
    try:
        os.makedirs(os.path.dirname(ESSAY_LOG_PATH), exist_ok=True)
        with open(ESSAY_LOG_PATH, "a", encoding="utf-8") as f:
            payload = {k: v for k, v in rec.items() if k not in ("jd_text", "about_text")}
            payload.update({"question": question, "sha": key})
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def get_registered_essay(value: str) -> Optional[Dict[str, Any]]:
    key = hashlib.sha256(_norm_text(value).encode()).hexdigest()
    with _LOCK:
        return _ESSAY_REGISTRY.get(key)


def verify_open_text_field(label: str, value: str) -> Tuple[bool, str]:
    """PRESUBMIT_REVIEW hook: verifies that ANY open-text answer has every claim, achievement, and metric
    grounded in profile.json/resume/GitHub/portfolio or the company's JD/about page."""
    is_hl, _, hl_reason = check_open_text_hard_limit(label)
    if is_hl:
        return False, hl_reason

    rec = get_registered_essay(value)
    if rec:
        ok, errs = verify_grounded_essay(rec["answer"], rec.get("claims", []), rec, question=label)
        if not ok:
            return False, "Ungrounded open-text claim(s): " + "; ".join(errs[:3])
        return True, f"Grounded open-text answer verified against record ({rec.get('company') or 'candidate facts'})"

    ctx = get_application_context()
    need_company = requires_company_grounding(label)
    if need_company and (not ctx or not _context_matches_question(ctx, label)):
        for pat in BANNED_PHRASES:
            m = re.search(pat, value or "", re.IGNORECASE)
            if m:
                return False, f"Ungrounded open-text answer contains generic praise ('{m.group(0)}')"
        return False, "Ungrounded company/role answer: no matching company JD/about sources available"

    sources = gather_company_sources(ctx) if (ctx and _context_matches_question(ctx, label)) else {}
    ok, errs = verify_grounded_essay(value, None, sources, question=label)
    if ok:
        return True, "Grounded open-text answer verified against candidate record and JD"
    return False, "Ungrounded open-text answer: " + "; ".join(errs[:3])


# Backwards-compatible alias
verify_essay_field = verify_open_text_field


def judge_context_for_fields(fields: List[Dict[str, Any]]) -> str:
    """Grounding evidence block for the LLM judge (for all verified open-text answers)."""
    blocks = []
    has_open = False
    for f in fields or []:
        val = str(f.get("value") or "")
        lbl = str(f.get("label") or f.get("name") or "")
        rec = get_registered_essay(val)
        if rec or (len(val) > 60 and is_open_text_question(lbl)):
            has_open = True
            if rec and (rec.get("about_text") or rec.get("jd_text")):
                blocks.append(
                    f"FIELD '{lbl}' — COMPANY ABOUT PAGE ({rec.get('about_source','')}):\n{rec.get('about_text','')[:2500]}\n"
                    f"JOB DESCRIPTION ({rec.get('jd_source','')}):\n{rec.get('jd_text','')[:4000]}"
                )
    if not has_open:
        return ""
    facts = "\n".join(f"[{k}] {v['text']}" for k, v in CANDIDATE_FACTS.items())
    return "VERIFIED CANDIDATE FACTS (resume/GitHub/portfolio/profile.json):\n" + facts + (
        "\n\n" + "\n\n".join(blocks) if blocks else ""
    )
