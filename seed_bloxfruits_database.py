#!/usr/bin/env python3
"""Populate the local SQLite knowledge DB with curated Blox Fruits starter knowledge."""
import json, os, sqlite3
from pathlib import Path

def seed_database(db_path=None):
    db_path = db_path or os.environ.get("BLOXFRUITS_KNOWLEDGE_DB", "bloxfruits_knowledge.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    seed_path = Path(__file__).with_name("bloxfruits_seed.json")
    entries = json.loads(seed_path.read_text(encoding="utf-8"))
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS knowledge_documents (page_id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_title ON knowledge_documents(title)")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Seed records use negative IDs; remove the previous bundled seed set first.
    # This prevents obsolete/fabricated records from an older package surviving on Render's disk.
    con.execute("DELETE FROM knowledge_documents WHERE page_id < 0")
    for i, item in enumerate(entries, start=1):
        con.execute("INSERT INTO knowledge_documents(page_id,title,url,content,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET title=excluded.title,url=excluded.url,content=excluded.content,updated_at=excluded.updated_at", (-i, item["title"], item["url"], item["content"], now))
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
    con.close()
    return len(entries), count

if __name__ == "__main__":
    added, total = seed_database()
    print(f"Curated Blox Fruits starter knowledge loaded: {added} topics; {total} total database documents.")
