"""
job_autoapply_pipeline.py

Full pipeline:
- Scrape free sources (YC, Startup.jobs, Wellfound, Indeed, Naukri, LinkedIn, Glassdoor, BigTech careers,
  RemoteOK, WeWorkRemotely, Remotive)
- Filter by resume keywords (extracted from user's CV)
- Parse/keep salary (N/A if absent)
- Attempt auto-login (cookies re-used) and auto-apply (best-effort) where feasible
- Export jobs_raw.xlsx and jobs_with_status.xlsx (status in {Applied, Pending, Flagged})
"""

import os
import re
import time
import random
import logging
import pickle
from dataclasses import dataclass, asdict
from typing import List, Tuple, Optional, Dict, Set
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
import pandas as pd
from dotenv import load_dotenv
from tqdm import tqdm

# Selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

# -------------- CONFIG / CREDENTIALS --------------
load_dotenv()

# Path to your resume PDF (default to the file you uploaded)
RESUME_PATH = os.getenv("RESUME_PATH", "CV-vyomesh_ML(22-6-25).pdf")
PHONE = os.getenv("PHONE", "+91 7043833651")
NAME = os.getenv("FULL_NAME", "Vyomesh Bhatt")

# Credentials for sites (set in .env if you want auto-login/apply)
CREDENTIALS = {
    "linkedin": (os.getenv("LINKEDIN_EMAIL"), os.getenv("LINKEDIN_PASSWORD")),
    "naukri": (os.getenv("NAUKRI_EMAIL"), os.getenv("NAUKRI_PASSWORD")),
    "wellfound": (os.getenv("WELLFOUND_EMAIL"), os.getenv("WELLFOUND_PASSWORD")),
    "indeed": (os.getenv("INDEED_EMAIL"), os.getenv("INDEED_PASSWORD"))
    # add more as needed
}

HEADLESS = os.getenv("HEADLESS", "True").lower() in ("1", "true", "yes")

# Keywords from your resume (from the CVs you provided)
RESUME_KEYWORDS = [
    "machine learning", "deep learning", "computer vision", "opencv", "image processing",
    "embedded", "tinyml", "edge ai", "esp32", "esp32-s3", "esp-idf", "ros", "robotics",
    "autonomous", "yolov5", "open3d", "lidar", "python", "c++", "tensorflow", "keras",
    "scikit-learn", "edge impulse", "tinyml", "embedded systems", "microcontroller", "esp32"
]

# Salary cutoffs (monthly). India = ₹70,000/month
SALARY_CUTOFFS = {
    "india": 70000,
    "united states": 4000,
    "usa": 4000,
    "us": 4000,
    "germany": 3000,
    "uk": 2500,
    "default": 0
}

# User agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
]

HEADERS = {"User-Agent": random.choice(USER_AGENTS)}

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------- DATA MODEL --------------
@dataclass
class JobPost:
    company: str
    role: str
    location: str
    link: str
    source: str
    salary: str = "N/A"
    remote: str = "Unknown"
    description_snippet: str = ""
    status: str = "Pending"   # Applied, Pending, Flagged

    def dedupe_key(self) -> Tuple[str, str, str, str]:
        return (self.company.strip().lower(), self.role.strip().lower(), self.location.strip().lower(), self.link.strip())

# -------------- HELPERS --------------
def sleep_jitter(base=0.7, jitter=0.8):
    time.sleep(base + random.random() * jitter)

def normalize_text(s: Optional[str]) -> str:
    return (s or "").strip()

# Simple salary parsing heuristics
CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "Rs": "INR",
    "INR": "INR", "USD": "USD", "EUR": "EUR", "GBP": "GBP"
}

def extract_salary_numbers(text: str):
    if not text: return None
    s = text.replace(",", "").replace("—", "-").replace("–","-")
    currency = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            currency = code
            break
    # find numeric groups and ranges
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", s)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if range_match:
        low = float(range_match.group(1))
    elif nums:
        low = float(nums[0])
    else:
        return None
    # detect unit
    unit = "monthly"
    if re.search(r"per\s*year|per\s*annum|pa|p\.a\.|annually|annual|yearly", s, re.I):
        unit = "yearly"
    if re.search(r"per\s*month|/month|monthly|month", s, re.I):
        unit = "monthly"
    monthly = low
    if unit == "yearly":
        monthly = low / 12.0
    else:
        # heuristics for large numbers
        if currency == "INR" and low > 200000:  # >2L likely annual
            monthly = low / 12.0
        if currency == "USD" and low > 100000:
            monthly = low / 12.0
    return monthly, currency or "UNKNOWN", unit

def meets_cutoff(job: JobPost) -> bool:
    if not job.salary or job.salary.upper() in ("N/A", "NOT PROVIDED", ""):
        return True
    parsed = extract_salary_numbers(job.salary)
    if not parsed:
        return True
    monthly_val, currency, unit = parsed
    # guess cutoff by location text
    loc = (job.location or "").lower()
    cutoff = SALARY_CUTOFFS.get("default", 0)
    for country, val in SALARY_CUTOFFS.items():
        if country == "default":
            continue
        if country in loc:
            cutoff = val
            break
    # fallback from currency
    if cutoff == SALARY_CUTOFFS.get("default", 0):
        if currency == "INR":
            cutoff = SALARY_CUTOFFS.get("india", cutoff)
        elif currency == "USD":
            cutoff = SALARY_CUTOFFS.get("united states", cutoff)
        elif currency == "EUR":
            cutoff = SALARY_CUTOFFS.get("germany", cutoff)
        elif currency == "GBP":
            cutoff = SALARY_CUTOFFS.get("uk", cutoff)
    return monthly_val >= cutoff

def matches_resume(job: JobPost) -> bool:
    text = " ".join([job.role or "", job.company or "", job.description_snippet or ""]).lower()
    for kw in RESUME_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

def dedupe_jobs(jobs: List[JobPost]) -> List[JobPost]:
    seen: Set[Tuple[str,str,str,str]] = set()
    out: List[JobPost] = []
    for j in jobs:
        key = j.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out

def export_jobs(jobs: List[JobPost], fname: str):
    df = pd.DataFrame([asdict(j) for j in jobs])
    order = ["company","role","location","remote","salary","link","source","status","description_snippet"]
    cols = [c for c in order if c in df.columns]
    df = df[cols]
    df.to_excel(fname, index=False)
    logging.info("Exported %d rows to %s", len(df), fname)

# -------------- SCRAPERS (requests-based / free) --------------
def scrape_ycombinator() -> List[JobPost]:
    url = "https://www.ycombinator.com/jobs"
    logging.info("Scraping YCombinator (requests)...")
    jobs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='/jobs/']"):
            try:
                link = a.get("href")
                if link and link.startswith("/"):
                    link = urljoin("https://www.ycombinator.com", link)
                txt = a.get_text(" ", strip=True)
                parts = txt.split(" • ")
                role = parts[0] if parts else txt
                company = parts[1] if len(parts) > 1 else "YC startup"
                jobs.append(JobPost(company=company, role=role, location="N/A", link=link, source="YCombinator", description_snippet=txt))
            except Exception:
                continue
    except Exception as e:
        logging.warning("YC scrape failed: %s", e)
    sleep_jitter(0.2,0.4)
    return jobs

def scrape_startup_jobs() -> List[JobPost]:
    url = "https://startup.jobs"
    logging.info("Scraping Startup.jobs (requests)...")
    jobs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.job-listing, li.job"):
            try:
                a = li.find("a")
                if not a: continue
                role = a.get_text(strip=True)
                link = urljoin(url, a.get("href",""))
                comp = li.select_one(".job-listing-company-name")
                company = comp.get_text(strip=True) if comp else "startup.jobs"
                jobs.append(JobPost(company=company, role=role, location="N/A", link=link, source="startup.jobs", description_snippet=role))
            except Exception:
                continue
    except Exception as e:
        logging.warning("startup.jobs failed: %s", e)
    sleep_jitter()
    return jobs

def scrape_wellfound(query: str="machine learning", location: str="India") -> List[JobPost]:
    logging.info("Scraping Wellfound (AngelList) (requests, best-effort)...")
    jobs = []
    base = "https://wellfound.com"
    url = f"https://wellfound.com/jobs?search%5Bquery%5D={quote_plus(query)}&search%5Blocations%5D={quote_plus(location)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("a[data-test='job-serp__job-card']"):
            try:
                link = card.get("href")
                if link and not link.startswith("http"):
                    link = urljoin(base, link)
                role_el = card.select_one("[data-test='job-serp__job-title']")
                comp_el = card.select_one("[data-test='job-serp__company-name']")
                loc_el = card.select_one("[data-test='job-serp__location']")
                role = role_el.get_text(strip=True) if role_el else card.get_text(strip=True)
                comp = comp_el.get_text(strip=True) if comp_el else "Wellfound"
                loctxt = loc_el.get_text(strip=True) if loc_el else "N/A"
                jobs.append(JobPost(company=comp, role=role, location=loctxt, link=link, source="Wellfound", description_snippet=role))
            except Exception:
                continue
    except Exception as e:
        logging.warning("Wellfound scrape failed: %s", e)
    sleep_jitter()
    return jobs

def scrape_indeed(query: str="machine learning", location: str="India", pages: int=1) -> List[JobPost]:
    logging.info("Scraping Indeed (requests, best-effort)...")
    jobs = []
    base = "https://www.indeed.com"
    for p in range(pages):
        start = p * 10
        url = f"https://www.indeed.com/jobs?q={quote_plus(query)}&l={quote_plus(location)}&start={start}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("a.tapItem, div.jobsearch-SerpJobCard")
            for c in cards:
                try:
                    title_el = c.select_one("h2.jobTitle") or c.select_one("h2.title")
                    role = title_el.get_text(strip=True) if title_el else c.get_text(strip=True)
                    comp = c.select_one("span.companyName") or c.select_one("span.company")
                    company = comp.get_text(strip=True) if comp else "Indeed"
                    loc = c.select_one("div.companyLocation") or c.select_one(".location")
                    loctxt = loc.get_text(strip=True) if loc else location
                    link = c.get("href", "")
                    if link and not link.startswith("http"):
                        link = urljoin(base, link)
                    jobs.append(JobPost(company=company, role=role, location=loctxt, link=link, source="Indeed", description_snippet=role))
                except Exception:
                    continue
        except Exception as e:
            logging.warning("Indeed failed: %s", e)
        sleep_jitter(0.6,1.2)
    return jobs

# Remote-first sites
def scrape_remoteok() -> List[JobPost]:
    url = "https://remoteok.com/remote-dev-jobs"
    logging.info("Scraping RemoteOK (requests)...")
    jobs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("tr.job"):
            try:
                role = row.get("data-search") or row.get("data-position") or row.get("data-title") or row.get_text(" ",strip=True)
                link = row.get("data-url")
                if link and not link.startswith("http"):
                    link = "https://remoteok.com" + link
                company = row.get("data-company") or "RemoteOK"
                jobs.append(JobPost(company=company, role=role, location="Remote", link=link, source="RemoteOK", description_snippet=role))
            except Exception:
                continue
    except Exception as e:
        logging.warning("RemoteOK failed: %s", e)
    sleep_jitter()
    return jobs

def scrape_weworkremotely() -> List[JobPost]:
    url = "https://weworkremotely.com/categories/remote-programming-jobs"
    logging.info("Scraping WeWorkRemotely (requests)...")
    jobs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.feature, li > .job"):
            try:
                a = li.find("a", href=True)
                if not a: continue
                title_sp = a.find("span", class_="title")
                comp_sp = a.find("span", class_="company")
                role = title_sp.get_text(strip=True) if title_sp else a.get_text(strip=True)
                company = comp_sp.get_text(strip=True) if comp_sp else "WeWorkRemotely"
                link = "https://weworkremotely.com" + a["href"]
                jobs.append(JobPost(company=company, role=role, location="Remote", link=link, source="WeWorkRemotely", description_snippet=role))
            except Exception:
                continue
    except Exception as e:
        logging.warning("WeWorkRemotely failed: %s", e)
    sleep_jitter()
    return jobs

def scrape_remotive() -> List[JobPost]:
    url = "https://remotive.com/remote-jobs/software-dev"
    logging.info("Scraping Remotive (requests)...")
    jobs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for div in soup.select("div.job-tile, div.job"):
            try:
                role = (div.select_one(".job-title") or div.select_one(".job-title a") or div).get_text(" ", strip=True)
                company = div.select_one(".company-name") and div.select_one(".company-name").get_text(strip=True) or "Remotive"
                link_tag = div.find("a", href=True)
                link = link_tag["href"] if link_tag else url
                jobs.append(JobPost(company=company, role=role, location="Remote", link=link, source="Remotive", description_snippet=role))
            except Exception:
                continue
    except Exception as e:
        logging.warning("Remotive failed: %s", e)
    sleep_jitter()
    return jobs

# Big tech careers heuristics
BIG_TECH_SITES = {
    "google": "https://careers.google.com/jobs/results/",
    "microsoft": "https://careers.microsoft.com/us/en/search-results",
    "amazon": "https://www.amazon.jobs/en/search",
    "meta": "https://www.metacareers.com/jobs"
}

def scrape_bigtech_generic() -> List[JobPost]:
    logging.info("Scraping BigTech careers (requests, heuristics)...")
    jobs = []
    for name, url in BIG_TECH_SITES.items():
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                txt = a.get_text(" ", strip=True)
                href = a["href"]
                if not txt: continue
                if any(k in href.lower() for k in ["/job/", "/jobs/", "/position/", "/careers", "job-openings"]) or any(k in txt.lower() for k in ["engineer","machine","research","software"]):
                    link = href if href.startswith("http") else urljoin(url, href)
                    role = txt[:150]
                    jobs.append(JobPost(company=name.capitalize(), role=role, location="N/A", link=link, source=f"{name}-careers", description_snippet=role))
        except Exception as e:
            logging.warning("BigTech fetch failed for %s: %s", name, e)
        sleep_jitter(0.2,0.6)
    return jobs

# -------------- SELENIUM DRIVER & LOGIN / COOKIES --------------
def make_selenium_driver(headless: bool = True):
    opts = Options()
    if headless:
        # modern headless
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    ua = random.choice(USER_AGENTS)
    opts.add_argument(f"--user-agent={ua}")
    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(30)
    return driver

def load_or_login_save_cookies(site: str, login_url: str, login_flow_callable, driver):
    # cookie file
    cookie_file = f"{site}_cookies.pkl"
    if os.path.exists(cookie_file):
        try:
            driver.get(login_url)
            cookies = pickle.load(open(cookie_file, "rb"))
            for c in cookies:
                try:
                    driver.add_cookie(c)
                except Exception:
                    continue
            driver.get(login_url)
            logging.info("Loaded cookies for %s", site)
            return True
        except Exception:
            logging.warning("Failed to load cookies for %s", site)
    # perform login flow callback that handles login via driver and returns True on success
    ok = login_flow_callable(driver)
    if ok:
        try:
            pickle.dump(driver.get_cookies(), open(cookie_file, "wb"))
            logging.info("Saved cookies for %s", site)
        except Exception:
            logging.warning("Could not save cookies for %s", site)
    return ok

# Concrete login flows (best-effort). You may need to update selectors and run headful for CAPTCHAs.
def linkedin_login_flow(driver) -> bool:
    email, pwd = CREDENTIALS.get("linkedin", (None, None))
    if not email or not pwd:
        logging.info("LinkedIn credentials not provided.")
        return False
    try:
        driver.get("https://www.linkedin.com/login")
        time.sleep(2)
        driver.find_element(By.ID, "username").clear(); driver.find_element(By.ID, "username").send_keys(email)
        driver.find_element(By.ID, "password").clear(); driver.find_element(By.ID, "password").send_keys(pwd)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        time.sleep(3)
        # simple check
        if "feed" in driver.current_url or "linkedin.com" in driver.current_url:
            logging.info("LinkedIn login likely succeeded.")
            return True
    except Exception as e:
        logging.warning("LinkedIn login failed: %s", e)
    return False

def naukri_login_flow(driver) -> bool:
    email, pwd = CREDENTIALS.get("naukri", (None, None))
    if not email or not pwd:
        logging.info("Naukri credentials not provided.")
        return False
    try:
        driver.get("https://www.naukri.com/nlogin/login")
        time.sleep(2)
        # Naukri has multiple login flows - try email login
        try:
            driver.find_element(By.ID, "usernameField").send_keys(email)
            driver.find_element(By.ID, "passwordField").send_keys(pwd)
            driver.find_element(By.XPATH, "//button[contains(.,'Login')]").click()
        except Exception:
            # fallback different selectors
            driver.find_element(By.NAME, "email").send_keys(email)
            driver.find_element(By.NAME, "password").send_keys(pwd)
            driver.find_element(By.XPATH, "//button[contains(.,'Login')]").click()
        time.sleep(4)
        # no reliable URL check; assume success if no errors shown
        logging.info("Attempted Naukri login.")
        return True
    except Exception as e:
        logging.warning("Naukri login failed: %s", e)
    return False

def wellfound_login_flow(driver) -> bool:
    email, pwd = CREDENTIALS.get("wellfound", (None, None))
    if not email or not pwd:
        logging.info("Wellfound credentials not provided.")
        return False
    try:
        driver.get("https://wellfound.com/signin")
        time.sleep(2)
        # try to find email/password
        try:
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "password").send_keys(pwd)
            driver.find_element(By.XPATH, "//button[contains(.,'Sign in') or contains(.,'Log in')]").click()
        except Exception:
            logging.warning("Wellfound login selectors failed; please login manually once and cookies will be saved.")
            return False
        time.sleep(3)
        logging.info("Attempted Wellfound login.")
        return True
    except Exception as e:
        logging.warning("Wellfound login failed: %s", e)
    return False

# -------------- AUTO-APPLY IMPLEMENTATIONS (best-effort) --------------
# These are *heuristic* apply flows. Many apply forms vary widely; we attempt safe simple flows.
def linkedin_easy_apply(driver, job: JobPost) -> None:
    try:
        driver.get(job.link)
        time.sleep(3)
        # LinkedIn uses a variety of apply buttons; try common selectors
        try:
            apply_button = driver.find_element(By.CSS_SELECTOR, "button.jobs-apply-button")
        except:
            try:
                apply_button = driver.find_element(By.CSS_SELECTOR, "button[data-control-name='apply_unify']")
            except:
                apply_button = None
        if not apply_button:
            job.status = "Pending"
            return
        apply_button.click()
        time.sleep(2)
        # attempt to attach resume
        try:
            # find file input
            file_input = driver.find_element(By.XPATH, "//input[@type='file']")
            if RESUME_PATH and os.path.exists(RESUME_PATH):
                file_input.send_keys(os.path.abspath(RESUME_PATH))
                time.sleep(1)
        except Exception:
            # some flows don't expose file inputs; continue
            pass
        # try fill phone
        try:
            tel = driver.find_element(By.XPATH, "//input[@type='tel' or @name='phoneNumber' or contains(@id,'phone')]")
            tel.clear(); tel.send_keys(PHONE)
        except Exception:
            pass
        # try submit (may be "Submit application" or "Next" requiring steps)
        try:
            submit_btn = driver.find_element(By.XPATH, "//button[contains(.,'Submit') or contains(.,'Apply') or contains(.,'Send application')]")
            submit_btn.click()
            time.sleep(2)
            job.status = "Applied"
            logging.info("Applied via LinkedIn to %s - %s", job.company, job.role)
            return
        except Exception:
            # cannot submit automatically
            job.status = "Flagged"
            return
    except Exception as e:
        logging.warning("LinkedIn apply error: %s", e)
        job.status = "Flagged"

def naukri_easy_apply(driver, job: JobPost) -> None:
    # Naukri's apply flows often open external pages; try simple apply if visible
    try:
        driver.get(job.link)
        time.sleep(3)
        # look for "Apply" buttons or "Quick Apply"
        try:
            apply_btn = driver.find_element(By.XPATH, "//button[contains(.,'Apply') or contains(.,'Apply Now') or contains(.,'Quick Apply')]")
            apply_btn.click()
            time.sleep(2)
            # try upload resume
            try:
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                if RESUME_PATH and os.path.exists(RESUME_PATH):
                    file_input.send_keys(os.path.abspath(RESUME_PATH))
                    time.sleep(1)
            except Exception:
                pass
            # try final submit
            try:
                submit = driver.find_element(By.XPATH, "//button[contains(.,'Submit') or contains(.,'Apply Now')]")
                submit.click()
                time.sleep(2)
                job.status = "Applied"
                logging.info("Applied via Naukri to %s - %s", job.company, job.role)
                return
            except Exception:
                job.status = "Flagged"
                return
        except Exception:
            job.status = "Pending"
            return
    except Exception as e:
        logging.warning("Naukri apply error: %s", e)
        job.status = "Flagged"

def wellfound_easy_apply(driver, job: JobPost) -> None:
    try:
        driver.get(job.link)
        time.sleep(3)
        # look for apply buttons
        try:
            apply_btn = driver.find_element(By.XPATH, "//button[contains(.,'Apply') or contains(.,'Save')]")
            apply_btn.click()
            time.sleep(2)
            # attempt file upload
            try:
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                if RESUME_PATH and os.path.exists(RESUME_PATH):
                    file_input.send_keys(os.path.abspath(RESUME_PATH))
                    time.sleep(1)
            except Exception:
                pass
            # Try final submit
            try:
                submit = driver.find_element(By.XPATH, "//button[contains(.,'Submit') or contains(.,'Continue') or contains(.,'Send')]")
                submit.click()
                time.sleep(2)
                job.status = "Applied"
                logging.info("Applied via Wellfound to %s - %s", job.company, job.role)
                return
            except Exception:
                job.status = "Flagged"
                return
        except Exception:
            job.status = "Pending"
            return
    except Exception as e:
        logging.warning("Wellfound apply error: %s", e)
        job.status = "Flagged"

def indeed_try_apply(driver, job: JobPost) -> None:
    try:
        driver.get(job.link)
        time.sleep(3)
        # Indeed sometimes has "Apply Now" or "Easily apply on Indeed"
        try:
            btn = driver.find_element(By.XPATH, "//button[contains(.,'Apply') or contains(.,'Easily apply')]")
            btn.click()
            time.sleep(2)
            # attempt to upload resume
            try:
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                if RESUME_PATH and os.path.exists(RESUME_PATH):
                    file_input.send_keys(os.path.abspath(RESUME_PATH))
                    time.sleep(1)
            except Exception:
                pass
            # attempt submit
            try:
                submit = driver.find_element(By.XPATH, "//button[contains(.,'Submit') or contains(.,'Apply')]")
                submit.click()
                time.sleep(2)
                job.status = "Applied"
                logging.info("Applied via Indeed to %s - %s", job.company, job.role)
                return
            except Exception:
                job.status = "Flagged"
                return
        except Exception:
            job.status = "Pending"
            return
    except Exception as e:
        logging.warning("Indeed apply error: %s", e)
        job.status = "Flagged"

# For other sites we mark Pending (manual) or implement later.

# -------------- ORCHESTRATOR --------------
def run_scrapers(config: Dict) -> List[JobPost]:
    all_jobs: List[JobPost] = []
    if config.get("yc", True):
        all_jobs.extend(scrape_ycombinator())
    if config.get("startup", True):
        all_jobs.extend(scrape_startup_jobs())
    if config.get("wellfound", True):
        all_jobs.extend(scrape_wellfound(query=config.get("query", "machine learning"), location=config.get("location", "India")))
    if config.get("indeed", True):
        all_jobs.extend(scrape_indeed(query=config.get("query", "machine learning"), location=config.get("location", "India"), pages=config.get("indeed_pages", 1)))
    if config.get("remoteok", True):
        all_jobs.extend(scrape_remoteok())
    if config.get("weworkremotely", True):
        all_jobs.extend(scrape_weworkremotely())
    if config.get("remotive", True):
        all_jobs.extend(scrape_remotive())
    if config.get("bigtech", True):
        all_jobs.extend(scrape_bigtech_generic())
    # Selenium scrapers for Naukri/LinkedIn/Glassdoor will be handled via selenium flows below to also allow auto-apply
    return all_jobs

def run_selenium_scans_and_apply(driver, jobs: List[JobPost], config: Dict):
    # We'll try to auto-apply for jobs on LinkedIn / Naukri / Wellfound / Indeed if link domain matches.
    for job in tqdm(jobs, desc="Applying / Flagging jobs"):
        try:
            link = job.link.lower()
            # Choose site-specific apply if link indicates the site
            if "linkedin.com" in link:
                linkedin_easy_apply(driver, job)
            elif "naukri.com" in link:
                naukri_easy_apply(driver, job)
            elif "wellfound.com" in link or "angel.co" in link:
                wellfound_easy_apply(driver, job)
            elif "indeed.com" in link:
                indeed_try_apply(driver, job)
            else:
                # For remote sites and bigtech careers often external or complex forms; mark as Pending
                job.status = "Pending"
        except Exception as e:
            logging.warning("Auto-apply loop error for %s: %s", job.link, e)
            if job.status != "Applied":
                job.status = "Flagged"

# -------------- MAIN FLOW --------------
def main():
    MAX_APPS_PER_RUN = 20   # configurable limit

    config = {
        "query": "machine learning",
        "location": "India",
        "yc": True,
        "startup": True,
        "wellfound": True,
        "indeed": True,
        "remoteok": True,
        "weworkremotely": True,
        "remotive": True,
        "bigtech": True,
        "naukri_pages": 1,
        "indeed_pages": 1
    }

    logging.info("Starting scrapers...")
    all_jobs = run_scrapers(config)
    logging.info("Scraped %d jobs (before selenium site scans)", len(all_jobs))

    # Selenium: scrape LinkedIn + Naukri (with login & cookies)
    driver = make_selenium_driver(headless=HEADLESS)
    try:
        # Attempt to login/save cookies
        try:
            load_or_login_save_cookies("linkedin", "https://www.linkedin.com/login", linkedin_login_flow, driver)
        except Exception:
            pass
        try:
            load_or_login_save_cookies("naukri", "https://www.naukri.com/nlogin/login", naukri_login_flow, driver)
        except Exception:
            pass
        try:
            load_or_login_save_cookies("wellfound", "https://wellfound.com/signin", wellfound_login_flow, driver)
        except Exception:
            pass

        # LinkedIn search results
        try:
            logging.info("Selenium: scanning LinkedIn search results...")
            search_url = f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(config['query'])}&location={quote_plus(config['location'])}"
            driver.get(search_url)
            time.sleep(3)
            cards = driver.find_elements(By.CSS_SELECTOR, ".jobs-search-results__list-item, .base-card")
            for c in cards[:80]:
                try:
                    role = c.find_element(By.CSS_SELECTOR, "a.job-card-list__title, a.base-card__full-link").text.strip()
                    comp = c.find_element(By.CSS_SELECTOR, ".job-card-container--company-name, a.job-card-container__company-name").text.strip()
                    loc = c.find_element(By.CSS_SELECTOR, ".job-card-container__metadata-item").text.strip()
                    link = c.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
                    job = JobPost(company=comp, role=role, location=loc, link=link, source="LinkedIn", description_snippet=role)
                    all_jobs.append(job)
                except Exception:
                    continue
        except Exception as e:
            logging.warning("LinkedIn selenium scan failed: %s", e)

        # Naukri search results
        try:
            logging.info("Selenium: scanning Naukri search results...")
            nurl = f"https://www.naukri.com/{quote_plus(config['query'])}-jobs-in-{quote_plus(config['location'])}"
            driver.get(nurl)
            time.sleep(3)
            cards = driver.find_elements(By.CSS_SELECTOR, ".jobTuple")
            for c in cards[:200]:
                try:
                    title_el = c.find_element(By.CSS_SELECTOR, "a.title")
                    role = title_el.text.strip()
                    link = title_el.get_attribute("href")
                    comp = c.find_element(By.CSS_SELECTOR, ".companyInfo .subTitle").text.strip() if c.find_elements(By.CSS_SELECTOR, ".companyInfo .subTitle") else "Naukri"
                    loc = c.find_element(By.CSS_SELECTOR, ".location").text.strip() if c.find_elements(By.CSS_SELECTOR, ".location") else config['location']
                    salary = c.find_element(By.CSS_SELECTOR, ".salary").text.strip() if c.find_elements(By.CSS_SELECTOR, ".salary") else "N/A"
                    job = JobPost(company=comp, role=role, location=loc, link=link, source="Naukri", salary=salary, description_snippet=role)
                    all_jobs.append(job)
                except Exception:
                    continue
        except Exception as e:
            logging.warning("Naukri selenium scan failed: %s", e)

    finally:
        # keep driver alive for later
        pass

    logging.info("Total jobs collected before dedupe: %d", len(all_jobs))
    all_jobs = dedupe_jobs(all_jobs)
    logging.info("After dedupe: %d", len(all_jobs))

    # Export raw list
    export_jobs(all_jobs, "jobs_raw.xlsx")

    # Filter shortlist with resume + salary cutoff
    shortlisted = []
    for j in all_jobs:
        j.role = normalize_text(j.role)
        j.company = normalize_text(j.company)
        j.location = normalize_text(j.location)
        j.description_snippet = normalize_text(j.description_snippet)
        if matches_resume(j) and meets_cutoff(j):
            shortlisted.append(j)
    logging.info("Shortlisted after filtering: %d", len(shortlisted))

    # Cap number of applications
    to_apply = shortlisted[:MAX_APPS_PER_RUN]
    skipped = shortlisted[MAX_APPS_PER_RUN:]
    for j in skipped:
        j.status = "Pending"   # mark rest as pending for manual apply

    # Run auto-apply for capped jobs
    driver_apply = make_selenium_driver(headless=HEADLESS)
    try:
        # reload cookies
        try:
            load_or_login_save_cookies("linkedin", "https://www.linkedin.com/login", linkedin_login_flow, driver_apply)
        except Exception:
            pass
        try:
            load_or_login_save_cookies("naukri", "https://www.naukri.com/nlogin/login", naukri_login_flow, driver_apply)
        except Exception:
            pass
        try:
            load_or_login_save_cookies("wellfound", "https://wellfound.com/signin", wellfound_login_flow, driver_apply)
        except Exception:
            pass

        run_selenium_scans_and_apply(driver_apply, to_apply, config)
    finally:
        driver_apply.quit()

    # final export with statuses
    export_jobs(shortlisted, "jobs_with_status.xlsx")
    logging.info("Done. Applied to %d jobs, capped at %d. Files: jobs_raw.xlsx, jobs_with_status.xlsx",
                 len(to_apply), MAX_APPS_PER_RUN)


if __name__ == "__main__":
    main()
