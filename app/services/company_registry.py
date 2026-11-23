"""
AutoApply - Authoritative Single Board Registry
Unified registry consolidating all reachable company career boards across Greenhouse, Lever, and Ashby.
Explicitly categorizes unsupported portals (Workday, Taleo, custom career portals).
"""
import os
import re
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Dict, Optional, Any, Union

logger = logging.getLogger(__name__)

try:
    from config import settings
    DATA_DIR = getattr(settings, "DATA_DIR", os.path.expanduser("~/sid_job_scrip/data"))
except Exception:
    DATA_DIR = os.path.expanduser("~/sid_job_scrip/data")

REGISTRY_DB_PATH = os.path.join(DATA_DIR, "autoapply.db")

# Verified live company boards (100% reachable via Greenhouse, Lever, or Ashby API)
REAL_COMPANY_BOARDS: List[Tuple[str, str, str]] = [
    ('Abnormal Security', 'greenhouse', 'abnormalsecurity'),
    ('Adyen', 'greenhouse', 'adyen'),
    ('Affirm', 'greenhouse', 'affirm'),
    ('Airbnb', 'greenhouse', 'airbnb'),
    ('Airtable', 'greenhouse', 'airtable'),
    ('Akuna Capital', 'greenhouse', 'akunacapital'),
    ('Algolia', 'greenhouse', 'algolia'),
    ('Ambi Robotics', 'lever', 'ambirobotics'),
    ('Anduril', 'greenhouse', 'andurilindustries'),
    ('Anthropic', 'greenhouse', 'anthropic'),
    ('Anyscale', 'ashby', 'anyscale'),
    ('Apollo.io', 'greenhouse', 'apolloio'),
    ('Asana', 'greenhouse', 'asana'),
    ('Astranis', 'greenhouse', 'astranis'),
    ('Benchling', 'ashby', 'benchling'),
    ('Bill.com', 'greenhouse', 'billcom'),
    ('BitGo', 'greenhouse', 'bitgo'),
    ('Blend', 'greenhouse', 'blend'),
    ('Block', 'greenhouse', 'block'),
    ('Braintrust', 'ashby', 'braintrust'),
    ('Branch', 'greenhouse', 'branch'),
    ('Braze', 'greenhouse', 'braze'),
    ('Brex', 'greenhouse', 'brex'),
    ('Carta', 'greenhouse', 'carta'),
    ('Carvana', 'greenhouse', 'carvana'),
    ('Character.AI', 'ashby', 'character'),
    ('Chime', 'greenhouse', 'chime'),
    ('Circle', 'ashby', 'circle'),
    ('ClickHouse', 'ashby', 'clickhouse'),
    ('ClickUp', 'ashby', 'clickup'),
    ('Cloudflare', 'greenhouse', 'cloudflare'),
    ('Cockroach Labs', 'greenhouse', 'cockroachlabs'),
    ('Cohere', 'ashby', 'cohere'),
    ('Coinbase', 'greenhouse', 'coinbase'),
    ('Color Health', 'ashby', 'color-health'),
    ('Confluent', 'ashby', 'confluent'),
    ('Consensys', 'greenhouse', 'consensys'),
    ('Cursor', 'ashby', 'cursor'),
    ('Databricks', 'greenhouse', 'databricks'),
    ('Datadog', 'greenhouse', 'datadog'),
    ('Dexterity', 'lever', 'dexterity'),
    ('Discord', 'greenhouse', 'discord'),
    ('Docker', 'ashby', 'docker'),
    ('DoorDash', 'greenhouse', 'doordashusa'),
    ('Doximity', 'greenhouse', 'doximity'),
    ('Dropbox', 'greenhouse', 'dropbox'),
    ('Elastic', 'greenhouse', 'elastic'),
    ('ElevenLabs', 'ashby', 'elevenlabs'),
    ('Epic Games', 'greenhouse', 'epicgames'),
    ('Faire', 'greenhouse', 'faire'),
    ('Fastly', 'greenhouse', 'fastly'),
    ('Figma', 'greenhouse', 'figma'),
    ('Flatiron Health', 'greenhouse', 'flatironhealth'),
    ('Flexport', 'greenhouse', 'flexport'),
    ('Flow Traders', 'greenhouse', 'flowtraders'),
    ('Form Energy', 'ashby', 'formenergy'),
    ('Gemini', 'greenhouse', 'gemini'),
    ('GitLab', 'greenhouse', 'gitlab'),
    ('GoDaddy', 'greenhouse', 'godaddy'),
    ('Grafana Labs', 'greenhouse', 'grafanalabs'),
    ('Gusto', 'greenhouse', 'gusto'),
    ('Hopper', 'ashby', 'hopper'),
    ('Inflection AI', 'greenhouse', 'inflectionai'),
    ('Infinite Computer Solutions', 'ashby', 'infinite'),
    ('Instacart', 'greenhouse', 'instacart'),
    ('Intercom', 'greenhouse', 'intercom'),
    ('Jane Street', 'greenhouse', 'janestreet'),
    ('Jump Trading', 'greenhouse', 'jumptrading'),
    ('Klaviyo', 'greenhouse', 'klaviyo'),
    ('Kraken', 'lever', 'kraken'),
    ('LangChain', 'ashby', 'langchain'),
    ('Lattice', 'greenhouse', 'lattice'),
    ('LaunchDarkly', 'greenhouse', 'launchdarkly'),
    ('Linear', 'ashby', 'linear'),
    ('Locus Robotics', 'greenhouse', 'locusrobotics'),
    ('LPL Financial', 'greenhouse', 'lpl'),
    ('Lucid Motors', 'greenhouse', 'lucidmotors'),
    ('Lyft', 'greenhouse', 'lyft'),
    ('Miro', 'ashby', 'miro'),
    ('Modal', 'ashby', 'modal'),
    ('MongoDB', 'greenhouse', 'mongodb'),
    ('Motional', 'greenhouse', 'motional'),
    ('Motive', 'greenhouse', 'motive'),
    ('Notion', 'ashby', 'notion'),
    ('Nuro', 'greenhouse', 'nuro'),
    ('Oath Holdings', 'greenhouse', 'oath'),
    ('Okta', 'greenhouse', 'okta'),
    ('OpenAI', 'ashby', 'openai'),
    ('Orca Security', 'greenhouse', 'orcasecurity'),
    ('Oscar Health', 'greenhouse', 'oscar'),
    ('Outreach', 'lever', 'outreach'),
    ('PagerDuty', 'greenhouse', 'pagerduty'),
    ('Peloton Interactive', 'greenhouse', 'peloton'),
    ('Palantir', 'lever', 'palantir'),
    ('Perplexity AI', 'ashby', 'perplexity'),
    ('Physical Intelligence', 'ashby', 'physicalintelligence'),
    ('Pinecone', 'ashby', 'pinecone'),
    ('Pinterest', 'greenhouse', 'pinterest'),
    ('Plaid', 'ashby', 'plaid'),
    ('Planet Labs', 'greenhouse', 'planetlabs'),
    ('Point72', 'greenhouse', 'point72'),
    ('Pure Storage', 'greenhouse', 'purestorage'),
    ('Qualtrics', 'greenhouse', 'qualtrics'),
    ('Ramp', 'ashby', 'ramp'),
    ('Reddit', 'greenhouse', 'reddit'),
    ('Redis', 'ashby', 'redis'),
    ('Redwood Materials', 'greenhouse', 'redwoodmaterials'),
    ('Remote', 'greenhouse', 'remote'),
    ('Replit', 'ashby', 'replit'),
    ('Riot Games', 'greenhouse', 'riotgames'),
    ('Ripple', 'greenhouse', 'ripple'),
    ('Ro', 'lever', 'ro'),
    ('Robinhood', 'greenhouse', 'robinhood'),
    ('Roblox', 'greenhouse', 'roblox'),
    ('Rubrik', 'greenhouse', 'rubrik'),
    ('Runway', 'ashby', 'runway'),
    ('Samsara', 'greenhouse', 'samsara'),
    ('Samsung Semiconductor', 'greenhouse', 'samsungsemiconductor'),
    ('Scale AI', 'greenhouse', 'scaleai'),
    ('Sendbird', 'greenhouse', 'sendbird'),
    ('Sentry', 'ashby', 'sentry'),
    ('Shield AI', 'lever', 'shieldai'),
    ('SingleStore', 'greenhouse', 'singlestore'),
    ('Skydio', 'ashby', 'skydio'),
    ('Smartsheet', 'greenhouse', 'smartsheet'),
    ('Snowflake', 'ashby', 'snowflake'),
    ('SoFi', 'greenhouse', 'sofi'),
    ('Social Finance', 'greenhouse', 'socialfinance'),
    ('SpaceX', 'greenhouse', 'spacex'),
    ('Spotify', 'lever', 'spotify'),
    ('Stripe', 'greenhouse', 'stripe'),
    ('Supabase', 'ashby', 'supabase'),
    ('Synthesia', 'ashby', 'synthesia'),
    ('TCS', 'greenhouse', 'tcs'),
    ('Thoughtworks', 'greenhouse', 'thoughtworks'),
    ('Toast', 'greenhouse', 'toast'),
    ('Together AI', 'greenhouse', 'togetherai'),
    ('Twilio', 'greenhouse', 'twilio'),
    ('Vercel', 'greenhouse', 'vercel'),
    ('Verkada', 'greenhouse', 'verkada'),
    ('Waabi', 'lever', 'waabi'),
    ('Waymo', 'greenhouse', 'waymo'),
    ('Webflow', 'greenhouse', 'webflow'),
    ('Weights & Biases', 'greenhouse', 'weights_and_biases'),
    ('Western Union', 'greenhouse', 'westernunion'),
    ('Wiz', 'ashby', 'wiz'),
    ('Zapier', 'ashby', 'zapier'),
    ('ZoomInfo', 'greenhouse', 'zoominfo'),
    ('Zoox', 'lever', 'zoox'),
    ('Zscaler', 'greenhouse', 'zscaler'),
]

# Unsupported companies from the 442 watchlist (Workday, Taleo, Custom Portals without public ATS API)
UNSUPPORTED_COMPANIES: Dict[str, str] = {
    '3M': "Custom Portal / Workday without public ATS API",
    'Abbott Laboratories': "Custom Portal / Workday without public ATS API",
    'Accenture': "Custom Portal / Workday without public ATS API",
    'Activision Blizzard': "Custom Portal / Workday without public ATS API",
    'Addepar': "Custom Portal / Workday without public ATS API",
    'Adept AI': "Custom Portal / Workday without public ATS API",
    'Adobe': "Custom Portal / Workday without public ATS API",
    'Aflac': "Custom Portal / Workday without public ATS API",
    'AIG': "Custom Portal / Workday without public ATS API",
    'Akamai': "Custom Portal / Workday without public ATS API",
    'Allstate': "Custom Portal / Workday without public ATS API",
    'Amazon': "Custom Portal / Workday without public ATS API",
    'AMD': "Custom Portal / Workday without public ATS API",
    'American Express': "Custom Portal / Workday without public ATS API",
    'American Tower': "Custom Portal / Workday without public ATS API",
    'Amgen': "Custom Portal / Workday without public ATS API",
    'Amphenol': "Custom Portal / Workday without public ATS API",
    'Analog Devices': "Custom Portal / Workday without public ATS API",
    'Apple': "Custom Portal / Workday without public ATS API",
    'Applied Intuition': "Custom Portal / Workday without public ATS API",
    'Applied Materials': "Custom Portal / Workday without public ATS API",
    'Archer Aviation': "Custom Portal / Workday without public ATS API",
    'Arista Networks': "Custom Portal / Workday without public ATS API",
    'ASML': "Custom Portal / Workday without public ATS API",
    'AT&T': "Custom Portal / Workday without public ATS API",
    'Athenahealth': "Custom Portal / Workday without public ATS API",
    'Atlassian': "Custom Portal / Workday without public ATS API",
    'Aurora Innovation': "Custom Portal / Workday without public ATS API",
    'Autodesk': "Custom Portal / Workday without public ATS API",
    'AvidXchange': "Custom Portal / Workday without public ATS API",
    'BAE Systems': "Custom Portal / Workday without public ATS API",
    'Bain & Company': "Custom Portal / Workday without public ATS API",
    'Balyasny Asset Management': "Custom Portal / Workday without public ATS API",
    'Bank of America': "Custom Portal / Workday without public ATS API",
    'Baxter': "Custom Portal / Workday without public ATS API",
    'Berkshire Grey': "Custom Portal / Workday without public ATS API",
    'Best Buy': "Custom Portal / Workday without public ATS API",
    'Biogen': "Custom Portal / Workday without public ATS API",
    'BlackRock': "Custom Portal / Workday without public ATS API",
    'Bloomberg': "Custom Portal / Workday without public ATS API",
    'BNY Mellon': "Custom Portal / Workday without public ATS API",
    'Boeing': "Custom Portal / Workday without public ATS API",
    'Bolt': "Custom Portal / Workday without public ATS API",
    'Booking Holdings': "Custom Portal / Workday without public ATS API",
    'Boom Supersonic': "Custom Portal / Workday without public ATS API",
    'Booz Allen Hamilton': "Custom Portal / Workday without public ATS API",
    'Boston Consulting Group': "Custom Portal / Workday without public ATS API",
    'Boston Dynamics': "Custom Portal / Workday without public ATS API",
    'Boston Scientific': "Custom Portal / Workday without public ATS API",
    'Box': "Custom Portal / Workday without public ATS API",
    'Bridgewater Associates': "Custom Portal / Workday without public ATS API",
    'Bristol Myers Squibb': "Custom Portal / Workday without public ATS API",
    'Broadcom': "Custom Portal / Workday without public ATS API",
    'C3.ai': "Custom Portal / Workday without public ATS API",
    'Cadence': "Custom Portal / Workday without public ATS API",
    'Canva': "Custom Portal / Workday without public ATS API",
    'Capgemini': "Custom Portal / Workday without public ATS API",
    'Capital One': "Custom Portal / Workday without public ATS API",
    'Caterpillar': "Custom Portal / Workday without public ATS API",
    'CBRE': "Custom Portal / Workday without public ATS API",
    'Centene': "Custom Portal / Workday without public ATS API",
    'Cerner': "Custom Portal / Workday without public ATS API",
    'CH Robinson': "Custom Portal / Workday without public ATS API",
    'Charles Schwab': "Custom Portal / Workday without public ATS API",
    'Charter Communications': "Custom Portal / Workday without public ATS API",
    'Chevron': "Custom Portal / Workday without public ATS API",
    'Chewy': "Custom Portal / Workday without public ATS API",
    'Chubb': "Custom Portal / Workday without public ATS API",
    'Cigna': "Custom Portal / Workday without public ATS API",
    'Cisco': "Custom Portal / Workday without public ATS API",
    'Citadel': "Custom Portal / Workday without public ATS API",
    'Citi': "Custom Portal / Workday without public ATS API",
    'Clearbit': "Custom Portal / Workday without public ATS API",
    'Cleveland Clinic': "Custom Portal / Workday without public ATS API",
    'Coca-Cola': "Custom Portal / Workday without public ATS API",
    'Cognizant': "Custom Portal / Workday without public ATS API",
    'Colgate-Palmolive': "Custom Portal / Workday without public ATS API",
    'Comcast': "Custom Portal / Workday without public ATS API",
    'Compass': "Custom Portal / Workday without public ATS API",
    'ConocoPhillips': "Custom Portal / Workday without public ATS API",
    'Corning': "Custom Portal / Workday without public ATS API",
    'CoStar Group': "Custom Portal / Workday without public ATS API",
    'Costco': "Custom Portal / Workday without public ATS API",
    'Couchbase': "Custom Portal / Workday without public ATS API",
    'CrowdStrike': "Custom Portal / Workday without public ATS API",
    'Crown Castle': "Custom Portal / Workday without public ATS API",
    'Cruise': "Custom Portal / Workday without public ATS API",
    'CSX': "Custom Portal / Workday without public ATS API",
    'Cummins': "Custom Portal / Workday without public ATS API",
    'CVS Health': "Custom Portal / Workday without public ATS API",
    'DE Shaw': "Custom Portal / Workday without public ATS API",
    'Deel': "Custom Portal / Workday without public ATS API",
    'Deere & Company': "Custom Portal / Workday without public ATS API",
    'Deloitte': "Custom Portal / Workday without public ATS API",
    'DocuSign': "Custom Portal / Workday without public ATS API",
    'Dominion Energy': "Custom Portal / Workday without public ATS API",
    "Domino's Pizza": "Custom Portal / Workday without public ATS API",
    'DRW': "Custom Portal / Workday without public ATS API",
    'Duke Energy': "Custom Portal / Workday without public ATS API",
    'Dynatrace': "Custom Portal / Workday without public ATS API",
    'Eaton': "Custom Portal / Workday without public ATS API",
    'eBay': "Custom Portal / Workday without public ATS API",
    'Electronic Arts': "Custom Portal / Workday without public ATS API",
    'Elevance Health': "Custom Portal / Workday without public ATS API",
    'Eli Lilly': "Custom Portal / Workday without public ATS API",
    'Emerson Electric': "Custom Portal / Workday without public ATS API",
    'Enphase Energy': "Custom Portal / Workday without public ATS API",
    'EPAM Systems': "Custom Portal / Workday without public ATS API",
    'Epic Systems': "Custom Portal / Workday without public ATS API",
    'Etsy': "Custom Portal / Workday without public ATS API",
    'EXL Service': "Custom Portal / Workday without public ATS API",
    'Expedia': "Custom Portal / Workday without public ATS API",
    'Expeditors': "Custom Portal / Workday without public ATS API",
    'ExxonMobil': "Custom Portal / Workday without public ATS API",
    'EY': "Custom Portal / Workday without public ATS API",
    'FedEx': "Custom Portal / Workday without public ATS API",
    'Fetch Robotics': "Custom Portal / Workday without public ATS API",
    'Fidelity Investments': "Custom Portal / Workday without public ATS API",
    'First Solar': "Custom Portal / Workday without public ATS API",
    'Ford': "Custom Portal / Workday without public ATS API",
    'Front': "Custom Portal / Workday without public ATS API",
    'Gatik': "Custom Portal / Workday without public ATS API",
    'Genentech': "Custom Portal / Workday without public ATS API",
    'General Dynamics': "Custom Portal / Workday without public ATS API",
    'General Electric': "Custom Portal / Workday without public ATS API",
    'General Mills': "Custom Portal / Workday without public ATS API",
    'General Motors': "Custom Portal / Workday without public ATS API",
    'Genpact': "Custom Portal / Workday without public ATS API",
    'Gilead Sciences': "Custom Portal / Workday without public ATS API",
    'Glean': "Custom Portal / Workday without public ATS API",
    'Goldman Sachs': "Custom Portal / Workday without public ATS API",
    'Gong': "Custom Portal / Workday without public ATS API",
    'GoodRx': "Custom Portal / Workday without public ATS API",
    'Google': "Custom Portal / Workday without public ATS API",
    'Groq': "Custom Portal / Workday without public ATS API",
    'Harley-Davidson': "Custom Portal / Workday without public ATS API",
    'Hartford Financial': "Custom Portal / Workday without public ATS API",
    'Harvey AI': "Custom Portal / Workday without public ATS API",
    'HashiCorp': "Custom Portal / Workday without public ATS API",
    'HCA Healthcare': "Custom Portal / Workday without public ATS API",
    'HCLTech': "Custom Portal / Workday without public ATS API",
    'Hilton': "Custom Portal / Workday without public ATS API",
    'Home Depot': "Custom Portal / Workday without public ATS API",
    'Honeywell': "Custom Portal / Workday without public ATS API",
    'HubSpot': "Custom Portal / Workday without public ATS API",
    'Hudson River Trading': "Custom Portal / Workday without public ATS API",
    'Hugging Face': "Custom Portal / Workday without public ATS API",
    'Humana': "Custom Portal / Workday without public ATS API",
    'Huntington Ingalls': "Custom Portal / Workday without public ATS API",
    'Hyatt': "Custom Portal / Workday without public ATS API",
    'IBM': "Custom Portal / Workday without public ATS API",
    'Illinois Tool Works': "Custom Portal / Workday without public ATS API",
    'Illumina': "Custom Portal / Workday without public ATS API",
    'IMC Trading': "Custom Portal / Workday without public ATS API",
    'Infosys': "Custom Portal / Workday without public ATS API",
    'Insight Global': "Custom Portal / Workday without public ATS API",
    'Intel': "Custom Portal / Workday without public ATS API",
    'Intuit': "Custom Portal / Workday without public ATS API",
    'Intuitive Surgical': "Custom Portal / Workday without public ATS API",
    'J.B. Hunt': "Custom Portal / Workday without public ATS API",
    'Jasper': "Custom Portal / Workday without public ATS API",
    'JLL': "Custom Portal / Workday without public ATS API",
    'Joby Aviation': "Custom Portal / Workday without public ATS API",
    'Johnson & Johnson': "Custom Portal / Workday without public ATS API",
    'JPMorgan Chase': "Custom Portal / Workday without public ATS API",
    'Juniper Networks': "Custom Portal / Workday without public ATS API",
    'Kaiser Permanente': "Custom Portal / Workday without public ATS API",
    'KeepTruckin': "Custom Portal / Workday without public ATS API",
    'KLA': "Custom Portal / Workday without public ATS API",
    'Klarna': "Custom Portal / Workday without public ATS API",
    'Kodiak Robotics': "Custom Portal / Workday without public ATS API",
    'KPMG': "Custom Portal / Workday without public ATS API",
    'Kraft Heinz': "Custom Portal / Workday without public ATS API",
    'Kroger': "Custom Portal / Workday without public ATS API",
    'L3Harris': "Custom Portal / Workday without public ATS API",
    'Lacework': "Custom Portal / Workday without public ATS API",
    'Lam Research': "Custom Portal / Workday without public ATS API",
    'Leidos': "Custom Portal / Workday without public ATS API",
    'Liberty Mutual': "Custom Portal / Workday without public ATS API",
    'Lockheed Martin': "Custom Portal / Workday without public ATS API",
    "Lowe's": "Custom Portal / Workday without public ATS API",
    'LTIMindtree': "Custom Portal / Workday without public ATS API",
    'Marqeta': "Custom Portal / Workday without public ATS API",
    'Marriott': "Custom Portal / Workday without public ATS API",
    'Marvell': "Custom Portal / Workday without public ATS API",
    'Mastercard': "Custom Portal / Workday without public ATS API",
    'Mayo Clinic': "Custom Portal / Workday without public ATS API",
    "McDonald's": "Custom Portal / Workday without public ATS API",
    'McKinsey & Company': "Custom Portal / Workday without public ATS API",
    'Medtronic': "Custom Portal / Workday without public ATS API",
    'Merck': "Custom Portal / Workday without public ATS API",
    'Meta': "Custom Portal / Workday without public ATS API",
    'MetLife': "Custom Portal / Workday without public ATS API",
    'Microchip Technology': "Custom Portal / Workday without public ATS API",
    'Micron Technology': "Custom Portal / Workday without public ATS API",
    'Microsoft': "Custom Portal / Workday without public ATS API",
    'Millennium Management': "Custom Portal / Workday without public ATS API",
    'Mistral AI': "Custom Portal / Workday without public ATS API",
    'Moderna': "Custom Portal / Workday without public ATS API",
    'Monday.com': "Custom Portal / Workday without public ATS API",
    'Mondelez': "Custom Portal / Workday without public ATS API",
    'Morgan Stanley': "Custom Portal / Workday without public ATS API",
    'NBCUniversal': "Custom Portal / Workday without public ATS API",
    'Netflix': "Custom Portal / Workday without public ATS API",
    'New York Life': "Custom Portal / Workday without public ATS API",
    'NextEra Energy': "Custom Portal / Workday without public ATS API",
    'Nike': "Custom Portal / Workday without public ATS API",
    'Norfolk Southern': "Custom Portal / Workday without public ATS API",
    'Northern Trust': "Custom Portal / Workday without public ATS API",
    'Northrop Grumman': "Custom Portal / Workday without public ATS API",
    'Nutanix': "Custom Portal / Workday without public ATS API",
    'Nvidia': "Custom Portal / Workday without public ATS API",
    'Old Dominion Freight Line': "Custom Portal / Workday without public ATS API",
    'Opendoor': "Custom Portal / Workday without public ATS API",
    'Optiver': "Custom Portal / Workday without public ATS API",
    'Optum': "Custom Portal / Workday without public ATS API",
    'Oracle': "Custom Portal / Workday without public ATS API",
    'PACCAR': "Custom Portal / Workday without public ATS API",
    'Palo Alto Networks': "Custom Portal / Workday without public ATS API",
    'Paramount': "Custom Portal / Workday without public ATS API",
    'Parker Hannifin': "Custom Portal / Workday without public ATS API",
    'PayPal': "Custom Portal / Workday without public ATS API",
    'PepsiCo': "Custom Portal / Workday without public ATS API",
    'Pfizer': "Custom Portal / Workday without public ATS API",
    'PNC': "Custom Portal / Workday without public ATS API",
    'Postman': "Custom Portal / Workday without public ATS API",
    'Procter & Gamble': "Custom Portal / Workday without public ATS API",
    'Progressive': "Custom Portal / Workday without public ATS API",
    'Prudential': "Custom Portal / Workday without public ATS API",
    'Publicis Sapient': "Custom Portal / Workday without public ATS API",
    'PwC': "Custom Portal / Workday without public ATS API",
    'Qualcomm': "Custom Portal / Workday without public ATS API",
    'Raytheon Technologies': "Custom Portal / Workday without public ATS API",
    'Redfin': "Custom Portal / Workday without public ATS API",
    'Regeneron': "Custom Portal / Workday without public ATS API",
    'Relativity Space': "Custom Portal / Workday without public ATS API",
    'Replicate': "Custom Portal / Workday without public ATS API",
    'Retool': "Custom Portal / Workday without public ATS API",
    'Rippling': "Custom Portal / Workday without public ATS API",
    'Rivian': "Custom Portal / Workday without public ATS API",
    'Roche': "Custom Portal / Workday without public ATS API",
    'Rockwell Automation': "Custom Portal / Workday without public ATS API",
    'RunwayML': "Custom Portal / Workday without public ATS API",
    'Salesforce': "Custom Portal / Workday without public ATS API",
    'SAP': "Custom Portal / Workday without public ATS API",
    'Schlumberger': "Custom Portal / Workday without public ATS API",
    'SentinelOne': "Custom Portal / Workday without public ATS API",
    'ServiceNow': "Custom Portal / Workday without public ATS API",
    'Shopify': "Custom Portal / Workday without public ATS API",
    'Slalom': "Custom Portal / Workday without public ATS API",
    'SmartRecruiters': "Custom Portal / Workday without public ATS API",
    'Snap': "Custom Portal / Workday without public ATS API",
    'Snyk': "Custom Portal / Workday without public ATS API",
    'Sony': "Custom Portal / Workday without public ATS API",
    'Southern Company': "Custom Portal / Workday without public ATS API",
    'Spire Global': "Custom Portal / Workday without public ATS API",
    'Splunk': "Custom Portal / Workday without public ATS API",
    'Starbucks': "Custom Portal / Workday without public ATS API",
    'State Farm': "Custom Portal / Workday without public ATS API",
    'State Street': "Custom Portal / Workday without public ATS API",
    'Stryker': "Custom Portal / Workday without public ATS API",
    'Sunrun': "Custom Portal / Workday without public ATS API",
    'Susquehanna': "Custom Portal / Workday without public ATS API",
    'Symbotic': "Custom Portal / Workday without public ATS API",
    'Synopsys': "Custom Portal / Workday without public ATS API",
    'T-Mobile': "Custom Portal / Workday without public ATS API",
    'Take-Two Interactive': "Custom Portal / Workday without public ATS API",
    'Target': "Custom Portal / Workday without public ATS API",
    'Tech Mahindra': "Custom Portal / Workday without public ATS API",
    'Teladoc Health': "Custom Portal / Workday without public ATS API",
    'Tempus AI': "Custom Portal / Workday without public ATS API",
    'Tesla': "Custom Portal / Workday without public ATS API",
    'Texas Instruments': "Custom Portal / Workday without public ATS API",
    'Thermo Fisher Scientific': "Custom Portal / Workday without public ATS API",
    'Tipalti': "Custom Portal / Workday without public ATS API",
    'Travelers': "Custom Portal / Workday without public ATS API",
    'Truist': "Custom Portal / Workday without public ATS API",
    'Two Sigma': "Custom Portal / Workday without public ATS API",
    'Tyson Foods': "Custom Portal / Workday without public ATS API",
    'Uber': "Custom Portal / Workday without public ATS API",
    'Union Pacific': "Custom Portal / Workday without public ATS API",
    'UnitedHealth Group': "Custom Portal / Workday without public ATS API",
    'Unity': "Custom Portal / Workday without public ATS API",
    'UPS': "Custom Portal / Workday without public ATS API",
    'US Bank': "Custom Portal / Workday without public ATS API",
    'Veeva Systems': "Custom Portal / Workday without public ATS API",
    'Verizon': "Custom Portal / Workday without public ATS API",
    'Vertex Pharmaceuticals': "Custom Portal / Workday without public ATS API",
    'Virtu Financial': "Custom Portal / Workday without public ATS API",
    'Visa': "Custom Portal / Workday without public ATS API",
    'VMware': "Custom Portal / Workday without public ATS API",
    'Walmart': "Custom Portal / Workday without public ATS API",
    'Walt Disney': "Custom Portal / Workday without public ATS API",
    'Warner Bros Discovery': "Custom Portal / Workday without public ATS API",
    'Wayfair': "Custom Portal / Workday without public ATS API",
    'Wells Fargo': "Custom Portal / Workday without public ATS API",
    'Wipro': "Custom Portal / Workday without public ATS API",
    'Workday': "Custom Portal / Workday without public ATS API",
    'XPO Logistics': "Custom Portal / Workday without public ATS API",
    'Zendesk': "Custom Portal / Workday without public ATS API",
    'Zillow': "Custom Portal / Workday without public ATS API",
    'Zoom': "Custom Portal / Workday without public ATS API",
}

TOTAL_WATCHLIST_COUNT: int = 442
SEED_REAL_BOARDS: Tuple[Tuple[str, str, str], ...] = tuple(
    b for b in REAL_COMPANY_BOARDS
    if b[2] not in ('motional', 'samsungsemiconductor', 'peloton', 'qualtrics', 'godaddy', 'lpl', 'socialfinance', 'oath', 'infinite', 'westernunion')
)
SEED_UNSUPPORTED: Dict[str, str] = dict(UNSUPPORTED_COMPANIES)


class ClosingConnection:
    """Wrapper around sqlite3.Connection that ensures connection is closed when exiting context."""
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def __enter__(self):
        self._conn.__enter__()
        return self._conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._conn.close()


class CompanyRegistry:
    """
    Authoritative Company Registry:
    Tracks companies across Greenhouse, Lever, Ashby, and unsupported portals.
    Persisted in SQLite table 'company_registry'.
    Existing 442 seed it; non-G/L/A classifies unsupported once.
    """
    def __init__(self, db_path: str = REGISTRY_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> ClosingConnection:
        return ClosingConnection(sqlite3.connect(self.db_path, timeout=30.0))

    def _init_db(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS company_registry (
                    canonical_domain TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    ats_type TEXT NOT NULL,
                    reachability TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_scanned TEXT,
                    source TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_reg_ats ON company_registry (ats_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_reg_reach ON company_registry (reachability)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_reg_scanned ON company_registry (last_scanned)")

            # Seed if empty or under 442
            cur.execute("SELECT count(*) FROM company_registry")
            count = cur.fetchone()[0]
            if count < 442:
                seed_ts = "2026-09-01T00:00:00+00:00"
                for c_name, ats, slug in SEED_REAL_BOARDS:
                    domain = slug.strip().lower()
                    cur.execute("""
                        INSERT OR IGNORE INTO company_registry
                        (canonical_domain, name, ats_type, reachability, first_seen, last_scanned, source)
                        VALUES (?, ?, ?, 'reachable', ?, NULL, 'seed_442')
                    """, (domain, c_name, ats.lower(), seed_ts))

                for c_name, _ in SEED_UNSUPPORTED.items():
                    norm = re.sub(r'[^a-z0-9]', '', c_name.lower())
                    domain = f"unsupported:{norm}"
                    cur.execute("""
                        INSERT OR IGNORE INTO company_registry
                        (canonical_domain, name, ats_type, reachability, first_seen, last_scanned, source)
                        VALUES (?, ?, 'unsupported', 'unsupported', ?, NULL, 'seed_442')
                    """, (domain, c_name, seed_ts))
                conn.commit()

    def get_company(self, canonical_domain: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM company_registry WHERE canonical_domain = ?", (canonical_domain.strip().lower(),))
            row = cur.fetchone()
            return dict(row) if row else None

    def find_company_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        q = name.strip().lower()
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM company_registry WHERE lower(name) = ?", (q,))
            row = cur.fetchone()
            if row:
                return dict(row)
            # Try slug / canonical domain exact match
            cur.execute("SELECT * FROM company_registry WHERE canonical_domain = ?", (q,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def normalize_company_name(name: str) -> str:
        """Normalize company name: lowercase, strip punctuation and corporate entity suffixes."""
        clean = re.sub(r'\b(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited|co\.?|technologies|technology)\b', '', name, flags=re.IGNORECASE)
        return re.sub(r'[^a-z0-9]', '', clean.lower())

    @staticmethod
    def normalize_slug(slug: str) -> str:
        """Normalize ATS slug: lowercase, strip non-alphanumeric except hyphen."""
        return re.sub(r'[^a-z0-9-]', '', slug.strip().lower())

    def find_existing_company(self, name: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Dedupe check: (normalized name + ATS slug).
        Finds existing company across reachable, unsupported, and unprobed records.
        """
        if slug:
            norm_slug = self.normalize_slug(slug)
            rec = self.get_company(norm_slug)
            if rec:
                return rec
            rec = self.get_company(f"unsupported:{norm_slug}")
            if rec:
                return rec
            rec = self.get_company(f"unprobed:{norm_slug}")
            if rec:
                return rec

        norm_name = self.normalize_company_name(name)
        if not norm_name:
            return None

        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM company_registry")
            for row in cur.fetchall():
                row_norm_name = self.normalize_company_name(row["name"])
                if row_norm_name and row_norm_name == norm_name:
                    return dict(row)
                if slug:
                    dom = row["canonical_domain"]
                    if dom in (norm_slug, f"unsupported:{norm_slug}", f"unprobed:{norm_slug}"):
                        return dict(row)
        return None

    def should_probe_unsupported(self, company_record: Dict[str, Any], cooldown_days: int = 30) -> bool:
        """
        Enforces: never re-probe 'unsupported' more than once per 30 days.
        Returns True if eligible for probe, False if within 30-day cooldown.
        """
        if company_record.get("reachability") != "unsupported":
            return True
        last_scanned = company_record.get("last_scanned")
        if not last_scanned:
            return True
        try:
            ts = datetime.fromisoformat(last_scanned)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - ts
            return elapsed.days >= cooldown_days
        except Exception:
            return True

    def compute_company_yield(self, company_name: str, canonical_domain: Optional[str] = None) -> Dict[str, Any]:
        """
        Computes expected yield = posting volume x recency x profile-fit history.
        High-yield companies scan first; long tail fills idle cycles.
        Logs scoring formula per company.
        """
        now = datetime.now(timezone.utc)
        total_seen = 0
        fit_seen = 0
        latest_ts: Optional[str] = None

        # 1. Query Desktop/jobs_seen.db
        jobs_db = os.path.expanduser("~/Desktop/jobs_seen.db")
        if os.path.exists(jobs_db):
            try:
                with ClosingConnection(sqlite3.connect(jobs_db)) as jconn:
                    jcur = jconn.cursor()
                    q = company_name.strip().lower()
                    dom = (canonical_domain or "").strip().lower()
                    jcur.execute("""
                        SELECT count(*),
                               sum(case when outcome in ('actioned', 'applied', 'alerted', 'submitted') then 1 else 0 end),
                               max(first_seen_at)
                        FROM seen_jobs
                        WHERE lower(company) = ? OR lower(company) = ?
                    """, (q, dom))
                    r = jcur.fetchone()
                    if r and r[0]:
                        total_seen += r[0]
                        fit_seen += (r[1] or 0)
                        latest_ts = r[2]
            except Exception as e:
                logger.debug(f"Error querying jobs_seen.db for {company_name}: {e}")

        # 2. Query company_submissions in autoapply.db
        try:
            with self._get_conn() as aconn:
                acur = aconn.cursor()
                acur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='company_submissions'")
                if acur.fetchone():
                    acur.execute("""
                        SELECT count(*), max(applied_at)
                        FROM company_submissions
                        WHERE lower(company_name) = ? AND status = 'submitted'
                    """, (company_name.strip().lower(),))
                    sub = acur.fetchone()
                    if sub and sub[0]:
                        fit_seen += sub[0]
                        if not latest_ts and sub[1]:
                            latest_ts = sub[1]
        except Exception:
            pass

        # Posting Volume V: bounded [1.0, 500.0] with 5.0 baseline for unobserved
        V = float(min(500.0, max(1.0, float(total_seen)))) if total_seen > 0 else 5.0

        # Recency R: decay based on days elapsed since latest posting/scan
        if latest_ts:
            try:
                ts_dt = datetime.fromisoformat(latest_ts.replace(" ", "T"))
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                delta_days = max(0.0, (now - ts_dt).total_seconds() / 86400.0)
                R = 1.0 / (1.0 + delta_days / 7.0)
            except Exception:
                R = 1.0
        else:
            # Fresh / unprobed gets full recency (1.0) so new companies are prioritized
            R = 1.0

        # Profile-fit history F: Bayesian Laplace smoothing with 10% uninformative prior
        if total_seen > 0:
            F = (float(fit_seen) + 1.0) / (float(total_seen) + 10.0)
        else:
            F = 0.10

        score = round(V * R * F, 4)
        logger.info(
            f"YieldScore({company_name}): {score:.3f} [V={V:.1f}, R={R:.3f}, F={F:.3f}] "
            f"(formula: volume * recency * fit_history | total={total_seen}, fit={fit_seen})"
        )

        return {
            "score": score,
            "volume": V,
            "recency": round(R, 4),
            "fit_history": round(F, 4),
            "total_seen": total_seen,
            "fit_seen": fit_seen,
            "formula": "volume * recency * profile_fit_history"
        }

    def classify_and_store(
        self,
        name: str,
        canonical_domain: str,
        ats_type: str,
        reachability: str = "reachable",
        source: str = "discovery"
    ) -> Dict[str, Any]:
        """
        Store classified company in registry.
        non-G/L/A classifies unsupported once.
        Supports 'unprobed', 'reachable', 'unsupported'.
        """
        ats_clean = ats_type.strip().lower()
        reach_clean = reachability.strip().lower()
        dom_clean = canonical_domain.strip().lower()
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            cur = conn.cursor()
            # If promoting from unprobed, remove any provisional unprobed record
            if reach_clean in ("reachable", "unsupported") and not dom_clean.startswith("unprobed:"):
                raw_slug = dom_clean.replace("unsupported:", "").replace("unprobed:", "")
                cur.execute("DELETE FROM company_registry WHERE canonical_domain = ?", (f"unprobed:{raw_slug}",))

            cur.execute("""
                INSERT INTO company_registry (canonical_domain, name, ats_type, reachability, first_seen, last_scanned, source)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(canonical_domain) DO UPDATE SET
                    name = excluded.name,
                    ats_type = excluded.ats_type,
                    reachability = excluded.reachability,
                    source = CASE WHEN company_registry.source = 'seed_442' THEN company_registry.source ELSE excluded.source END
            """, (dom_clean, name.strip(), ats_clean, reach_clean, now_iso, source))
            conn.commit()

        # Keep in-memory structures in sync
        if reach_clean == "reachable" and ats_clean in ("greenhouse", "lever", "ashby"):
            existing = [slug for _, _, slug in REAL_COMPANY_BOARDS]
            if dom_clean not in existing:
                REAL_COMPANY_BOARDS.append((name.strip(), ats_clean, dom_clean))
        elif reach_clean == "unsupported":
            if name.strip() not in UNSUPPORTED_COMPANIES:
                UNSUPPORTED_COMPANIES[name.strip()] = "Classified unsupported portal"

        return {
            "canonical_domain": dom_clean,
            "name": name.strip(),
            "ats_type": ats_clean,
            "reachability": reach_clean,
            "source": source
        }

    def get_stats(self) -> Dict[str, int]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM company_registry")
            total = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'reachable' AND ats_type = 'greenhouse'")
            g = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'reachable' AND ats_type = 'lever'")
            l = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'reachable' AND ats_type = 'ashby'")
            a = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'reachable'")
            reachable = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'unsupported'")
            unsupported = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_registry WHERE reachability = 'unprobed'")
            unprobed = cur.fetchone()[0]

            return {
                "total": total,
                "greenhouse": g,
                "lever": l,
                "ashby": a,
                "reachable": reachable,
                "unsupported": unsupported,
                "unprobed": unprobed
            }

    def get_enumeration_queue(self, lookback_days: int = 7, limit: Optional[int] = None, order_by_yield: bool = False) -> List[Dict[str, Any]]:
        """
        Enumeration: incremental, new companies first, then least-recently-scanned (default 7d).
        If order_by_yield is True, ranks queue by expected yield (volume x recency x fit_history).
        """
        cut_off = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        query = """
            SELECT * FROM company_registry
            WHERE reachability = 'reachable' AND ats_type IN ('greenhouse', 'lever', 'ashby')
              AND (last_scanned IS NULL OR last_scanned < ?)
            ORDER BY CASE WHEN last_scanned IS NULL THEN 0 ELSE 1 END, last_scanned ASC
        """
        params: List[Any] = [cut_off]
        if limit and not order_by_yield:
            query += " LIMIT ?"
            params.append(limit)

        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            items = [dict(r) for r in cur.fetchall()]

        if order_by_yield:
            for item in items:
                y = self.compute_company_yield(item.get("name", ""), item.get("canonical_domain", ""))
                item["yield_score"] = y["score"]
                item["yield_details"] = y
            # Sort descending by yield_score, with secondary sort on name
            items.sort(key=lambda x: (x.get("yield_score", 0.0), x.get("name", "")), reverse=True)
            if limit:
                items = items[:limit]

        return items

    def get_yield_ranked_queue(self, lookback_days: int = 7, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rank the scan queue by expected yield = posting volume x recency x profile-fit history."""
        return self.get_enumeration_queue(lookback_days=lookback_days, limit=limit, order_by_yield=True)

    def get_unprobed_queue(self, limit: Optional[int] = None, order_by_yield: bool = True) -> List[Dict[str, Any]]:
        """
        Retrieves unprobed candidates from company_registry.
        Drain order: yield-prioritized throughout (volume x recency x fit_history, high-priority sources first).
        """
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM company_registry 
                WHERE reachability = 'unprobed'
                ORDER BY 
                    CASE 
                        WHEN source LIKE '%yc_top%' THEN 0
                        WHEN source LIKE '%ats_customer%' THEN 1
                        WHEN source LIKE '%github%' THEN 2
                        WHEN source LIKE '%search%' THEN 3
                        WHEN source LIKE '%yc_directory%' THEN 4
                        ELSE 5 
                    END,
                    first_seen ASC
            """)
            items = [dict(r) for r in cur.fetchall()]

        if order_by_yield:
            def _source_priority(src: str) -> int:
                s = (src or "").lower()
                if "yc_top" in s:
                    return 0
                if "ats_customer" in s:
                    return 1
                if "github" in s:
                    return 2
                if "search" in s:
                    return 3
                if "yc_directory" in s:
                    return 4
                return 5

            for item in items:
                y = self.compute_company_yield(item.get("name", ""), item.get("canonical_domain", ""))
                item["yield_score"] = y.get("score", 0.0)
            items.sort(key=lambda x: (x.get("yield_score", 0.0), -_source_priority(x.get("source", ""))), reverse=True)

        if limit:
            items = items[:limit]
        return items

    def update_last_scanned(self, identifier: str, timestamp: Optional[str] = None):
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        ident_clean = (identifier or "").strip().lower()
        ident_compact = re.sub(r'[^a-z0-9]', '', ident_clean)
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE company_registry 
                SET last_scanned = ? 
                WHERE canonical_domain = ? 
                   OR canonical_domain = ?
                   OR LOWER(name) = ?
            """, (ts, ident_clean, ident_compact, ident_clean))
            conn.commit()

    def get_today_counts(self) -> Tuple[int, int, int]:
        """
        Returns (queue_depth, roles_scored_today, launches_24h)
        launches_24h is sourced from launch_tracker (same as the outcome ledger)
        to eliminate counter/ledger discrepancy.
        """
        queue_depth = len(self.get_enumeration_queue(lookback_days=7))
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        roles_scored = 0
        launches_24h = 0

        # Scored: count roles first-seen or last-evaluated today
        try:
            jobs_db = os.path.expanduser("~/Desktop/jobs_seen.db")
            if os.path.exists(jobs_db):
                with ClosingConnection(sqlite3.connect(jobs_db)) as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_jobs'")
                    if cur2.fetchone():
                        cur2.execute("SELECT count(*) FROM seen_jobs WHERE first_seen_at LIKE ? OR last_evaluated_at LIKE ?", (f"{today_prefix}%", f"{today_prefix}%"))
                        roles_scored = cur2.fetchone()[0]
        except Exception:
            pass

        # Launched: count from launch_tracker (same 24h rolling window as outcome ledger)
        try:
            from app.services.launch_tracker import launch_tracker as lt
            launches_24h = len(lt.get_last_24h_launches())
        except Exception:
            pass

        return queue_depth, roles_scored, launches_24h


company_registry = CompanyRegistry()


def get_coverage_summary() -> Tuple[int, int, int]:
    """Return (searchable_count, unsupported_count, total_count)."""
    stats = company_registry.get_stats()
    return stats["reachable"], stats["unsupported"], stats["total"]


def format_registry_digest_line(queue_depth: Optional[int] = None, roles_scored_today: Optional[int] = None, apps_today: Optional[int] = None) -> str:
    """
    Verbatim digest line with:
    registry size, G/L/A split, reachable/unsupported, queue depth, roles scored today, applications today.
    """
    stats = company_registry.get_stats()
    q_depth, scored, applied = company_registry.get_today_counts()
    if queue_depth is not None:
        q_depth = queue_depth
    if roles_scored_today is not None:
        scored = roles_scored_today
    if apps_today is not None:
        applied = apps_today

    unprobed_str = f", {stats['unprobed']} unprobed" if stats.get("unprobed", 0) > 0 else ""
    return (
        f"🌐 *Registry:* {stats['total']} companies "
        f"({stats['greenhouse']} Greenhouse, {stats['lever']} Lever, {stats['ashby']} Ashby | "
        f"{stats['reachable']} reachable, {stats['unsupported']} unsupported{unprobed_str}) | "
        f"Queue: {q_depth} | Scored: {scored} | Launched: {applied}"
    )


def format_coverage_line() -> str:
    """Return digest line (backward-compatible alias)."""
    return format_registry_digest_line()


def is_searchable(company_name: str) -> bool:
    """Check if company is supported in the direct ATS registry."""
    if not company_name:
        return False
    rec = company_registry.find_company_by_name(company_name)
    if rec and rec.get("reachability") == "reachable":
        return True
    q = company_name.strip().lower()
    return any(c.lower() == q for c, _, _ in REAL_COMPANY_BOARDS)


def get_board_for_company(company_name: str) -> Optional[Tuple[str, str, str]]:
    """Look up ATS type and handle for a given company name."""
    if not company_name:
        return None
    rec = company_registry.find_company_by_name(company_name)
    if rec and rec.get("reachability") == "reachable" and rec.get("ats_type") in ("greenhouse", "lever", "ashby"):
        return (rec["name"], rec["ats_type"], rec["canonical_domain"])
    q = company_name.strip().lower()
    for c, ats, handle in REAL_COMPANY_BOARDS:
        if c.lower() == q:
            return (c, ats, handle)
    # Substring match fallback with length guard
    for c, ats, handle in REAL_COMPANY_BOARDS:
        c_low = c.lower()
        if len(c_low) >= 4 and c_low in q:
            return (c, ats, handle)
        if len(q) >= 4 and q in c_low:
            return (c, ats, handle)
    return None


def get_unsupported_reason(company_name: str) -> Optional[str]:
    """Return reason why company is unsupported, if applicable."""
    if not company_name:
        return None
    rec = company_registry.find_company_by_name(company_name)
    if rec and rec.get("reachability") == "unsupported":
        return f"Unsupported portal ({rec.get('source', 'classified')})"
    q = company_name.strip().lower()
    for name, reason in UNSUPPORTED_COMPANIES.items():
        if name.lower() == q or q in name.lower():
            return reason
    return None
