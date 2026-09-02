"""
PIC Scraper — collects organizational contact info + staff names/roles
from NGO / Corporate websites.

Four ways to run (see the GitHub Actions workflow for how the web form
maps onto these):

  1. --discover --sector "Retail Corporate" [--query "..."] [--max-results 8]
     GENERAL PURPOSE MODE. Searches the open web for organizations
     matching the sector/keyword, guesses each site's contact and
     team/about pages, scrapes them, and saves discovered orgs into
     config.json so they're part of the permanent library.

  2. --sector "Environmental NGO"
     Re-scrape every org already saved in config.json under that sector
     (no new search — just refreshes what's already known).

  3. --org-name "New Org" --org-sector "Retail Corporate"
     --team-url "..." --contact-url "..." [--render-js] [--no-save]
     Scrape ONE specific org you already know the URLs for.

  4. --all
     Scrape every org in config.json (used for the weekly scheduled run).

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It does not guess or construct individual personal emails. Most NGOs
and corporates only publish a general contact email, not personal
ones. Guessing emails produces unverified data and edges toward
spam-list building, so this only reports what's actually published.

It does not scrape LinkedIn, Facebook, Instagram, and similar
platforms even if they show up in search results — scraping those
directly breaches their terms of service regardless of intent.

It checks robots.txt before fetching any page and skips pages that
disallow it.

OUTPUT
------
data/results.csv — appended to, never overwritten. Re-runs skip rows
already present (dedup by Org+Record Type+Name+Source URL).
data/last_run.txt — UTC timestamp of the most recent run.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, unquote
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
OUTPUT_PATH = "data/results.csv"
LAST_RUN_PATH = "data/last_run.txt"
BOT_NAME = "PIC-Research-Bot"
HEADERS = {
    "User-Agent": f"Mozilla/5.0 (compatible; {BOT_NAME}/1.0; "
                  f"+contact: replace-with-your-contact-email@example.com)"
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

# Platforms we never scrape directly, even if they appear in search
# results — scraping these breaches their terms of service regardless
# of the reason. Their own listing/company pages are not a substitute
# for an organization's own site anyway (data is often stale there).
SEARCH_BLOCKLIST_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "google.com", "tiktok.com",
    "pinterest.com", "glassdoor.com", "indeed.com",
}

CONTACT_LINK_HINTS = ["contact", "hubungi", "kontak"]
TEAM_LINK_HINTS = ["team", "tim", "about", "tentang", "struktur", "pengurus",
                   "leadership", "staff", "kepengurusan", "our-team",
                   "who-we-are", "profil", "profile"]

_robots_cache = {}


def robots_allowed(url: str) -> bool:
    """Check robots.txt for this URL's domain, caching per-domain results.
    If robots.txt can't be reached at all, default to allow (most sites
    that omit robots.txt intend that)."""
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain not in _robots_cache:
        rp = RobotFileParser()
        rp.set_url(domain + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots_cache[domain] = rp
    rp = _robots_cache[domain]
    if rp is None:
        return True
    try:
        return rp.can_fetch(BOT_NAME, url)
    except Exception:
        return True


def domain_of(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


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
    if not robots_allowed(url):
        print(f"    [!] robots.txt disallows fetching {url} — skipping.", file=sys.stderr)
        return ""
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


# ---------------------------------------------------------------------
# Discovery mode — general-purpose: find orgs for ANY sector/keyword
# ---------------------------------------------------------------------

def duckduckgo_search(query: str, max_results: int = 8):
    """No-API-key web search via DuckDuckGo's HTML endpoint. Best-effort:
    DuckDuckGo may rate-limit or change its markup, so treat results as
    a starting point to review, not a guaranteed complete list."""
    results = []
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Search failed: {e}", file=sys.stderr)
        return results

    soup = BeautifulSoup(resp.text, "lxml")
    seen_domains = set()
    links = soup.select("a.result__a") or soup.select("a[href]")
    for a in links:
        href = a.get("href", "")
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            href = unquote(qs["uddg"][0])
        if not href.startswith("http"):
            continue
        d = domain_of(href)
        if any(b in d for b in SEARCH_BLOCKLIST_DOMAINS):
            continue
        if d in seen_domains:
            continue
        seen_domains.add(d)
        title = a.get_text(" ", strip=True)
        results.append((title, href))
        if len(results) >= max_results:
            break
    return results


def find_subpages(homepage_url: str, html: str):
    """Best-effort guess at a contact page and a team/about page from a
    homepage's nav links (checks English and Indonesian wording)."""
    soup = BeautifulSoup(html, "lxml")
    contact_url, team_url = None, None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        combined = (href + " " + text).lower()
        full_url = urljoin(homepage_url, href)
        if domain_of(full_url) != domain_of(homepage_url):
            continue
        if not contact_url and any(h in combined for h in CONTACT_LINK_HINTS):
            contact_url = full_url
        if not team_url and any(h in combined for h in TEAM_LINK_HINTS):
            team_url = full_url
    return contact_url, team_url


def guess_org_name(homepage_url: str, html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        title = re.split(r"[|\-–—]", title)[0].strip()
        if title:
            return title
    return domain_of(homepage_url).split(".")[0].capitalize()


def discover_and_scrape(sector, query, max_results, config, existing_keys, today):
    print(f"\n=== Discovering organizations for sector: {sector} ===")
    print(f"  Search query: {query}")
    results = duckduckgo_search(query, max_results)
    print(f"  Found {len(results)} candidate site(s) (after filtering out social platforms).")

    discovered_orgs = []
    for title, homepage_url in results:
        print(f"\n  Checking: {homepage_url}")
        try:
            html = fetch_html(homepage_url, render_js=False)
        except Exception as e:
            print(f"    [!] Failed to fetch homepage: {e}", file=sys.stderr)
            continue
        if not html or len(html) < 200:
            print("    [!] Page looks empty (JS-rendered site, or robots.txt blocked it) — skipping.")
            continue

        contact_url, team_url = find_subpages(homepage_url, html)
        name = guess_org_name(homepage_url, html)
        print(f"    Guessed name: {name}")
        print(f"    Contact page: {contact_url or homepage_url}")
        print(f"    Team page: {team_url or '(none found — will still check homepage)'}")

        org_entry = {
            "name": name,
            "sector": sector,
            "team_pages": [team_url] if team_url else [],
            "contact_pages": [contact_url] if contact_url else [homepage_url],
            "render_js": False,
        }
        already = any(o["name"].lower() == name.lower() for o in config["organizations"])
        if not already:
            config["organizations"].append(org_entry)
        discovered_orgs.append(org_entry)
        time.sleep(1)  # be polite between requests

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(discovered_orgs)} organization(s) to config.json under '{sector}'.")

    return scrape_orgs(discovered_orgs, existing_keys, today)


# ---------------------------------------------------------------------
# Core scraping (used by all modes once we know which orgs to visit)
# ---------------------------------------------------------------------

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
            if not html:
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
            if not html:
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
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--query", default="")
    parser.add_argument("--max-results", default="8")
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
    os.makedirs("data", exist_ok=True)
    existing_keys = load_existing_keys(OUTPUT_PATH)

    if args.discover and args.sector.strip():
        query = args.query.strip() or f"{args.sector.strip()} organization Indonesia"
        new_rows = discover_and_scrape(
            args.sector.strip(), query, int(args.max_results or 8),
            config, existing_keys, today,
        )

    elif args.org_name.strip():
        new_org = {
            "name": args.org_name.strip(),
            "sector": args.org_sector.strip() or "Uncategorized",
            "team_pages": [args.team_url.strip()] if args.team_url.strip() else [],
            "contact_pages": [args.contact_url.strip()] if args.contact_url.strip() else [],
            "render_js": str2bool(args.render_js),
        }
        if str2bool(args.save_to_library):
            already = any(o["name"].lower() == new_org["name"].lower()
                          for o in config["organizations"])
            if not already:
                config["organizations"].append(new_org)
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                print(f"Saved '{new_org['name']}' to config.json for future runs.")
        new_rows = scrape_orgs([new_org], existing_keys, today)

    elif args.all:
        new_rows = scrape_orgs(config["organizations"], existing_keys, today)

    elif args.sector.strip():
        orgs_to_scrape = [o for o in config["organizations"]
                          if o["sector"].strip().lower() == args.sector.strip().lower()]
        if not orgs_to_scrape:
            print(f"No orgs found in config.json for sector '{args.sector}'. "
                  f"Nothing to scrape. (Tip: use --discover to find some.)")
        new_rows = scrape_orgs(orgs_to_scrape, existing_keys, today)

    else:
        new_rows = scrape_orgs(config["organizations"], existing_keys, today)

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
