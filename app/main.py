import csv
import io
import json
import os
import secrets
import threading
import time
from urllib.parse import quote, urlsplit
from html import escape
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from pydantic import BaseModel, Field, ValidationError

from . import cards, db
from .scoring import by_category, credit_label, effective_scale, fmt_pct, overall, score

UNITS = {"people": ("people", "lives"), "jobs": ("jobs",), "climate": ("tCO2e",),
         "nature": ("animals", "hectares"), "economy": "USD or CUR-YYYY", "tech": None,
         "culture": ("certified-units", "grammys", "academy-awards", "guinness-records")}


def _check_unit(category, unit):
    allowed = UNITS[category]
    if category == "economy":
        if unit != "USD" and (category, unit) not in rates():
            raise HTTPException(422, "economy unit must be USD or a currency-year with an official rate, e.g. EUR-2019")
        return
    if allowed is not None and unit not in allowed:
        raise HTTPException(422, f"unit for {category} must be one of {allowed}")


def rates():
    return {(c["category"], c["unit"]): float(c["usd_per_unit"]) for c in db.conversions()}

app = FastAPI(title="Output", docs_url=None, redoc_url=None, openapi_url=None)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Writes (except submitting pending entries) and ?preview=1 need the admin key when ADMIN_KEY is set.
OPEN_POSTS = {"/api/entries", "/submit", "/import"}


def _key(request):
    return request.headers.get("x-admin-key") or request.query_params.get("admin_key") or ""


@app.middleware("http")
async def admin_guard(request: Request, call_next):
    want = os.environ.get("ADMIN_KEY")
    needs = (request.method == "POST" and request.url.path not in OPEN_POSTS) or \
        request.query_params.get("preview", "").lower() in ("1", "true")
    if want and needs and not secrets.compare_digest(_key(request), want):
        return JSONResponse({"detail": "admin key required"}, status_code=401)
    return await call_next(request)


def _admin_ok(request):
    want = os.environ.get("ADMIN_KEY")
    return not want or secrets.compare_digest(_key(request), want)


# ---------- rate limit, security headers, page cache ----------
RATE_LIMITED = {"/submit", "/import"}
RATE_MAX, RATE_WINDOW = 10, 3600
_rate = {}
_rate_lock = threading.Lock()


def _client_ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def rate_ok(ip, now=None):
    """Sliding window: at most RATE_MAX posts per RATE_WINDOW seconds per IP."""
    now = time.time() if now is None else now
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
        ok = len(hits) < RATE_MAX
        if ok:
            hits.append(now)
        _rate[ip] = hits
        return ok


SECURITY_HEADERS = {"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "strict-origin-when-cross-origin"}

# Cached public GET pages; any successful write (approve, reject, unproven, edit, publish, rename, image) clears it.
CACHE_TTL = 600  # safety net for writes made directly in the database
CACHE_PREFIXES = ("/people/", "/cards/", "/api/ranking", "/api/people/")
CACHE_EXACT = {"/", "/sitemap.xml"}
_cache = {}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _cacheable(request):
    path = request.url.path
    if request.method != "GET" or not (path in CACHE_EXACT or path.startswith(CACHE_PREFIXES)):
        return False
    q = request.query_params
    return not (q.get("preview") or q.get("admin_key") or request.headers.get("x-admin-key"))


@app.middleware("http")
async def cache_and_headers(request: Request, call_next):
    if request.method == "POST" and request.url.path in RATE_LIMITED and not (
            os.environ.get("ADMIN_KEY") and _admin_ok(request)) and not rate_ok(_client_ip(request)):
        resp = JSONResponse({"detail": "too many submissions, try again later"}, status_code=429,
                            headers={"Retry-After": str(RATE_WINDOW)})
    elif _cacheable(request):
        key = (db.database_url() or db.DB_PATH, request.headers.get("x-forwarded-proto", ""), str(request.url))
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            resp = Response(hit[1], status_code=hit[2], headers=hit[3])
        else:
            resp = await call_next(request)
            body = b"".join([c async for c in resp.body_iterator])
            headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
            if resp.status_code == 200:
                with _cache_lock:
                    if len(_cache) > 2000:
                        _cache.clear()
                    _cache[key] = (time.time(), body, 200, headers)
            resp = Response(body, status_code=resp.status_code, headers=headers)
    else:
        resp = await call_next(request)
        if request.method != "GET" and resp.status_code < 400:
            clear_cache()
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


@app.get("/openapi.json", include_in_schema=False)
def openapi_json(request: Request):
    if not _admin_ok(request):
        raise HTTPException(404, "Not Found")
    return JSONResponse(app.openapi())


def _docs_url(request):
    k = _key(request)
    return "/openapi.json" + (f"?admin_key={quote(k)}" if k else "")


@app.get("/docs", include_in_schema=False)
def docs(request: Request):
    if not _admin_ok(request):
        raise HTTPException(404, "Not Found")
    return get_swagger_ui_html(openapi_url=_docs_url(request), title="Output API")


@app.get("/redoc", include_in_schema=False)
def redoc(request: Request):
    if not _admin_ok(request):
        raise HTTPException(404, "Not Found")
    return get_redoc_html(openapi_url=_docs_url(request), title="Output API")


@app.on_event("startup")
def _startup():
    db.init()


class EntryIn(BaseModel):
    person_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    source: str = Field(min_length=1)
    scale: float = Field(ge=0)
    direction: Literal[1, -1]
    credit_share: float = Field(1.0, gt=0, le=1)
    credit_basis: Literal["direct", "sourced_share", "even_split", "organization"] = "direct"
    credit_note: str | None = None
    is_estimate: bool = False
    category: Literal["people", "jobs", "climate", "nature", "economy", "tech", "culture"] = "people"
    unit: str = Field("people", min_length=1)


class CategoryIn(BaseModel):
    category: Literal["people", "jobs", "climate", "nature", "economy", "tech", "culture"]
    unit: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)


class CreditIn(BaseModel):
    credit_share: float = Field(gt=0, le=1)
    credit_basis: Literal["direct", "sourced_share", "even_split", "organization"]
    credit_note: str | None = None
    is_estimate: bool = False
    reviewer_id: str = Field(min_length=1)


class ApproveIn(BaseModel):
    reviewer_id: str = Field(min_length=1)


def _decorate(e):
    e = dict(e)
    e["is_estimate"] = bool(e.get("is_estimate"))
    e["effective_scale"] = effective_scale(e)
    e["credit_label"] = credit_label(e)
    return e


def _with_sources(entries):
    extra = db.entry_sources({e["id"] for e in entries})
    for e in entries:
        e["extra_sources"] = extra.get(e["id"], [])
    return entries


def person_view(person_id, include_estimates=True):
    entries = _with_sources([_decorate(e) for e in db.person_entries(person_id)])
    approved = [e for e in entries if e["status"] == "approved"]
    counted = [e for e in approved if include_estimates or not e["is_estimate"]]
    estimates = [e for e in approved if e["is_estimate"] and not include_estimates]
    not_counted = [e for e in entries if e["status"] in ("unproven", "rejected")]
    people = [e for e in approved if (e.get("category") or "people") == "people"]
    cats = [{"category": c, "unit": u, **sc} for (c, u), sc in sorted(by_category(approved, include_estimates).items())]
    ov = overall(approved, rates(), include_estimates)
    ov["not_convertible"] = [f"{c} ({u})" for c, u in ov["not_convertible"]]
    other = other_lines(approved, rates(), include_estimates)
    return {"person_id": person_id, **score(people, include_estimates), "categories": cats,
            "overall": ov, "other": other, "entries": counted,
            "not_counted": not_counted, "estimates": estimates, "include_estimates": include_estimates}


def _check_pending(entry_id):
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    if e["status"] != "pending":
        raise HTTPException(409, f"entry is not pending (status={e['status']})")


def do_approve(entry_id, reviewer_id):
    _check_pending(entry_id)
    return db.approve_entry(entry_id, reviewer_id)


def do_mark_unproven(entry_id, reviewer_id):
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    if e["status"] not in ("pending", "approved"):
        raise HTTPException(409, f"entry cannot be marked unproven (status={e['status']})")
    return db.mark_unproven(entry_id, reviewer_id)


# ---------- API ----------
@app.post("/api/entries", status_code=201)
def api_submit(entry: EntryIn):
    _check_unit(entry.category, entry.unit)
    return db.insert_entry(**entry.model_dump())


@app.post("/api/entries/{entry_id}/category")
def api_category(entry_id: int, body: CategoryIn):
    if not db.get_entry(entry_id):
        raise HTTPException(404, "entry not found")
    _check_unit(body.category, body.unit)
    return db.set_category(entry_id, body.category, body.unit, body.reviewer_id)


@app.get("/api/conversions")
def api_conversions():
    return db.conversions()


@app.post("/api/entries/{entry_id}/approve")
def api_approve(entry_id: int, body: ApproveIn):
    return do_approve(entry_id, body.reviewer_id)


@app.post("/api/entries/{entry_id}/approve-estimate")
def api_approve_estimate(entry_id: int, body: ApproveIn):
    """Re-check: approve an unproven entry flagged is_estimate (estimate rule)."""
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    if e["status"] != "unproven" or not e["is_estimate"]:
        raise HTTPException(409, "only unproven estimate entries can be approved here")
    return db.approve_unproven(entry_id, body.reviewer_id)


@app.post("/api/entries/{entry_id}/reapprove")
def api_reapprove(entry_id: int, body: ApproveIn):
    """Re-check: approve an unproven entry that passes the current policy."""
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    if e["status"] != "unproven":
        raise HTTPException(409, "only unproven entries can be re-approved")
    return db.reapprove(entry_id, body.reviewer_id)


@app.post("/api/entries/{entry_id}/mark-unproven")
def api_mark_unproven(entry_id: int, body: ApproveIn):
    return do_mark_unproven(entry_id, body.reviewer_id)


@app.post("/api/entries/{entry_id}/credit")
def api_credit(entry_id: int, body: CreditIn):
    if not db.get_entry(entry_id):
        raise HTTPException(404, "entry not found")
    return db.update_credit(entry_id, body.credit_share, body.credit_basis, body.credit_note,
                            body.is_estimate, body.reviewer_id)


class SourceIn(BaseModel):
    url: str = Field(min_length=1)
    quote: str | None = None
    reviewer_id: str = Field(min_length=1)
    make_primary: bool = False


@app.post("/api/entries/{entry_id}/sources", status_code=201)
def api_add_source(entry_id: int, body: SourceIn):
    if not db.get_entry(entry_id):
        raise HTTPException(404, "entry not found")
    e = db.add_entry_source(entry_id, body.url.strip(), (body.quote or "").strip() or None,
                            body.reviewer_id, body.make_primary)
    return _with_sources([dict(e)])[0]


class QuoteIn(BaseModel):
    quote: str = Field(min_length=1)


@app.post("/api/entries/{entry_id}/sources/{source_id}/quote")
def api_set_source_quote(entry_id: int, source_id: int, body: QuoteIn):
    if not db.set_source_quote(entry_id, source_id, body.quote.strip()):
        raise HTTPException(404, "source not found")
    return api_list_sources(entry_id)


@app.post("/api/entries/{entry_id}/sources/{source_id}/remove")
def api_remove_source(entry_id: int, source_id: int):
    if not db.remove_source(entry_id, source_id):
        raise HTTPException(404, "source not found")
    return api_list_sources(entry_id)


@app.get("/api/entries/{entry_id}/sources")
def api_list_sources(entry_id: int):
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    return {"entry_id": entry_id, "source": e["source"], "extra_sources": db.entry_sources({entry_id}).get(entry_id, [])}


@app.get("/api/queue")
def api_queue():
    return _with_sources([dict(e) for e in db.queue()])


def public_ids():
    """Publicly visible people: published flag AND at least one approved (counted) entry."""
    pub = db.published_ids()
    return {e["person_id"] for e in db.all_entries() if e["status"] == "approved" and e["person_id"] in pub}


def _visible(person_id, preview):
    """Unpublished people are hidden (404) unless the reviewer preview flag is set."""
    if not preview and person_id not in public_ids():
        raise HTTPException(404, "person not published")


class PublishIn(BaseModel):
    published: bool


@app.post("/api/people/{person_id}/publish")
def api_publish(person_id: str, body: PublishIn):
    return db.set_published(person_id, body.published)


@app.get("/api/people/{person_id}")
def api_person(person_id: str, include_estimates: bool = True, preview: bool = False):
    _visible(person_id, preview)
    return person_view(person_id, include_estimates)


# ---------- Web UI ----------
ADSENSE_DEFAULT = "ca-pub-1880568133397191"
CONTACT_EMAIL = "musicamalonllc@gmail.com"


def adsense_client():
    c = (os.environ.get("ADSENSE_CLIENT") or ADSENSE_DEFAULT).strip()
    return c if c.startswith("ca-") else "ca-" + c


def adsense_head():
    c = escape(adsense_client())
    return (f'<meta name="google-adsense-account" content="{c}">'
            f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={c}" '
            'crossorigin="anonymous"></script>')


SITE_FOOTER = ('<footer class="site-footer"><a href="/">Rankings</a> · <a href="/about">About</a> · '
               '<a href="/privacy">Privacy</a></footer>')
SITE_NAME = "Output"
DEFAULT_DESC = ("Famous people ranked by the total measurable impact of their existence on Earth, good and bad. "
                "Every counted claim needs 2 independent, trusted sources.")
FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
               'fill="#222"/><circle cx="32" cy="32" r="17" fill="none" stroke="#fff" stroke-width="8"/></svg>')


def head_common(title, desc, canonical):
    """Viewport, title, description, canonical, favicon and AdSense: shared by every HTML page."""
    can = f'<link rel="canonical" href="{escape(canonical)}">' if canonical else ""
    return (f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><meta name="description" content="{escape(desc)}">{can}'
            '<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="icon" href="/favicon.ico" sizes="32x32">'
            f'{adsense_head()}')


def shell(title, desc, canonical, body, head="", extra_css=""):
    return HTMLResponse(f"""<!doctype html><html lang="en"><head>{head_common(title, desc, canonical)}{head}
<style>{HOME_CSS}{PAGE_CSS}{extra_css}</style></head>
<body><header class="top"><a class="brand" href="/">Output</a><nav><a href="/">Rankings</a><a href="/about">About</a></nav></header>
<main>{body}</main>{SITE_FOOTER}</body></html>""")


def page(title, body, head="", desc=DEFAULT_DESC, canonical=None, h1=None):
    return shell(f"{title} \u00b7 {SITE_NAME}", desc, canonical,
                 f"<article class='doc'><h1>{escape(h1 or title)}</h1>{body}</article>", head)


def sources_cell(e):
    out = link(e["source"])
    for x in e.get("extra_sources") or []:
        q = f" <small>“{escape(x['quote'])}”</small>" if x.get("quote") else ""
        out += f"<br><span class='extra-source'>+ {link(x['url'])}{q}</span>"
    return out


def link(src):
    s = escape(src)
    if src.startswith(("http://", "https://")):
        return f'<a href="{s}" target="_blank" rel="noopener">{s}</a>'
    return s


def short_link(src):
    """Source link labelled by its site, so long URLs don't break phone layouts."""
    if not src.startswith(("http://", "https://")):
        return escape(src)
    host = urlsplit(src).netloc.removeprefix("www.") or src
    return f'<a href="{escape(src)}" target="_blank" rel="noopener" title="{escape(src)}">{escape(host)}</a>'


def public_sources(e):
    out = short_link(e["source"])
    for x in e.get("extra_sources") or []:
        q = f" <q>{escape(x['quote'])}</q>" if x.get("quote") else ""
        out += f"<br><span class='extra-source'>+ {short_link(x['url'])}{q}</span>"
    return out


_BIG_NUM = ((1e15, " quadrillion"), (1e12, " trillion"), (1e9, " billion"), (1e6, " million"))


def num(v, sign=False):
    """Readable number: 14 · 9,237.5 · 120 million · 1.25 billion (never 1.2e+08)."""
    sg = ("+" if v > 0 else "") if sign else ""
    sg = "-" if v < 0 else sg
    a = abs(v)
    for div, suf in _BIG_NUM:
        if a >= div:
            x = a / div
            return f"{sg}{x:,.0f}{suf}" if x >= 100 else f"{sg}{x:.3g}{suf}"
    if a == int(a):
        return f"{sg}{a:,.0f}"
    return f"{sg}{a:,.2f}".rstrip("0").rstrip(".")


PHOTO_DIR = os.path.join(STATIC_DIR, "people")
try:
    with open(os.path.join(PHOTO_DIR, "manifest.json")) as _f:
        PHOTO_MANIFEST = json.load(_f)
except (OSError, ValueError):
    PHOTO_MANIFEST = {}


def local_photo_path(person_id, image_url):
    """Our mirrored copy of a photo, only while the DB still points at the same source image."""
    if image_url and PHOTO_MANIFEST.get(person_id) == image_url:
        path = os.path.join(PHOTO_DIR, f"{person_id}.jpg")
        if os.path.exists(path):
            return path
    return None


def photo_src(person_id, image_url):
    return f"/static/people/{quote(person_id)}.jpg" if local_photo_path(person_id, image_url) else image_url


class ImageIn(BaseModel):
    image_url: str | None = None
    image_credit: str | None = None
    image_license_url: str | None = None


class NameIn(BaseModel):
    display_name: str | None = None


@app.post("/api/people/{person_id}/name")
def api_person_name(person_id: str, body: NameIn):
    return db.set_display_name(person_id, (body.display_name or "").strip() or None)


def pretty(person_id, names=None):
    """Display name: admin override if set, else derived from the id."""
    names = db.display_names() if names is None else names
    if names.get(person_id):
        return names[person_id]
    return " ".join(w.capitalize() for w in person_id.replace("_", "-").split("-") if w)


def initials(person_id):
    words = pretty(person_id).split()
    return "".join(w[0] for w in words[:2]).upper() or "?"


OVERALL_MIN_TABS = 2
OVERALL_EXPLAIN = ("Overall averages each person's position across the category tabs: 100 = 1st on a tab, 0 = last. "
                   "Nothing is converted into money or into another category. Needs at least 2 tabs.")


def _tab_positions(ranked):
    """{person_id: (rank, of, percentile)}: 100 for 1st, 0 for last, linear; ties share a rank; one person = 100."""
    nets = [p["net"] for p in ranked]
    n = len(nets)
    out = {}
    for p in ranked:
        rank = 1 + sum(1 for x in nets if x > p["net"])
        out[p["person_id"]] = (rank, n, 100.0 if n == 1 else 100.0 * (n - rank) / (n - 1))
    return out


def ranking(include_estimates=True):
    """Overall = mean of a person's percentile positions across the category tabs (no unit conversion).
    Culture is one tab: its percentile is the mean over the culture sub-tabs the person appears on.
    Ranked people need OVERALL_MIN_TABS tabs; others follow with ranked=False and score None."""
    pub = db.published_ids()
    counted = {}
    for e in db.all_entries():
        if e["person_id"] in pub and e["status"] == "approved" and (include_estimates or not e.get("is_estimate")):
            counted.setdefault(e["person_id"], []).append(e)
    if not counted:
        return []
    names, images = db.display_names(), db.people_images()
    pos = {}  # pid -> [(tab label, rank, of, pct)]
    for key, label, _, _ in TABS:
        if key == "overall" or key in NEUTRAL_TABS:
            continue
        if key == "culture":
            sub = {}
            for u in culture_unit_keys():
                for pid, (rk, n, pc) in _tab_positions(tab_ranking("culture", include_estimates, u)).items():
                    sub.setdefault(pid, []).append((CULTURE_UNITS[u][1], rk, n, pc))
            for pid, items in sub.items():
                lbl = ", ".join(f"{l} #{rk}" for l, rk, _, _ in items)
                pos.setdefault(pid, []).append(("Culture", lbl, sum(x[3] for x in items) / len(items)))
            continue
        for pid, (rk, n, pc) in _tab_positions(tab_ranking(key, include_estimates)).items():
            pos.setdefault(pid, []).append((label.replace(" $", ""), f"#{rk}", pc))
    people = []
    for pid, es in counted.items():
        tabs = pos.get(pid, [])
        ranked = len(tabs) >= OVERALL_MIN_TABS
        score_ = sum(t[2] for t in tabs) / len(tabs) if tabs else None
        people.append({"person_id": pid, "name": pretty(pid, names), "ranked": ranked,
                       "score": round(score_, 1) if ranked else None,
                       "tabs": [{"tab": t[0], "position": t[1], "percentile": round(t[2], 1)} for t in tabs],
                       "positions": ", ".join(f"{t[0]} {t[1]}" if t[0] != "Culture" else f"{t[1]}" for t in tabs),
                       "tab_count": len(tabs), "counted_entries": len(es),
                       "estimated": any(e.get("is_estimate") for e in es), "image_url": images.get(pid)})
    people.sort(key=lambda p: (not p["ranked"], -(p["score"] or 0), -p["tab_count"], -p["counted_entries"],
                               p["name"].lower(), p["person_id"]))
    return people


@app.post("/api/people/{person_id}/image")
def api_person_image(person_id: str, body: ImageIn):
    clean = lambda v: (v or "").strip() or None
    return db.set_person_image(person_id, clean(body.image_url), clean(body.image_credit),
                               clean(body.image_license_url))


@app.post("/people-image")
def ui_person_image(person_id: str = Form(...), image_url: str = Form("")):
    db.set_person_image(person_id, image_url.strip() or None)
    return RedirectResponse("/", status_code=303)


HOME_CSS = """
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.tab{padding:6px 12px;border:1px solid #ccc;border-radius:16px;text-decoration:none;color:inherit}.tab.active{background:#222;color:#fff;border-color:#222}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#222;background:#fafafa}
.top{display:flex;align-items:center;justify-content:space-between;padding:16px 28px;border-bottom:1px solid #eee;background:#fff}
.top .brand{font-weight:600;font-size:1.1em;color:#222;text-decoration:none}
.top nav a{margin-left:20px;color:#555;text-decoration:none}
.top nav a:hover{color:#000}
main{max-width:1100px;margin:0 auto;padding:36px 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:32px 24px}
.card{display:flex;flex-direction:column;align-items:center;text-decoration:none;color:inherit;padding:12px;border-radius:12px;transition:background .15s,transform .15s}
.card:hover{background:#fff;transform:translateY(-2px);box-shadow:0 2px 10px rgba(0,0,0,.06)}
.avatar{width:112px;height:112px;border-radius:50%;object-fit:cover;background:#e4e4e7;display:flex;align-items:center;justify-content:center;font-size:2em;font-weight:600;color:#71717a}
.name{margin-top:12px;font-weight:500;text-align:center}
.badge{background:#fde68a;color:#78350f;border-radius:4px;padding:1px 6px;font-size:.75em;font-weight:500}
.other{font-size:.8em;font-weight:500;margin-top:2px}
.score{margin-top:4px;font-weight:600;font-variant-numeric:tabular-nums}
.net{margin-top:4px;color:#666;font-variant-numeric:tabular-nums}
.empty{color:#777}
.toggle{margin:0 0 24px;font-size:.9em}.toggle a{color:#555}
.pos{color:#1a8f4c;font-weight:600}.neg{color:#c93a3a;font-weight:600}.na{color:#999}
details{margin-top:48px;color:#555}
details input{padding:6px;margin:4px}
"""


def usd(v):
    sign = "-" if v < 0 else ("+" if v > 0 else "")
    v = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            return f"{sign}${x:,.0f}{suf}" if x >= 1000 else f"{sign}${x:.3g}{suf}"
    return f"{sign}${v:.3g}"


def other_lines(entries, r, include_estimates=True):
    """Counted totals of non-convertible categories, in their own units: [(sign, text)]."""
    out = []
    for (c, u), sc in sorted(by_category([e for e in entries if e.get("status") == "approved"], include_estimates).items()):
        if (c, u) in r:
            continue
        label = u if c == u or c == "people" else f"{c} {u}"
        if sc["positive_total"]:
            out.append(("+", f"+{sc['positive_total']:,.0f} {label}"))
        if sc["negative_total"]:
            out.append(("-", f"-{sc['negative_total']:,.0f} {label}"))
    return out


def other_html(lines):
    return "".join(f'<div class="other {"pos" if s == "+" else "neg"}">{escape(t)}</div>' for s, t in lines)


def share_label(p):
    if p["positive_pct"] is None:
        return '<span class="na">\u2014</span>'
    return (f'<span class="pos">{fmt_pct(p["positive_pct"])}</span> · '
            f'<span class="neg">{fmt_pct(p["negative_pct"])}</span>')


TABS = [("lives", "Lives", "people", "lives"), ("people", "People", "people", "people"), ("jobs", "Jobs", "jobs", None),
        ("economy", "Economy $", "economy", None), ("climate", "Climate", "climate", None),
        ("nature", "Nature", "nature", None),
        ("culture", "Culture", "culture", None), ("influence", "Influence", None, None), ("overall", "Overall", None, None)]
NEUTRAL_TABS = {"influence"}  # reach/attention: never positive/negative, never in Overall or the public rule

try:
    with open(os.path.join(os.path.dirname(__file__), "influence.json")) as _f:
        INFLUENCE = json.load(_f)
except (OSError, ValueError):
    INFLUENCE = {"people": {}}
INFLUENCE_UNITS = {"languages": ("languages", "Languages"), "attention": ("views", "Attention")}
INFLUENCE_DEFAULT = "languages"


def influence_explain(unit):
    if (unit or INFLUENCE_DEFAULT) == "languages":
        return ("Influence measures reach, not impact. Languages = the number of Wikipedia language editions with an "
                f"article about the person (Wikidata sitelinks, the original record, as of {INFLUENCE.get('languages_as_of', '')}; "
                "the measure MIT's Pantheon uses). Neutral and not part of Overall.")
    return ("Influence measures reach, not impact. Attention = English Wikipedia page views for "
            f"{INFLUENCE.get('period', '')} (Wikimedia Pageviews API, the original record). Neutral and not part of Overall.")


def influence_ranking(unit=None):
    """Public people by Wikipedia language editions (default) or 12-month English page views. Neutral."""
    unit = unit or INFLUENCE_DEFAULT
    field = "languages" if unit == "languages" else "views"
    names, images, pub = db.display_names(), db.people_images(), public_ids()
    out = [{"person_id": pid, "name": pretty(pid, names), "net": float(d[field]),
            "label": f"{num(d[field])} {'languages' if field == 'languages' else 'views'}", "lines": [],
            "estimated": False, "image_url": images.get(pid), "wikipedia_title": d["title"], "wikidata": d.get("wikidata")}
           for pid, d in INFLUENCE.get("people", {}).items() if pid in pub and d.get(field) is not None]
    out.sort(key=lambda p: (-p["net"], p["person_id"]))
    return out
DEFAULT_TAB = "lives"
CULTURE_RANK_UNITS = ("certified-units", "grammys")  # rank by certified units once any are approved, else Grammys
# Culture sub-toggles: ?tab=culture&unit=<key>; each ranks one unit only, never summed.
CULTURE_UNITS = {"certified": ("certified-units", "Certified units"), "grammys": ("grammys", "Grammys"),
                 "oscars": ("academy-awards", "Oscars"), "guinness": ("guinness-records", "Guinness records")}


def culture_unit_keys():
    """Sub-toggles to show: certified units only once any are counted."""
    pub = db.published_ids()
    has_cert = any(e["unit"] == "certified-units" and e["status"] == "approved" and e["person_id"] in pub
                   for e in db.all_entries())
    return [k for k in CULTURE_UNITS if k != "certified" or has_cert]


def _culture_unit(tab, unit):
    if unit is None:
        return None
    if tab == "influence" and unit in INFLUENCE_UNITS:
        return unit
    if tab != "culture" or unit not in CULTURE_UNITS:
        raise HTTPException(422, f"unit is only for tab=culture, one of {list(CULTURE_UNITS)}")
    return unit


TAB_DESC = {"lives": "lives saved and lost", "people": "people helped and harmed", "jobs": "jobs created and cut",
            "economy": "economic value created and destroyed, in US dollars", "climate": "greenhouse emissions (tCO2e)",
            "nature": "animals and hectares of nature protected and harmed", "culture": "certified sales, Grammys, Oscars and records",
            "overall": "their average position across the category tabs",
            "influence": "reach (Wikipedia language editions and page views), not impact"}


def tab_path(tab, unit=None):
    if tab == DEFAULT_TAB and not unit:
        return "/"
    return f"/?tab={tab}" + (f"&unit={unit}" if unit else "")


def sub_label(tab, unit):
    return (INFLUENCE_UNITS if tab == "influence" else CULTURE_UNITS)[unit][1]


def tab_meta(tab, unit=None):
    """(h1, <title>, meta description) for a ranking tab."""
    label = next(t[1] for t in TABS if t[0] == tab).replace(" $", "")
    if unit:
        label += f" \u00b7 {sub_label(tab, unit)}"
    h1 = f"{label} ranking: famous people by {TAB_DESC[tab]}" if not unit else f"{label} ranking"
    title = f"{label} ranking \u00b7 Output" if tab != DEFAULT_TAB or unit else "Output \u00b7 Famous people ranked by their impact on Earth"
    desc = (f"Famous people ranked by {TAB_DESC[tab] if not unit else sub_label(tab, unit)}. "
            "Only claims backed by 2 independent, trusted sources count; estimates are labeled est.")
    return h1, title, desc


def tab_ranking(tab, include_estimates=True, culture_unit=None):
    """People with counted entries in the tab's category, sorted by net in its own unit
    (economy: converted to USD with official rates). Returns [{person_id, name, net, lines, estimated, image_url}]."""
    if tab == "influence":
        return influence_ranking(culture_unit)
    _, _, cat, unit = next(t for t in TABS if t[0] == tab)
    r = rates()
    images = db.people_images()
    by_person = {}
    pub = db.published_ids()
    names = db.display_names()
    for e in db.all_entries():
        if e["person_id"] not in pub or e["status"] != "approved" or (not include_estimates and e.get("is_estimate")):
            continue
        c, u = e.get("category") or "people", e.get("unit") or "people"
        if c == cat and (unit is None or u == unit):
            by_person.setdefault(e["person_id"], []).append(e)
    have = {e.get("unit") for es in by_person.values() for e in es}
    rank_unit = CULTURE_UNITS[culture_unit][0] if culture_unit else next((u for u in CULTURE_RANK_UNITS if u in have), CULTURE_RANK_UNITS[-1])
    out = []
    for pid, es in by_person.items():
        if cat == "culture" and not any(e.get("unit") == rank_unit for e in es):
            continue  # no entry in the ranking unit: shown on the person page only
        net, lines = 0.0, []
        for (c, u), sc in sorted(by_category(es, include_estimates).items()):
            k = r.get((c, u), 1.0) if cat == "economy" else 1.0
            pos, neg = sc["positive_total"] * k, sc["negative_total"] * k
            if cat != "culture" or u == rank_unit:
                net += pos - neg
            fmt = usd if cat == "economy" else (lambda v, u=u: f"{'+' if v > 0 else ''}{v:,.0f} {u}")
            if pos:
                lines.append(("+", fmt(pos)))
            if neg:
                lines.append(("-", fmt(-neg)))
        if cat == "economy" and lines:
            tp = sum(sc["positive_total"] * r.get(k, 1.0) for k, sc in by_category(es, include_estimates).items())
            tn = sum(sc["negative_total"] * r.get(k, 1.0) for k, sc in by_category(es, include_estimates).items())
            lines = ([("+", usd(tp))] if tp else []) + ([("-", usd(-tn))] if tn else [])
        if not lines:
            continue
        units = {e.get("unit") for e in es}
        label = usd(net) if cat == "economy" else f"{net:+,.0f} {rank_unit}" if cat == "culture" else (f"{net:+,.0f} {units.pop()}" if len(units) == 1 else "")
        out.append({"person_id": pid, "name": pretty(pid, names), "net": net, "label": label, "lines": lines,
                    "estimated": any(e.get("is_estimate") for e in es), "image_url": images.get(pid)})
    out.sort(key=lambda p: (-p["net"], p["person_id"]))
    return out


@app.get("/api/ranking")
def api_ranking(tab: str = DEFAULT_TAB, include_estimates: bool = True, unit: str | None = None):
    if tab not in {t[0] for t in TABS}:
        raise HTTPException(422, "unknown tab")
    unit = _culture_unit(tab, unit)
    return ranking(include_estimates) if tab == "overall" else tab_ranking(tab, include_estimates, unit)


@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request, include_estimates: bool = True, tab: str = DEFAULT_TAB, unit: str = ""):
    if tab not in {t[0] for t in TABS}:
        tab = DEFAULT_TAB
    unit = unit if (tab == "culture" and unit in CULTURE_UNITS) or (tab == "influence" and unit in INFLUENCE_UNITS) else None
    if tab == "influence" and unit == INFLUENCE_DEFAULT:
        unit = None
    uq = f"&unit={unit}" if unit else ""
    base = _base(request)
    title = next(t[1] for t in TABS if t[0] == tab) + (f" \u00b7 {sub_label(tab, unit)}" if unit else "")
    og = og_tags(f"Output \u00b7 Top 5 {title}", tab_meta(tab, unit)[2],
                 f"{base}/?tab={tab}{uq}", f"/cards/tab/{tab}.png" + (f"?unit={unit}" if unit else ""), base)
    cards = []
    q = "" if include_estimates else "?include_estimates=0"
    ex = "" if include_estimates else "&include_estimates=0"
    toggle = (f'<a href="/?tab={tab}{uq}&include_estimates=0">Exclude estimates</a>' if include_estimates
              else f'<a href="/?tab={tab}{uq}">Include estimates</a>')
    tabs = "".join(f'<a class="tab{" active" if k == tab else ""}" href="/?tab={k}{ex}">{lbl}</a>' for k, lbl, _, _ in TABS)
    if tab == "culture":
        ranked = tab_ranking(tab, include_estimates, unit)
        cur = unit or next((k for k, (u, _) in CULTURE_UNITS.items() if ranked and ranked[0]["label"].endswith(" " + u)),
                           "certified" if "certified" in culture_unit_keys() else "grammys")
        tabs += ('</nav><nav class="tabs sub">' + "".join(
            f'<a class="tab{" active" if k == cur else ""}" href="/?tab=culture&unit={k}{ex}">{CULTURE_UNITS[k][1]}</a>'
            for k in culture_unit_keys()))
    if tab == "influence":
        tabs += ('</nav><nav class="tabs sub">' + "".join(
            f'<a class="tab{" active" if k == (unit or INFLUENCE_DEFAULT) else ""}" href="/?tab=influence'
            f'{"" if k == INFLUENCE_DEFAULT else "&unit=" + k}">{lbl}</a>' for k, (_, lbl) in INFLUENCE_UNITS.items()))
    if tab != "overall":
        for p in (ranked if tab == "culture" else tab_ranking(tab, include_estimates, unit if tab == "influence" else None)):
            pid = escape(p["person_id"])
            av = (f'<img class="avatar" src="{escape(photo_src(p["person_id"], p["image_url"]))}" alt="{escape(p["name"])}" loading="lazy">' if p["image_url"]
                  else f'<div class="avatar placeholder">{escape(initials(p["person_id"]))}</div>')
            cards.append(f'<a class="card" href="/people/{pid}{q}" data-person="{pid}">{av}'
                         f'<div class="name">{escape(p["name"])}</div><div class="score">{escape(p["label"])}'
                         f'{" <span class=badge>estimate</span>" if p["estimated"] else ""}</div>{other_html(p["lines"])}</a>')
    rest = []
    for p in (ranking(include_estimates) if tab == "overall" else []):
        pid = escape(p["person_id"])
        if p["image_url"]:
            av = f'<img class="avatar" src="{escape(photo_src(p["person_id"], p["image_url"]))}" alt="{escape(p["name"])}" loading="lazy">'
        else:
            av = f'<div class="avatar placeholder">{escape(initials(p["person_id"]))}</div>'
        sc = f'Score {p["score"]:.0f}' if p["ranked"] else ""
        (cards if p["ranked"] else rest).append(
            f'<a class="card" href="/people/{pid}{q}" data-person="{pid}">{av}'
            f'<div class="name">{escape(p["name"])}</div><div class="score">{sc}'
            f'{" <span class=badge>estimate</span>" if p["estimated"] else ""}</div>'
            f'<div class="positions">{escape(p["positions"])}</div></a>')
    grid = f'<div class="grid">{"".join(cards)}</div>' if cards else '<p class="empty">No counted entries in this category yet.</p>'
    if tab == "influence":
        grid = f'<p class="explain">{escape(influence_explain(unit))}</p>' + grid
    if tab == "overall":
        grid = (f'<p class="explain">{OVERALL_EXPLAIN}</p>' + grid
                + (f'<h2 class="nedy">Not enough data yet</h2><p class="muted">Counted on fewer than {OVERALL_MIN_TABS} '
                   f'category tabs, so not ranked on Overall yet. Still shown on their tabs.</p>'
                   f'<div class="grid">{"".join(rest)}</div>' if rest else ""))
    h1, ptitle, desc = tab_meta(tab, unit)
    canonical = base + tab_path(tab, unit)
    return shell(ptitle, desc, canonical,
                 f'<h1 class="tab-h1">{escape(h1)}</h1><nav class="tabs">{tabs}</nav><p class="toggle">{toggle}</p>{grid}', og)


@app.get("/submit", response_class=HTMLResponse)
def ui_submit_page(msg: str = ""):
    m = f"<p><b>{escape(msg)}</b></p>" if msg else ""
    return page("Submit entry", m + """
<form method="post" action="/submit">
<p>person_id <input name="person_id" required></p>
<p>action <input name="action" required size="60"></p>
<p>source (URL or citation) <input name="source" required size="60"></p>
<p>scale <input name="scale" type="number" step="any" min="0" required></p>
<p>direction <select name="direction"><option value="1">+1</option><option value="-1">-1</option></select></p>
<p>credit_share (0&lt;x&le;1, optional) <input name="credit_share" type="number" step="any" min="0" max="1" placeholder="1.0"></p>
<p>credit_basis <select name="credit_basis"><option value="direct">direct</option><option value="sourced_share">sourced_share</option><option value="even_split">even_split</option><option value="organization">organization</option></select></p>
<p>credit_note (optional) <input name="credit_note" size="60" placeholder="e.g. Source lists 4 contributors"></p>
<p><label><input type="checkbox" name="is_estimate" value="1"> is_estimate</label></p>
<button>Submit to queue</button></form>
<h2>Import into queue</h2>
<form method="post" action="/import" enctype="multipart/form-data">
<p>Paste JSON (object or list of objects):<br><textarea name="json_text" rows="6" cols="80"></textarea></p>
<p>or upload CSV (header: person_id,action,source,scale,direction; optional credit_share,credit_basis,credit_note,is_estimate): <input type="file" name="csv_file" accept=".csv"></p>
<button>Import</button></form>""")


@app.post("/submit")
def ui_submit(person_id: str = Form(...), action: str = Form(...), source: str = Form(...),
              scale: float = Form(...), direction: int = Form(...), credit_share: str = Form(""),
              credit_basis: str = Form("direct"), credit_note: str = Form(""), is_estimate: str = Form("")):
    try:
        e = EntryIn(person_id=person_id, action=action, source=source, scale=scale, direction=direction,
                    credit_share=float(credit_share) if credit_share.strip() else 1.0,
                    credit_basis=credit_basis or "direct", credit_note=credit_note.strip() or None,
                    is_estimate=bool(is_estimate))
    except ValidationError as ex:
        raise HTTPException(422, ex.errors())
    db.insert_entry(**e.model_dump())
    return RedirectResponse("/submit?msg=Entry+queued+for+review", status_code=303)


@app.post("/import")
async def ui_import(json_text: str = Form(""), csv_file: UploadFile | None = File(None)):
    rows = []
    if json_text.strip():
        data = json.loads(json_text)
        rows += data if isinstance(data, list) else [data]
    if csv_file is not None and csv_file.filename:
        text = (await csv_file.read()).decode("utf-8-sig")
        for r in csv.DictReader(io.StringIO(text)):
            try:
                r["direction"] = int(r["direction"])
            except (KeyError, TypeError, ValueError):
                pass
            rows.append(r)
    try:
        entries = [EntryIn(**{k: r[k] for k in EntryIn.model_fields if r.get(k) not in (None, "")}) for r in rows]
    except ValidationError as ex:
        raise HTTPException(422, ex.errors())
    for e in entries:
        db.insert_entry(**e.model_dump())
    return RedirectResponse(f"/submit?msg=Imported+{len(entries)}+entries+into+queue", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def ui_queue(request: Request):
    k = _key(request)
    q = f"?admin_key={quote(k)}" if k else ""
    rows = "".join(
        f"<tr><td>{e['id']}</td><td>{escape(e['person_id'])}</td><td>{escape(e['action'])}</td>"
        f"<td>{sources_cell(e)}</td><td>{e['scale']}</td><td>{e['direction']:+d}</td><td>{e['created_at']}</td>"
        f"<td><form method='post' action='/queue/{e['id']}/approve{q}'><input name='reviewer_id' placeholder='reviewer_id' required>"
        f"<button>Approve</button></form>"
        f"<form method='post' action='/queue/{e['id']}/mark-unproven{q}'><input name='reviewer_id' placeholder='reviewer_id' required>"
        f"<button>Mark unproven</button></form></td></tr>"
        for e in _with_sources([dict(e) for e in db.queue()])
    ) or "<tr><td colspan=8>Queue is empty.</td></tr>"
    return page("Review queue", "<table><tr><th>id</th><th>person</th><th>action</th><th>source</th><th>scale</th>"
                f"<th>dir</th><th>created_at</th><th>review</th></tr>{rows}</table>")


@app.post("/queue/{entry_id}/approve")
def ui_approve(request: Request, entry_id: int, reviewer_id: str = Form(...)):
    do_approve(entry_id, reviewer_id)
    return RedirectResponse("/queue" + (f"?admin_key={quote(_key(request))}" if _key(request) else ""), status_code=303)


@app.post("/queue/{entry_id}/mark-unproven")
def ui_mark_unproven(request: Request, entry_id: int, reviewer_id: str = Form(...)):
    do_mark_unproven(entry_id, reviewer_id)
    return RedirectResponse("/queue" + (f"?admin_key={quote(_key(request))}" if _key(request) else ""), status_code=303)


@app.get("/person")
def ui_person_lookup(person_id: str):
    return RedirectResponse(f"/people/{person_id}", status_code=303)


def _credit_cell(e):
    badge = " <span class='badge'>est.</span>" if e["is_estimate"] else ""
    note = f"<br><small>{escape(e['credit_note'])}</small>" if e.get("credit_note") else ""
    return f"{escape(e['credit_label'])}{badge}{note}"


def _cat_table(v):
    r = rates()
    rows = "".join(
        f"<tr><td>{escape(c['category'])}</td><td>{escape(c['unit'])}</td>"
        f"<td style='color:#1a8f4c'>{c['positive_total']:g}</td><td style='color:#c93a3a'>{c['negative_total']:g}</td>"
        f"<td>{c['net']:g}</td><td>{'' if (c['category'], c['unit']) in r else 'not convertible'}</td></tr>"
        for c in v["categories"]) or "<tr><td colspan=6>Nothing counted.</td></tr>"
    ov = v["overall"]
    return ("<h2>By category</h2><table class='cats'><tr><th>category</th><th>unit</th><th>positive</th><th>negative</th>"
            f"<th>net</th><th></th></tr>{rows}</table>"
            f"<p>Overall score (USD, official rates only): <b>{usd(ov['overall'])}</b>"
            + (" <span class='badge'>estimate</span> (driven by estimate entries)" if ov["estimated"] else "") + " · " +
            f"<span style='color:#1a8f4c'>{fmt_pct(ov['positive_pct'])}</span> / "
            f"<span style='color:#c93a3a'>{fmt_pct(ov['negative_pct'])}</span>"
            + (f" · not convertible: {escape(', '.join(ov['not_convertible']))}" if ov["not_convertible"] else "")
            + " (<a href='/api/conversions'>rates</a>)</p>"
            + ("<p>Not convertible (own units): " + " · ".join(
                f"<span style='color:{'#1a8f4c' if sg == '+' else '#c93a3a'}'>{escape(t)}</span>" for sg, t in v["other"]) + "</p>"
               if v["other"] else ""))


def _base(request):
    """Public origin for absolute OG URLs (Render terminates TLS, so honour X-Forwarded-Proto)."""
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.url.netloc}"


def og_tags(title, desc, page_url, card_path, base):
    """Open Graph + Twitter tags; X/Facebook get the 1200x675 card."""
    img = f"{base}{card_path}{'&' if '?' in card_path else '?'}size=x"
    t, dsc, u, i = escape(title), escape(desc), escape(page_url), escape(img)
    return (f'<meta property="og:type" content="website"><meta property="og:title" content="{t}">'
            f'<meta property="og:description" content="{dsc}"><meta property="og:url" content="{u}">'
            f'<meta property="og:image" content="{i}"><meta property="og:image:width" content="1200">'
            f'<meta property="og:image:height" content="675"><meta name="twitter:card" content="summary_large_image">'
            f'<meta name="twitter:title" content="{t}"><meta name="twitter:description" content="{dsc}">'
            f'<meta name="twitter:image" content="{i}">')


def person_card_lines(person_id, fmt=lambda v: f"{v:,.0f}"):
    """Counted totals per category in their own units; economy (currency-years) converted to USD with official rates, others in their own units."""
    v = person_view(person_id, True)
    r = rates()
    est = {((e.get("category") or "people"), (e.get("unit") or "people")) for e in v["entries"] if e.get("is_estimate")}
    lines, econ_pos, econ_neg, econ_est = [], 0.0, 0.0, False
    for c in v["categories"]:
        key = (c["category"], c["unit"])
        if c["category"] == "economy" and key in r:
            econ_pos += c["positive_total"] * r[key]
            econ_neg += c["negative_total"] * r[key]
            econ_est = econ_est or key in est
            continue
        label = c["unit"] if c["category"] in (c["unit"], "people", "culture") else f"{c['category']} {c['unit']}"
        if c["positive_total"]:
            lines.append(("+", f"+{fmt(c['positive_total'])} {label}", key in est))
        if c["negative_total"]:
            lines.append(("-", f"-{fmt(c['negative_total'])} {label}", key in est))
    if econ_pos:
        lines.insert(0, ("+", f"{usd(econ_pos)} economy", econ_est))
    if econ_neg:
        lines.insert(1 if econ_pos else 0, ("-", f"{usd(-econ_neg)} economy", econ_est))
    return lines


def _size(size):
    if size not in cards.SIZES:
        raise HTTPException(422, f"size must be one of {sorted(cards.SIZES)}")
    return size


PNG_HEADERS = {"Cache-Control": "public, max-age=600"}


@app.get("/cards/person/{person_id}.png")
def card_person(person_id: str, size: str = "portrait", preview: bool = False):
    _visible(person_id, preview)
    pr = db.get_person(person_id)
    photo = credit = None
    if pr and pr["image_url"] and pr["image_credit"]:  # photos only with a credit line
        local = local_photo_path(person_id, pr["image_url"])
        photo = cards.load_photo(local) if local else cards.fetch_photo(pr["image_url"])
        credit = pr["image_credit"]
    png = cards.person_card(pretty(person_id), initials(person_id), person_card_lines(person_id), _size(size), photo, credit)
    return Response(png, media_type="image/png", headers=PNG_HEADERS)


# Share-card labels for culture units (singular, plural). Card rendering only.
UNIT_LABELS = {"academy-awards": ("Oscar", "Oscars"), "grammys": ("Grammy", "Grammys"),
               "guinness-records": ("Guinness record", "Guinness records"),
               "certified-units": ("certified unit", "certified units")}
_BIG = ((1e15, " quadrillion"), (1e12, " trillion"), (1e9, " billion"), (1e6, " million"))


def card_usd(v):
    """Readable USD for cards: +$14.2 quadrillion, +$15.6 billion, +$950K."""
    sign = "-" if v < 0 else ("+" if v > 0 else "")
    a = abs(v)
    for div, suf in _BIG:
        if a >= div:
            x = a / div
            return f"{sign}${x:,.0f}{suf}" if x >= 1000 else f"{sign}${x:.3g}{suf}"
    return usd(v)


def unit_label(net, unit):
    one, many = UNIT_LABELS.get(unit, (unit, unit))
    return f"{net:+,.0f} {one if abs(net) == 1 else many}"


def card_tab_data(tab, unit=None):
    """(title, rows, note) for /cards/tab/{tab}.png. rows: [(name, sign, label, estimated)], at most 5."""
    t = next(t for t in TABS if t[0] == tab)
    note = "Sourced, counted entries only"
    if tab == "overall":  # average tab position; only ranked people
        rows = [(p["name"], "+", f"Score {p['score']:.0f}", p["estimated"]) for p in ranking() if p["ranked"]][:5]
        note = "Average position across category tabs (100 = 1st)"
    elif tab == "culture":
        ranked = tab_ranking(tab, culture_unit=unit)
        ru = CULTURE_UNITS[unit][0] if unit else next(
            (u for u in CULTURE_RANK_UNITS if any(p["label"].endswith(" " + u) for p in ranked)), CULTURE_RANK_UNITS[-1])
        rows = [(p["name"], "+" if p["net"] >= 0 else "-", unit_label(p["net"], ru), p["estimated"]) for p in ranked[:5]]
    elif tab == "influence":
        rows = [(p["name"], "=", p["label"], False) for p in tab_ranking(tab, culture_unit=unit)[:5]]
        note = ("Reach, not impact \u00b7 Wikipedia language editions (Wikidata) " + INFLUENCE.get("languages_as_of", "")
                if (unit or INFLUENCE_DEFAULT) == "languages" else
                "Reach, not impact \u00b7 English Wikipedia views " + INFLUENCE.get("period", ""))
    else:
        rows = [(p["name"], "+" if p["net"] >= 0 else "-", p["label"], p["estimated"]) for p in tab_ranking(tab)[:5]]
    top = f"Top {len(rows)}" if rows else "Top"
    title = f"{top} \u00b7 {t[1]}" + (f" \u00b7 {sub_label(tab, unit)}" if unit else "")
    return title, rows, note


@app.get("/cards/tab/{tab}.png")
def card_tab(tab: str, size: str = "portrait", unit: str | None = None):
    if not any(t[0] == tab for t in TABS):
        raise HTTPException(404, "unknown tab")
    unit = _culture_unit(tab, unit)
    title, rows, note = card_tab_data(tab, unit)
    png = cards.tab_card(title, rows, _size(size), note)
    return Response(png, media_type="image/png", headers=PNG_HEADERS)


def person_description(name, lines):
    """Short factual sentence from counted category totals."""
    if not lines:
        return f"{name} on Output: no counted entries yet. Unproven claims are listed separately and don't affect the score."
    parts = ", ".join(t + (" (est.)" if est else "") for _, t, est in lines[:6])
    return f"{name} on Output, counted impact backed by 2 trusted sources: {parts}."


def _entry_rows(es, cols):
    out = ""
    for e in es:
        tds = [f"<td class='act'>{escape(e['action'])}</td>", f"<td class='src' data-l='Sources'>{public_sources(e)}</td>",
               f"<td class='r' data-l='Scale'>{num(e['scale'])}</td>"]
        if "credit" in cols:
            tds.append(f"<td data-l='Credit'>{_credit_cell(e)}</td><td class='r' data-l='Counted'>{num(e['effective_scale'])}</td>")
        if "status" in cols:
            tds.append(f"<td data-l='Status'>{escape(e['status'])}</td>")
        out += f"<tr class='{'pos' if e['direction'] > 0 else 'neg'}-row'>{''.join(tds)}</tr>"
    return out


def _entries_table(es, cols, empty):
    head = "<th>Claim</th><th>Sources</th><th class='r'>Scale</th>"
    if "credit" in cols:
        head += "<th>Credit</th><th class='r'>Counted</th>"
    if "status" in cols:
        head += "<th>Status</th>"
    n = head.count("<th")
    body = _entry_rows(es, cols) or f"<tr><td colspan={n} class='na'>{empty}</td></tr>"
    return f"<div class='tw'><table class='entries'><tr>{head}</tr>{body}</table></div>"


def _person_totals(v):
    rows = "".join(
        f"<tr><td>{escape(c['category'])}</td><td>{escape(c['unit'])}</td>"
        f"<td class='r pos'>{num(c['positive_total'], True) if c['positive_total'] else '0'}</td>"
        f"<td class='r neg'>{num(-c['negative_total']) if c['negative_total'] else '0'}</td>"
        f"<td class='r'>{num(c['net'], True)}</td></tr>"
        for c in v["categories"]) or "<tr><td colspan=5 class='na'>Nothing counted yet.</td></tr>"
    me = next((p for p in ranking() if p["person_id"] == v["person_id"]), None)
    overall = ""
    if me:
        overall = ("<p class='overall'>" + (f"Overall score <b>{me['score']:.0f}</b> (average tab position, 100 = 1st), from "
                   if me["ranked"] else f"Overall: not enough data yet (needs {OVERALL_MIN_TABS} category tabs). Tab positions: ")
                   + f"{escape(me['positions'])}</p>")
    inf = INFLUENCE.get("people", {}).get(v["person_id"])
    if inf:
        url = "https://en.wikipedia.org/wiki/" + quote(inf["title"].replace(" ", "_"))
        langs = (f"Wikipedia articles in <b>{num(inf['languages'])}</b> languages "
                 f"(<a href='https://www.wikidata.org/wiki/{escape(inf.get('wikidata', ''))}' target='_blank' rel='noopener'>Wikidata</a>, "
                 f"as of {escape(INFLUENCE.get('languages_as_of', ''))}); " if inf.get("languages") is not None else "")
        overall += (f"<p class='influence muted'>Influence (reach, not impact): {langs}{num(inf['views'])} English "
                    f"<a href='{escape(url)}' target='_blank' rel='noopener'>Wikipedia</a> page views, "
                    f"{escape(INFLUENCE.get('period', ''))}. Not counted in the score.</p>")
    return ("<h2>Counted totals by category</h2><p class='muted'>Each category is totalled in its own unit; units are never added together.</p>"
            "<div class='tw'><table class='cats'><tr><th>Category</th><th>Unit</th><th class='r'>Positive</th>"
            f"<th class='r'>Negative</th><th class='r'>Net</th></tr>{rows}</table></div>{overall}")


PAGE_CSS = """
main{padding:24px 16px}
.doc{max-width:860px;margin:0 auto;line-height:1.55}
h1{font-size:1.6em;margin:.2em 0 .4em;line-height:1.25}.tab-h1{font-size:1.25em;font-weight:600;color:#333}
h2{font-size:1.2em;margin-top:1.8em}h3{font-size:1.05em}
.muted{color:#666;font-size:.9em}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0}
table{border-collapse:collapse;width:100%;background:#fff;font-size:.92em}
td,th{border-bottom:1px solid #eee;padding:8px 6px;text-align:left;vertical-align:top}
th{font-weight:600;color:#555;background:#f4f4f5}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.act{min-width:180px}td.src{min-width:120px;word-break:break-word}td.src q{color:#666;font-size:.9em}
.pos-row td:first-child{border-left:4px solid #1a8f4c}.neg-row td:first-child{border-left:4px solid #c93a3a}
.person-head{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.person-head img,.person-head .avatar{width:120px;height:120px;border-radius:50%;object-fit:cover;flex:none}
.photo-credit{font-size:.75em;color:#666;margin:.3em 0 0}
.side{border-radius:10px;padding:2px 14px 10px;margin:14px 0;background:#fff}
.side.green{border-top:4px solid #1a8f4c}.side.red{border-top:4px solid #c93a3a}
.side h3.pos,.nc-pos{color:#1a8f4c}.side h3.neg,.nc-neg{color:#c93a3a}
.uncounted{color:#555}.positions{font-size:.78em;color:#666;text-align:center;margin-top:2px}.explain{color:#555;font-size:.9em;margin:0 0 20px}.overall{background:#fff;padding:10px 12px;border-radius:8px}
.site-footer{margin:3em 0 1em;padding:1em 16px 0;border-top:1px solid #ddd;font-size:.85em;color:#666;text-align:center}
.site-footer a{color:#555}
@media (max-width:600px){.top{padding:12px 16px}.top nav a{margin-left:14px}main{padding:16px 12px}
.grid{grid-template-columns:repeat(2,1fr);gap:16px 8px}.avatar{width:96px;height:96px}h1{font-size:1.35em}
table.cats{font-size:.82em}table.cats td,table.cats th{padding:6px 4px}
table.entries,table.entries tbody,table.entries tr,table.entries td{display:block;width:100%}
table.entries tr:first-child{display:none}table.entries tr{border-bottom:1px solid #e5e5e5;padding:6px 0 6px 10px}
table.entries td{border:0;padding:3px 0;text-align:left;white-space:normal;min-width:0}
table.entries td.act{font-weight:500}table.entries td[data-l]::before{content:attr(data-l) ": ";color:#888;font-size:.85em}
.pos-row td:first-child,.neg-row td:first-child{border-left:0}.pos-row{border-left:4px solid #1a8f4c}.neg-row{border-left:4px solid #c93a3a}}
"""


@app.get("/people/{person_id}", response_class=HTMLResponse)
def ui_person(person_id: str, request: Request, include_estimates: bool = True, preview: bool = False):
    _visible(person_id, preview)
    v = person_view(person_id, include_estimates)
    pid = escape(person_id)
    name = pretty(person_id)
    toggle = (f"<a href='/people/{pid}?include_estimates=0'>Exclude estimates</a>" if include_estimates
              else f"<a href='/people/{pid}'>Include estimates</a>")
    pos = [e for e in v["entries"] if e["direction"] > 0]
    neg = [e for e in v["entries"] if e["direction"] < 0]
    counted = ("<h2>Counted entries</h2><p class='muted'>Approved claims with 2 trusted sources; these drive the totals "
               "(counted = scale \u00d7 credit share).</p>"
               f"<section class='side green'><h3 class='pos'>Positive</h3>{_entries_table(pos, ('credit',), 'None.')}</section>"
               f"<section class='side red'><h3 class='neg'>Negative</h3>{_entries_table(neg, ('credit',), 'None.')}</section>")
    est = ""
    if v["estimates"]:
        est = ("<h2>Estimates (excluded)</h2><p class='muted'>Approved but marked as estimates; not counted unless estimates are included.</p>"
               + _entries_table(v["estimates"], ("credit",), "None."))
    def nc_table(sign, title):
        cls = "pos" if sign > 0 else "neg"
        es = [e for e in v["not_counted"] if e["direction"] == sign]
        return (f"<section class='side {'green' if sign > 0 else 'red'} uncounted'><h3 class='nc-{cls}'>{title}</h3>"
                f"{_entries_table(es, ('status',), 'None.')}</section>")
    nc = nc_table(1, "Not counted, positive") + nc_table(-1, "Not counted, negative")
    pr = db.get_person(person_id)
    photo = f'<div class="avatar">{escape(initials(person_id))}</div>'
    credit = ""
    if pr and pr["image_url"]:
        photo = f'<img src="{escape(photo_src(person_id, pr["image_url"]))}" alt="{escape(name)}">'
        if pr["image_credit"] or pr["image_license_url"]:
            lic = pr["image_license_url"] or ""
            lic_html = (f' (<a href="{escape(lic)}" target="_blank" rel="noopener">license</a>)'
                        if lic.startswith(("http://", "https://")) else "")
            credit = f'<p class="photo-credit">Photo: {escape(pr["image_credit"] or "")}{lic_html}</p>'
    base = _base(request)
    desc = person_description(name, person_card_lines(person_id, num))
    head = og_tags(f"{name} on Output", desc, f"{base}/people/{pid}", f"/cards/person/{pid}.png", base)
    body = (f"<div class='person-head'>{photo}<div><h1>{escape(name)}</h1>{credit}<p class='toggle'>{toggle}</p></div></div>"
            f"{_person_totals(v)}{counted}{est}"
            "<h2>Not counted / unproven</h2><p class='muted'>Unproven or rejected claims. They are shown for transparency and do not affect the totals.</p>"
            f"{nc}")
    return shell(f"{name}: impact on Earth, counted \u00b7 Output", desc, f"{base}/people/{pid}",
                 f"<article class='doc'>{body}</article>", head)


# ---------- SEO ----------
@app.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    return Response(f"User-agent: *\nAllow: /\n\nSitemap: {_base(request)}/sitemap.xml\n", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request):
    base = _base(request)
    paths = [tab_path(t[0]) for t in TABS] + [tab_path("culture", u) for u in culture_unit_keys()]
    paths += [tab_path("influence", u) for u in INFLUENCE_UNITS if u != INFLUENCE_DEFAULT]
    paths += [f"/people/{quote(pid)}" for pid in sorted(public_ids())] + ["/about", "/privacy"]
    urls = "".join(f"<url><loc>{escape(base + p)}</loc></url>" for p in dict.fromkeys(paths))
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    media_type="application/xml")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


_favicon_ico = None


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    global _favicon_ico
    if _favicon_ico is None:
        _favicon_ico = cards.favicon_ico()
    return Response(_favicon_ico, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


# ---------- AdSense / static pages ----------
@app.get("/ads.txt", include_in_schema=False)
def ads_txt():
    pub = adsense_client().removeprefix("ca-")
    return Response(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n", media_type="text/plain")


@app.get("/privacy", response_class=HTMLResponse)
def ui_privacy(request: Request):
    return page("Privacy policy", f"""
<p>Output (outputrank.com) does not ask you to create an account and does not collect personal data from visitors,
beyond the standard server logs our host keeps (such as IP address, browser type, page requested and time), which are
used only to run and protect the site.</p>
<h2>Advertising and cookies</h2>
<p>We use Google AdSense to show ads. Google and its partners use cookies to serve ads based on your prior visits to
this and other websites. Google's use of advertising cookies lets it and its partners serve ads based on your visits
to this site and/or other sites on the Internet.</p>
<p>You can learn how Google uses data and how to control it here:
<a href="https://policies.google.com/technologies/ads" target="_blank" rel="noopener">https://policies.google.com/technologies/ads</a>.
You can opt out of personalized advertising in <a href="https://adssettings.google.com" target="_blank" rel="noopener">Google Ads Settings</a>.</p>
<h2>Submissions</h2>
<p>If you submit an entry, the text and sources you enter are stored and may be published after review. Do not include personal information in submissions.</p>
<h2>Contact</h2>
<p>Questions about this policy: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>""",
                desc="Output privacy policy: no accounts, standard server logs, Google AdSense cookies.",
                canonical=_base(request) + "/privacy")


@app.get("/about", response_class=HTMLResponse)
def ui_about(request: Request):
    return page("About Output", f"""
<p>Output ranks famous people by the total measurable impact of their existence on Earth, good and bad. Every person
has a positive side and a negative side, counted in concrete units: lives, jobs, economy (USD) and culture.</p>
<h2>Sourcing rules</h2>
<ul>
<li>Every counted claim needs <b>2 independent, very trusted sources</b> agreeing on the number; a 3rd source breaks ties.</li>
<li>Very trusted means peer-reviewed journals, government and official bodies, national archives, major museums and
memorials, university presses and major newspapers of record.</li>
<li>Company figures: an official SEC filing (or another legally required filing to a regulator) is enough on its own for a
company's jobs or employee count. Self-reported impact claims (for example a company's own safety or lives-saved figures)
don't count on their own.</li>
<li>When sources disagree, the lowest documented number is used.</li>
<li>Negative claims need a court or official finding.</li>
<li>Estimates are allowed when 2 very trusted sources give an approximate figure, and are labeled <i>est.</i></li>
<li>Claims that don't meet these rules are still shown on the person's page as <i>unproven</i>, but don't affect the score.</li>
<li>The <b>Overall</b> tab averages each person's position across the category tabs (Lives, People, Jobs,
Economy, Climate, Nature, Culture): 1st on a tab scores 100, last scores 0, linear in between, and ties share a score.
Culture counts as one tab (its Grammys, Oscars and Guinness sub-tabs are averaged). Nothing is converted into money
and no category is converted into another. A person needs at least 2 tabs to be ranked; ties go to more tabs, then
more counted entries.</li>
<li>The <b>Influence</b> tab shows reach, not impact. <i>Languages</i> (the main measure, for lasting influence) counts
the Wikipedia language editions with an article about the person, from Wikidata (the original record; the same measure
MIT's Pantheon uses). <i>Attention</i> is English Wikipedia page views over the last 12 months, from the Wikimedia
Pageviews API. Influence is neutral, never positive or negative, and is not part of Overall.</li>
<li>Outcomes of companies and foundations a person led or funded are credited to them, split by stated shares or evenly among named people.</li>
</ul>
<p>Every entry is reviewed before it counts. Anyone can <a href="/submit">submit</a> a claim with sources.</p>
<p>Contact: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>""",
                desc="How Output ranks famous people: sourcing rules, what counts, estimates and unproven claims.", canonical=_base(request) + "/about")
