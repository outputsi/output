import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("NET_OUTPUT_DB", "net_output.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    scale REAL NOT NULL,
    direction INTEGER NOT NULL CHECK (direction IN (1, -1)),
    created_at TIMESTAMP NOT NULL,
    reviewed_at TIMESTAMP NULL,
    reviewer_id TEXT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def insert_entry(person_id, action, source, scale, direction):
    with connect() as c:
        cur = c.execute(
            "INSERT INTO entries (person_id, action, source, scale, direction, created_at) VALUES (?,?,?,?,?,?)",
            (person_id, action, source, scale, direction, now()),
        )
        return get_entry(cur.lastrowid, c)


def get_entry(entry_id, c=None):
    c = c or connect()
    r = c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    return dict(r) if r else None


def approve_entry(entry_id, reviewer_id):
    with connect() as c:
        c.execute(
            "UPDATE entries SET reviewed_at=?, reviewer_id=? WHERE id=? AND reviewed_at IS NULL",
            (now(), reviewer_id, entry_id),
        )
        return get_entry(entry_id, c)


def queue():
    c = connect()
    return [dict(r) for r in c.execute("SELECT * FROM entries WHERE reviewed_at IS NULL ORDER BY id")]


def person_entries(person_id):
    c = connect()
    return [dict(r) for r in c.execute("SELECT * FROM entries WHERE person_id=? ORDER BY id", (person_id,))]
