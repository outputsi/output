import csv
import io
import json
from html import escape
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from . import db
from .scoring import score

app = FastAPI(title="Output")


class EntryIn(BaseModel):
    person_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    source: str = Field(min_length=1)
    scale: float = Field(ge=0)
    direction: Literal[1, -1]


class ApproveIn(BaseModel):
    reviewer_id: str = Field(min_length=1)


def person_view(person_id):
    entries = db.person_entries(person_id)
    approved = [e for e in entries if e["reviewed_at"] is not None]
    return {"person_id": person_id, **score(approved), "entries": approved}


def do_approve(entry_id, reviewer_id):
    e = db.get_entry(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    if e["reviewed_at"] is not None:
        raise HTTPException(409, "entry already approved")
    return db.approve_entry(entry_id, reviewer_id)


# ---------- API ----------
@app.post("/api/entries", status_code=201)
def api_submit(entry: EntryIn):
    return db.insert_entry(**entry.model_dump())


@app.post("/api/entries/{entry_id}/approve")
def api_approve(entry_id: int, body: ApproveIn):
    return do_approve(entry_id, body.reviewer_id)


@app.get("/api/queue")
def api_queue():
    return db.queue()


@app.get("/api/people/{person_id}")
def api_person(person_id: str):
    return person_view(person_id)


# ---------- Web UI ----------
def page(title, body):
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:4px;text-align:left}}.n{{font-size:1.4em;margin-right:2em}}</style></head>
<body><nav><a href="/">Submit / Import</a> | <a href="/queue">Queue</a> |
<form style="display:inline" action="/person"><input name="person_id" placeholder="person_id"><button>View person</button></form></nav>
<h1>{escape(title)}</h1>{body}</body></html>""")


def link(src):
    s = escape(src)
    if src.startswith(("http://", "https://")):
        return f'<a href="{s}" target="_blank" rel="noopener">{s}</a>'
    return s


@app.get("/", response_class=HTMLResponse)
def ui_home(msg: str = ""):
    m = f"<p><b>{escape(msg)}</b></p>" if msg else ""
    return page("Submit entry", m + """
<form method="post" action="/submit">
<p>person_id <input name="person_id" required></p>
<p>action <input name="action" required size="60"></p>
<p>source (URL or citation) <input name="source" required size="60"></p>
<p>scale <input name="scale" type="number" step="any" min="0" required></p>
<p>direction <select name="direction"><option value="1">+1</option><option value="-1">-1</option></select></p>
<button>Submit to queue</button></form>
<h2>Import into queue</h2>
<form method="post" action="/import" enctype="multipart/form-data">
<p>Paste JSON (object or list of objects):<br><textarea name="json_text" rows="6" cols="80"></textarea></p>
<p>or upload CSV (header: person_id,action,source,scale,direction): <input type="file" name="csv_file" accept=".csv"></p>
<button>Import</button></form>""")


@app.post("/submit")
def ui_submit(person_id: str = Form(...), action: str = Form(...), source: str = Form(...),
              scale: float = Form(...), direction: int = Form(...)):
    try:
        e = EntryIn(person_id=person_id, action=action, source=source, scale=scale, direction=direction)
    except ValidationError as ex:
        raise HTTPException(422, ex.errors())
    db.insert_entry(**e.model_dump())
    return RedirectResponse("/?msg=Entry+queued+for+review", status_code=303)


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
        entries = [EntryIn(**{k: r.get(k) for k in EntryIn.model_fields}) for r in rows]
    except ValidationError as ex:
        raise HTTPException(422, ex.errors())
    for e in entries:
        db.insert_entry(**e.model_dump())
    return RedirectResponse(f"/?msg=Imported+{len(entries)}+entries+into+queue", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def ui_queue():
    rows = "".join(
        f"<tr><td>{e['id']}</td><td>{escape(e['person_id'])}</td><td>{escape(e['action'])}</td>"
        f"<td>{link(e['source'])}</td><td>{e['scale']}</td><td>{e['direction']:+d}</td><td>{e['created_at']}</td>"
        f"<td><form method='post' action='/queue/{e['id']}/approve'><input name='reviewer_id' placeholder='reviewer_id' required>"
        f"<button>Approve</button></form></td></tr>"
        for e in db.queue()
    ) or "<tr><td colspan=8>Queue is empty.</td></tr>"
    return page("Review queue", "<table><tr><th>id</th><th>person</th><th>action</th><th>source</th><th>scale</th>"
                f"<th>dir</th><th>created_at</th><th>approve</th></tr>{rows}</table>")


@app.post("/queue/{entry_id}/approve")
def ui_approve(entry_id: int, reviewer_id: str = Form(...)):
    do_approve(entry_id, reviewer_id)
    return RedirectResponse("/queue", status_code=303)


@app.get("/person")
def ui_person_lookup(person_id: str):
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.get("/people/{person_id}", response_class=HTMLResponse)
def ui_person(person_id: str):
    v = person_view(person_id)
    rows = "".join(
        f"<tr><td>{escape(e['action'])}</td><td>{link(e['source'])}</td><td>{e['scale']}</td>"
        f"<td>{e['direction']:+d}</td><td>{e['reviewed_at']}</td><td>{escape(e['reviewer_id'])}</td></tr>"
        for e in v["entries"]
    ) or "<tr><td colspan=6>No approved entries.</td></tr>"
    return page(f"Person: {person_id}",
                f"<p><span class='n'>Positive: {v['positive_total']}</span><span class='n'>Negative: {v['negative_total']}</span>"
                f"<span class='n'>Net: {v['net']}</span></p>"
                "<table><tr><th>action</th><th>source</th><th>scale</th><th>dir</th><th>reviewed_at</th><th>reviewer</th></tr>"
                f"{rows}</table>")
