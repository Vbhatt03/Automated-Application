"""
main.py
Master runner that executes:
1. Job scraping + auto-apply
2. Contact discovery + outreach prep
"""

import subprocess
import sys
import os

SRC_DIR = "src"

def run_script(script_path):
    """Run another Python script and stream logs."""
    print(f"\n⚡ Running {script_path} ...\n")
    result = subprocess.run([sys.executable, script_path], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"❌ {script_path} failed:\n{result.stderr}")
    else:
        print(f"✅ {script_path} finished successfully:\n{result.stdout}")


def main():
    # Step 1 + 2: Scraping & Auto-apply
    scraper_file = os.path.join(SRC_DIR, "Job_applier.py")
    if os.path.exists(scraper_file):
        run_script(scraper_file)
    else:
        print("❌ job_scraper_apply.py not found in src/!")

    # Step 3 + 5: Contact Discovery + Outreach
    contact_file = os.path.join(SRC_DIR, "Contact.py")
    if os.path.exists(contact_file):
        run_script(contact_file)
    else:
        print("❌ contact_discovery_and_outreach.py not found in src/!")


if __name__ == "__main__":
    main()
