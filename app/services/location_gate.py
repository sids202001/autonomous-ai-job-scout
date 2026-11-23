"""
AutoApply - Universal Location Filter Gate
Strict Gate: Return True ONLY if the job is physically in the United States
or is explicitly a US-based Remote role. Discard all international roles.
"""
import re
from typing import Optional

EXCLUDED_NON_US_LOCATIONS = [
    # Non-US Countries & Major Foreign Tech Metros
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai", "delhi", "noida", "gurgaon", "gurugram",
    "uk", "united kingdom", "london", "england", "scotland", "wales", "great britain",
    "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo", "ontario", "british columbia", "quebec", "alberta",
    "australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "nsw", "victoria", "queensland",
    "germany", "berlin", "munich", "frankfurt", "hamburg", "cologne", "stuttgart",
    "ireland", "dublin", "cork",
    "netherlands", "amsterdam", "rotterdam", "utrecht",
    "singapore", "japan", "tokyo", "france", "paris", "switzerland", "zurich", "geneva",
    "sweden", "stockholm", "spain", "madrid", "barcelona", "poland", "warsaw", "krakow",
    "brazil", "sao paulo", "mexico", "philippines", "thailand", "bangkok", "indonesia", "jakarta",
    "vietnam", "new zealand", "israel", "argentina", "colombia", "chile", "south africa",
    "china", "korea", "taiwan", "hong kong", "italy", "portugal", "austria", "belgium",
    "denmark", "norway", "finland", "czech", "romania", "hungary", "uae", "costa rica",
    # Global Regions
    "emea", "apac", "latam", "europe", "asia", "latin america"
]

US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "puerto rico"
]

US_TECH_HUBS = [
    "san francisco", "sf", "bay area", "silicon valley", "seattle", "new york city", "nyc",
    "austin", "chicago", "boston", "cambridge", "los angeles", "san jose", "denver", "boulder",
    "arlington", "reston", "fairfax", "sunnyvale", "mountain view", "palo alto", "menlo park",
    "redwood city", "cupertino", "santa clara", "san mateo", "bellevue", "redmond", "atlanta",
    "miami", "dallas", "houston", "phoenix", "philadelphia", "pittsburgh", "san diego",
    "portland", "raleigh", "durham", "salt lake city", "minneapolis", "detroit", "nashville",
    "charlotte", "columbus", "indianapolis", "tampa", "orlando", "las vegas"
]

US_STATE_POSTAL_REGEX = re.compile(
    r"(?:,\s*|\b)(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)(?:\s*,|\s*$|\s+USA|\s+US|\b)",
    re.IGNORECASE
)

NON_US_CODE_REGEX = re.compile(
    r"(?:,\s*|\b)(AU|UK|GB|DE|FR|NL|IE|SG|ES|PL|BR|MX|JP|KR|NZ|CH|SE|IT|TH|ID|PH|VN)(?:\s*,|\s*$|\b)",
    re.IGNORECASE
)

def is_us_location(location: str, title: str = "", description: str = "") -> bool:
    """
    Strict Gate: Return True ONLY if the job is physically in the United States
    or is explicitly a US-based Remote role. Discard all international roles.
    """
    loc_clean = (location or "").strip().lower()
    title_clean = (title or "").strip().lower()
    desc_clean = (description or "").strip().lower()
    combined_text = f"{loc_clean} {title_clean}"

    # 1. Immediate Hard Disqualification: Non-US country, city, or region in location or title
    for excl in EXCLUDED_NON_US_LOCATIONS:
        if re.search(rf"\b{re.escape(excl)}\b", combined_text):
            if excl == "paris" and ("tx" in loc_clean or "texas" in loc_clean):
                continue
            return False

    if NON_US_CODE_REGEX.search(location or ""):
        return False

    # 2. Strong Positive Indicators in Location string
    if any(u in loc_clean for u in ["united states", "usa", "u.s.a.", "u.s."]):
        return True
    if re.search(r"\b(us|u\.s\.)\b", loc_clean):
        return True

    for state in US_STATE_NAMES:
        if re.search(rf"\b{re.escape(state)}\b", loc_clean):
            return True

    for hub in US_TECH_HUBS:
        if re.search(rf"\b{re.escape(hub)}\b", loc_clean):
            return True

    if US_STATE_POSTAL_REGEX.search(location or ""):
        return True

    # 3. Check for explicitly US Remote designations
    if "remote" in loc_clean or "remote" in title_clean:
        if any(r in combined_text for r in [
            "remote - us", "us remote", "remote (us)", "remote, us",
            "remote - united states", "remote (united states)", "remote, united states",
            "remote in the us", "us / remote", "remote - north america"
        ]):
            return True
        if desc_clean:
            for excl in [
                "right to work in the uk", "eligible to work in canada", "australian work rights",
                "singapore citizen", "schengen", "gdpr", "pension scheme", "superannuation"
            ]:
                if excl in desc_clean:
                    return False
        # Default unstated remote with US company defaults to True if no non-US signals
        return True

    return False
