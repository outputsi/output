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


CREDIT_BASES = ("direct", "sourced_share", "even_split", "organization")
# (column, sqlite ddl, postgres ddl) added by init() migration on both backends
CREDIT_COLS = [
    ("credit_share", "REAL DEFAULT 1.0 CHECK (credit_share IS NULL OR (credit_share > 0 AND credit_share <= 1))",
     "DOUBLE PRECISION DEFAULT 1.0 CHECK (credit_share IS NULL OR (credit_share > 0 AND credit_share <= 1))"),
    ("credit_basis", "TEXT DEFAULT 'direct' CHECK (credit_basis IS NULL OR credit_basis IN ('direct','sourced_share','even_split','organization'))",
     "TEXT DEFAULT 'direct' CHECK (credit_basis IS NULL OR credit_basis IN ('direct','sourced_share','even_split','organization'))"),
    ("credit_note", "TEXT NULL", "TEXT NULL"),
    ("is_estimate", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
    ("category", "TEXT DEFAULT 'people'", "TEXT DEFAULT 'people'"),
    ("unit", "TEXT DEFAULT 'people'", "TEXT DEFAULT 'people'"),
]

# Officially published conversion rates to the common unit (USD). (category, unit, usd_per_unit, source_url, note)
CONVERSIONS = [
    ("people", "lives", 14_200_000.0,
     "https://www.transportation.gov/office-policy/transportation-policy/revised-departmental-guidance-on-valuation-of-a-statistical-life-in-economic-analysis",
     "US DOT value of a statistical life, $14.2M (2025 base year). Applies only to deaths caused / lives saved."),
    ("climate", "tCO2e", 190.0,
     "https://www.epa.gov/system/files/documents/2023-12/epa%5Fscghg%5F2023%5Freport%5Ffinal.pdf",
     "US EPA 2023 social cost of CO2, $190/tonne (2020 emissions, 2% near-term discount, 2020 dollars)."),
    ("economy", "USD", 1.0, "", "Already USD; nominal dollars of the year stated, no inflation adjustment."),
    ("economy", "EUR-2019", 1.1194, "https://www.federalreserve.gov/releases/g5a/20200102/",
     "Federal Reserve G.5A annual average 2019: 1.1194 USD per euro."),
    ("economy", "EUR-2016", 1.1072, "https://www.federalreserve.gov/releases/g5a/20170104/",
     "Federal Reserve G.5A annual average 2016: 1.1072 USD per euro."),
    ("economy", "EUR-2020", 1.1410, "https://www.federalreserve.gov/releases/g5a/20210104/",
     "Federal Reserve G.5A annual average 2020: 1.1410 USD per euro."),
    ("economy", "EUR-2004", 1.2438, "https://www.federalreserve.gov/releases/g5a/20050103/",
     "Federal Reserve G.5A annual average 2004: 1.2438 USD per euro."),
    ("economy", "EUR-2008", 1.4726, "https://www.federalreserve.gov/releases/g5a/20090102/",
     "Federal Reserve G.5A annual average 2008: 1.4726 USD per euro."),
    ("economy", "EUR-2013", 1.3281, "https://www.federalreserve.gov/releases/g5a/20150102/",
     "Federal Reserve G.5A annual average 2013: 1.3281 USD per euro."),
    ("economy", "EUR-2021", 1.183, "https://www.federalreserve.gov/releases/g5a/20220103/",
     "Federal Reserve G.5A annual average 2021: 1.183 USD per euro."),
    ("economy", "EUR-2023", 1.0817, "https://www.federalreserve.gov/releases/g5a/20250106/",
     "Federal Reserve G.5A annual average 2023: 1.0817 USD per euro."),
    ("economy", "EUR-2024", 1.082, "https://www.federalreserve.gov/releases/g5a/20250106/",
     "Federal Reserve G.5A annual average 2024: 1.082 USD per euro."),
    ("economy", "EUR-2025", 1.1306, "https://www.federalreserve.gov/releases/g5a/20260105/",
     "Federal Reserve G.5A annual average 2025: 1.1306 USD per euro."),
]


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


PEOPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    person_id TEXT PRIMARY KEY,
    image_url TEXT NULL
);
"""

_initialized = set()


def init():
    """Create the table if missing. Safe to call repeatedly."""
    key = database_url() or DB_PATH
    with connect() as (cur, ph):
        pg = ph == "%s"
        cur.execute(PG_SCHEMA if pg else SQLITE_SCHEMA)
        cur.execute(PEOPLE_SCHEMA)
        # Optional photo credit columns on people.
        for name in ("image_credit", "image_license_url"):
            if pg:
                cur.execute(f"ALTER TABLE people ADD COLUMN IF NOT EXISTS {name} TEXT NULL")
            elif name not in [r[1] for r in cur.execute("PRAGMA table_info(people)").fetchall()]:
                cur.execute(f"ALTER TABLE people ADD COLUMN {name} TEXT NULL")
        # Migrate tables created before the status column existed.
        if pg:
            cur.execute(f"ALTER TABLE entries ADD COLUMN IF NOT EXISTS status {STATUS_DDL}")
        else:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(entries)").fetchall()]
            if "status" not in cols:
                cur.execute(f"ALTER TABLE entries ADD COLUMN status {STATUS_DDL}")
        cur.execute("UPDATE entries SET status='approved' WHERE reviewed_at IS NOT NULL AND status='pending'")
        # Credit columns (nullable, defaults keep existing scores unchanged).
        if pg:
            for name, _, pg_ddl in CREDIT_COLS:
                cur.execute(f"ALTER TABLE entries ADD COLUMN IF NOT EXISTS {name} {pg_ddl}")
        else:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(entries)").fetchall()]
            for name, lite_ddl, _ in CREDIT_COLS:
                if name not in cols:
                    cur.execute(f"ALTER TABLE entries ADD COLUMN {name} {lite_ddl}")
        if pg:  # allow the 'organization' credit basis on existing tables
            cur.execute("ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_credit_basis_check")
            cur.execute("ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_credit_basis_chk")
            cur.execute("ALTER TABLE entries ADD CONSTRAINT entries_credit_basis_chk CHECK (credit_basis IS NULL OR "
                        "credit_basis IN ('direct','sourced_share','even_split','organization'))")
        cur.execute("UPDATE entries SET credit_share=1.0 WHERE credit_share IS NULL")
        cur.execute("UPDATE entries SET credit_basis='direct' WHERE credit_basis IS NULL")
        cur.execute("UPDATE entries SET is_estimate=0 WHERE is_estimate IS NULL")
        cur.execute("UPDATE entries SET category='people' WHERE category IS NULL")
        cur.execute("UPDATE entries SET unit='people' WHERE unit IS NULL")
        cur.execute("CREATE TABLE IF NOT EXISTS conversions (category TEXT NOT NULL, unit TEXT NOT NULL, "
                    "usd_per_unit DOUBLE PRECISION NOT NULL, source_url TEXT, note TEXT, PRIMARY KEY (category, unit))"
                    if pg else
                    "CREATE TABLE IF NOT EXISTS conversions (category TEXT NOT NULL, unit TEXT NOT NULL, "
                    "usd_per_unit REAL NOT NULL, source_url TEXT, note TEXT, PRIMARY KEY (category, unit))")
        for row in CONVERSIONS:
            cur.execute(_q("DELETE FROM conversions WHERE category=? AND unit=?", ph), row[:2])
            cur.execute(_q("INSERT INTO conversions (category, unit, usd_per_unit, source_url, note) VALUES (?,?,?,?,?)", ph), row)
        # Additional sources per entry.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS entry_sources ("
            + ("id SERIAL PRIMARY KEY" if pg else "id INTEGER PRIMARY KEY AUTOINCREMENT")
            + ", entry_id INTEGER NOT NULL, url TEXT NOT NULL, quote TEXT NULL, "
            "added_by TEXT NULL, added_at TEXT NOT NULL)")
    _initialized.add(key)


def _ensure():
    if (database_url() or DB_PATH) not in _initialized:
        init()


def insert_entry(person_id, action, source, scale, direction,
                 credit_share=1.0, credit_basis="direct", credit_note=None, is_estimate=False,
                 category="people", unit="people"):
    _ensure()
    with connect() as (cur, ph):
        sql = ("INSERT INTO entries (person_id, action, source, scale, direction, created_at, "
               "credit_share, credit_basis, credit_note, is_estimate, category, unit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        params = (person_id, action, source, scale, direction, now(),
                  credit_share, credit_basis, credit_note, int(bool(is_estimate)), category, unit)
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


def reapprove(entry_id, reviewer_id):
    return _review(entry_id, reviewer_id, "approved", ("unproven",))


def approve_unproven(entry_id, reviewer_id):
    return _review(entry_id, reviewer_id, "approved", ("unproven",))


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


def all_entries():
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT * FROM entries ORDER BY id")
        return [_row(r) for r in cur.fetchall()]


def people_images():
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT person_id, image_url FROM people")
        return {r["person_id"]: r["image_url"] for r in cur.fetchall()}


def get_person(person_id):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("SELECT person_id, image_url, image_credit, image_license_url FROM people WHERE person_id=?", ph),
                    (person_id,))
        r = cur.fetchone()
        return _row(r) if r else None


def set_person_image(person_id, image_url, image_credit=None, image_license_url=None):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(
            _q("INSERT INTO people (person_id, image_url, image_credit, image_license_url) VALUES (?, ?, ?, ?) "
               "ON CONFLICT (person_id) DO UPDATE SET image_url = excluded.image_url, "
               "image_credit = excluded.image_credit, image_license_url = excluded.image_license_url", ph),
            (person_id, image_url, image_credit, image_license_url),
        )
        cur.execute(_q("SELECT person_id, image_url, image_credit, image_license_url FROM people WHERE person_id=?", ph),
                    (person_id,))
        return _row(cur.fetchone())


def update_credit(entry_id, credit_share, credit_basis, credit_note, is_estimate, reviewer_id):
    """Update credit fields on an existing entry; records who changed it in reviewer_id."""
    _ensure()
    with connect() as (cur, ph):
        cur.execute(
            _q("UPDATE entries SET credit_share=?, credit_basis=?, credit_note=?, is_estimate=?, reviewer_id=? WHERE id=?", ph),
            (credit_share, credit_basis, credit_note, int(bool(is_estimate)), reviewer_id, entry_id),
        )
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        return _row(cur.fetchone())


def add_entry_source(entry_id, url, quote, added_by, make_primary=False):
    """Attach an extra source; with make_primary, the URL replaces entries.source instead."""
    _ensure()
    with connect() as (cur, ph):
        if make_primary:
            cur.execute(_q("UPDATE entries SET source=? WHERE id=?", ph), (url, entry_id))
        else:
            cur.execute(_q("INSERT INTO entry_sources (entry_id, url, quote, added_by, added_at) VALUES (?,?,?,?,?)", ph),
                        (entry_id, url, quote, added_by, now()))
    return get_entry(entry_id)


def entry_sources(entry_ids=None):
    """Map entry_id -> list of extra sources."""
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT id, entry_id, url, quote, added_by, added_at FROM entry_sources ORDER BY id")
        out = {}
        for r in cur.fetchall():
            r = _row(r)
            if entry_ids is None or r["entry_id"] in entry_ids:
                out.setdefault(r["entry_id"], []).append(r)
        return out


def conversions():
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT * FROM conversions ORDER BY category, unit")
        return [_row(r) for r in cur.fetchall()]


def set_category(entry_id, category, unit, reviewer_id):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("UPDATE entries SET category=?, unit=?, reviewer_id=? WHERE id=?", ph),
                    (category, unit, reviewer_id, entry_id))
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        return _row(cur.fetchone())
