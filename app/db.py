"""Storage layer. Uses Postgres when DATABASE_URL is set, else local SQLite."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("NET_OUTPUT_DB", "net_output.db")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    scale REAL NOT NULL,
    direction INTEGER NOT NULL CHECK (direction IN (1, -1)),
    created_at TIMESTAMP NOT NULL,
    reviewed_at TIMESTAMP NULL,
    reviewer_id TEXT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','unproven'))
);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id SERIAL PRIMARY KEY,
    person_id TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    scale DOUBLE PRECISION NOT NULL,
    direction INTEGER NOT NULL CHECK (direction IN (1, -1)),
    created_at TIMESTAMP NOT NULL,
    reviewed_at TIMESTAMP NULL,
    reviewer_id TEXT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','unproven'))
);
"""


STATUS_DDL = "TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','unproven'))"


def database_url():
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url or None


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


@contextmanager
def connect():
    """Yields (cursor, placeholder). Commits on success, always closes."""
    url = database_url()
    if url:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ph = "%s"
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        ph = "?"
    try:
        yield cur, ph
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _q(sql, ph):
    return sql.replace("?", ph)


_initialized = set()


def init():
    """Create the table if missing. Safe to call repeatedly."""
    key = database_url() or DB_PATH
    with connect() as (cur, ph):
        pg = ph == "%s"
        cur.execute(PG_SCHEMA if pg else SQLITE_SCHEMA)
        # Migrate tables created before the status column existed.
        if pg:
            cur.execute(f"ALTER TABLE entries ADD COLUMN IF NOT EXISTS status {STATUS_DDL}")
        else:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(entries)").fetchall()]
            if "status" not in cols:
                cur.execute(f"ALTER TABLE entries ADD COLUMN status {STATUS_DDL}")
        cur.execute("UPDATE entries SET status='approved' WHERE reviewed_at IS NOT NULL AND status='pending'")
    _initialized.add(key)


def _ensure():
    if (database_url() or DB_PATH) not in _initialized:
        init()


def insert_entry(person_id, action, source, scale, direction):
    _ensure()
    with connect() as (cur, ph):
        sql = "INSERT INTO entries (person_id, action, source, scale, direction, created_at) VALUES (?,?,?,?,?,?)"
        params = (person_id, action, source, scale, direction, now())
        if ph == "%s":
            cur.execute(_q(sql + " RETURNING id", ph), params)
            new_id = cur.fetchone()["id"]
        else:
            cur.execute(sql, params)
            new_id = cur.lastrowid
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (new_id,))
        return _row(cur.fetchone())


def get_entry(entry_id):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        return _row(cur.fetchone())


def approve_entry(entry_id, reviewer_id):
    return _review(entry_id, reviewer_id, "approved")


def mark_unproven(entry_id, reviewer_id):
    return _review(entry_id, reviewer_id, "unproven", ("pending", "approved"))


def _review(entry_id, reviewer_id, status, from_statuses=("pending",)):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(
            _q("UPDATE entries SET status=?, reviewed_at=?, reviewer_id=? WHERE id=? AND status IN ("
               + ",".join("?" * len(from_statuses)) + ")", ph),
            (status, now(), reviewer_id, entry_id, *from_statuses),
        )
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        return _row(cur.fetchone())


def queue():
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT * FROM entries WHERE status='pending' ORDER BY id")
        return [_row(r) for r in cur.fetchall()]


def person_entries(person_id):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("SELECT * FROM entries WHERE person_id=? ORDER BY id", ph), (person_id,))
        return [_row(r) for r in cur.fetchall()]
