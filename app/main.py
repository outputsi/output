import csv
import io
import json
from html import escape
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from . import db
from .scoring import by_category, credit_label, effective_scale, fmt_pct, overall, score

UNITS = {"people": ("people", "lives"), "jobs": ("jobs",), "climate": ("tCO2e",),
         "nature": ("animals", "hectares"), "economy": ("USD",), "tech": None, "culture": None}


def _check_unit(category, unit):
    allowed = UNITS[category]
    if allowed is not None and unit not in allowed:
        raise HTTPException(422, f"unit for {category} must be one of {allowed}")


def rates():
    return {(c["category"], c["unit"]): float(c["usd_per_unit"]) for c in db.conversions()}

app = FastAPI(title="Output")


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
    return {"person_id": person_id, **score(people, include_estimates), "categories": cats,
            "overall": ov, "entries": counted,
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


@app.get("/api/entries/{entry_id}/sources")
def api_list_sources(entry_id: int):
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    return {"entry_id": entry_id, "source": e["source"], "extra_sources": db.entry_sources({entry_id}).get(entry_id, [])}


@app.get("/api/queue")
def api_queue():
    return _with_sources([dict(e) for e in db.queue()])


@app.get("/api/people/{person_id}")
def api_person(person_id: str, include_estimates: bool = True):
    return person_view(person_id, include_estimates)


# ---------- Web UI ----------
def page(title, body):
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:4px;text-align:left}}.n{{font-size:1.4em;margin-right:2em}}</style></head>
<body><nav><a href="/">Home</a> | <a href="/submit">Submit</a> | <a href="/queue">Queue</a> |
<form style="display:inline" action="/person"><input name="person_id" placeholder="person_id"><button>View person</button></form></nav>
<h1>{escape(title)}</h1>{body}</body></html>""")


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


class ImageIn(BaseModel):
    image_url: str | None = None
    image_credit: str | None = None
    image_license_url: str | None = None


def pretty(person_id):
    return " ".join(w.capitalize() for w in person_id.replace("_", "-").split("-") if w)


def initials(person_id):
    words = pretty(person_id).split()
    return "".join(w[0] for w in words[:2]).upper() or "?"


def ranking(include_estimates=True):
    entries = db.all_entries()
    images = db.people_images()
    by_person = {}
    for e in entries:
        by_person.setdefault(e["person_id"], []).append(e)
    people = []
    r = rates()
    for pid, es in by_person.items():
        counted = any(e["status"] == "approved" and (include_estimates or not e.get("is_estimate")) for e in es)
        if not counted:
            continue  # hidden until something counts
        ov = overall(es, r, include_estimates)
        people.append({"person_id": pid, "name": pretty(pid), "net": ov["overall"], "positive_pct": ov["positive_pct"],
                       "negative_pct": ov["negative_pct"], "not_convertible": ov["not_convertible"],
                       "image_url": images.get(pid)})
    people.sort(key=lambda p: (-p["net"], p["person_id"]))
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
            return f"{sign}${v / div:.3g}{suf}"
    return f"{sign}${v:.3g}"


def share_label(p):
    if p["positive_pct"] is None:
        return '<span class="na">\u2014</span>'
    return (f'<span class="pos">{fmt_pct(p["positive_pct"])}</span> · '
            f'<span class="neg">{fmt_pct(p["negative_pct"])}</span>')


@app.get("/", response_class=HTMLResponse)
def ui_home(include_estimates: bool = True):
    cards = []
    q = "" if include_estimates else "?include_estimates=0"
    toggle = ('<a href="/?include_estimates=0">Exclude estimates</a>' if include_estimates
              else '<a href="/">Include estimates</a>')
    for p in ranking(include_estimates):
        pid = escape(p["person_id"])
        if p["image_url"]:
            av = f'<img class="avatar" src="{escape(p["image_url"])}" alt="{escape(p["name"])}">'
        else:
            av = f'<div class="avatar placeholder">{escape(initials(p["person_id"]))}</div>'
        cards.append(f'<a class="card" href="/people/{pid}{q}" data-person="{pid}">{av}'
                     f'<div class="name">{escape(p["name"])}</div><div class="score">{usd(p["net"])}</div>'
                     f'<div class="net">{share_label(p)}</div></a>')
    grid = f'<div class="grid">{"".join(cards)}</div>' if cards else '<p class="empty">No people yet.</p>'
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Output</title><style>{HOME_CSS}</style></head>
<body><header class="top"><a class="brand" href="/">Output</a><nav><a href="/submit">Submit</a><a href="/queue">Queue</a></nav></header>
<main><p class="toggle">{toggle}</p>{grid}
<details><summary>Set a person's image</summary>
<form method="post" action="/people-image"><input name="person_id" placeholder="person_id" required>
<input name="image_url" placeholder="https://... (blank to clear)" size="40"><button>Save</button></form></details>
</main></body></html>""")


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
def ui_queue():
    rows = "".join(
        f"<tr><td>{e['id']}</td><td>{escape(e['person_id'])}</td><td>{escape(e['action'])}</td>"
        f"<td>{sources_cell(e)}</td><td>{e['scale']}</td><td>{e['direction']:+d}</td><td>{e['created_at']}</td>"
        f"<td><form method='post' action='/queue/{e['id']}/approve'><input name='reviewer_id' placeholder='reviewer_id' required>"
        f"<button>Approve</button></form>"
        f"<form method='post' action='/queue/{e['id']}/mark-unproven'><input name='reviewer_id' placeholder='reviewer_id' required>"
        f"<button>Mark unproven</button></form></td></tr>"
        for e in _with_sources([dict(e) for e in db.queue()])
    ) or "<tr><td colspan=8>Queue is empty.</td></tr>"
    return page("Review queue", "<table><tr><th>id</th><th>person</th><th>action</th><th>source</th><th>scale</th>"
                f"<th>dir</th><th>created_at</th><th>review</th></tr>{rows}</table>")


@app.post("/queue/{entry_id}/approve")
def ui_approve(entry_id: int, reviewer_id: str = Form(...)):
    do_approve(entry_id, reviewer_id)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{entry_id}/mark-unproven")
def ui_mark_unproven(entry_id: int, reviewer_id: str = Form(...)):
    do_mark_unproven(entry_id, reviewer_id)
    return RedirectResponse("/queue", status_code=303)


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
            f"<p>Overall score (USD, official rates only): <b>{usd(ov['overall'])}</b> · "
            f"<span style='color:#1a8f4c'>{fmt_pct(ov['positive_pct'])}</span> / "
            f"<span style='color:#c93a3a'>{fmt_pct(ov['negative_pct'])}</span>"
            + (f" · not convertible: {escape(', '.join(ov['not_convertible']))}" if ov["not_convertible"] else "")
            + " (<a href='/api/conversions'>rates</a>)</p>")


@app.get("/people/{person_id}", response_class=HTMLResponse)
def ui_person(person_id: str, include_estimates: bool = True):
    v = person_view(person_id, include_estimates)
    pid = escape(person_id)
    toggle = (f"<a href='/people/{pid}?include_estimates=0'>Exclude estimates</a>" if include_estimates
              else f"<a href='/people/{pid}'>Include estimates</a>")
    rows = "".join(
        f"<tr><td>{escape(e['action'])}</td><td>{sources_cell(e)}</td><td>{e['scale']}</td>"
        f"<td>{_credit_cell(e)}</td><td>{e['effective_scale']:g}</td>"
        f"<td>{e['direction']:+d}</td><td>{e['reviewed_at']}</td><td>{escape(e['reviewer_id'] or '')}</td></tr>"
        for e in v["entries"]
    ) or "<tr><td colspan=8>No approved entries.</td></tr>"
    est = ""
    if v["estimates"]:
        est_rows = "".join(
            f"<tr><td>{escape(e['action'])}</td><td>{sources_cell(e)}</td><td>{e['scale']}</td>"
            f"<td>{_credit_cell(e)}</td><td>{e['effective_scale']:g}</td><td>{e['direction']:+d}</td></tr>"
            for e in v["estimates"]
        )
        est = ("<h2>Estimates (excluded)</h2><p>Approved but marked as estimates; not counted unless estimates are included.</p>"
               "<table style='color:#666'><tr><th>action</th><th>source</th><th>scale</th><th>credit</th><th>effective</th><th>dir</th></tr>"
               f"{est_rows}</table>")
    nc = "".join(
        f"<tr><td>{escape(e['action'])}</td><td>{sources_cell(e)}</td><td>{e['scale']}</td>"
        f"<td>{e['direction']:+d}</td><td>{escape(e['status'])}</td></tr>"
        for e in v["not_counted"]
    ) or "<tr><td colspan=5>None.</td></tr>"
    photo = ""
    pr = db.get_person(person_id)
    if pr and pr["image_url"]:
        photo = f'<p><img src="{escape(pr["image_url"])}" alt="{pid}" style="width:120px;height:120px;border-radius:50%;object-fit:cover"></p>'
        if pr["image_credit"] or pr["image_license_url"]:
            lic = pr["image_license_url"] or ""
            lic_html = (f' (<a href="{escape(lic)}" target="_blank" rel="noopener">license</a>)'
                        if lic.startswith(("http://", "https://")) else "")
            photo += f'<p class="photo-credit" style="font-size:.75em;color:#666;margin-top:-.5em">Photo: {escape(pr["image_credit"] or "")}{lic_html}</p>'
    return page(f"Person: {person_id}", photo +
                "<style>.badge{background:#fde68a;color:#78350f;border-radius:4px;padding:1px 6px;font-size:.8em}</style>"
                f"<p>{toggle}</p>{_cat_table(v)}"
                f"<p><span class='n'>Positive: {v['positive_total']}</span><span class='n'>Negative: {v['negative_total']}</span>"
                f"<span class='n'>Net: {v['net']}</span><span class='n'>Positive share: <span style='color:{"#999" if v["positive_pct"] is None else "#1a8f4c"}'>{fmt_pct(v['positive_pct'])}</span></span><span class='n'>Negative share: <span style='color:{"#999" if v["negative_pct"] is None else "#c93a3a"}'>{fmt_pct(v['negative_pct'])}</span></span></p>"
                "<h2>Counted entries</h2><p>Approved entries; these drive the totals (effective = scale × credit share).</p>"
                "<table><tr><th>action</th><th>source</th><th>scale</th><th>credit</th><th>effective</th><th>dir</th><th>reviewed_at</th><th>reviewer</th></tr>"
                f"{rows}</table>{est}"
                "<hr style='margin:2em 0;border:2px solid #999'>"
                "<h2>Not counted / unproven</h2><p>Unproven or rejected entries. They do not affect the totals.</p>"
                "<table style='color:#666'><tr><th>action</th><th>source</th><th>scale</th><th>dir</th><th>status</th></tr>"
                f"{nc}</table>")
