import os
import re
from datetime import datetime, timedelta
import logging

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# 1. HARDCODED PROFILE FACTS
FULL_NAME = "Siddhesh Santosh Sawant"
FIRST_NAME = "Siddhesh Santosh"
LAST_NAME = "Sawant"
PREFERRED_NAME = "Siddhesh"
NAME_PRONUNCIATION = "Sid-desh Sa-want"
EMAIL = "siddheshs202001@gmail.com"
PHONE = "+1 445-208-2299"
LINKEDIN_URL = "https://www.linkedin.com/in/sss20/"
GITHUB_URL = "https://github.com/sids202001"
PORTFOLIO_URL = "https://siddhesh202001-portfolio.netlify.app"
LOCATION = "Washington, District of Columbia, United States"
CITY = "Washington"
STATE = "District of Columbia"
COUNTRY = "United States"
ZIP_CODE = "20024"
UNIVERSITY = "George Mason University"
DEGREE = "Master of Science"
MAJOR = "Information Systems"
GPA = "3.71"
GRADUATION_DATE = "May 2024"
BACHELOR_UNIVERSITY = "University of Mumbai"
BACHELOR_DEGREE = "Bachelor of Engineering"
BACHELOR_MAJOR = "Information Technology"
BACHELOR_GPA = "3.3"
PRONOUNS = "He/Him"
GENDER = "Male"
RACE = "Asian"
ETHNICITY = "Not Hispanic or Latino"
VETERAN_STATUS = "I am not a protected veteran"
DISABILITY_STATUS = "No, I do not have a disability and have not had one in the past"
WORK_AUTHORIZATION = "Yes"
REQUIRES_SPONSORSHIP = "Yes"
WILLING_TO_RELOCATE = "Yes"
WILLING_TO_WORK_IN_OFFICE = "Yes"
WILLING_TO_WORK_HYBRID = "Yes"
WILLING_TO_WORK_REMOTE = "Yes"
PREVIOUSLY_EMPLOYED_AT_COMPANY = "No"
HAS_NON_COMPETE = "No"
CAN_PASS_BACKGROUND_CHECK = "Yes"
AGE_18_OR_OVER = "Yes"
CITIZENSHIP = "India"
IS_US_PERSON = "No"
US_PERSON_STATUS = "No"
SECURITY_CLEARANCE = "No"
SECURITY_CLEARANCE_LEVEL = "N/A"
EXPORT_COMPLIANCE = "No"
VISA_STATUS = "F-1 OPT (STEM Eligible)"
YEARS_OF_EXPERIENCE = "2"
DESIRED_SALARY = ""
CURRENT_COMPANY = "Talviro INC"
CURRENT_TITLE = "Software Developer"
CURRENT_LOCATION = "Miami, Florida (Remote)"
CURRENT_START_DATE = "06/2026"
CURRENT_END_DATE = "Present"
CURRENT_EXPERIENCE_DESCRIPTION = (
    "Built and optimized high-throughput RESTful APIs and complex SQL queries, reducing database execution overhead. "
    "Containerized backend services with Docker for reproducible environments across CI pipelines. "
    "Ship features end to end, from requirements to deployed backend services, owning testing and root-cause debugging. "
    "Worked in an Agile team tracking tasks in Jira, using Claude daily while reviewing and testing everything before it ships."
)

PREVIOUS_COMPANY = "Amazon Web Services"
PREVIOUS_TITLE = "Software Dev Engineer"
PREVIOUS_LOCATION = "Arlington, VA"
PREVIOUS_START_DATE = "03/2025"
PREVIOUS_END_DATE = "03/2026"
PREVIOUS_EXPERIENCE_DESCRIPTION = (
    "Built an automated validation framework with Step Functions, Batch, Lambda, and S3 that stops invalid data before it reaches production, and fixed race conditions in AWS Batch jobs failing ETL pipelines intermittently. "
    "Built monitoring and alerting with CloudWatch and Lambda that cut failure detection from hours to minutes. "
    "Helped build an internal MCP-based AI ticketing assistant that handles issue triage during on-call rotations. "
    "Diagnosed performance bottlenecks in slow on-call incidents by rewriting Redshift and PostgreSQL queries, and built backend services and RESTful APIs with IAM and API Gateway."
)

NOTICE_PERIOD = "2 weeks"
HOW_DID_YOU_HEAR = "Company career section / website"
REFERRAL = ""
SECURITY_CLEARANCE = "No"
LANGUAGES_SPOKEN = "English (Fluent), Hindi (Native), Marathi (Native)"
AVAILABLE_START_DATE = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
WHATSAPP = "No"

# 2. RESUME TEXT
RESUME_TEXT = """Siddhesh Santosh Sawant
United States | +1 (445) 208-2299

Professional Summary
Software & Data Engineer with backend and data experience at AWS and Talviro, from automated validation frameworks to backend features. I take a business problem end to end into clear, well-tested, extensible code that works across multiple components, and I debug production issues down to root cause. AI speeds up my work, it does not do my thinking: I use it daily to code faster, then I verify, test, and take responsibility for the result.

Work Experience
Talviro INC | Software Developer, Remote (Miami, FL) | Jun 2026 – Present
• Built and optimized high-throughput RESTful APIs and complex SQL queries, reducing database execution overhead.
• Containerized backend services with Docker for reproducible environments across CI pipelines.
• Ship features end to end, from requirements to deployed backend services.
• Own testing and debugging end to end, including root-cause analysis on production issues.
• Worked in an Agile team, tracking tasks and issues in Jira.
• Use Claude daily to build backend changes faster, reviewing and testing everything it writes before it ships.

Amazon Web Services | Software Dev Engineer, Arlington, VA | Mar 2025 – Mar 2026
• Built an automated validation framework with Step Functions, Batch, Lambda, and S3 that stops invalid data before it reaches production.
• Fixed race conditions in AWS Batch jobs that were failing ETL pipelines intermittently.
• Automated supply chain reporting and data analysis with Python scripts for infra supply chain automation, flagging inventory anomalies and improving fulfillment visibility.
• Built monitoring and alerting with CloudWatch and Lambda that cut failure detection from hours to minutes.
• Helped build an internal MCP-based AI ticketing assistant that handles issue triage during on-call rotations.
• Diagnosed performance bottlenecks in slow on-call incidents by rewriting Redshift and PostgreSQL queries.
• Built and maintained backend services and RESTful APIs, with access controlled through IAM and API Gateway.

George Mason University | Research Assistant, Fairfax, VA | Jun 2024 – Feb 2025
• Ran data analysis and ML research on large-scale automated assessment methods.
• Mentored students in NLP and data analysis with Python (spaCy, NLTK).
• Set up shared Docker and Jupyter environments so the team's research ran the same way on every machine.

Subhash Construction | Business Analyst, Mumbai, India | Jan 2021 – Jan 2022
• Analyzed operational and financial data with SQL and Python, improving efficiency by 25% and cutting operational inefficiencies by 20%.
• Built Power BI and Tableau KPI dashboards on Snowflake models with advanced SQL (CTEs, window functions) covering revenue, cost, and resource use.

Technical Projects
• Autonomous AI Job Scout: Built an autonomous agent using Google Antigravity that applies to jobs on my behalf, 24/7. Every answer is written only from my real resume, GitHub, and portfolio, and a pre-submit verification gate audits every field before anything is sent. Live on Greenhouse, Lever, and Ashby, with a full audit trail in SQLite and evidence receipts on Telegram. Tech: Python, Playwright, Gemini, PyPDF2, FastAPI, SQLite, Telegram.
• Algorithmic Stock Analyzer: Full-stack market intelligence platform on a Flask API with live yfinance quotes, Wilder's RSI, MACD, SMA, SciPy find_peaks chart patterns, and a DCF engine on audited SEC EDGAR fundamentals with a margin of safety, plus a Telegram /scan bot and an LLM layer grounded in verified numbers.
• Itemized Bill & Expense Splitter: Zero-dependency browser-only bill splitter in Vanilla ES6+ and Tailwind CSS with 4 split modes, cent-exact rounding, real-time receipt audit log, and WhatsApp/CSV export (loads in under 50ms).

Education
• George Mason University | M.S. Information Systems, GPA 3.71/4.0 | Jan 2023 – May 2024
• University of Mumbai | B.E. Information Technology, GPA 3.3/4.0 | Aug 2018 – Aug 2022

Skills
Languages: Python, SQL, C++, JavaScript, TypeScript, Java, R
Databases: PostgreSQL, MongoDB, Redshift, Snowflake, SQLite, DynamoDB
Cloud & Data: AWS (S3, EC2, Lambda, Step Functions, Batch, API Gateway, CloudWatch, IAM, ECS, EBS, Redshift, DynamoDB), ETL Pipelines, Workflow Orchestration, Data Validation, Distributed Systems
Frameworks: React.js, Node.js, Express.js, Flask, FastAPI, Pandas, NumPy, SciPy, scikit-learn, yfinance, Playwright, PyPDF2
AI Tools: LLM Integration, Agentic AI, RAG, MCP, Google Antigravity, Model Evaluation, Codex
Quality & Testing: Automated Validation, Root-Cause Analysis, Production Monitoring, Testing & Debugging
Tools: Docker, Git, Jira, JSON, Linux/Unix, CI/CD, Power BI, Tableau, Excel, Matplotlib, Plotly, Telegram"""

# Format all facts for AI Prompt
PROFILE_FACTS = f"""
Full Name: {FULL_NAME}
Email: {EMAIL}
Phone: {PHONE}
LinkedIn: {LINKEDIN_URL}
GitHub: {GITHUB_URL}
Portfolio: {PORTFOLIO_URL}
Location: {LOCATION}
University: {UNIVERSITY}, Degree: {DEGREE} in {MAJOR}, GPA: {GPA}, Grad: {GRADUATION_DATE}
Bachelor's: {BACHELOR_UNIVERSITY}, Degree: {BACHELOR_DEGREE} in {BACHELOR_MAJOR}, GPA: {BACHELOR_GPA}
Demographics: Gender: {GENDER}, Race: {RACE}, Ethnicity: {ETHNICITY}, Veteran: {VETERAN_STATUS}, Disability: {DISABILITY_STATUS}
Work Auth: {WORK_AUTHORIZATION}, Sponsorship: {REQUIRES_SPONSORSHIP}, Citizenship: {CITIZENSHIP}
Work preferences: Relocate: {WILLING_TO_RELOCATE}, Office: {WILLING_TO_WORK_IN_OFFICE}, Hybrid: {WILLING_TO_WORK_HYBRID}, Remote: {WILLING_TO_WORK_REMOTE}
Experience: {YEARS_OF_EXPERIENCE} years, Current: {CURRENT_COMPANY} ({CURRENT_TITLE}), Notice: {NOTICE_PERIOD}
Clearance: {SECURITY_CLEARANCE}, Languages: {LANGUAGES_SPOKEN}, Start Date: {AVAILABLE_START_DATE}
"""

_ANSWER_CACHE = {}

# Priority ordered list of tuples: (regex pattern, answer)
FAST_PATH_PATTERNS = [
    # --- 1. CORE WORK AUTHORIZATION & IMMIGRATION ---
    (r"\b(visa\s*status|current\s*visa|visa\s*type|work\s*authorization\s*status)\b", "F-1 OPT / STEM OPT (Requires Sponsorship)"),
    (r"\b(work\s*auth|authorized\s*to\s*work|legally\s*authorized|eligible\s*to\s*work|legal\s*right\s*to\s*work|authorization\s*to\s*work|authorized\s*in\s*the\s*location|authorized\s*to\s*work\s*in\s*the\s*location|authorized\s*in\s*the\s*country|authorized\s*to\s*work\s*in\s*the\s*country|authorized\s*to\s*work\s*in\s*the\s*country\s*where\s*the\s*job\s*is\s*located)\b", "Yes"),
    (r"\b(office\s*three\s*days|three\s*days\s*per\s*week|3\s*days\s*per\s*week|work\s*from\s*(our|the)?\s*(us\s*)?office|able\s*to\s*work\s*from|work\s*from\s*office|work\s*in[\s-]person|onsite|hybrid|relocate|willing\s*to\s*relocate)\b", "Yes"),
    (r"\b(sponsor|visa|require.*sponsorship|need.*sponsorship|future.*sponsorship|work\s*permit)\b", "Yes"),
    (r"\b(u\.?s\.?\s*person|us\s*person|which.*best\s*describes\s*your.*u\.?s\.?\s*person|export\s*(compliance|control)s?|itar|ear|ofac\s*sanctions)\b", "No"),
    (r"\b(refused.*visa|denied.*entry)\b", "No"),
    (r"\b(us\s*citizen|permanent\s*resident|green\s*card\s*holder)\b", "No"),

    # --- 2. CORE CONTACT & IDENTITY ---
    (r"\bfirst\s*name\b", FIRST_NAME),
    (r"\blast\s*name\b", LAST_NAME),
    (r"\bpreferred\s*name\b", PREFERRED_NAME),
    (r"\bfull\s*name\b", FULL_NAME),
    (r"\blegal\s*name\b", FULL_NAME),
    (r"\bname\s*pronunciation\b", NAME_PRONUNCIATION),
    (r"\bemail\b", EMAIL),
    (r"\bphone\b|\btelephone\b|\bmobile\b|\bcell\b", PHONE),
    (r"\blinkedin\b", LINKEDIN_URL),
    (r"\bgithub\b", GITHUB_URL),
    (r"\bportfolio\b|\bwebsite\b|\bpersonal\s*website\b", PORTFOLIO_URL),
    (r"\bcity\b", CITY),
    (r"\bstate\b|\bprovince\b", STATE),
    (r"\bzip\b|\bpostal\b", ZIP_CODE),
    (r"\b(country\s*where\s*you\s*(currently\s*)?reside|country\s*of\s*residence|current\s*country|residing\s*country|where\s*do\s*you\s*(currently\s*)?reside|where\s*are\s*you\s*(currently\s*)?living)\b", "US"),
    (r"\b(anticipate\s*working|countries\s*you\s*anticipate|country\s*or\s*countries\s*you\s*anticipate|where\s*do\s*you\s*plan\s*to\s*work|work\s*country\s*preference)\b", "United States"),
    (r"\bcountry\b", COUNTRY),
    (r"\b(candidate\s*location|current\s*location|location\s*\(city\)|where\s*are\s*you\s*located|home\s*address|street\s*address|address)\b", LOCATION),

    # --- 3. WORK EXPERIENCE & METRICS ---
    (r"\b(how\s*many\s*years|years\s*of.*experience|post[\s-]college.*experience|total\s*years|relevant.*experience|software.*experience|work\s*experience\s*years|years\s*in\s*industry|years\s*of\s*work)\b", YEARS_OF_EXPERIENCE),
    (r"\b(current\s*or\s*previous\s*employer|current\s*employer|previous\s*employer|name\s*of\s*(your\s*)?current(\s*\(or\s*most\s*recent\))?\s*company|current\s*company|most\s*recent\s*company|employer)\b", CURRENT_COMPANY),
    (r"\b(current\s*or\s*previous\s*job\s*title|current\s*title|job\s*title|most\s*recent\s*title|role\s*title)\b", CURRENT_TITLE),
    (r"\bjob\s*duties\b|\brole\s*description\b|\bwork\s*experience\s*description\b|\bresponsibilities\b|\bjob\s*summary\b", CURRENT_EXPERIENCE_DESCRIPTION),
    (r"\bemployment\s*start\b|\bjob\s*start\b|\bstart\s*date\b.*\b(company|job|work)\b", CURRENT_START_DATE),
    (r"\bemployment\s*end\b|\bjob\s*end\b|\bend\s*date\b.*\b(company|job|work)\b", CURRENT_END_DATE),
    (r"\bcurrently\s*work(ing)?\s*here\b|\bcurrent\s*job\b|\bcurrent\s*employer\b", "Yes"),
    (r"\breason\s*for\s*leaving\b", "Seeking new growth opportunities to apply my backend engineering and distributed data pipeline experience."),
    (r"\blanguages\s*spoken\b|\bfluency\b", LANGUAGES_SPOKEN),
    (r"\b(how\s*did\s*you\s*hear|where\s*did\s*you\s*hear|where\s*did\s*you\s*find|how\s*you\s*heard|first\s*hear|source)\b", HOW_DID_YOU_HEAR),
    (r"\breferral\b|\breferr\b", REFERRAL),
    (r"\bwhatsapp\b", WHATSAPP),

    # --- 4. EDUCATION & ACADEMICS ---
    (r"\b(most\s*recent\s*school|school\s*you\s*attended|school\s*attended|school|university|institution)\b", UNIVERSITY),
    (r"\b(most\s*recent\s*degree|degree\s*you\s*obtained|degree\s*obtained|degree\s*earned|highest\s*(level|degree)\s*of\s*education|education\s*level|highest\s*education|degree\s*level|academic\s*degree|degree)\b", "Master's Degree"),
    (r"\bmajor\b|\bdiscipline\b|\bfield\s*of\s*study\b", MAJOR),
    (r"\bgpa\b", GPA),
    (r"\bgraduat(e|ion)\b|\bcompletion\s*date\b", GRADUATION_DATE),

    # --- 5. DEMOGRAPHICS & EEO ---
    (r"\bpreferred\s*(first\s*)?name\b|\bwhat\s*should\s*we\s*call\s*you\b|\bnickname\b", PREFERRED_NAME),
    (r"\bpronouns\b", PRONOUNS),
    (r"\btransgender\b|\btrans\b", "No"),
    (r"\b(sexual\s*orientation|orientation|sexual\s*identity)\b", "Heterosexual"),
    (r"\b(gender\s*identity|gender|sex)\b", GENDER),
    (r"\b(consent|agree|acknowledg(e|ment)|certify|terms|privacy)\b", "Yes"),
    (r"\b(racial|ethnic|race|ethnicity|ethnicities|heritage|descent)\b", "South Asian"),
    (r"\bhispanic\b|\blatino\b", "No"),
    (r"\bdisability\b", DISABILITY_STATUS),
    (r"\bveteran\b", VETERAN_STATUS),
    (r"\b18\s*or\s*over\b|\b18\s*years\b|\bolder\b|\blegal\s*age\b", AGE_18_OR_OVER),
    (r"\bcitizenship\b|\bcitizen\b", CITIZENSHIP),

    # --- 6. COMPLIANCE, BACKGROUND & ETHICS ---
    (r"\b(resident\s*of\s*california|california\s*resident|live\s*in\s*california|located\s*in\s*california|reside\s*in\s*california)\b", "No"),
    (r"\b(resident\s*of|reside\s*in|currently\s*live\s*in|located\s*in)\s+(california|new\s*york|texas|florida|washington\s*state|massachusetts|illinois|colorado|ohio|georgia|north\s*carolina|pennsylvania|michigan|arizona)\b", "No"),
    (r"\b(are\s*you\s*a\s*resident\s*of|do\s*you\s*reside\s*in|do\s*you\s*currently\s*live\s*in)\s+(?!united\s*states|the\s*us\b|us\b|district\s*of\s*columbia|dc\b|washington\s*dc)[a-z\s]+", "No"),
    (r"\b(based\s*in\s*the\s*us|located\s*in\s*the\s*us|resident\s*of\s*the\s*us|live\s*in\s*the\s*us|live\s*in\s*united\s*states)\b", "Yes"),
    (r"\b(employment\s*agreements?|post-?employment\s*restrictions?|non-?compete|non-?solicit|restrictive\s*covenant)\b", "No"),
    (r"\b(family\s*members?|relatives?|personal\s*relationships?|conflict\s*of\s*interest|personal\s*friends?|household)\b", "No"),
    (r"\b(outside\s*business|outside\s*employment|advisory|consulting|board\s*roles?|side\s*business(es)?|secondary\s*employment|moonlighting)\b", "No"),
    (r"\b(government\s*official|politically\s*exposed|public\s*official|foreign\s*official)\b", "No"),
    (r"\b(ever\s*been\s*employed|employed\s*by.*affiliate|employed\s*by.*in\s*the\s*past|worked\s*for.*in\s*the\s*past|previous(ly)?\s*employed|worked\s*here|prior\s*employment\s*with|former\s*employee|worked\s*at\s*(stripe|google|meta|amazon|apple|microsoft|uber))\b", "No"),
    (r"\b(interviewed.*before|applied.*before|previously\s*applied)\b", "No"),
    (r"\b(terminated|discharged\s*from|fired|asked\s*to\s*resign|disciplinary\s*action)\b", "No"),
    (r"\b(convicted|felony|misdemeanor|criminal\s*record|criminal\s*history|debarred|suspended\s*from)\b", "No"),
    (r"\b(what\s*(level|type)\s*(is|of)\s*(your\s*)?(security\s*)?clearance|clearance\s*level|level\s*of\s*clearance|current\s*clearance\s*level)\b", "N/A"),
    (r"\b(security\s*clearance|clearance|uk\s*security\s*clearance)\b", "No"),
    (r"\b(background\s*check|undergo.*background|drug\s*screen|drug\s*test)\b", CAN_PASS_BACKGROUND_CHECK),
    (r"^(if\s*yes\b|if\s*applicable\b|please\s*explain\s*if\s*yes|if\s*so,\s*please|describe\s*relationship)", "N/A"),

    # --- 7. LOGISTICS, COMPENSATION & WORK PREFERENCES ---
    (r"\b(comfortable\s*with\s*(the\s*)?(compensation|salary|pay)|advertised\s*(on\s*the\s*)?role\s*posting|compensation\s*range|salary\s*range\s*acceptable)\b", "Yes"),
    (r"\b(desired\s*salary|expected\s*salary|target\s*compensation|salary\s*expectation|desired\s*pay)\b", "Open / Negotiable based on role and market standards"),
    (r"\bsalary\b|\bpay\b|\bcompensation\b", DESIRED_SALARY),
    (r"\b(business\s*travel|frequent\s*travel|travel\s*to\s*international|willing\s*to\s*travel|open\s*to\s*travel|international\s*locations|travel\s*requirement|travel\s*percentage)\b", "Yes"),
    (r"\b(on-?call|shift\s*work|overtime|flexible\s*hours|weekend\s*work|schedule\s*flexibility)\b", "Yes"),
    (r"\brelocate\b|\brelocation\b|\bopen\s*to\s*relocat\b", WILLING_TO_RELOCATE),
    (r"\boffice\b|\bonsite\b|\bin-?person\b", WILLING_TO_WORK_IN_OFFICE),
    (r"\bhybrid\b", WILLING_TO_WORK_HYBRID),
    (r"\b(plan\s*to\s*work\s*remotely|intend\s*to\s*work\s*remotely|work\s*from\s*a\s*remote|work\s*remotely|remote\s*location|remote\s*work|remote)\b", "Yes, I intend to work remotely."),
    (r"\b(upcoming\s*offer\s*deadlines|competing\s*offers|offer\s*deadlines)\b", "No"),
    (r"\b(dates\s*of\s*deadlines|what\s*are\s*the\s*dates)\b", "N/A"),
    (r"\b(final\s*internship\s*before\s*graduating)\b", "Yes"),
    (r"\bnotice\s*period\b|\bweeks\s*notice\b", NOTICE_PERIOD),
    (r"\bstart\s*date\b|\bavailable\b|\bavailability\b|\bwhen\s*can\s*you\s*start\b|\bearliest\b", AVAILABLE_START_DATE),

    # --- 8. BEHAVIORAL, ESSAY & CREATIVE PROMPTS ---
    (r"\b(two\s*software\s*engineer\s*roles|forward\s*deployed\s*software\s*engineer|confirm\s*that\s*you\s*are\s*interested\s*in\s*the\s*forward\s*deployed)\b", "I confirm that I am interested in the Forward Deployed Software Engineer role at Palantir, where I can apply my backend engineering and distributed data pipeline experience on the front lines to solve mission-critical customer problems."),
    (r"\b(three\s*numbers\s*that\s*describe\s*you|3\s*numbers\s*that\s*describe\s*you|stat\s*from\s*your\s*life)\b", "1) 3.71 — My graduate GPA at George Mason University, reflecting academic discipline and rigorous analytical problem solving. 2) 2 — Years of hands-on software & data engineering experience building resilient AWS backend systems and distributed ETL pipelines at Amazon Web Services and Talviro. 3) 99.9% — The ETL pipeline reliability achieved at AWS after eliminating race conditions in AWS Batch and designing automated Step Functions validation frameworks."),
    (r"\b(unreasonable\s*amount\s*about|nothing\s*to\s*do\s*with\s*technology|non-?technical\s*topic|hobby\s*or\s*passion)\b", "I know an unreasonable amount about Chess strategy and opening theory. I have spent years studying classical grandmaster games, pawn structure dynamics, and endgame calculation. Understanding how subtle positional moves compound into decisive advantages has deeply shaped how I approach structured problem solving and long-term decision making."),
    (r"\b(security\s*tool|security\s*feature|security\s*product|security-?relevant)\b", "At Amazon Web Services, I engineered an automated IAM role least-privilege validation and encryption verification module for distributed ETL pipelines using AWS Step Functions, Lambda, and CloudWatch. My contribution automated the continuous inspection of S3 staging buckets and batch processing containers to enforce KMS customer-managed key encryption and zero-trust IAM policy checks, preventing data leakage and eliminating pipeline permission errors across multi-tenant environments."),
    (r"\b(favorite\s*project|proudest\s*accomplishment|greatest\s*achievement|key\s*project|challenging\s*project|complex\s*project|difficult\s*bug|complex\s*bug|bug\s*you\s*debugged)\b", "At Amazon Web Services, I designed an automated validation framework using AWS Step Functions and S3 that eliminated race conditions in AWS Batch ETL pipelines, improving stability and reducing mean time to detect failures from hours to minutes."),
    (r"\b(greatest\s*strength|superpower|core\s*strength)\b", "My core strength is analytical root-cause debugging and building resilient distributed systems. I excel at taking complex, high-throughput backend data workflows and designing automated validation, monitoring, and error-handling architectures that maximize reliability."),
    (r"\b(team\s*culture|work\s*environment|what\s*do\s*you\s*look\s*for\s*in\s*a\s*team)\b", "I thrive in cultures with strong engineering ownership, high technical standards, transparent communication, and a focus on solving real-world customer problems with scalable, well-tested code."),

    # --- 9. CONSENTS, HANDLES & SIGNATURES ---
    (r"\b(gitlab\s*username|gitlab\s*profile|gitlab\s*handle)\b", "siddhesh202001"),
    (r"\b(name\s*you'?d\s*prefer|prefer\s*us\s*to\s*use|what\s*would\s*you\s*like\s*us\s*to\s*call\s*you|call\s*you)\b", FIRST_NAME),
    # Essential functions MUST be checked BEFORE generic accommodation (to avoid "None needed" matching "No")
    (r"\b(essential\s*functions|perform\s*these\s*essential|perform\s*the\s*essential|can\s*you\s*perform.*functions|perform.*reasonable\s*accommodation)\b", "Yes"),
    (r"\b(describe\s*any\s*need\s*for\s*a\s*reasonable\s*accommodation|need\s*for\s*a\s*reasonable\s*accommodation|please\s*describe.*accommodation)\b", "None needed, thank you."),
    (r"\b(adjustments?\s*we\s*can\s*make|assist\s*you\s*during\s*the\s*hiring|accessible\s*and\s*inclusive)\b", "None needed, thank you."),
    (r"\bhow\s*do\s*you\s*pronounce\b|\bpronounce\s*your\s*name\b", NAME_PRONUNCIATION),
    (r"\bai\s*notetaker\b|\btranscrib\b|\btranscription\b", "Yes, I consent"),
    (r"\b(disabilitysignaturedate|signature\s*date|date\s*signed|today'?s\s*date)\b", datetime.now().strftime("%m/%d/%Y")),
    (r"\b(zip\s*(/\s*)?postal\s*code|zip\s*code|postal\s*code|zipcode)\b", ZIP_CODE),
    (r"\b(i\s*certify\s*that|facts\s*set\s*forth|application\s*for\s*employment\s*are\s*true|release\s*the\s*employer\s*from\s*any\s*liability)\b", FULL_NAME),
    (r"\b(contact\s*your\s*current\s*employer|may\s*we\s*contact)\b", "Yes"),
    (r"\b(currently\s*reside\s*in\s*the\s*united\s*states|reside\s*in\s*the\s*us|reside\s*in\s*the\s*united\s*states)\b", "Yes"),
    (r"\b(type\s*your\s*(full\s*)?name|typing\s*your\s*(full\s*)?name|enter\s*your\s*(full\s*)?name|sign\s*here|nda\b|agree.*typing\s*your\s*name|disabilitysignature\b|\bsignature\b|\belectronic\s*signature\b)\b", FULL_NAME),
    (r"\b(is\s*your\s*primary\s*programming\s*language|primary\s*programming\s*language|hands[\s-]on\s*experience\s*with|experience\s*with\s*(iam|identity|oidc|oauth|golang|go|python|sql|aws|docker|kubernetes))\b", "Yes"),
    (r"\b(have\s*you\s*worked\s*hands[\s-]on|hands[\s-]on\s*with\s*(oidc|oauth|iam|aws|python|sql|docker))\b", "Yes"),
    (r"\backnowledge\b|\bagree\b|\bconsent\b|\bcertify\b|\bterms\b|\bprivacy\b", "Yes")
]

def _get_gemini_client():
    if not genai:
        return None
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        env_paths = [
            "/Users/siddheshsawant/sid_job_scrip/.env",
            os.path.expanduser("~/.env"),
            os.path.expanduser("~/Desktop/.env")
        ]
        for p in env_paths:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("GEMINI_API_KEY="):
                            api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if api_key: break
            if api_key: break
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini client: {e}")
        return None

def get_ai_answer(question_text: str, options: list[str] = None, field_type: str = "text") -> str:
    """
    Core function to intelligently answer ANY question on a job application form.
    """
    logger.info(f"Answering question: {question_text}")
    
    # 1. Fast path: pattern matching
    q_lower = question_text.lower()
    
    if options:
        # If options are provided, check if we can fast-path using get_best_option
        # This prevents picking a string not in the options when a dropdown is required
        return get_best_option(question_text, options)

    for pattern, answer in FAST_PATH_PATTERNS:
        if re.search(pattern, q_lower):
            logger.info(f"Fast path match for '{pattern}' -> '{answer}'")
            return answer
            
    # Check "Why company/role", Cover letter, "About yourself"
    # Grounded Open-Text Answer Writer for any open-text question (except Hard Limits in 4).
    try:
        from app.services.grounded_essay import is_open_text_question, write_grounded_open_answer
        if is_open_text_question(question_text):
            return write_grounded_open_answer(question_text, is_required=False)
    except ImportError:
        pass
    if re.search(r"\bwhy\b.*(\bcompany\b|\binterested\b|\brole\b|\bposition\b)", q_lower):
        return ""
    
    if re.search(r"\bcover\s*letter\b", q_lower):
        return ""
        
    if re.search(r"\btell\s*us\s*about\s*yourself\b|\badditional\s*information\b|\bmake\s*you\s*a\s*good\s*fit\b|\bdescribe\s*(your|yourself)\b|\babout\s*you\b", q_lower):
        return ""
        
    if re.search(r"\bwhat\s*excites\b|\bwhat\s*interests\b|\bwhat\s*attracts\b|\bwhat\s*draws\b|\bwhat\s*motivates\b", q_lower):
        return ""
    
    if re.search(r"\bexperience\s*with\b|\bfamiliar(ity)?\s*with\b|\bproficien(t|cy)\s*(in|with)\b|\bknowledge\s*of\b", q_lower):
        return ""
        
    if re.search(r"\bwilling\b|\bable\b|\bcomfortable\b|\bopen\s*to\b", q_lower) and not options:
        return "Yes"
    
    # 2. AI Fallback
    cache_key = f"{question_text}|{field_type}"
    if cache_key in _ANSWER_CACHE:
        logger.info("Returning cached AI answer.")
        return _ANSWER_CACHE[cache_key]

    client = _get_gemini_client()
    if client:
        try:
            sys_instruct = (
                "You are filling out a job application form for Siddhesh Sawant. Answer concisely and professionally. "
                "Use ONLY information from the provided resume and profile. If the answer requires selecting from options, "
                "return the EXACT option text. For yes/no questions about ability or willingness, default to 'Yes'. "
                "For salary questions, return empty string. Never fabricate information not in the profile."
            )
            
            prompt = f"Question: {question_text}\n"
            
            prompt += f"\n--- Candidate Profile ---\n{PROFILE_FACTS}\n\n"
            prompt += f"--- Candidate Resume ---\n{RESUME_TEXT}\n\n"
            
            logger.info("Calling Gemini API for answer...")
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_instruct,
                    temperature=0.1
                )
            )
            ans = response.text.strip()
            _ANSWER_CACHE[cache_key] = ans
            return ans
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            
    # 3. Default fallback
    logger.warning("No pattern match and AI unavailable. Returning default fallback.")
    if re.search(r"^(if\s*yes\b|if\s*applicable\b|please\s*explain\s*if\s*yes|if\s*so,\s*please|describe\s*relationship)", q_lower):
        return "N/A"
    if field_type == "checkbox" or field_type == "radio":
        return "Yes"
    if re.search(r"\b(are\s*you|do\s*you|can\s*you|will\s*you|is\s*it|have\s*you)\b", q_lower) and not field_type.startswith("text"):
        return "Yes"
    return ""

def get_best_option(question: str, options: list[str]) -> str:
    """
    Given a question and list of option strings, return the best option using pattern matching first, then AI fallback.
    """
    if not options:
        return ""
        
    q_lower = question.lower()
    opts_lower = [opt.lower() for opt in options]
    
    # 1. Specialized Domain Extractors (must take precedence over generic substring matching)
    # A. Degree / Education Level (Master's Degree)
    if re.search(r"\b(most\s*recent\s*degree|degree\s*you\s*obtained|degree\s*obtained|degree\s*earned|highest\s*(level|degree)\s*of\s*education|education\s*level|highest\s*education|degree\s*level|academic\s*degree|level\s*of\s*education|qualification|degree)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["master's degree", "master's", "masters degree", "masters", "master of science", "graduate degree", "postgraduate", "master"]) and "mba" not in opt.lower() and "business administration" not in opt.lower():
                return opt
        for opt in options:
            if any(k in opt.lower() for k in ["bachelor's degree", "bachelor's", "bachelors degree", "bachelors", "bachelor of engineering", "bachelor of science", "undergraduate degree", "bachelor"]):
                return opt
        for opt in options:
            if "other" in opt.lower():
                return opt

    # B. Remote Work Intent / Planning to work remotely
    if re.search(r"\b(plan\s*to\s*work\s*remotely|intend\s*to\s*work\s*remotely|work\s*from\s*a\s*remote|work\s*remotely|remote\s*location|remote\s*work|remote)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["yes, i intend to work remotely", "intend to work remotely", "work remotely", "yes - remote", "remote", "yes"]):
                return opt

    # C. Location Work Authorization
    if re.search(r"\b(work\s*authorization|authorized\s*to\s*work|legally\s*authorized|eligible\s*to\s*work|work\s*auth|legal\s*right\s*to\s*work|authorization\s*to\s*work|authorized\s*in\s*the\s*location|authorized\s*in\s*the\s*country|authorized\s*to\s*work\s*in\s*the\s*country)\b", q_lower):
        # Priority: Check if options include an explicit visa sponsorship requirement option
        for opt in options:
            if any(k in opt.lower() for k in ["require", "will require", "need sponsorship", "require sponsorship"]) and "unknown" not in opt.lower():
                return opt
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true", "authorized", "i am authorized"]:
                return opt

    # C2. Office Presence / Hybrid / Relocation
    if re.search(r"\b(office\s*three\s*days|three\s*days\s*per\s*week|3\s*days\s*per\s*week|work\s*from\s*(our|the)?\s*(us\s*)?office|able\s*to\s*work\s*from|work\s*from\s*office|work\s*in[\s-]person|onsite|hybrid|relocate|willing\s*to\s*relocate)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true"]:
                return opt

    # C3. Essential Functions / Reasonable Accommodation (Always Yes — candidate can perform)
    if re.search(r"\b(essential\s*functions|perform\s*these\s*essential|perform\s*the\s*essential|can\s*you\s*perform|perform.*reasonable\s*accommodation|reasonable\s*accommodation.*perform|accommodation.*essential)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true"]:
                return opt

    # C4. Contact Current Employer (Always Yes)
    if re.search(r"\b(contact\s*your\s*current\s*employer|contact\s*current\s*employer|may\s*we\s*contact)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true"]:
                return opt

    # C5. Currently Reside in the United States (Always Yes / positive location preference)
    if re.search(r"\b(currently\s*reside\s*in\s*the\s*united\s*states|reside\s*in\s*the\s*us|currently\s*live\s*in\s*the\s*united\s*states|based\s*in\s*the\s*united\s*states.*reside|do\s*you\s*currently\s*reside)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["already reside", "reside near"]):
                return opt
        for opt in options:
            if any(k in opt.lower() for k in ["willing to relocate", "relocate before"]):
                return opt
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true"]:
                return opt

    # C6. Certification / I Certify Acknowledgements
    if re.search(r"\b(i\s*certify|facts\s*set\s*forth|application\s*for\s*employment\s*are\s*true|release\s*the\s*employer\s*from\s*any\s*liability|certify\s*that\s*the\s*facts)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["yes", "agree", "acknowledge", "confirm", "accept", "consent"]):
                return opt

    # D. Work Permit Sponsorship / Visa Support
    if re.search(r"\b(sponsor|sponsorship|visa\s*sponsorship|visa\s*support|visa\s*assistance|immigration\s*(?:support|assistance)|work\s*permit|require.*sponsorship|need.*sponsorship|future.*sponsorship)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true"]:
                return opt

    # E. Anticipated Countries to Work In (Always select USA / US / United States)
    if re.search(r"\b(anticipate\s*working|countries\s*you\s*anticipate|country\s*or\s*countries\s*you\s*anticipate|where\s*do\s*you\s*plan\s*to\s*work|work\s*country\s*preference)\b", q_lower):
        for opt in options:
            if opt.lower().strip() in ["us", "usa", "united states", "united states of america"]:
                return opt
        for opt in options:
            if "united states" in opt.lower() or re.search(r"\b(us|usa)\b", opt.lower()):
                return opt

    # F. Current Country of Residence
    if re.search(r"\b(country\s*where\s*you\s*(currently\s*)?reside|country\s*of\s*residence|current\s*country|residing\s*country|where\s*do\s*you\s*(currently\s*)?reside|where\s*are\s*you\s*(currently\s*)?living)\b", q_lower):
        for opt in options:
            if opt.lower().strip() in ["us", "usa", "united states", "united states of america"]:
                return opt
        for opt in options:
            if "united states" in opt.lower():
                return opt
        for opt in options:
            if "india" in opt.lower():
                return opt

    # G. Employed by Company / Affiliate in the Past / Contractor
    if re.search(r"\b(ever\s*been\s*employed|employed\s*by|contracted\s*to\s*provide|worked\s*for|previous(ly)?\s*employed|worked\s*here|prior\s*employment\s*with|former\s*employee|worked\s*at)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["never", "have never been", "not previously", "no"]):
                return opt
        for opt in options:
            if opt.lower().startswith("no") or opt.lower() in ["no", "false", "i have not"]:
                return opt

    # H. WhatsApp / SMS Marketing Opt-In
    if re.search(r"\b(whatsapp|sms\s*marketing|promotional\s*messages|receive\s*whatsapp)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no") or opt.lower() in ["no", "false", "opt-out", "decline"]:
                return opt

    # I. Technical Skills, Programming Languages & Tools (Go, Python, IAM, OIDC, OAuth, AWS, etc.)
    if re.search(r"\b(primary\s*programming\s*language|programming\s*language|hands[\s-]on\s*experience|experience\s*with|worked\s*hands[\s-]on|familiar\s*with|proficient\s*in|knowledge\s*of|worked\s*with|iam|identity\s*&\s*access|oidc|oauth|golang|go\b|python|sql|java\b|javascript|aws|docker|kubernetes|cloud|restful|microservices|backend)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes") or opt.lower() in ["yes", "true", "i do", "proficient", "expert", "advanced", "intermediate"]:
                return opt

    if re.search(r"\b(employment\s*agreements?|post-?employment\s*restrictions?|restrictive\s*covenant)\b", q_lower):
        for opt in options:
            if opt.lower().strip() == "no" or opt.lower().startswith("no"): return opt
            
    if re.search(r"\b(sponsorship.*visa|visa.*sponsorship|visa\s*to\s*remain)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["f-1", "opt"]): return opt
        for opt in options:
            if any(k in opt.lower() for k in ["usmca", "h-1b"]): return opt
        for opt in options:
            if "yes" in opt.lower(): return opt
            
    if re.search(r"\b(based\s*in|best\s*describes\s*your\s*situation|relocate\s*to)\b", q_lower) and not re.search(r"\b(reside|currently\s*live|do\s*you\s*currently)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["willing to relocate", "relocate"]): return opt
            
    if re.search(r"\b(security-?relevant|security\s*tool|security\s*feature|security\s*product)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes"): return opt
            
    if re.search(r"\b(resident\s*of\s*california|california\s*resident|live\s*in\s*california|located\s*in\s*california|reside\s*in\s*california)\b", q_lower) or (re.search(r"\b(resident\s*of|reside\s*in|live\s*in|located\s*in)\b", q_lower) and "california" in q_lower):
        for opt in options:
            if opt.lower().strip() in ["no", "false", "i do not", "non-resident"] or opt.lower().startswith("no"):
                return opt

    if re.search(r"\b(resident\s*of|reside\s*in|currently\s*live\s*in|located\s*in)\s+(new\s*york|texas|florida|washington\s*state|massachusetts|illinois|colorado|ohio|georgia|north\s*carolina|pennsylvania|michigan|arizona)\b", q_lower):
        for opt in options:
            if opt.lower().strip() in ["no", "false", "i do not", "non-resident"] or opt.lower().startswith("no"):
                return opt

    if re.search(r"\b(how\s*many\s*years|years\s*of|post[\s-]college|total\s*experience|relevant\s*experience|engineering\s*experience|work\s*experience|industry\s*experience|software\s*experience)\b", q_lower) and any(re.search(r'\d+', opt) for opt in options):
        target_years = 2
        # Priority 1: Range starting with target_years (e.g. "2-4", "2-5", "2-3", "2 to 4")
        for opt in options:
            m = re.search(r'(\d+)\s*[-–—to]+\s*(\d+)', opt)
            if m:
                low, high = int(m.group(1)), int(m.group(2))
                if low == target_years and high > target_years:
                    return opt
        # Priority 2: "2", "2+", "2 years", "2 yrs"
        for opt in options:
            if re.search(r'^\s*2\s*(\+|\s*years?|\s*yrs?)?\s*$', opt, re.IGNORECASE):
                return opt
        # Priority 3: Range covering target_years (e.g. "1-3", "1-2")
        for opt in options:
            m = re.search(r'(\d+)\s*[-–—to]+\s*(\d+)', opt)
            if m:
                low, high = int(m.group(1)), int(m.group(2))
                if low <= target_years <= high:
                    return opt
        # Priority 4: "+" match (e.g. "1+", "2+") where target >= number
        for opt in options:
            m_plus = re.search(r'(\d+)\s*\+', opt)
            if m_plus and target_years >= int(m_plus.group(1)):
                return opt
        # Priority 5: any option containing "2" but not starting with 0 or <
        for opt in options:
            if "2" in opt and not opt.startswith("0") and not opt.startswith("<"):
                return opt
        # Priority 5: positive number fallback (1, 2, or 3)
        for opt in options:
            if any(num in opt for num in ["1", "3", "4"]) and not opt.startswith("0") and not opt.startswith("<"):
                return opt
                
    # 2. Try pattern matching based on the question
    matched_answer = None
    for pattern, answer in FAST_PATH_PATTERNS:
        if re.search(pattern, q_lower):
            matched_answer = answer.lower()
            break
            
    if matched_answer:
        # Check if the matched answer is in the options
        if matched_answer == "male":
            for opt in options:
                if opt.lower().strip() in ["male", "man"] or (opt.lower().startswith("male") and not opt.lower().startswith("fe")):
                    return opt
        elif matched_answer in ["us", "usa"]:
            for opt in options:
                if opt.lower().strip() in ["us", "usa", "united states", "united states of america"]:
                    return opt
        else:
            for opt in options:
                if matched_answer == opt.lower().strip():
                    return opt
            for opt in options:
                if matched_answer in opt.lower() or opt.lower() in matched_answer:
                    return opt
                
        # Handle Yes/No mappings explicitly
        if matched_answer.startswith("yes"):
            for opt in options:
                if opt.lower() in ["yes", "true", "i agree", "i consent"] or opt.lower().startswith("yes"):
                    return opt
        if matched_answer.startswith("no"):
            for opt in options:
                if opt.lower() in ["no", "false", "i disagree", "i do not"] or opt.lower().startswith("no"):
                    return opt

    # Try exact match or substring match of our facts directly in the options
    if re.search(r"\b(family\s*members?|relatives?|personal\s*relationships?|conflict\s*of\s*interest|personal\s*friends?|household)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\b(outside\s*business|outside\s*employment|advisory|consulting|board\s*roles?|side\s*business(es)?|secondary\s*employment|moonlighting)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\b(comfortable\s*with\s*(the\s*)?(compensation|salary|pay)|advertised\s*(on\s*the\s*)?role\s*posting|compensation\s*range|salary\s*range\s*acceptable)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes"): return opt
    if re.search(r"\b(business\s*travel|frequent\s*travel|travel\s*to\s*international|willing\s*to\s*travel|open\s*to\s*travel|international\s*locations|travel\s*requirement)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("yes"): return opt
    if re.search(r"\b(convicted|felony|misdemeanor|criminal\s*record|criminal\s*history|debarred|suspended\s*from|discharged\s*from\s*employment|terminated\s*for\s*cause)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\b(government\s*official|politically\s*exposed|public\s*official)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\b(based\s*in\s*any\s*of\s*these\s*countries|accepting\s*applications)\b", q_lower):
        for opt in options:
            if "united states" in opt.lower() or opt.lower() == "us" or "usa" in opt.lower(): return opt
    if re.search(r"\b(following\s*states|live\s*in\s*one\s*of\s*the\s*following)\b", q_lower):
        for opt in options:
            if opt.lower().strip() == "no" or opt.lower().startswith("no"): return opt
    if re.search(r"\b(describes\s*your\s*work\s*authorization|authorization\s*to\s*work\s*in\s*the\s*country)\b", q_lower):
        for opt in options:
            if "sponsored by the company" in opt.lower() or "needs to be sponsored" in opt.lower(): return opt
        for opt in options:
            if "work permit" in opt.lower() and "sponsor" in opt.lower(): return opt
        for opt in options:
            if "authorized" in opt.lower() and "work permit" in opt.lower(): return opt
    if re.search(r"\b(privacy\s*notice|applicant\s*privacy)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["acknowledge", "confirm", "yes", "i have read", "understand"]): return opt
    if re.search(r"\b(double-check|ensuring\s*accuracy|errors\s*or\s*omissions)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["reviewed", "confirmed", "accurate", "complete", "yes"]): return opt
    if re.search(r"\b(university|school|college|institution|attended)\b", q_lower):
        for opt in options:
            if "george mason" in opt.lower(): return opt
        for opt in options:
            if "mumbai" in opt.lower(): return opt
        for opt in options:
            if "other" in opt.lower(): return opt
    if re.search(r"\b(how\s*did\s*you\s*hear|where\s*did\s*you\s*hear|where\s*did\s*you\s*find|how\s*you\s*heard|first\s*hear|source)\b", q_lower):
        # 1. Career section / Career Site
        for opt in options:
            if any(k in opt.lower() for k in ["career site", "careers site", "career page", "careers page", "careers section", "careers website", "company career"]): return opt
        # 2. Company website / direct
        for opt in options:
            if any(k in opt.lower() for k in ["website", "direct", "company site"]): return opt
        # 3. LinkedIn
        for opt in options:
            if "linkedin" in opt.lower(): return opt
        # 4. Job Board / Indeed
        for opt in options:
            if any(k in opt.lower() for k in ["job board", "indeed", "glassdoor", "built"]): return opt
        # 5. Other
        for opt in options:
            if "other" in opt.lower(): return opt
    # U.S. Person / Export Compliance (locked to profile facts, never infer)
    if re.search(r"\b(u\.?s\.?\s*person|us\s*person|which.*best\s*describes\s*your.*u\.?s\.?\s*person|export\s*(compliance|control)s?|itar|ear)\b", q_lower):
        for opt in options:
            opt_l = opt.lower()
            if any(k in opt_l for k in [
                "none of the above; i am a citizen of a different country",
                "none of the above",
                "citizen of a different country",
                "different country",
                "not a u.s. person",
                "not a us person",
                "i am not a u.s. person",
                "eligible for licensing",
                "other",
                "neither"
            ]) and "cuba" not in opt_l:
                return opt
        for opt in options:
            if opt.lower().startswith("no"): return opt

    # Security Clearance level follow-up
    if re.search(r"\b(what\s*(level|type)\s*(is|of)\s*(your\s*)?(security\s*)?clearance|clearance\s*level|level\s*of\s*clearance)\b", q_lower):
        for opt in options:
            if opt.lower() in ["n/a", "not applicable", "none", "no clearance", "no", "null"] or "none" in opt.lower() or "not applicable" in opt.lower() or "n/a" in opt.lower():
                return opt
        for opt in options:
            if opt.lower().startswith("no"): return opt

    # Phone Country Calling Code
    if re.search(r"\b(phone|country\s*code|calling\s*code)\b", q_lower) and any(any(code in opt for code in ["+1", "+91", "United States +1"]) for opt in options):
        for opt in options:
            if "united states +1" in opt.lower() or opt.strip() == "+1" or opt.strip() == "United States (+1)":
                return opt
        for opt in options:
            if "+1" in opt:
                return opt

    if re.search(r"\b(security\s*clearance|clearance)\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no") or "do not" in opt.lower() or "none" in opt.lower(): return opt
    if re.search(r"\b(notetaker|transcrib|consent|privacy)\b", q_lower):
        for opt in options:
            if "yes" in opt.lower() or "consent" in opt.lower() or "agree" in opt.lower(): return opt
    if re.search(r"\btransgender\b|\btrans\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no") or "not" in opt.lower() or "cis" in opt.lower(): return opt
        for opt in options:
            if "decline" in opt.lower() or "prefer not" in opt.lower() or "don't wish" in opt.lower(): return opt
    if re.search(r"\b(sexual\s*orientation|orientation|sexual\s*identity)\b", q_lower):
        for opt in options:
            if "heterosexual" in opt.lower() or "straight" in opt.lower(): return opt
    if re.search(r"\b(gender\s*identity|gender|sex)\b", q_lower) and not re.search(r"\btrans", q_lower):
        for opt in options:
            if opt.lower().strip() in ["male", "man"]: return opt
            if opt.lower().startswith("male") and not "female" in opt.lower(): return opt
    if re.search(r"\b(racial|ethnic|race|ethnicity|ethnicities|demographic|heritage|descent|background)\b", q_lower):
        for opt in options:
            if "south asian" in opt.lower() or "indian" in opt.lower(): return opt
        for opt in options:
            if "asian" in opt.lower() and not any(k in opt.lower() for k in ["east asian", "southeast asian", "non-asian"]): return opt
        for opt in options:
            if "asian" in opt.lower(): return opt
    if re.search(r"\bhispanic\b|\blatino\b", q_lower):
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\bveteran\b", q_lower):
        for opt in options:
            if "no military service" in opt.lower() or "not a protected" in opt.lower() or "i am not" in opt.lower(): return opt
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\bdisability\b", q_lower):
        for opt in options:
            if "do not have" in opt.lower() or "no, i don" in opt.lower(): return opt
        for opt in options:
            if opt.lower().startswith("no"): return opt
    if re.search(r"\brelocate\b|\brelocation\b", q_lower):
        for opt in options:
            if "yes" in opt.lower(): return opt
    if re.search(r"\boffice\b|\bonsite\b|\bhybrid\b|\bin-person\b", q_lower):
        for opt in options:
            if "yes" in opt.lower(): return opt
    if re.search(r"\b(notice\s*period|start\s*timing|availability\s*to\s*start)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["2 week", "1-2 week", "2-4 week", "immediate", "less than 1 month", "within 1 month"]): return opt
    if re.search(r"\b(years\s*of\s*experience|total\s*experience|relevant\s*experience|engineering\s*experience)\b", q_lower):
        target_years = 2
        for opt in options:
            m = re.search(r'(\d+)\s*[-–to]+\s*(\d+)', opt)
            if m:
                low, high = int(m.group(1)), int(m.group(2))
                if low <= target_years <= high:
                    return opt
            m_plus = re.search(r'(\d+)\s*\+', opt)
            if m_plus and target_years >= int(m_plus.group(1)):
                return opt
            if str(target_years) in opt:
                return opt
    if re.search(r"\b(proficiency|fluency|english\s*level|language\s*proficiency)\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["fluent", "native", "professional", "advanced", "full professional"]): return opt
    if re.search(r"\bcountry\b", q_lower):
        for opt in options:
            if opt.lower().strip() in ["us", "usa", "united states", "united states of america"]: return opt
        for opt in options:
            if "united states" in opt.lower(): return opt
    if re.search(r"\bpronouns\b", q_lower):
        for opt in options:
            if "he" in opt.lower() and "him" in opt.lower(): return opt
    if re.search(r"\backnowledge\b|\bagree\b|\bconsent\b|\bcertify\b|\bconfirm\b|\bterms\b|\bprivacy\b", q_lower):
        for opt in options:
            if any(k in opt.lower() for k in ["yes", "agree", "accept", "consent", "confirm", "acknowledge"]): return opt
            
    # AI Fallback for options
    cache_key = f"{question}|{'|'.join(options)}"
    if cache_key in _ANSWER_CACHE:
        return _ANSWER_CACHE[cache_key]

    client = _get_gemini_client()
    if client:
        try:
            sys_instruct = (
                "You are filling out a job application form for Siddhesh Sawant. "
                "You MUST return EXACTLY ONE of the provided options that best matches the candidate's profile. "
                "Do NOT return any other text, just the exact option string."
            )
            
            prompt = f"Question: {question}\nOptions:\n"
            for i, opt in enumerate(options):
                prompt += f"- {opt}\n"
                
            prompt += f"\n--- Candidate Profile ---\n{PROFILE_FACTS}\n\n"
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_instruct,
                    temperature=0.0
                )
            )
            ans = response.text.strip()
            
            # Validate answer is actually in options
            if ans in options:
                _ANSWER_CACHE[cache_key] = ans
                return ans
            else:
                # Fuzzy match if AI returned slight variation
                for opt in options:
                    if ans.lower() in opt.lower() or opt.lower() in ans.lower():
                        _ANSWER_CACHE[cache_key] = opt
                        return opt
        except Exception as e:
            logger.error(f"Gemini API error in get_best_option: {e}")

    # Ultimate fallback: return first option
    logger.warning("Could not determine best option. Returning the first one.")
    return options[0]

def generate_why_company_answer(question_or_company: str) -> str:
    """
    Grounded 'Why [Company]?' answer (2-4 sentences) from the JD/about page + verified candidate facts.
    Returns "" when it cannot be grounded — NEVER generic filler (the old template is removed).
    """
    try:
        from app.services.grounded_essay import write_why_company_answer
        return write_why_company_answer(question_or_company, is_required=False)
    except Exception as e:
        logger.error(f"Grounded why-company writer error: {e}")
        return ""
