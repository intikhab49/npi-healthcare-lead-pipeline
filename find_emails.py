"""Stage 3: Crawl practice websites and extract email addresses.

Reads outputs/with_websites.csv, adds an `emails` column (semicolon-separated),
writes outputs/final_leads.csv. Resume-safe like Stage 2.
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

from common import (
    RotatingSession,
    extract_emails,
    load_config,
    request_with_retries,
)

INPUT = "outputs/with_websites.csv"
OUTPUT = "outputs/final_leads.csv"
FIELDS = ["npi", "name", "phone", "address", "city", "state", "zip", "taxonomy", "provider_type", "website", "search_query", "emails"]

LINK_KEYWORDS = ("contact", "about", "team", "reach", "email", "staff")
SUB_URL_CACHE = {}


def find_subpage_urls(session, cfg, base_url, html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text() or "").lower()
        haystack = f"{text} {href.lower()}"
        if not any(k in haystack for k in LINK_KEYWORDS):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        if absolute.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        parsed = urllib.parse.urlparse(absolute)
        if parsed.netloc and parsed.netloc != urllib.parse.urlparse(base_url).netloc:
            continue
        if absolute not in candidates:
            candidates.append(absolute)
    return candidates[:cfg["email"]["max_sub_pages"]]


def fetch_html(session, cfg, url):
    try:
        resp = request_with_retries(
            session, url, retries=cfg["email"]["retries"],
            timeout=cfg["email"]["timeout_seconds"], allow_redirects=True,
        )
        if 200 <= resp.status_code < 400:
            return resp.text
    except requests.RequestException as e:
        print(f"    [!] fetch failed {url}: {e}", file=sys.stderr)
    return None


def fetch_html_with_fallback(session, cfg, url):
    html = fetch_html(session, cfg, url)
    if html is not None:
        return html
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.get(
            url,
            timeout=cfg["email"]["timeout_seconds"],
            impersonate="chrome",
            allow_redirects=True,
        )
        if 200 <= resp.status_code < 400:
            return resp.text
    except Exception as e:
        print(f"    [!] curl_cffi fallback failed {url}: {e}", file=sys.stderr)
    return None


def extract_from_url(session, cfg, url):
    html = fetch_html_with_fallback(session, cfg, url)
    if html is None:
        return set()
    return extract_emails(html)


def find_emails(session, cfg, base_url):
    emails = extract_from_url(session, cfg, base_url)
    if emails:
        return emails

    html = fetch_html_with_fallback(session, cfg, base_url)
    if html is None:
        return set()
    sub_urls = find_subpage_urls(session, cfg, base_url, html)
    for sub in sub_urls:
        if sub in SUB_URL_CACHE:
            sub_emails = SUB_URL_CACHE[sub]
        else:
            sub_emails = extract_from_url(session, cfg, sub)
            SUB_URL_CACHE[sub] = sub_emails
        emails |= sub_emails
        if emails:
            break
        time.sleep(random.uniform(*cfg["email"]["delay_range"]))
    return emails


def load_done_rows():
    done = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done[row["npi"]] = row
    return done


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract emails from practice websites")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Stop after this many seconds (0 = no limit). Used for scheduled auto-resume runs.")
    args = parser.parse_args()

    cfg = load_config()
    session = RotatingSession(cfg["user_agents"])
    done = load_done_rows()

    with open(INPUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    out_mode = "w" if not done else "a"
    out_handle = open(OUTPUT, out_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_handle, fieldnames=FIELDS)
    if out_mode == "w":
        writer.writeheader()

    import time as _time
    start_time = _time.monotonic()
    found = skipped = 0
    try:
        for row in tqdm(rows, desc="Extracting emails", unit="site"):
            if args.max_runtime and _time.monotonic() - start_time > args.max_runtime:
                print(f"  [!] Reached max runtime ({args.max_runtime}s). Stopping cleanly.", file=sys.stderr)
                break
            if row["npi"] in done:
                skipped += 1
                continue
            url = (row.get("website") or "").strip()
            if not url:
                row["emails"] = ""
                writer.writerow(row)
                out_handle.flush()
                continue
            emails = find_emails(session, cfg, url)
            row["emails"] = "; ".join(sorted(emails))
            writer.writerow(row)
            out_handle.flush()
            if emails:
                found += 1
            time.sleep(random.uniform(*cfg["email"]["delay_range"]))
    finally:
        out_handle.close()

    print(f"\nDone: emails found on {found} sites, {skipped} already processed.")
    print(f"Output -> {OUTPUT}")


if __name__ == "__main__":
    main()
