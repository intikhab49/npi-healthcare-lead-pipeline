"""Stage 2: Find official practice websites via DuckDuckGo search.

Reads outputs/raw_providers.csv, adds a `website` column, writes
outputs/with_websites.csv. Resume-safe: rows already processed (present
in the output file) are skipped, so a crashed run restarts where it left off.
"""
import csv
import os
import random
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from common import RotatingSession, load_config, request_with_retries

INPUT = "outputs/raw_providers.csv"
OUTPUT = "outputs/with_websites.csv"
FIELDS = ["npi", "name", "phone", "address", "city", "state", "zip", "taxonomy", "provider_type", "website", "search_query"]

DOMAIN_REGEX = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def normalize_domain(url):
    url = url.strip()
    if not url:
        return ""
    m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:/|$)", url, re.I)
    return m.group(1).lower() if m else ""


GENERIC_TOKENS = {
    "mental", "health", "clinic", "clinics", "care", "caring", "therap", "therapy",
    "behavioral", "behaviour", "counsel", "counseling", "counselling", "psych", "psy",
    "psychiatry", "psychiatric", "wellness", "services", "center", "centre", "group",
    "associates", "assoc", "practice", "medical", "medicine", "physician", "doctor",
    "dr", "md", "phd", "patients", "treatment", "hospital", "clinic", "community",
    "family", "children", "child", "adult", "texas", "dallas", "houston", "austin",
    "elpaso", "el", "paso", "san", "antonio", "tx", "usa", "us",
}


def is_relevant_domain(domain, practice_name):
    if not domain:
        return False
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld not in {"com", "org", "net", "us", "io", "co", "health", "care", "clinic", "therapist", "therapy"}:
        return False
    blacklist = {
        "facebook.com", "linkedin.com", "twitter.com", "instagram.com", "youtube.com",
        "yelp.com", "healthgrades.com", "vitals.com", "zocdoc.com", "psychologytoday.com",
        "webmd.com", "md.com", "doximity.com", "advisory.com", "google.com", "bing.com",
        "duckduckgo.com", "wikipedia.org", "yahoo.com", "maps.google.com",
        "everydayhealth.com", "apple.com",
        "medicare.gov", "medicaid.gov", "npi.io",
        "npiregistry.cms.hhs.gov", "carepaths.com", "sharecare.com",
        "wellness.com", "goodtherapy.org", "therapyden.com", "openlist.com",
        "yellowpages.com", "bbb.org", "mapquest.com", "trackerhead.online",
        "ourhealthnetwork.com", "theguardian.com", "elpasoconecta.us",
        "ratemds.com", "healthcaremagic.com", "medifind.com", "caredash.com",
        "npinumbers.net", "npiprofile.com", "npilookup.com", "npi-data.com",
        "usdoctor.com", "medicarelist.org", "practicelink.com", "sov.health",
        "turquoise.health", "findcarenow.com", "doctorshospitals.com",
        "patientsmatter.com", "mdmaster.com", "matchcollege.com",
        "mentaltherapy.io", "mentalhealth.com", "healthcare6.com",
        "elpasotexas.gov", "elpasotexas.gov", "mentalhealth.gov", "betterhelp.com",
        "talkspace.com", "healthline.com", "verywellmind.com", "medscape.com",
        "medicinenet.com", "rxlist.com", "psychcentral.com", "psychologytoday.com",
        "therapygroup.io", "counseling.org", "therapists.com", "mentalhealthmatch.com",
        "psychology.com", "whathealth.com", "kernodle.com", "goodtherapy.org",
        "therapydirectory.com", "psychiatry.org", "psychiatricnews.com",
    }
    if any(domain == b or domain.endswith("." + b) for b in blacklist):
        return False
    if not practice_name:
        return True
    name_tokens = [t for t in re.sub(r"[^a-z0-9]", " ", practice_name.lower()).split() if len(t) > 2]
    distinctive = [t for t in name_tokens if t not in GENERIC_TOKENS]
    if not distinctive:
        return False
    return any(t in domain for t in distinctive)


def build_query(row):
    practice = row["name"]
    city = row["city"]
    state = row["state"]
    return f'"{practice}" {city} {state} mental health practice'


def search_website(session, cfg, query):
    """Return first organic result URL (str) or None."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    for attempt in range(cfg["search"]["retries"]):
        try:
            with DDGS() as ddgs:
                results = list(
                    ddgs.text(
                        query,
                        region="us-en",
                        safesearch="moderate",
                        max_results=cfg["search"]["max_results_per_query"],
                    )
                )
            for res in results:
                href = res.get("href") or ""
                domain = normalize_domain(href)
                if not domain:
                    continue
                if is_relevant_domain(domain, query):
                    return href if href.startswith("http") else f"https://{href}"
            return None
        except Exception as e:
            if attempt == cfg["search"]["retries"] - 1:
                print(f"    [!] search failed after retries: {e}", file=sys.stderr)
                return None
            time.sleep(cfg["search"]["delay_range"][0] + random.random() * cfg["search"]["delay_range"][1])
    return None


def verify_domain_live(session, cfg, url):
    try:
        resp = request_with_retries(
            session, url, retries=1, timeout=cfg["search"]["request_timeout"],
            allow_redirects=True,
        )
        return 200 <= resp.status_code < 400
    except requests.RequestException:
        return False


def load_done_rows():
    done = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done[row["npi"]] = row
    return done


QUERY_CACHE = {}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Find practice websites via DuckDuckGo")
    parser.add_argument("--only-orgs", action="store_true", help="Only search organizations (skip individuals)")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process in this run (0 = all)")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Stop after this many seconds (0 = no limit). Used for scheduled auto-resume runs.")
    args = parser.parse_args()

    cfg = load_config()
    session = RotatingSession(cfg["user_agents"])
    done = load_done_rows()

    with open(INPUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if args.only_orgs:
        rows = [r for r in rows if r.get("provider_type") == "Organization"]
    if args.limit > 0:
        rows = rows[: args.limit]

    out_mode = "w" if not done else "a"
    out_handle = open(OUTPUT, out_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_handle, fieldnames=FIELDS)
    if out_mode == "w":
        writer.writeheader()

    import time as _time
    start_time = _time.monotonic()
    found = skipped = 0
    try:
        for row in tqdm(rows, desc="Finding websites", unit="provider"):
            if args.max_runtime and _time.monotonic() - start_time > args.max_runtime:
                print(f"  [!] Reached max runtime ({args.max_runtime}s). Stopping cleanly.", file=sys.stderr)
                break
            if row["npi"] in done:
                skipped += 1
                continue
            query = build_query(row)
            if query in QUERY_CACHE:
                url = QUERY_CACHE[query]
            else:
                url = search_website(session, cfg, query)
                if url and not verify_domain_live(session, cfg, url):
                    url = None
                QUERY_CACHE[query] = url
            row["website"] = url or ""
            row["search_query"] = query
            writer.writerow(row)
            out_handle.flush()
            if url:
                found += 1
            delay = random.uniform(*cfg["search"]["delay_range"])
            time.sleep(delay)
    finally:
        out_handle.close()

    print(f"\nDone: {found} websites found, {skipped} already processed.")
    print(f"Output -> {OUTPUT}")


if __name__ == "__main__":
    main()
