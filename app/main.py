import math
import csv
import io
import json
import os
import secrets
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit
from html import escape
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from pydantic import BaseModel, Field, ValidationError

from . import cards, db, prose
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
OPEN_POSTS = {"/api/entries", "/submit", "/import", "/feedback", "/v"}


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
RATE_LIMITED = {"/submit", "/import", "/feedback"}
RATE_MAX, RATE_WINDOW = 10, 3600
_rate = {}
_rate_lock = threading.Lock()


def _client_ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


# ---------- visitor stats: counted only when a real browser beacons after the page is open ----------
_views = {}
_views_lock = threading.Lock()
_views_flushed = [time.time()]
VIEW_FLUSH_SECONDS = 30
_beacon_hits = {}
OPT_OUT_COOKIE = "out_skip"
# Tiny first-party beacon. It does not run for automated browsers (navigator.webdriver) and waits
# until the page has been visible, so a fetch of the HTML alone is not a visit.
VIEW_BEACON = (
    "<script>(function(){if(navigator.webdriver)return;var s=false;function send(){if(s||document.visibilityState==="
    "'hidden')return;s=true;var b=JSON.stringify({p:location.pathname+(location.search||''),r:document.referrer||''});"
    "try{if(navigator.sendBeacon)navigator.sendBeacon('/v',new Blob([b],{type:'application/json'}));"
    "else fetch('/v',{method:'POST',headers:{'content-type':'application/json'},body:b,keepalive:true})}catch(e){}}"
    "function arm(){setTimeout(send,1200)}if(document.visibilityState==='visible')arm();"
    "else document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')arm()},"
    "{once:true});addEventListener('pointerdown',send,{once:true,passive:true})})();</script>"
)


def _view_path(raw):
    """Public page path a beacon is allowed to count. Reviewer previews are not visits."""
    parts = urlsplit(raw or "")
    path = parts.path or "/"
    q = dict(qc.split("=", 1) if "=" in qc else (qc, "") for qc in parts.query.split("&") if qc)
    if q.get("preview", "").lower() in ("1", "true"):
        return None
    if path == "/":
        tab, unit = q.get("tab", ""), q.get("unit", "")
        if tab and not re.fullmatch(r"[a-z0-9-]{1,32}", tab):
            tab = ""
        if unit and not re.fullmatch(r"[a-z0-9-]{1,32}", unit):
            unit = ""
        return "/" + (f"?tab={tab}" if tab else "") + (f"&unit={unit}" if tab and unit else "")
    if path in ("/about", "/methodology", "/contact", "/terms", "/privacy", "/feedback", "/guides"):
        return path
    if path.startswith("/guides/"):
        slug = path[len("/guides/"):]
        if slug in {g["slug"] for g in prose.GUIDES}:
            return path
    if path.startswith("/people/"):
        slug = path[len("/people/"):]
        if slug and "/" not in slug and len(slug) <= 80 and re.fullmatch(r"[A-Za-z0-9._~-]{1,80}", slug):
            return "/people/" + slug
    return None


def _referrer_domain(raw, host):
    ref = urlsplit(raw or "").netloc.lower().removeprefix("www.")
    if ref and host and ref == host.lower().removeprefix("www."):
        return ""
    return ref[:100]


def _beacon_allowed(ip):
    """Quiet cap so a loop cannot fill the log. Extra calls are ignored, not errors."""
    now = time.time()
    with _views_lock:
        hits = [t for t in _beacon_hits.get(ip, []) if now - t < 600]
        if len(hits) >= 40:
            _beacon_hits[ip] = hits
            return False
        hits.append(now)
        _beacon_hits[ip] = hits
        return True


def note_visit(day, path, ref, country, ip_hash, ua_class, counted):
    country = country or ""
    if not (country.isalpha() or country == "T1"):
        country = ""
    k = (day, path[:200], (ref or "")[:100], country, ip_hash, ua_class, int(bool(counted)))
    with _views_lock:
        _views[k] = _views.get(k, 0) + 1
        due = time.time() - _views_flushed[0] > VIEW_FLUSH_SECONDS
    if due:
        threading.Thread(target=flush_views, daemon=True).start()


def flush_views():
    with _views_lock:
        batch = dict(_views)
        _views.clear()
        _views_flushed[0] = time.time()
    try:
        db.add_visits(batch)
    except Exception:
        with _views_lock:  # keep the counts for the next try
            for k, n in batch.items():
                _views[k] = _views.get(k, 0) + n


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
CACHE_EXACT = {"/", "/sitemap.xml", "/methodology", "/contact", "/terms", "/guides"}
CACHE_EXACT.update(f"/guides/{g['slug']}" for g in prose.GUIDES)
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


_CANONICAL_HOST = "outputrank.com"
_DEV_HOSTS = frozenset({"testserver", "localhost", "127.0.0.1", "::1"})


def _request_host(request):
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return raw.split(",")[0].strip().split(":")[0].lower()


def _canonical_redirect(request):
    """301 every public host that isn't outputrank.com onto the canonical origin, path and query kept."""
    host = _request_host(request)
    if not host or host == _CANONICAL_HOST or host in _DEV_HOSTS:
        return None
    path = request.url.path or "/"
    query = request.url.query
    dest = f"https://{_CANONICAL_HOST}{path}" + (f"?{query}" if query else "")
    return RedirectResponse(dest, status_code=301)


@app.middleware("http")
async def cache_and_headers(request: Request, call_next):
    token = db.begin_request_cache()
    try:
        return await _cache_and_headers(request, call_next)
    finally:
        db.end_request_cache(token)


async def _cache_and_headers(request: Request, call_next):
    redir = _canonical_redirect(request)
    if redir is not None:
        resp = redir
    elif request.method == "POST" and request.url.path in RATE_LIMITED and not (
            os.environ.get("ADMIN_KEY") and _admin_ok(request)) and not rate_ok(_client_ip(request)):
        resp = JSONResponse({"detail": "too many submissions, try again later"}, status_code=429,
                            headers={"Retry-After": str(RATE_WINDOW)})
    elif _cacheable(request):
        key = (db.database_url() or db.DB_PATH, request.headers.get("x-forwarded-proto", ""),
               os.environ.get("GOOGLE_SITE_VERIFICATION", ""), str(request.url))
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
        if request.method != "GET" and resp.status_code < 400 and request.url.path not in ("/feedback", "/v"):
            clear_cache()
    if _team_opt_in_request(request):
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
        resp.set_cookie(OPT_OUT_COOKIE, "1", max_age=31536000, httponly=True, samesite="lax",
                        secure=secure, path="/")
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _team_opt_in_request(request):
    """A valid admin key on any URL means this browser is the team: don't count it."""
    want = os.environ.get("ADMIN_KEY")
    got = _key(request)
    return bool(want and got and secrets.compare_digest(got, want))


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
    threading.Thread(target=_warm_visitors, daemon=True).start()


def _warm_visitors():
    """Load the hosting-network list off the request path, so the first page stays fast."""
    from .visitors import is_datacenter
    is_datacenter("127.0.0.1")


@app.on_event("shutdown")
def _shutdown():
    flush_views()


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
               '<a href="/guides">Guides</a> · <a href="/methodology">How we score</a> · '
               '<a href="/contact">Contact</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · '
               '<a href="/feedback">Suggest a person or feature</a></footer>')
SITE_NAME = "Output"
DEFAULT_DESC = ("Famous people ranked by the total measurable impact of their existence on Earth, good and bad. "
                "Every counted claim needs 2 independent, trusted sources.")
FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
               'fill="#222"/><circle cx="32" cy="32" r="17" fill="none" stroke="#fff" stroke-width="8"/></svg>')


def head_common(title, desc, canonical):
    """Viewport, title, description, canonical, favicon and AdSense: shared by every HTML page."""
    can = f'<link rel="canonical" href="{escape(canonical)}">' if canonical else ""
    token = os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip()
    verify = f'<meta name="google-site-verification" content="{escape(token)}">' if token else ""
    return (f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><meta name="description" content="{escape(desc)}">{can}{verify}'
            '<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="icon" href="/favicon.ico" sizes="32x32">'
            f'{adsense_head()}')


def shell(title, desc, canonical, body, head="", extra_css=""):
    return HTMLResponse(f"""<!doctype html><html lang="en"><head>{head_common(title, desc, canonical)}{head}
<style>{HOME_CSS}{PAGE_CSS}{extra_css}</style></head>
<body><header class="top"><a class="brand" href="/">Output</a><nav><a href="/">Rankings</a><a href="/about">About</a><a href="/guides">Guides</a><a href="/methodology">How we score</a><a href="/contact">Contact</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a></nav></header>
<main>{body}</main>{SITE_FOOTER}{VIEW_BEACON}</body></html>""")


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


def public_sources(e, public=False):
    out = short_link(e["source"])
    for x in e.get("extra_sources") or []:
        quote = x.get("quote") or ""
        if public:
            quote = _clean_brackets(quote)
        q = f" <q>{escape(quote)}</q>" if quote else ""
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


def img_fallback(person_id):
    """onerror: swap a broken photo for the initials placeholder."""
    return (' onerror="this.outerHTML=\'&lt;div class=&quot;avatar initials-fallback&quot;&gt;'
            f'{escape(initials(person_id))}&lt;/div&gt;\'"')


def initials(person_id):
    words = pretty(person_id).split()
    return "".join(w[0] for w in words[:2]).upper() or "?"


OVERALL_MIN_TABS = 1  # everyone with a counted entry is ranked
OVERALL_EXPLAIN = ("Overall ranks everyone by total net impact across all categories. Each measure is scored by size on a "
                   "log scale against the largest amount anyone has on it (that person = \u00b1100); harms stay negative and "
                   "always subtract. Weights: lives count 3x, money (economy) 0.25x, culture awards share 0.5x, everything else 1x. "
                   "Nothing is converted into money or across categories. "
                   "<a href=\"/methodology\">How we score</a>.")
MONEY_UNIT = 1_000_000  # economy is measured in millions of USD, so amounts under ~$1M barely register
MEASURE_WEIGHT = {"lives": 3.0, "USD": 0.25}
CULTURE_WEIGHT = 0.5  # shared by all culture sub-measures


def _log_score(v, biggest, unit=1.0):
    """Signed magnitude on a log scale: sign(v) * log(1+|v|/unit) / log(1+biggest/unit), in [-1, 1]."""
    if v == 0 or biggest <= 0:
        return 0.0
    x = math.log1p(abs(v) / unit) / math.log1p(biggest / unit)
    return x if v > 0 else -x


def overall_measure(c, u):
    """Measure key: economy currencies are one measure (converted to USD with official rates)."""
    return (c, "USD") if c == "economy" else (c, u)


MEASURE_LABEL = {"lives": "Lives", "people": "People", "jobs": "Jobs", "USD": "Economy", "tCO2e": "Climate",
                 "grammys": "Grammys", "academy-awards": "Oscars", "guinness-records": "Guinness records",
                 "certified-units": "Certified units", "animals": "Nature animals", "hectares": "Nature hectares"}


def ranking(include_estimates=True):
    """Overall: every person with a counted entry, by the sum of signed per-measure positions (see OVERALL_EXPLAIN).
    Returns [{person_id, name, ranked, score, tabs, positions, tab_count, counted_entries, estimated, image_url}]."""
    pub = db.published_ids()
    counted = {}
    for e in db.all_entries():
        if e["person_id"] in pub and e["status"] == "approved" and (include_estimates or not e.get("is_estimate")):
            counted.setdefault(e["person_id"], []).append(e)
    if not counted:
        return []
    r = rates()
    names, images = db.display_names(), db.people_images()
    nets = {}  # pid -> {measure: net}
    for pid, es in counted.items():
        for (c, u), sc in by_category(es, include_estimates).items():
            if c in NEUTRAL_TABS:
                continue
            k = r.get((c, u), 1.0) if c == "economy" else 1.0
            m = overall_measure(c, u)
            nets.setdefault(pid, {}).setdefault(m, 0.0)
            nets[pid][m] += (sc["positive_total"] - sc["negative_total"]) * k
    measures = {m for d in nets.values() for m in d}
    n_culture = sum(1 for c, _ in measures if c == "culture") or 1
    biggest = {m: max(abs(d.get(m, 0.0)) for d in nets.values()) for m in measures}
    people = []
    for pid, es in counted.items():
        tabs, total = [], 0.0
        for m, v in sorted(nets.get(pid, {}).items()):
            if v == 0:
                continue
            w = CULTURE_WEIGHT / n_culture if m[0] == "culture" else MEASURE_WEIGHT.get(m[1], 1.0)
            pts = 100.0 * w * _log_score(v, biggest[m], MONEY_UNIT if m[0] == "economy" else 1.0)
            total += pts
            tabs.append({"tab": MEASURE_LABEL.get(m[1], m[1]), "position": f"{pts:+.0f}", "percentile": round(pts, 1)})
        people.append({"person_id": pid, "name": pretty(pid, names), "ranked": True, "score": round(total, 1),
                       "tabs": tabs, "positions": ", ".join(f"{t['tab']} {t['position']}" for t in tabs),
                       "tab_count": len(tabs), "counted_entries": len(es),
                       "estimated": any(e.get("is_estimate") for e in es), "image_url": images.get(pid)})
    people.sort(key=lambda p: (-p["score"], -p["counted_entries"], p["name"].lower(), p["person_id"]))
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
.top{display:flex;align-items:center;justify-content:space-between;gap:12px 20px;flex-wrap:wrap;padding:16px 28px;border-bottom:1px solid #eee;background:#fff}
.top .brand{font-weight:600;font-size:1.1em;color:#222;text-decoration:none}
.top nav{display:flex;flex-wrap:wrap;gap:6px 16px}
.top nav a{color:#555;text-decoration:none}
.home-intro{max-width:42rem;line-height:1.55;margin:4px 0 12px}
.home-intro h2{margin-top:0}
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
HARMS_ONLY_EXPLAIN = ("No positive impact is counted on this tab yet, only harms such as fines and losses. "
                      "People are ordered by net (positive minus negative), so the top spot is the smallest counted harm, not a win.")
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
            "overall": "total net impact across all categories (harms subtract)",
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
    cards = []
    shown = []
    q = "" if include_estimates else "?include_estimates=0"
    ex = "" if include_estimates else "&include_estimates=0"
    toggle = (f'<a href="/?tab={tab}{uq}&include_estimates=0">Exclude estimates</a>' if include_estimates
              else f'<a href="/?tab={tab}{uq}">Include estimates</a>')
    tabs = "".join(f'<a class="tab{" active" if k == tab else ""}" href="/?tab={k}{ex}">{lbl}</a>' for k, lbl, _, _ in TABS)
    if tab == "culture":
        ranked = tab_ranking(tab, include_estimates, unit)
        shown = ranked
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
        if tab != "culture":
            shown = tab_ranking(tab, include_estimates, unit if tab == "influence" else None)
        for p in shown:
            pid = escape(p["person_id"])
            av = (f'<img class="avatar" src="{escape(photo_src(p["person_id"], p["image_url"]))}" alt="{escape(p["name"])}" loading="lazy"{img_fallback(p["person_id"])}>' if p["image_url"]
                  else f'<div class="avatar placeholder">{escape(initials(p["person_id"]))}</div>')
            cards.append(f'<a class="card" href="/people/{pid}{q}" data-person="{pid}">{av}'
                         f'<div class="name">{escape(p["name"])}</div><div class="score">{escape(p["label"])}'
                         f'{" <span class=badge>estimate</span>" if p["estimated"] else ""}</div>{other_html(p["lines"])}</a>')
    rest = []
    if tab == "overall":
        shown = ranking(include_estimates)
    for p in shown if tab == "overall" else []:
        pid = escape(p["person_id"])
        if p["image_url"]:
            av = f'<img class="avatar" src="{escape(photo_src(p["person_id"], p["image_url"]))}" alt="{escape(p["name"])}" loading="lazy"{img_fallback(p["person_id"])}>'
        else:
            av = f'<div class="avatar placeholder">{escape(initials(p["person_id"]))}</div>'
        sc = f'Score {p["score"]:+.0f}' if p["ranked"] else ""
        (cards if p["ranked"] else rest).append(
            f'<a class="card" href="/people/{pid}{q}" data-person="{pid}">{av}'
            f'<div class="name">{escape(p["name"])}</div><div class="score">{sc}'
            f'{" <span class=badge>estimate</span>" if p["estimated"] else ""}</div>'
            f'<div class="positions">{escape(p["positions"])}</div></a>')
    grid = f'<div class="grid">{"".join(cards)}</div>' if cards else '<p class="empty">No counted entries in this category yet.</p>'
    if tab == "influence":
        grid = f'<p class="explain">{escape(influence_explain(unit))}</p>' + grid
    elif tab not in ("overall", "culture") and shown and all(p["net"] <= 0 for p in shown):
        grid = f'<p class="explain harms-only">{escape(HARMS_ONLY_EXPLAIN)}</p>' + grid
    if tab == "overall":
        grid = (f'<p class="explain">{OVERALL_EXPLAIN}</p>' + grid
                + (f'<h2 class="nedy">Not enough data yet</h2><p class="muted">Counted on fewer than {OVERALL_MIN_TABS} '
                   f'category tabs, so not ranked on Overall yet. Still shown on their tabs.</p>'
                   f'<div class="grid">{"".join(rest)}</div>' if rest else ""))
    h1, ptitle, desc = tab_meta(tab, unit)
    # Tab and unit views are near-copies of the homepage, so they point at / .
    canonical = base + "/"
    og = og_tags(ptitle, desc, canonical, f"/cards/tab/{tab}.png" + (f"?unit={unit}" if unit else ""), base)
    og += item_list_ld(h1, shown, base)
    if not include_estimates:
        og += '<meta name="robots" content="noindex,follow">'
    intro = ""
    if "tab" not in request.query_params:
        intro = f"<section class='home-intro'>{prose.page_body('homepage-intro.md')}</section>"
    return shell(ptitle, desc, canonical,
                 f'<h1 class="tab-h1">{escape(h1)}</h1>{intro}<nav class="tabs">{tabs}</nav><p class="toggle">{toggle}</p>{grid}', og)


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


_BRACKET_NOTE = re.compile(r"\s*\[[^\[\]]*(?:CONFIRMS|EDITOR|INTERNAL|TODO|REVIEW|NOTE)[^\[\]]*\]", re.I)
_HARD_NOTE = re.compile(r"(?i)(\brule\s*\d|\brules\s+\d|\breplaces entry\b|\breplaces\s*#|\bsupersedes\s*#)")
_SOFT_NOTE = re.compile(
    r"(?i)(\bmagnitude\b|\bcounterfactual\b|\bno people count\b|\bplaceholder\b|\blower bound\b|"
    r"\bshared credit\b|\boverlaps\b|\bvague\b|\bscale 0\b)")
_INTERNAL_CREDIT = re.compile(
    r"(?i)(\bapproved\b|\bre-checked\b|\brechecked\b|\brule\s*\d|\brules\s+\d|\breplaces\b|\bsupersedes\b|"
    r"\bfiling rule\b|\borganization credited\b|\bheadcount at one date\b|\bguinness record count\b|"
    r"\bmagnitude\b|\blowest figure\b|\blowest documented\b|\bno court finding\b|\bprimary record\b|"
    r"\bestimate rule\b|\bcounterfactual\b|\bplaceholder\b|\bscale 0\b)")


def _clean_brackets(text):
    text = _BRACKET_NOTE.sub(" ", text or "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _drop_segment(part):
    if _HARD_NOTE.search(part):
        return True
    return len(part) <= 180 and bool(_SOFT_NOTE.search(part))


def public_action(text):
    """Claim text for public pages: editor tags and trailing reviewer clauses removed."""
    text = _clean_brackets(text)
    if not text:
        return ""
    kept = [p.strip() for p in text.split(";") if p.strip() and not _drop_segment(p.strip())]
    return "; ".join(kept)


def public_credit_note(note):
    """Public credit explanation. Reviewer process notes stay off the public page."""
    note = _clean_brackets(note)
    if not note or _INTERNAL_CREDIT.search(note):
        return ""
    return note


def _credit_cell(e, public=False):
    badge = " <span class='badge'>est.</span>" if e["is_estimate"] else ""
    note = e.get("credit_note") or ""
    if public:
        note = public_credit_note(note)
    note_html = f"<br><small>{escape(note)}</small>" if note else ""
    return f"{escape(e['credit_label'])}{badge}{note_html}"


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


def ld_script(data):
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/ld+json">{payload}</script>'


def item_list_ld(name, people, base):
    return ld_script({
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": p["name"],
             "url": f"{base}/people/{quote(p['person_id'])}"}
            for i, p in enumerate(people, 1)],
    })


def person_ld(name, url, image, same_as):
    data = {"@context": "https://schema.org", "@type": "Person", "name": name, "url": url}
    if image:
        data["image"] = image
    if same_as:
        data["sameAs"] = same_as
    return ld_script(data)


def breadcrumb_ld(crumbs):
    return ld_script({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": n, "item": u}
            for i, (n, u) in enumerate(crumbs, 1)],
    })


def _abs_url(base, src):
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        return src
    return base + (src if src.startswith("/") else "/" + src)


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
    if tab == "overall":  # total net impact score
        rows = [(p["name"], "+", f"Score {p['score']:+.0f}", p["estimated"]) for p in ranking() if p["ranked"]][:5]
        note = "Total net impact: each measure \u00b1100, harms subtract"
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


# Plain words for a counted total. (positive pair, negative pair) of (singular, plural).
# Numbers still come from the existing category totals; only the wording is for readers.
_UNIT_PHRASES = {
    "lives": (("life saved", "lives saved"), ("life lost", "lives lost")),
    "people": (("person helped", "people helped"), ("person harmed", "people harmed")),
    "jobs": (("job created", "jobs created"), ("job cut", "jobs cut")),
    "tCO2e": (("tonne of CO2e cut", "tonnes of CO2e cut"), ("tonne of CO2e emitted", "tonnes of CO2e emitted")),
    "animals": (("animal protected", "animals protected"), ("animal harmed", "animals harmed")),
    "hectares": (("hectare protected", "hectares protected"), ("hectare harmed", "hectares harmed")),
    "academy-awards": (("Oscar", "Oscars"), ("Oscar lost", "Oscars lost")),
    "grammys": (("Grammy", "Grammys"), ("Grammy lost", "Grammys lost")),
    "guinness-records": (("Guinness World Record", "Guinness World Records"),
                         ("Guinness World Record lost", "Guinness World Records lost")),
    "certified-units": (("certified unit", "certified units"), ("certified unit lost", "certified units lost")),
}


def _money(n):
    n = abs(n)
    if n >= 1_000_000:
        return f"${num(n)}"
    if n == int(n):
        return f"${n:,.0f}"
    return f"${n:,.2f}".rstrip("0").rstrip(".")


def side_phrase(category, unit, amount, positive, estimated):
    """Readable counted total, e.g. '~1 billion lives saved (est.)' or '2 Guinness World Records'."""
    n = abs(amount)
    if category == "economy":
        core = f"{_money(n)} in economic value" if positive else f"{_money(n)} in economic losses"
    else:
        forms = _UNIT_PHRASES.get(unit)
        if forms:
            one, many = forms[0 if positive else 1]
            word = one if n == 1 else many
        else:
            word = (unit or category or "").replace("-", " ")
        core = f"{num(n)} {word}"
    return f"~{core} (est.)" if estimated else core


def _tab_for(category, unit):
    for key, _label, cat, fixed in TABS:
        if cat != category:
            continue
        if key == "culture":
            uk = next((k for k, (uu, _) in CULTURE_UNITS.items() if uu == unit), None)
            return "culture", uk
        if fixed is None or fixed == unit:
            return key, None
    return None, None


def counted_phrases(person_id, include_estimates=True):
    """One phrase per counted positive or negative total. Economy is summed in USD, as on the cards."""
    v = person_view(person_id, include_estimates)
    est_keys = {((e.get("category") or "people"), (e.get("unit") or "people"))
                for e in v["entries"] if e.get("is_estimate")}
    r = rates()
    econ_pos = econ_neg = 0.0
    econ_est = False
    has_econ = False
    out = []
    for c in v["categories"]:
        key = (c["category"], c["unit"])
        if c["category"] == "economy" and key in r:
            econ_pos += c["positive_total"] * r[key]
            econ_neg += c["negative_total"] * r[key]
            econ_est = econ_est or key in est_keys
            has_econ = True
            continue
        tab, uk = _tab_for(c["category"], c["unit"])
        if c["positive_total"]:
            out.append({"tab": tab, "unit_key": uk, "amount": c["positive_total"], "positive": True,
                        "text": side_phrase(c["category"], c["unit"], c["positive_total"], True, key in est_keys)})
        if c["negative_total"]:
            out.append({"tab": tab, "unit_key": uk, "amount": c["negative_total"], "positive": False,
                        "text": side_phrase(c["category"], c["unit"], c["negative_total"], False, key in est_keys)})
    if has_econ:
        if econ_pos:
            out.append({"tab": "economy", "unit_key": None, "amount": econ_pos, "positive": True,
                        "text": side_phrase("economy", "USD", econ_pos, True, econ_est)})
        if econ_neg:
            out.append({"tab": "economy", "unit_key": None, "amount": econ_neg, "positive": False,
                        "text": side_phrase("economy", "USD", econ_neg, False, econ_est)})
    return [p for p in out if p["tab"]]


def person_placement(person_id, include_estimates=True):
    """(phrases lead-first, tab, unit key, ranked rows) for the tab where this person ranks highest.

    Ties go to the earlier tab (Lives before the rest). The lead phrase is the larger side of that total.
    """
    phrases = counted_phrases(person_id, include_estimates)
    if not phrases:
        return [], None, None, []
    groups = {}
    for p in phrases:
        groups.setdefault((p["tab"], p["unit_key"]), []).append(p)
    best = None
    for (tab, uk), group in groups.items():
        ranked = tab_ranking(tab, include_estimates, uk)
        idx = next((i for i, row in enumerate(ranked) if row["person_id"] == person_id), 10**6)
        tab_i = next(i for i, t in enumerate(TABS) if t[0] == tab)
        unit_i = list(CULTURE_UNITS).index(uk) if uk in CULTURE_UNITS else 0
        key = (idx, tab_i, unit_i)
        if best is None or key < best[0]:
            best = (key, tab, uk, ranked, group)
    _, tab, uk, ranked, group = best
    lead_group = sorted(group, key=lambda p: (-abs(p["amount"]), not p["positive"]))
    rest = [p for p in phrases if p not in group]
    rest.sort(key=lambda p: (next(i for i, t in enumerate(TABS) if t[0] == p["tab"]), not p["positive"], -abs(p["amount"])))
    return lead_group + rest, tab, uk, ranked


def person_meta(name, phrases):
    """(<title>, meta description) leading with the top counted stat in plain words."""
    if not phrases:
        return (f"{name}: impact on Earth, counted \u00b7 Output",
                f"{name} on Output: no counted entries yet. Unproven claims are listed separately and don't affect the score.")
    bits = ", ".join(p["text"] for p in phrases[:6])
    return (f"{name}: {phrases[0]['text']} \u00b7 Output",
            f"{name}: {bits}. Counted impact on Output, backed by 2 trusted sources.")


def person_intro(name, phrases, tab, unit_key, ranked, person_id):
    """Opening line plus links to the other people on this person's top tab. No facts beyond counted totals."""
    if not phrases or not tab:
        return f"<p class='lede'>{escape(name)} has no counted entries yet.</p>"
    lead, rest = phrases[0]["text"], [p["text"] for p in phrases[1:6]]
    if rest:
        sentence = f"{name}’s counted impact is {lead}, and also {', '.join(rest)}."
    else:
        sentence = f"{name}’s counted impact is {lead}."
    label = next(t[1] for t in TABS if t[0] == tab).replace(" $", "")
    if unit_key:
        label += f" \u00b7 {sub_label(tab, unit_key)}"
    href = tab_path(tab, unit_key)
    tab_a = f'<a href="{escape(href)}">{escape(label)} ranking</a>'
    others = [p for p in ranked if p["person_id"] != person_id]
    if not others:
        related = f"<p class='related'>See the {tab_a}.</p>"
    else:
        show = others[:8]
        bits = [f'<a href="/people/{escape(p["person_id"])}">{escape(p["name"])}</a>' for p in show]
        if len(bits) > 1:
            links = ", ".join(bits[:-1]) + " and " + bits[-1]
        else:
            links = bits[0]
        more = (f' <a href="{escape(href)}">{len(others) - len(show)} more</a>'
                if len(others) > len(show) else "")
        related = f"<p class='related'>Also on the {tab_a}: {links}.{more}</p>"
    return f"<p class='lede'>{escape(sentence)}</p>{related}"


def _entry_rows(es, cols, public=False):
    out = ""
    for e in es:
        action = public_action(e["action"]) if public else e["action"]
        tds = [f"<td class='act'>{escape(action)}</td>", f"<td class='src' data-l='Sources'>{public_sources(e, public)}</td>",
               f"<td class='r' data-l='Scale'>{num(e['scale'])}</td>"]
        if "credit" in cols:
            tds.append(f"<td data-l='Credit'>{_credit_cell(e, public)}</td><td class='r' data-l='Counted'>{num(e['effective_scale'])}</td>")
        if "status" in cols:
            tds.append(f"<td data-l='Status'>{escape(e['status'])}</td>")
        out += f"<tr class='{'pos' if e['direction'] > 0 else 'neg'}-row'>{''.join(tds)}</tr>"
    return out


def _entries_table(es, cols, empty, public=False):
    head = "<th>Claim</th><th>Sources</th><th class='r'>Scale</th>"
    if "credit" in cols:
        head += "<th>Credit</th><th class='r'>Counted</th>"
    if "status" in cols:
        head += "<th>Status</th>"
    n = head.count("<th")
    body = _entry_rows(es, cols, public) or f"<tr><td colspan={n} class='na'>{empty}</td></tr>"
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
        overall = ("<p class='overall'>" + (f"Overall score <b>{me['score']:+.0f}</b> (sum of signed measure scores; harms subtract), from "
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
.guide-index{padding-left:1.2em}.guide-index li{margin:.85em 0}.guide-index .muted{display:block}
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
.lede{font-size:1.05em;margin:.4em 0 .2em}
.related{margin:.2em 0 1em;font-size:.95em}
.side{border-radius:10px;padding:2px 14px 10px;margin:14px 0;background:#fff}
.side.green{border-top:4px solid #1a8f4c}.side.red{border-top:4px solid #c93a3a}
.side h3.pos,.nc-pos{color:#1a8f4c}.side h3.neg,.nc-neg{color:#c93a3a}
.uncounted{color:#555}.positions{font-size:.78em;color:#666;text-align:center;margin-top:2px}.explain{color:#555;font-size:.9em;margin:0 0 20px}.overall{background:#fff;padding:10px 12px;border-radius:8px}
.site-footer{margin:3em 0 1em;padding:1em 16px 0;border-top:1px solid #ddd;font-size:.85em;color:#666;text-align:center}
.site-footer a{color:#555}
.summary{background:#fff;border:1px solid #eee;border-radius:10px;padding:4px 16px 12px;margin:10px 0 18px}
.summary h2{margin-top:.8em}
@media (max-width:600px){.top{padding:12px 16px}main{padding:16px 12px}
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
    public = not preview
    counted = ("<h2>Counted entries</h2><p class='muted'>Approved claims with 2 trusted sources; these drive the totals "
               "(counted = scale \u00d7 credit share).</p>"
               f"<section class='side green'><h3 class='pos'>Positive</h3>{_entries_table(pos, ('credit',), 'None.', public)}</section>"
               f"<section class='side red'><h3 class='neg'>Negative</h3>{_entries_table(neg, ('credit',), 'None.', public)}</section>")
    est = ""
    if v["estimates"]:
        est = ("<h2>Estimates (excluded)</h2><p class='muted'>Approved but marked as estimates; not counted unless estimates are included.</p>"
               + _entries_table(v["estimates"], ("credit",), "None.", public))
    def nc_table(sign, title):
        cls = "pos" if sign > 0 else "neg"
        es = [e for e in v["not_counted"] if e["direction"] == sign]
        return (f"<section class='side {'green' if sign > 0 else 'red'} uncounted'><h3 class='nc-{cls}'>{title}</h3>"
                f"{_entries_table(es, ('status',), 'None.', public)}</section>")
    nc = nc_table(1, "Not counted, positive") + nc_table(-1, "Not counted, negative")
    pr = db.get_person(person_id)
    photo = f'<div class="avatar">{escape(initials(person_id))}</div>'
    credit = ""
    if pr and pr["image_url"]:
        photo = f'<img src="{escape(photo_src(person_id, pr["image_url"]))}" alt="{escape(name)}"{img_fallback(person_id)}>'
        if pr["image_credit"] or pr["image_license_url"]:
            lic = pr["image_license_url"] or ""
            lic_html = (f' (<a href="{escape(lic)}" target="_blank" rel="noopener">license</a>)'
                        if lic.startswith(("http://", "https://")) else "")
            credit = f'<p class="photo-credit">Photo: {escape(pr["image_credit"] or "")}{lic_html}</p>'
    base = _base(request)
    canonical = f"{base}/people/{pid}"
    phrases, top_tab, top_unit, ranked = person_placement(person_id, include_estimates)
    title, desc = person_meta(name, phrases)
    intro = person_intro(name, phrases, top_tab, top_unit, ranked, person_id)
    summary = prose.summary_section(person_id)
    if summary:
        intro = intro.replace("</p>", "</p>" + summary, 1)
    image = _abs_url(base, photo_src(person_id, pr["image_url"])) if pr and pr["image_url"] else None
    inf = INFLUENCE.get("people", {}).get(person_id) or {}
    same = []
    if inf.get("title"):
        same.append("https://en.wikipedia.org/wiki/" + quote(inf["title"].replace(" ", "_")))
    if inf.get("wikidata"):
        same.append(f"https://www.wikidata.org/wiki/{inf['wikidata']}")
    head = (og_tags(title, desc, canonical, f"/cards/person/{pid}.png", base)
            + person_ld(name, canonical, image, same)
            + breadcrumb_ld([("Output", base + "/"), (name, canonical)]))
    fb = f"<a href='/feedback?page=/people/{quote(person_id)}'>Suggest a correction or a claim</a>"
    body = (f"<div class='person-head'>{photo}<div><h1>{escape(name)}</h1>{credit}<p class='toggle'>{toggle} · {fb}</p></div></div>"
            f"{intro}{_person_totals(v)}{counted}{est}"
            "<h2>Not counted / unproven</h2><p class='muted'>Unproven or rejected claims. They are shown for transparency and do not affect the totals.</p>"
            f"{nc}")
    return shell(title, desc, canonical, f"<article class='doc'>{body}</article>", head)


# ---------- feedback + admin pages ----------
def _admin_required(request):
    want = os.environ.get("ADMIN_KEY")
    if not want or not secrets.compare_digest(_key(request), want):
        raise HTTPException(404, "Not Found")


@app.get("/feedback", response_class=HTMLResponse)
def ui_feedback(request: Request, page: str = "", sent: bool = False):
    if sent:
        body = "<p>Thank you! Your message was received.</p><p><a href='/'>Back to the rankings</a></p>"
    else:
        pg = page if page.startswith("/") else ""
        body = f"""<p>Tell us who should be added, a claim we missed (with sources if you have them), or a feature idea.</p>
<form method="post" action="/feedback" class="fb">
<input type="hidden" name="page" value="{escape(pg)}">
<p><label>Message<br><textarea name="message" rows="6" required maxlength="2000" style="width:100%"></textarea></label></p>
<p><label>Email (optional, only if you want a reply)<br><input type="email" name="email" maxlength="200" style="width:100%"></label></p>
<p class="hp" style="position:absolute;left:-5000px" aria-hidden="true"><label>Website <input name="website" tabindex="-1" autocomplete="off"></label></p>
<p><button>Send</button></p></form>"""
    return page_(request, body)


def page_(request, body):
    return page("Suggest a person or feature", body, desc="Suggest a person, a missing claim or a feature for Output.",
                canonical=_base(request) + "/feedback")


@app.post("/feedback")
def ui_feedback_post(message: str = Form(""), email: str = Form(""), page: str = Form(""), website: str = Form("")):
    if website.strip():  # honeypot: silently drop
        return RedirectResponse("/feedback?sent=1", status_code=303)
    message, email = message.strip(), email.strip()
    if not message or len(message) > 2000:
        raise HTTPException(422, "message must be 1-2000 characters")
    if email and (len(email) > 200 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise HTTPException(422, "invalid email")
    db.add_feedback(message, email or None, page[:200] if page.startswith("/") else None)
    return RedirectResponse("/feedback?sent=1", status_code=303)


@app.post("/v", include_in_schema=False)
async def view_beacon(request: Request):
    """Count a visit only after the page's own script asks. Always 204, so a failed call is invisible."""
    try:
        _accept_beacon(request, await request.body())
    except Exception:
        pass
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _accept_beacon(request, body):
    from .visitors import classify_visit, eastern_day, ip_hash
    origin = request.headers.get("origin", "")
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "none"):
        return
    if origin:
        host = urlsplit(origin).hostname or ""
        if host not in {_request_host(request), _CANONICAL_HOST, "www.outputrank.com", "localhost",
                        "127.0.0.1", "testserver"}:
            return
    elif site not in ("same-origin", "none"):
        return
    if body and len(body) > 2000:
        return
    try:
        data = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return
    if not isinstance(data, dict):
        return
    path = _view_path(str(data.get("p") or ""))
    if not path:
        return
    ip = _client_ip(request)
    day = eastern_day()
    hashed = ip_hash(ip, day)
    if not _beacon_allowed(hashed):
        return
    country = (request.headers.get("cf-ipcountry") or request.headers.get("x-country-code") or "").upper()[:2]
    label, counted = classify_visit(ip, request.headers.get("user-agent", ""), country,
                                    request.cookies.get(OPT_OUT_COOKIE) == "1")
    ref = _referrer_domain(str(data.get("r") or ""), _request_host(request))
    stored_country = country if country.isalpha() or country == "T1" else ""
    note_visit(day, path, ref, stored_country, hashed, label, counted)


def _kv_table(title, rows, link=False):
    trs = "".join(f"<tr><td>{(f'<a href={chr(39)}{escape(k)}{chr(39)}>{escape(k)}</a>' if link else escape(k or '-'))}</td>"
                  f"<td class='r'>{n:,}</td></tr>" for k, n in rows) or "<tr><td colspan=2 class='na'>None yet.</td></tr>"
    return f"<h2>{title}</h2><div class='tw'><table><tr><th>{title}</th><th class='r'>Views</th></tr>{trs}</table></div>"


@app.get("/admin/stats", response_class=HTMLResponse)
def admin_stats(request: Request, days: int = 30):
    _admin_required(request)
    flush_views()
    st = db.pageview_stats(max(1, min(days, 366)))
    total = sum(n for _, n in st["by_day"])
    uniq = {d: n for d, n in st["uniques_by_day"]}
    unique_total = sum(uniq.values())
    day_rows = "".join(
        f"<tr><td>{escape(d)}</td><td class='r'>{n:,}</td><td class='r'>{uniq.get(d, 0):,}</td></tr>"
        for d, n in st["by_day"]) or "<tr><td colspan=3 class='na'>None yet.</td></tr>"
    body = (f"<p>Last {days} days: <b>{total:,}</b> page views from <b>{unique_total:,}</b> real visitors "
            "(unique per day). A view counts only after the page has been open in a normal browser. "
            "Bots, link previews, hosting networks, Tor, headless browsers, and this team's browsers are left out. "
            "Days are US Eastern time. Addresses are stored only as a hash that changes every day.</p>"
            f"<h2>Day</h2><div class='tw'><table><tr><th>Day (Eastern)</th><th class='r'>Views</th>"
            f"<th class='r'>Unique visitors</th></tr>{day_rows}</table></div>"
            + _kv_table("Page / tab", st["pages"], True)
            + _kv_table("Person page", st["people"], True) + _kv_table("Referrer", st["referrers"])
            + _kv_table("Country", st["countries"])
            + _kv_table("Not counted", st["dropped"])
            + _kv_table("Visitor hash", st["hashes"]))
    return page("Visitor stats", body, head='<meta name="robots" content="noindex">')


@app.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request):
    _admin_required(request)
    rows = "".join(f"<tr><td>{escape(f['created_at'][:16].replace('T', ' '))} UTC</td><td>{escape(f['message'])}</td>"
                   f"<td>{escape(f['email'] or '')}</td><td>{escape(f['page'] or '')}</td></tr>"
                   for f in db.list_feedback()) or "<tr><td colspan=4 class='na'>No feedback yet.</td></tr>"
    return page("Feedback", "<div class='tw'><table><tr><th>When</th><th>Message</th><th>Email</th><th>Page</th></tr>"
                f"{rows}</table></div>", head='<meta name="robots" content="noindex">')


# ---------- SEO ----------
@app.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    return Response(f"User-agent: *\nAllow: /\nDisallow: /admin/\n\nSitemap: {_base(request)}/sitemap.xml\n", media_type="text/plain")


def _sitemap_dates():
    """YYYY-MM-DD of the newest entry, plus the newest date for each person."""
    site, by_person = None, {}
    for e in db.all_entries():
        days = []
        for key in ("reviewed_at", "created_at"):
            raw = str(e.get(key) or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                days.append(raw)
        if not days:
            continue
        day = max(days)
        site = day if site is None or day > site else site
        pid = e["person_id"]
        if pid not in by_person or day > by_person[pid]:
            by_person[pid] = day
    if site is None:
        site = time.strftime("%Y-%m-%d", time.gmtime())
    return site, by_person


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request):
    base = _base(request)
    paths = ["/"]
    paths += [f"/people/{quote(pid)}" for pid in sorted(public_ids())]
    paths += ["/about", "/methodology", "/contact", "/terms", "/privacy", "/guides"]
    paths += [f"/guides/{g['slug']}" for g in prose.GUIDES]
    site_day, by_person = _sitemap_dates()
    urls = ""
    for p in dict.fromkeys(paths):
        day = site_day
        if p.startswith("/people/"):
            day = by_person.get(unquote(p[len("/people/"):]), site_day)
        urls += f"<url><loc>{escape(base + p)}</loc><lastmod>{day}</lastmod></url>"
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
<h2>Visitor statistics</h2>
<p>We count anonymous page views with our own statistics, and only after a page has been open in a browser for a
moment. We store the page, the day (US Eastern time), the referring website's domain, the country when our host
provides it, a short label for the kind of browser, and a hash of the network address that changes every day so we
can count unique visitors. We don't use a cookie for this for ordinary visitors, and we don't store the network
address itself. The hash can't be used to recognize someone on a later day.</p>
<h2>Feedback</h2>
<p>If you use the "Suggest a person or feature" form, we store your message, the page you sent it from and, only if you
give it, your email address, so we can reply. We don't share it.</p>
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
<li>The <b>Overall</b> tab ranks every person with a counted entry by total net impact across all categories.
Each measure (lives, people, jobs, economy in USD, climate tCO2e, nature, and each culture award) is scored on its own, by
size on a log scale: score = 100 &times; weight &times; log(1 + amount) / log(1 + largest amount anyone has on that measure),
negative for harms. So 10&times; more counts one step more, not 10&times; more, and no unit can dominate (each measure is
capped at &plusmn;100 &times; weight). Economy is measured in millions of dollars, so small fines barely register. Weights:
lives 3&times; (life and death matters most), economy 0.25&times; (fines and settlements are penalties, not direct harm to
people), culture awards share 0.5&times;, everything else 1&times;. The scores are added up, so every harm lowers a score
and never raises it. Economy amounts are converted to USD with official exchange rates; nothing else is converted into
money or across categories.</li>
<li>The <b>Influence</b> tab shows reach, not impact. <i>Languages</i> (the main measure, for lasting influence) counts
the Wikipedia language editions with an article about the person, from Wikidata (the original record; the same measure
MIT's Pantheon uses). <i>Attention</i> is English Wikipedia page views over the last 12 months, from the Wikimedia
Pageviews API. Influence is neutral, never positive or negative, and is not part of Overall.</li>
<li>Outcomes of companies and foundations a person led or funded are credited to them, split by stated shares or evenly among named people.</li>
</ul>
<p>Every entry is reviewed before it counts. Anyone can <a href="/submit">submit</a> a claim with sources.</p>
<h2>Who runs Output</h2>
<p>Output is an independent project created and run by <b>Many Malon</b>, a music producer and entrepreneur, through his company <b>MUSICA MALON LLC</b>. It isn't funded or controlled by any of the people, companies or organizations it ranks.</p>
<p>Many started Output to answer a simple question with evidence instead of opinion: <i>what measurable difference did a famous person actually make?</i> Every number on the site has to clear the sourcing rules above. Each counted entry links to its sources, so you can check it yourself.</p>
<p>The site may carry ads to cover running costs. Advertisers have no say in rankings, scores or which claims are counted.</p>
<p>Read <a href="/methodology">how scoring works</a>, see our <a href="/terms">Terms</a>, or <a href="/contact">contact us</a> with corrections and questions.</p>""",
                desc="How Output ranks famous people: sourcing rules, what counts, estimates and unproven claims.", canonical=_base(request) + "/about")


@app.get("/guides", response_class=HTMLResponse)
def ui_guides(request: Request):
    items = "".join(
        f"<li><a href=\"/guides/{g['slug']}\">{escape(g['title'])}</a> "
        f"<span class='muted'>{escape(g['dek'])}</span></li>"
        for g in prose.GUIDES)
    return page("Guides", f"""
<p>These guides walk through Output's rankings using the people, filings, and scores already on the site.
The rules they rely on are written out on <a href="/methodology">How we score</a>.</p>
<ul class="guide-index">{items}</ul>""",
                desc="Guides to Output's rankings: Borlaug, jobs, lives, unproven claims, units, harms, and estimates.",
                canonical=_base(request) + "/guides")


@app.get("/guides/{slug}", response_class=HTMLResponse)
def ui_guide(slug: str, request: Request):
    guide = next((g for g in prose.GUIDES if g["slug"] == slug), None)
    if guide is None:
        raise HTTPException(404, "Not Found")
    return page(guide["title"], prose.guide_html(slug), desc=guide["desc"],
                canonical=_base(request) + f"/guides/{slug}", h1=guide["h1"])


@app.get("/methodology", response_class=HTMLResponse)
def ui_methodology(request: Request):
    return page("How we score", prose.page_body("methodology.md"),
                desc="How Output turns a sourced claim into a score: what counts, sourcing rules, weights and the Overall ranking.",
                canonical=_base(request) + "/methodology", h1="How Output scores people")


@app.get("/contact", response_class=HTMLResponse)
def ui_contact(request: Request):
    return page("Contact", prose.page_body("contact.md"),
                desc="Contact Output, run by Many Malon through MUSICA MALON LLC, about corrections, missing claims and privacy.",
                canonical=_base(request) + "/contact")


@app.get("/terms", response_class=HTMLResponse)
def ui_terms(request: Request):
    return page("Terms of Use", prose.page_body("terms.md"),
                desc="Terms of use for Output at outputrank.com, owned and run by MUSICA MALON LLC.",
                canonical=_base(request) + "/terms")
