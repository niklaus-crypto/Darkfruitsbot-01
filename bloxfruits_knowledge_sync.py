#!/usr/bin/env python3
"""Build/update the local Dark Fruits Blox Fruits knowledge database.

Uses the public MediaWiki Action API of the community-run Blox Fruits Wiki.
Run: python bloxfruits_knowledge_sync.py
Optional: python bloxfruits_knowledge_sync.py --max-pages 200

The script only reads public pages, waits between requests, and stores article text
locally. It deliberately does not store current dealer stock as static knowledge.
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# The first domain is the currently surfaced community wiki. Some hosts block
# API traffic from cloud providers, so try known alternate public wiki endpoints.
WIKI_ENDPOINTS = [
    ("https://bloxfruits-wiki.com/api.php", "https://bloxfruits-wiki.com/wiki/"),
    ("https://bloxfruitswiki.org/api.php", "https://bloxfruitswiki.org/wiki/"),
    ("https://blox-fruits.fandom.com/api.php", "https://blox-fruits.fandom.com/wiki/"),
]
API_URL, BASE_URL = WIKI_ENDPOINTS[0]
DEFAULT_DB = "bloxfruits_knowledge.db"
USER_AGENT = "Mozilla/5.0 (compatible; DarkFruitsKnowledgeSync/1.1; educational game knowledge reader)"


def clean_text(value: str) -> str:
    value = re.sub(r"\[\d+\]", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def make_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS knowledge_documents (
        page_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        content TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_title ON knowledge_documents(title)")
    con.commit()
    return con


def fetch_json(session: requests.Session, params: dict) -> dict:
    """Fetch MediaWiki API data, rotating through known sources on access/API failures."""
    global API_URL, BASE_URL
    last_error = None
    # Start at the currently selected endpoint, then try each remaining endpoint.
    start_idx = next((i for i, pair in enumerate(WIKI_ENDPOINTS) if pair[0] == API_URL), 0)
    ordered = WIKI_ENDPOINTS[start_idx:] + WIKI_ENDPOINTS[:start_idx]
    for api_url, base_url in ordered:
        API_URL, BASE_URL = api_url, base_url
        for attempt in range(3):
            try:
                response = session.get(API_URL, params=params, timeout=30)
                if response.status_code in (401, 403, 404):
                    last_error = RuntimeError(f"Wiki endpoint {API_URL} returned HTTP {response.status_code}")
                    break
                if response.status_code == 429 or response.status_code >= 500:
                    wait_header = response.headers.get("Retry-After", "")
                    try:
                        wait = int(wait_header) if wait_header else 2 ** attempt
                    except ValueError:
                        wait = 2 ** attempt
                    print(f"Wiki endpoint returned {response.status_code}; waiting {min(wait, 30)}s")
                    time.sleep(min(wait, 30))
                    continue
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    last_error = RuntimeError(f"Wiki API at {API_URL} returned non-JSON content")
                    break
                if "error" in data:
                    last_error = RuntimeError(f"Wiki API error at {API_URL}: {data['error']}")
                    break
                return data
            except requests.RequestException as exc:
                last_error = exc
                break
    raise RuntimeError(f"All wiki API endpoints failed; keeping existing seeded database intact. Last error: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--max-pages", type=int, default=2000, help="Maximum articles to sync (default: 2000)")
    parser.add_argument("--delay", type=float, default=0.6, help="Seconds between API requests (default: 0.6)")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    con = make_db(args.db)
    continuation = None
    page_ids: list[int] = []
    print(f"Collecting page IDs from community wiki APIs (first try: {API_URL}); seeded knowledge is preserved if all sources fail …")
    while len(page_ids) < args.max_pages:
        params = {"action": "query", "list": "allpages", "aplimit": "max", "format": "json", "apfilterredir": "nonredirects"}
        if continuation:
            params.update(continuation)
        data = fetch_json(session, params)
        page_ids.extend(int(p["pageid"]) for p in data.get("query", {}).get("allpages", []))
        continuation = data.get("continue")
        print(f"Found {min(len(page_ids), args.max_pages)} page IDs…")
        if not continuation:
            break
        time.sleep(args.delay)
    page_ids = page_ids[:args.max_pages]
    if not page_ids:
        raise RuntimeError("The wiki API returned no pages. Check the API URL or try again later.")

    synced = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for offset in range(0, len(page_ids), 20):
        batch = page_ids[offset:offset + 20]
        data = fetch_json(session, {
            "action": "query", "pageids": "|".join(map(str, batch)),
            "prop": "extracts|info", "explaintext": 1, "exsectionformat": "plain",
            "inprop": "url", "format": "json",
        })
        pages = data.get("query", {}).get("pages", {})
        records = []
        for page in pages.values():
            title = clean_text(page.get("title", ""))
            content = clean_text(BeautifulSoup(page.get("extract", ""), "html.parser").get_text(" ", strip=True))
            if not title or len(content) < 40:
                continue
            url = page.get("fullurl") or BASE_URL + quote(title.replace(" ", "_"), safe="()'!,.-_~")
            records.append((int(page["pageid"]), title, url, content, now))
        con.executemany("""INSERT INTO knowledge_documents(page_id,title,url,content,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET
            title=excluded.title,url=excluded.url,content=excluded.content,updated_at=excluded.updated_at""", records)
        con.commit()
        synced += len(records)
        print(f"Synced {min(offset + len(batch), len(page_ids))}/{len(page_ids)} IDs; {synced} article texts saved")
        time.sleep(args.delay)

    count = con.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
    con.close()
    print(f"Done. {synced} articles refreshed; {count} total articles in {Path(args.db).resolve()}")
    print("Run this script again periodically to refresh articles. Live stock stays in the bot's stock tracker.")

if __name__ == "__main__":
    main()
