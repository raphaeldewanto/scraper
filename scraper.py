"""
PIC Scraper — collects organizational contact info + staff names/roles
from NGO / Corporate websites.

Runs three ways (see the GitHub Actions workflow for how the web form
maps onto these):

  1. --sector "Environmental NGO"
     Scrape every org already in config.json under that sector.

  2. --org-name "New Org" --org-sector "Retail Corporate"
     --team-url "..." --contact-url "..." [--render-js] [--no-save]
     Scrape ONE new org on the fly. By default it's also saved into
     config.json so future sector-wide or --all runs pick it up too.

  3. --all
     Scrape every org in config.json (used for scheduled/recurring runs).

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It does not guess or construct individual personal emails. Most NGOs
and corporates only publish a general contact email, not personal
ones. Guessing emails produces unverified data and edges toward
spam-list building, so this only reports what's actually published.

OUTPUT
------
data/results.csv — appended to, never overwritten. Re-runs skip rows
already present (dedup by Org+Record Type+Name+Source URL).
data/last_run.txt — UTC timestamp of the most recent run, for display
on the landing page.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
OUTPUT_PATH = "data/results.csv"
LAST_RUN_PATH = "data/last_run.txt"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PIC-Research-Bot/1.0; "
                  "+contact: replace-with-your-contact-email@example.com)"
}
TIMEOUT = 15

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+62|62|0)8\d{2}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"
    r"|(?:\+?\d{1,3}[\s\-]?)?\(?\d{2,4}\)?[\s\-]\d{3,4}[\s\-]\d{3,4}"
)

NAV_STOPWORDS = {
    "about", "about us", "contact", "contact us", "home", "team", "our team",
    "career", "careers", "privacy policy", "news", "events", "publication",
    "publications", "articles", "gallery", "faq", "partners", "donate",
    "mission", "values", "mission & values", "sign up", "our office",
    "connect with us", "read more",
}


def is_probable_name(text: str) -> bool:
    text = text.strip()
    lowered = text.lower()
    if not text:
        return False
    if lowered in NAV_STOPWORDS:
        return False
    if any(lowered.startswith(w + " ") or lowered == w for w in
           ("about", "contact", "our", "the", "read", "connect", "sign")):
        return False
    if any(ch.isdigit() for ch in text):
        return False
    words = text.replace("–", "-").split(" - ")[0].split()
    if not (2 <= len(words) <= 6):
        return False
    caps = sum(1 for w in words if w[:1].isupper())
    return caps >= max(2, len(words) - 1)


def fetch_html(url: str, render_js: bool = False) -> str:
    if render_js:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(f"  [!] {url} needs JS rendering but Playwright isn't "
                  f"installed.", file=sys.stderr)
            return ""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="networkidle")
            html = page.content()
            browser.close()
            return html
    else:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text


def extract_contact_info(html: str):
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    emails = sorted(set(EMAIL_RE.findall(text)))
    phones = sorted(set(m.strip() for m in PHONE_RE.findall(text)))
    return emails, phones


def extract_team_members(html: str):
    soup = BeautifulSoup(html, "lxml")
    people = []
    headings = soup.find_all(["h1", "h2", "h3", "h4"])

    for h in headings:
        raw = h.get_text(" ", strip=True)
        if not raw:
            continue
        parts = re.split(r"\s[–—-]\s", raw, maxsplit=1)
        candidate_name = parts[0].strip()
        if not is_probable_name(candidate_name):
            continue
        role = parts[1].strip() if len(parts) > 1 else ""
        if not role:
            sib = h.find_next(["p"])
            if sib:
                sib_text = sib.get_text(" ", strip=True)
                snippet = re.split(r"[–—.]", sib_text, maxsplit=1)[0]
                if 3 <= len(snippet.split()) <= 8:
                    role = snippet.strip()
        people.append((candidate_name, role or "(role not found — check source)"))

    seen = set()
    unique_people = []
    for name, role in people:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique_people.append((name, role))
    return unique_people


def load_existing_keys(path):
    keys = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                keys.add((row["Org Name"], row["Record Type"], row["PIC Name"], row["Source URL"]))
    return keys


def scrape_orgs(orgs, existing_keys, today):
    new_rows = []
    for org in orgs:
        name = org["name"]
        sector = org["sector"]
        render_js = org.get("render_js", False)
        print(f"\n=== {name} ({sector}) ===")

        for url in org.get("contact_pages", []):
            if not url:
                continue
            print(f"  Contact page: {url}")
            try:
                html = fetch_html(url, render_js)
            except Exception as e:
                print(f"    [!] Failed to fetch: {e}", file=sys.stderr)
                continue
            emails, phones = extract_contact_info(html)
            key = (name, "Org Contact", "", url)
            if (emails or phones) and key not in existing_keys:
                new_rows.append({
                    "Org Name": name, "Sector": sector, "Record Type": "Org Contact",
                    "PIC Name": "", "Role/Title": "",
                    "Org Email": "; ".join(emails), "Org Phone": "; ".join(phones),
                    "Source URL": url, "Date Collected": today,
                })
                existing_keys.add(key)
            print(f"    Found emails: {emails or 'none'} | phones: {phones or 'none'}")

        for url in org.get("team_pages", []):
            if not url:
                continue
            print(f"  Team page: {url}")
            try:
                html = fetch_html(url, render_js)
            except Exception as e:
                print(f"    [!] Failed to fetch: {e}", file=sys.stderr)
                continue
            people = extract_team_members(html)
            print(f"    Found {len(people)} candidate name(s).")
            for person_name, role in people:
                key = (name, "Team Member", person_name, url)
                if key not in existing_keys:
                    new_rows.append({
                        "Org Name": name, "Sector": sector, "Record Type": "Team Member",
                        "PIC Name": person_name, "Role/Title": role,
                        "Org Email": "", "Org Phone": "",
                        "Source URL": url, "Date Collected": today,
                    })
                    existing_keys.add(key)
    return new_rows


def str2bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", default="")
    parser.add_argument("--org-name", default="")
    parser.add_argument("--org-sector", default="")
    parser.add_argument("--team-url", default="")
    parser.add_argument("--contact-url", default="")
    parser.add_argument("--render-js", default="false")
    parser.add_argument("--save-to-library", default="true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    today = datetime.now(timezone.utc).date().isoformat()

    if args.org_name.strip():
        new_org = {
            "name": args.org_name.strip(),
            "sector": args.org_sector.strip() or "Uncategorized",
            "team_pages": [args.team_url.strip()] if args.team_url.strip() else [],
            "contact_pages": [args.contact_url.strip()] if args.contact_url.strip() else [],
            "render_js": str2bool(args.render_js),
        }
        orgs_to_scrape = [new_org]
        if str2bool(args.save_to_library):
            already = any(o["name"].lower() == new_org["name"].lower()
                          for o in config["organizations"])
            if not already:
                config["organizations"].append(new_org)
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                print(f"Saved '{new_org['name']}' to config.json for future runs.")
    elif args.all:
        orgs_to_scrape = config["organizations"]
    elif args.sector.strip():
        orgs_to_scrape = [o for o in config["organizations"]
                          if o["sector"].strip().lower() == args.sector.strip().lower()]
        if not orgs_to_scrape:
            print(f"No orgs found in config.json for sector '{args.sector}'. "
                  f"Nothing to scrape.")
    else:
        orgs_to_scrape = config["organizations"]

    os.makedirs("data", exist_ok=True)
    existing_keys = load_existing_keys(OUTPUT_PATH)
    new_rows = scrape_orgs(orgs_to_scrape, existing_keys, today)

    file_exists = os.path.exists(OUTPUT_PATH)
    with open(OUTPUT_PATH, "a", newline="", encoding="utf-8") as f:
        fieldnames = ["Org Name", "Sector", "Record Type", "PIC Name", "Role/Title",
                      "Org Email", "Org Phone", "Source URL", "Date Collected"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    print(f"\nDone. {len(new_rows)} new row(s) written to {OUTPUT_PATH}.")


if __name__ == "__main__":
    main()
