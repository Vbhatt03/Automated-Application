# ...existing code...
import os
import re
import requests
import pandas as pd
from dotenv import load_dotenv

# ---------------------
# Load API keys
# ---------------------
load_dotenv()
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")
SNOV_API_USER = os.getenv("SNOV_API_USER")
SNOV_API_SECRET = os.getenv("SNOV_API_SECRET")
CLEARBIT_API_KEY = os.getenv("CLEARBIT_API_KEY")

EXCEL_FILE = "jobs_with_status.xlsx"
OUTPUT_FILE = "jobs_with_contacts.xlsx"

# ---------------------
# Email validation
# ---------------------
def is_valid_email(email: str) -> bool:
    if not email:
        return False
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    return re.match(pattern, email) is not None

# ---------------------
# Hunter.io API
# ---------------------
def hunter_domain_search(company: str, domain_hint=None):
    """
    Fetch recruiter/company emails using Hunter.io API.
    """
    if not HUNTER_API_KEY:
        return []
    base_url = "https://api.hunter.io/v2/domain-search"
    params = {
        "company": company,
        "api_key": HUNTER_API_KEY,
        "limit": 5
    }
    if domain_hint:
        params["domain"] = domain_hint
    try:
        r = requests.get(base_url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            emails = []
            for e in data.get("data", {}).get("emails", []):
                mail = e.get("value")
                if is_valid_email(mail):
                    emails.append(mail)
            return emails
    except Exception:
        pass
    return []

# ---------------------
# Snov.io API
# ---------------------
def snov_domain_search(company: str, domain_hint=None):
    """
    Fetch emails using Snov.io API.
    """
    if not SNOV_API_USER or not SNOV_API_SECRET:
        return []
    # Get access token
    try:
        token_resp = requests.post(
            "https://api.snov.io/v1/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": SNOV_API_USER,
                "client_secret": SNOV_API_SECRET
            },
            timeout=10
        )
        if token_resp.status_code != 200:
            return []
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return []
        # Use domain search
        domain = domain_hint or ""
        if not domain:
            # Try to get domain from company name using Clearbit
            domain = clearbit_domain_lookup(company)
        if not domain:
            return []
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"domain": domain, "type": "all", "limit": 5}
        r = requests.get("https://api.snov.io/v2/domain-emails-with-info", headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            emails = []
            for e in data.get("emails", []):
                mail = e.get("email")
                if is_valid_email(mail):
                    emails.append(mail)
            return emails
    except Exception:
        pass
    return []

# ---------------------
# Clearbit API (domain lookup only)
# ---------------------
def clearbit_domain_lookup(company: str):
    """
    Use Clearbit to get the domain for a company.
    """
    if not CLEARBIT_API_KEY:
        return None
    try:
        headers = {"Authorization": f"Bearer {CLEARBIT_API_KEY}"}
        params = {"name": company}
        r = requests.get("https://company.clearbit.com/v2/companies/find", headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get("domain")
    except Exception:
        pass
    return None

# ---------------------
# Main pipeline
# ---------------------
def main():
    df = pd.read_excel(EXCEL_FILE)
    recruiter_contacts = []

    for _, row in df.iterrows():
        status = str(row.get("status", "")).lower()
        company = str(row.get("company", ""))
        domain_hint = None

        emails = []
        if status == "applied":
            # Try Hunter.io first
            emails = hunter_domain_search(company)
            # If Hunter fails, try Snov.io
            if not emails:
                # Try to get domain from Clearbit if possible
                domain_hint = clearbit_domain_lookup(company)
                emails = snov_domain_search(company, domain_hint)
            # If still no emails, try Clearbit for domain (for reference)
            if not emails and not domain_hint:
                domain_hint = clearbit_domain_lookup(company)
            email_str = ", ".join(emails) if emails else (domain_hint if domain_hint else "N/A")
        else:
            email_str = ""

        recruiter_contacts.append(email_str)

    df["Recruiter Contacts"] = recruiter_contacts
    df.to_excel(OUTPUT_FILE, index=False)
    print(f"✅ Saved file with recruiter contacts → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()