"""
contact_discovery_and_outreach.py
Step 3: Contact Discovery (Hunter.io API)
Step 5: Outreach Preparation (Cold Email + LinkedIn Messages)
"""

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
        print("⚠️ No Hunter.io API key found in .env")
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
    except Exception as e:
        print(f"⚠️ Hunter.io lookup failed for {company}: {e}")
    return []


# ---------------------
# Classify company type
# ---------------------
def classify_company(job):
    """
    Heuristic: classify as startup, bigtech, or midlevel.
    """
    name = str(job['company']).lower()
    source = str(job.get('source', '')).lower()

    bigtech_keywords = ["google", "microsoft", "amazon", "meta", "apple",
                        "netflix", "nvidia", "intel", "oracle", "adobe"]
    startup_sources = ["yc", "wellfound", "startup", "remoteok", "weworkremotely", "remotive"]

    if any(b in name for b in bigtech_keywords):
        return "bigtech"
    elif any(s in source for s in startup_sources):
        return "startup"
    else:
        return "midlevel"


# ---------------------
# Cold outreach templates
# ---------------------
def generate_cold_email(job, email):
    ctype = classify_company(job)

    if ctype == "startup":
        return f"""Subject: Excited about {job['role']} at {job['company']}

Hi {job['company']} Team,

I came across the {job['role']} role at {job['company']} and it immediately resonated with me. 
As someone deeply involved in Machine Learning, Edge AI and Robotics projects (including leading a 50+ member rover team), 
I thrive in fast-paced environments where ideas quickly turn into real products.

I’d love to contribute my skills to help {job['company']} build and scale faster. 
Let me know if we could connect for a quick chat.

Best,  
Vyomesh  
Email: {email if email else "your_email_here"}  
LinkedIn: linkedin.com/in/yourprofile
"""

    elif ctype == "bigtech":
        return f"""Subject: Application Follow-Up – {job['role']} at {job['company']}

Dear {job['company']} Recruitment Team,

I recently applied for the {job['role']} role and wanted to follow up directly. 
With my background in Computer Vision, Embedded AI, and ML research, I’m eager to contribute to impactful large-scale systems 
that align with {job['company']}'s mission.

Please let me know if additional details would be helpful. I’d be delighted to discuss how my experience can add value.

Sincerely,  
Vyomesh  
Email: {email if email else "your_email_here"}  
LinkedIn: linkedin.com/in/yourprofile
"""

    else:  # midlevel / generic
        return f"""Subject: Interest in {job['role']} at {job['company']}

Hi {job['company']} Hiring Team,

I recently submitted an application for the {job['role']} role at {job['company']}. 
My experience spans ML model deployment, robotics software, and embedded systems, which I believe aligns with this position.

I’d be happy to share more details about my background and projects if helpful. Looking forward to your response.

Best regards,  
Vyomesh  
Email: {email if email else "your_email_here"}  
LinkedIn: linkedin.com/in/yourprofile
"""


def generate_linkedin_message(job):
    ctype = classify_company(job)

    if ctype == "startup":
        return f"""Hi! Just applied for the {job['role']} role at {job['company']}. 
Excited about what you’re building – would love to connect and explore how I can contribute."""
    elif ctype == "bigtech":
        return f"""Hello, I recently applied for the {job['role']} role at {job['company']}. 
I’d appreciate connecting to stay updated and learn more about the team."""
    else:
        return f"""Hi, I applied for the {job['role']} role at {job['company']}. 
Would be great to connect and understand more about the opportunity."""


# ---------------------
# Main pipeline
# ---------------------
def main():
    df = pd.read_excel(EXCEL_FILE)

    recruiter_contacts = []
    cold_emails = []
    linkedin_msgs = []

    for _, row in df.iterrows():
        status = str(row.get("status", "")).lower()
        company = str(row.get("company", ""))
        role = str(row.get("role", ""))

        emails = []
        cold_email = ""
        linkedin_msg = ""

        if status == "applied":
            emails = hunter_domain_search(company)
            email_str = ", ".join(emails) if emails else "N/A"

            cold_email = generate_cold_email(row, emails[0] if emails else "")
            linkedin_msg = generate_linkedin_message(row)
        else:
            email_str = ""

        recruiter_contacts.append(email_str)
        cold_emails.append(cold_email)
        linkedin_msgs.append(linkedin_msg)

    df["Recruiter Contacts"] = recruiter_contacts
    df["Cold Email Draft"] = cold_emails
    df["LinkedIn Message"] = linkedin_msgs

    df.to_excel(OUTPUT_FILE, index=False)
    print(f"✅ Saved enriched file with contacts & outreach drafts → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
