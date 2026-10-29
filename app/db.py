"""Storage layer. Uses Postgres when DATABASE_URL is set, else local SQLite."""
import contextvars
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
    ("economy", "EUR-2017", 1.1301, "https://www.federalreserve.gov/releases/g5a/20190102/",
     "Federal Reserve G.5A annual average 2017: 1.1301 USD per euro."),
    ("economy", "EUR-2018", 1.1817, "https://www.federalreserve.gov/releases/g5a/20190102/",
     "Federal Reserve G.5A annual average 2018: 1.1817 USD per euro."),
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
    ("economy", "EUR-2022", 1.0534, "https://www.federalreserve.gov/releases/g5a/20250106/",
     "Federal Reserve G.5A annual average 2022: 1.0534 USD per euro."),
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


# A fresh Neon connection is about half a second. Pages used to open one per query.
_pools = {}
_pool_lock = threading.Lock()
_request_reads = contextvars.ContextVar("output_request_reads", default=None)


def begin_request_cache():
    """Share bulk reads for the rest of this request. Returns a reset token."""
    return _request_reads.set({})


def end_request_cache(token):
    _request_reads.reset(token)


def _invalidate_reads():
    cache = _request_reads.get()
    if cache is not None:
        cache.clear()


def _pg_pool(url):
    with _pool_lock:
        pool = _pools.get(url)
        if pool is None:
            from psycopg2.pool import ThreadedConnectionPool
            pool = ThreadedConnectionPool(1, 12, url, keepalives=1, keepalives_idle=30,
                                          keepalives_interval=10, keepalives_count=5)
            _pools[url] = pool
        return pool


def _pg_checkout(pool):
    import psycopg2
    for _ in range(2):
        conn = pool.getconn()
        if not conn.closed:
            return conn
        pool.putconn(conn, close=True)
    raise psycopg2.OperationalError("postgres connection is closed")


@contextmanager
def connect():
    """Yields (cursor, placeholder). Commits on success. Postgres connections go back to the pool."""
    url = database_url()
    if url:
        import psycopg2.extras
        pool = _pg_pool(url)
        conn = _pg_checkout(pool)
        broken = False
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur, "%s"
                conn.commit()
            except Exception as exc:
                # A server that has closed an idle socket often leaves conn.closed == 0
                # until the next command. Don't put that connection back in the pool.
                import psycopg2
                broken = bool(conn.closed) or isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
                if not broken:
                    try:
                        conn.rollback()
                    except Exception:
                        broken = True
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    broken = True
        finally:
            pool.putconn(conn, close=broken)
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        yield cur, "?"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _snapshot():
    """Load the bulk tables once per request, in one transaction. False outside a request."""
    cache = _request_reads.get()
    if cache is None:
        return False
    if cache.get("_snap"):
        return True
    people = entries = sources = conversions = None
    for attempt in (1, 2):
        try:
            with connect() as (cur, ph):
                cur.execute("SELECT person_id, image_url, image_credit, image_license_url, display_name, published FROM people")
                people = [_row(r) for r in cur.fetchall()]
                cur.execute("SELECT * FROM entries ORDER BY id")
                entries = [_row(r) for r in cur.fetchall()]
                cur.execute("SELECT id, entry_id, url, quote, added_by, added_at FROM entry_sources ORDER BY id")
                sources = {}
                for r in cur.fetchall():
                    r = _row(r)
                    sources.setdefault(r["entry_id"], []).append(r)
                cur.execute("SELECT * FROM conversions ORDER BY category, unit")
                conversions = [_row(r) for r in cur.fetchall()]
            break
        except Exception as exc:
            # Reads only. One retry covers a pooled connection Neon already closed.
            import psycopg2
            if attempt == 2 or not isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                raise
    cache["people"] = people
    cache["all_entries"] = entries
    cache["entry_sources"] = sources
    cache["conversions"] = conversions
    cache["_snap"] = True
    return True


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
        for name in ("image_credit", "image_license_url", "display_name"):
            if pg:
                cur.execute(f"ALTER TABLE people ADD COLUMN IF NOT EXISTS {name} TEXT NULL")
            elif name not in [r[1] for r in cur.execute("PRAGMA table_info(people)").fetchall()]:
                cur.execute(f"ALTER TABLE people ADD COLUMN {name} TEXT NULL")
        # People 'published' flag. First time it is added, mark everyone who already exists as published.
        if pg:
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='people' AND column_name='published'")
            has_pub = cur.fetchone() is not None
        else:
            has_pub = "published" in [r[1] for r in cur.execute("PRAGMA table_info(people)").fetchall()]
        if not has_pub:
            cur.execute("ALTER TABLE people ADD COLUMN published INTEGER NOT NULL DEFAULT 0")
            cur.execute("INSERT INTO people (person_id) SELECT DISTINCT person_id FROM entries WHERE true "
                        "ON CONFLICT (person_id) DO NOTHING")
            cur.execute("UPDATE people SET published=1")
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
        # Old server-side totals. Left in place; the stats page no longer reads them.
        cur.execute("CREATE TABLE IF NOT EXISTS pageviews (day TEXT NOT NULL, path TEXT NOT NULL, "
                    "referrer TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '', views INTEGER NOT NULL DEFAULT 0, "
                    "PRIMARY KEY (day, path, referrer, country))")
        # Beacon visits. ip_hash is a daily-salted hash, never the address. counted=0 is an audit row.
        cur.execute("CREATE TABLE IF NOT EXISTS visit_log (day TEXT NOT NULL, path TEXT NOT NULL, "
                    "referrer TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '', "
                    "ip_hash TEXT NOT NULL DEFAULT '', ua_class TEXT NOT NULL DEFAULT 'browser', "
                    "counted INTEGER NOT NULL DEFAULT 1, views INTEGER NOT NULL DEFAULT 0, "
                    "PRIMARY KEY (day, path, referrer, country, ip_hash, ua_class, counted))")
        cur.execute("CREATE TABLE IF NOT EXISTS feedback ("
                    + ("id SERIAL PRIMARY KEY" if pg else "id INTEGER PRIMARY KEY AUTOINCREMENT")
                    + ", created_at TEXT NOT NULL, message TEXT NOT NULL, email TEXT NULL, page TEXT NULL)")
        _apply_entry_corrections(cur, ph)
    _initialized.add(key)


# GM's FY2025 Form 10-K states approximately 88,000 hourly and 68,000 salaried employees
# at 31 Dec 2025. 88,000 + 68,000 = 156,000. The same filing's table, in thousands, shows
# Total Worldwide 155. Count the hourly + salaried sum on that one approved jobs entry.
_BARRA_HEADCOUNT_SOURCE = "%gm-20251231%"
_BARRA_OLD_SCALE = 155000
_BARRA_NEW_SCALE = 156000
_BARRA_ACTION = (
    "General Motors headcount 156,000 worldwide at 31 Dec 2025 "
    "(Form 10-K FY2025: approximately 88,000 hourly and 68,000 salaried employees). "
    "The filing's table in thousands shows Total Worldwide 155."
)
_BARRA_NOTE = (
    "SEC filing rule. Counted 88,000 hourly + 68,000 salaried = 156,000 from the same 10-K. "
    "Table in thousands shows Total Worldwide 155. Organization credited alone; "
    "Barra chair and CEO (chair since 2016), same 10-K. Headcount at one date."
)


def _apply_entry_corrections(cur, ph):
    cur.execute(
        _q("UPDATE entries SET scale=?, action=?, credit_note=? "
           "WHERE person_id=? AND category=? AND unit=? AND direction=1 AND status='approved' "
           "AND scale=? AND source LIKE ?", ph),
        (_BARRA_NEW_SCALE, _BARRA_ACTION, _BARRA_NOTE,
         "mary-barra", "jobs", "jobs", _BARRA_OLD_SCALE, _BARRA_HEADCOUNT_SOURCE),
    )


def add_visits(counts):
    """counts: {(day, path, referrer, country, ip_hash, ua_class, counted): n}."""
    if not counts:
        return
    _ensure()
    with connect() as (cur, ph):
        sql = _q("INSERT INTO visit_log (day, path, referrer, country, ip_hash, ua_class, counted, views) "
                 "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (day, path, referrer, country, ip_hash, ua_class, counted) "
                 "DO UPDATE SET views = visit_log.views + excluded.views", ph)
        for (day, path, ref, country, ip_hash, ua_class, counted), n in counts.items():
            cur.execute(sql, (day, path[:200], ref[:100], country[:2], ip_hash[:32], ua_class[:20], int(counted), int(n)))
        _invalidate_reads()


def add_pageviews(counts):
    """Legacy {(day, path, referrer, country): n}, stored as counted browser visits with no hash."""
    add_visits({(*k, "", "browser", 1): n for k, n in counts.items()})


def pageview_stats(days=30):
    """Counted (real) visits for the admin page, plus how many were dropped and why."""
    from .visitors import eastern_day
    _ensure()
    today = datetime.fromisoformat(eastern_day())
    since = (today - timedelta(days=days)).date().isoformat()
    out = {}
    with connect() as (cur, ph):
        for name, sql in (
                ("by_day", "SELECT day AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND day >= ? "
                           "GROUP BY day ORDER BY day DESC"),
                ("pages", "SELECT path AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND day >= ? "
                          "AND path NOT LIKE ? GROUP BY path ORDER BY n DESC LIMIT 30"),
                ("people", "SELECT path AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND day >= ? "
                           "AND path LIKE ? GROUP BY path ORDER BY n DESC LIMIT 30"),
                ("referrers", "SELECT referrer AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND day >= ? "
                              "AND referrer <> '' GROUP BY referrer ORDER BY n DESC LIMIT 30"),
                ("countries", "SELECT country AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND day >= ? "
                              "AND country <> '' GROUP BY country ORDER BY n DESC LIMIT 30"),
                ("uniques_by_day", "SELECT day AS k, COUNT(DISTINCT ip_hash) AS n FROM visit_log "
                                   "WHERE counted=1 AND ip_hash <> '' AND day >= ? GROUP BY day ORDER BY day DESC"),
                ("dropped", "SELECT ua_class AS k, SUM(views) AS n FROM visit_log WHERE counted=0 AND day >= ? "
                            "GROUP BY ua_class ORDER BY n DESC"),
                ("hashes", "SELECT ip_hash AS k, SUM(views) AS n FROM visit_log WHERE counted=1 AND ip_hash <> '' "
                           "AND day >= ? GROUP BY ip_hash ORDER BY n DESC LIMIT 30")):
            cur.execute(_q(sql, ph), (since, "/people/%") if "LIKE ?" in sql else (since,))
            out[name] = [(r["k"], int(r["n"])) for r in map(_row, cur.fetchall())]
    return out


def add_feedback(message, email, page):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("INSERT INTO feedback (created_at, message, email, page) VALUES (?,?,?,?)", ph),
                    (now(), message, email, page))
        _invalidate_reads()


def list_feedback(limit=200):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("SELECT id, created_at, message, email, page FROM feedback ORDER BY id DESC LIMIT ?", ph), (limit,))
        return [_row(r) for r in cur.fetchall()]


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
        row = _row(cur.fetchone())
        _invalidate_reads()
        return row


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
        row = _row(cur.fetchone())
        _invalidate_reads()
        return row


def queue():
    _ensure()
    with connect() as (cur, ph):
        cur.execute("SELECT * FROM entries WHERE status='pending' ORDER BY id")
        return [_row(r) for r in cur.fetchall()]


def _people_rows():
    _ensure()
    if _snapshot():
        return _request_reads.get()["people"]
    with connect() as (cur, ph):
        cur.execute("SELECT person_id, image_url, image_credit, image_license_url, display_name, published FROM people")
        return [_row(r) for r in cur.fetchall()]


def person_entries(person_id):
    _ensure()
    if _snapshot():
        return [dict(r) for r in _request_reads.get()["all_entries"] if r["person_id"] == person_id]
    with connect() as (cur, ph):
        cur.execute(_q("SELECT * FROM entries WHERE person_id=? ORDER BY id", ph), (person_id,))
        return [_row(r) for r in cur.fetchall()]


def all_entries():
    _ensure()
    if _snapshot():
        rows = _request_reads.get()["all_entries"]
    else:
        with connect() as (cur, ph):
            cur.execute("SELECT * FROM entries ORDER BY id")
            rows = [_row(r) for r in cur.fetchall()]
    return [dict(r) for r in rows]


def people_images():
    return {r["person_id"]: r["image_url"] for r in _people_rows()}


def display_names():
    return {r["person_id"]: r["display_name"] for r in _people_rows() if r.get("display_name") is not None}


def set_display_name(person_id, display_name):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("INSERT INTO people (person_id, display_name) VALUES (?, ?) "
                       "ON CONFLICT (person_id) DO UPDATE SET display_name = excluded.display_name", ph),
                    (person_id, display_name))
        _invalidate_reads()
        return {"person_id": person_id, "display_name": display_name}


def published_ids():
    return {r["person_id"] for r in _people_rows() if r.get("published") == 1}


def set_published(person_id, published):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("INSERT INTO people (person_id, published) VALUES (?, ?) "
                       "ON CONFLICT (person_id) DO UPDATE SET published = excluded.published", ph),
                    (person_id, int(bool(published))))
        _invalidate_reads()
        return {"person_id": person_id, "published": bool(published)}


def get_person(person_id):
    for r in _people_rows():
        if r["person_id"] == person_id:
            return {"person_id": r["person_id"], "image_url": r["image_url"],
                    "image_credit": r["image_credit"], "image_license_url": r["image_license_url"]}
    return None


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
        row = _row(cur.fetchone())
        _invalidate_reads()
        return row


def update_credit(entry_id, credit_share, credit_basis, credit_note, is_estimate, reviewer_id):
    """Update credit fields on an existing entry; records who changed it in reviewer_id."""
    _ensure()
    with connect() as (cur, ph):
        cur.execute(
            _q("UPDATE entries SET credit_share=?, credit_basis=?, credit_note=?, is_estimate=?, reviewer_id=? WHERE id=?", ph),
            (credit_share, credit_basis, credit_note, int(bool(is_estimate)), reviewer_id, entry_id),
        )
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        row = _row(cur.fetchone())
        _invalidate_reads()
        return row


def add_entry_source(entry_id, url, quote, added_by, make_primary=False):
    """Attach an extra source; with make_primary, the URL replaces entries.source instead."""
    _ensure()
    with connect() as (cur, ph):
        if make_primary:
            cur.execute(_q("UPDATE entries SET source=? WHERE id=?", ph), (url, entry_id))
        else:
            cur.execute(_q("INSERT INTO entry_sources (entry_id, url, quote, added_by, added_at) VALUES (?,?,?,?,?)", ph),
                        (entry_id, url, quote, added_by, now()))
        _invalidate_reads()
    return get_entry(entry_id)


def set_source_quote(entry_id, source_id, quote):
    """Correct the quote of one extra source; returns False if it does not belong to the entry."""
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("UPDATE entry_sources SET quote=? WHERE id=? AND entry_id=?", ph), (quote, source_id, entry_id))
        ok = cur.rowcount > 0
        _invalidate_reads()
        return ok


def remove_source(entry_id, source_id):
    """Remove one extra source from an entry; False if it does not belong to the entry."""
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("DELETE FROM entry_sources WHERE id=? AND entry_id=?", ph), (source_id, entry_id))
        ok = cur.rowcount > 0
        _invalidate_reads()
        return ok


def entry_sources(entry_ids=None):
    """Map entry_id -> list of extra sources."""
    _ensure()
    if _snapshot():
        full = _request_reads.get()["entry_sources"]
    else:
        with connect() as (cur, ph):
            cur.execute("SELECT id, entry_id, url, quote, added_by, added_at FROM entry_sources ORDER BY id")
            full = {}
            for r in cur.fetchall():
                r = _row(r)
                full.setdefault(r["entry_id"], []).append(r)
    return {eid: [dict(s) for s in srcs] for eid, srcs in full.items()
            if entry_ids is None or eid in entry_ids}


def conversions():
    _ensure()
    if _snapshot():
        rows = _request_reads.get()["conversions"]
    else:
        with connect() as (cur, ph):
            cur.execute("SELECT * FROM conversions ORDER BY category, unit")
            rows = [_row(r) for r in cur.fetchall()]
    return [dict(r) for r in rows]


def set_category(entry_id, category, unit, reviewer_id):
    _ensure()
    with connect() as (cur, ph):
        cur.execute(_q("UPDATE entries SET category=?, unit=?, reviewer_id=? WHERE id=?", ph),
                    (category, unit, reviewer_id, entry_id))
        cur.execute(_q("SELECT * FROM entries WHERE id=?", ph), (entry_id,))
        row = _row(cur.fetchone())
        _invalidate_reads()
        return row
