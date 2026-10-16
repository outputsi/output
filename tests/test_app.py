import pytest
from fastapi.testclient import TestClient
from app.scoring import score

A = "2026-01-01T00:00:00"


def e(scale, d, approved=True):
    return {"scale": scale, "direction": d, "reviewed_at": A if approved else None,
            "status": "approved" if approved else "pending"}


def test_empty():
    assert score([]) == {"positive_total": 0, "negative_total": 0, "net": 0, "positive_pct": None, "negative_pct": None}


def test_mixed():
    assert score([e(10, 1), e(3, -1), e(5, 1)]) == {"positive_total": 15, "negative_total": 3, "net": 12, "positive_pct": pytest.approx(83.333333), "negative_pct": pytest.approx(16.666667)}


def test_all_negative():
    assert score([e(4, -1), e(6, -1)]) == {"positive_total": 0, "negative_total": 10, "net": -10, "positive_pct": 0, "negative_pct": 100}


def test_floats():
    r = score([e(0.1, 1), e(0.2, 1), e(0.05, -1)])
    assert r["positive_total"] == pytest.approx(0.3)
    assert r["negative_total"] == pytest.approx(0.05)
    assert r["net"] == pytest.approx(0.25)


def test_unapproved_excluded_pure():
    assert score([e(100, 1, approved=False), e(2, 1)])["net"] == 2


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    return TestClient(app)


def sub(c, **kw):
    body = {"person_id": "p1", "action": "x", "source": "https://ex.com", "scale": 1, "direction": 1, **kw}
    c.post(f"/api/people/{body['person_id']}/publish", json={"published": True})
    return c.post("/api/entries", json=body)


def test_unapproved_excluded_api(client):
    r = sub(client, scale=50)
    assert r.status_code == 201 and r.json()["reviewed_at"] is None
    p = client.get("/api/people/p1").json()
    assert p["net"] == 0 and p["entries"] == []
    assert len(client.get("/api/queue").json()) == 1


def test_approve_flow(client):
    a = sub(client, scale=10).json()["id"]
    b = sub(client, scale=4, direction=-1).json()["id"]
    sub(client, scale=999)  # stays queued
    for i in (a, b):
        r = client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "rev1"})
        assert r.status_code == 200 and r.json()["reviewer_id"] == "rev1" and r.json()["reviewed_at"]
    p = client.get("/api/people/p1").json()
    assert (p["positive_total"], p["negative_total"], p["net"]) == (10, 4, 6)
    assert [x["source"] for x in p["entries"]] == ["https://ex.com"] * 2
    assert len(client.get("/api/queue").json()) == 1
    assert client.post(f"/api/entries/{a}/approve", json={"reviewer_id": "r"}).status_code == 409
    assert client.post("/api/entries/9999/approve", json={"reviewer_id": "r"}).status_code == 404


def test_invalid_direction(client):
    assert sub(client, direction=0).status_code == 422
    assert sub(client, direction=2).status_code == 422


def test_ui_smoke(client):
    assert client.get("/?tab=overall").status_code == 200
    r = client.post("/submit", data={"person_id": "u", "action": "a", "source": "https://s.org",
                                     "scale": "3", "direction": "1"})
    assert r.status_code == 200
    r = client.post("/import", data={"json_text": '[{"person_id":"u","action":"b","source":"Book p.4","scale":2,"direction":-1}]'},
                    files={"csv_file": ("x.csv", b"person_id,action,source,scale,direction\nu,c,https://c.io,5,1\n", "text/csv")})
    assert r.status_code == 200
    q = client.get("/api/queue").json()
    assert len(q) == 3
    html = client.get("/queue").text
    assert "Approve" in html
    for e in q:
        client.post(f"/queue/{e['id']}/approve", data={"reviewer_id": "r"})
    client.post("/api/people/u/publish", json={"published": True})
    page = client.get("/people/u").text
    assert "Net: 6.0" in page and 'href="https://s.org"' in page and "Book p.4" in page


def test_unproven_pure():
    u = {"scale": 50, "direction": 1, "reviewed_at": A, "status": "unproven"}
    assert score([u, e(2, -1)])["net"] == -2


def test_unproven_does_not_affect_net(client):
    a = sub(client, scale=10).json()["id"]
    u = sub(client, scale=99, source="https://claim.example").json()["id"]
    client.post(f"/api/entries/{a}/approve", json={"reviewer_id": "r"})
    r = client.post(f"/api/entries/{u}/mark-unproven", json={"reviewer_id": "many"})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "unproven" and j["reviewer_id"] == "many" and j["reviewed_at"]
    assert (j["scale"], j["direction"], j["source"]) == (99, 1, "https://claim.example")
    p = client.get("/api/people/p1").json()
    assert (p["positive_total"], p["negative_total"], p["net"]) == (10, 0, 10)
    assert [x["id"] for x in p["entries"]] == [a]
    assert [x["id"] for x in p["not_counted"]] == [u]
    assert client.get("/api/queue").json() == []
    assert client.post(f"/api/entries/{u}/mark-unproven", json={"reviewer_id": "x"}).status_code == 409
    assert client.post(f"/api/entries/{u}/approve", json={"reviewer_id": "x"}).status_code == 409
    assert client.post("/api/entries/999/mark-unproven", json={"reviewer_id": "x"}).status_code == 404
    html = client.get("/people/p1").text
    assert "Counted entries" in html and "Not counted / unproven" in html and "unproven" in html


def test_ui_mark_unproven(client):
    i = sub(client).json()["id"]
    assert "Mark unproven" in client.get("/queue").text
    client.post(f"/queue/{i}/mark-unproven", data={"reviewer_id": "r"})
    assert client.get("/api/people/p1").json()["not_counted"][0]["status"] == "unproven"


def test_sqlite_migration(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, action TEXT NOT NULL, "
              "source TEXT NOT NULL, scale REAL NOT NULL, direction INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, "
              "reviewed_at TIMESTAMP NULL, reviewer_id TEXT NULL)")
    c.execute("INSERT INTO entries VALUES (1,'p','a','s',3,1,'t','t','r'),(2,'p','b','s',5,1,'t',NULL,NULL)")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    assert db.get_entry(1)["status"] == "approved" and db.get_entry(2)["status"] == "pending"


def test_approved_marked_unproven_drops_out(client):
    a = sub(client, scale=10).json()["id"]
    b = sub(client, scale=3, direction=-1).json()["id"]
    for i in (a, b):
        client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"})
    assert client.get("/api/people/p1").json()["net"] == 7
    r = client.post(f"/api/entries/{a}/mark-unproven", json={"reviewer_id": "source-check"})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "unproven" and j["reviewer_id"] == "source-check" and j["reviewed_at"]
    p = client.get("/api/people/p1").json()
    assert (p["positive_total"], p["negative_total"], p["net"]) == (0, 3, -3)
    assert [x["id"] for x in p["not_counted"]] == [a]
    assert client.post(f"/api/entries/{a}/mark-unproven", json={"reviewer_id": "x"}).status_code == 409


def _approve(c, **kw):
    i = sub(c, **kw).json()["id"]
    c.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"})
    return i


def test_home_ranking_order(client):
    _approve(client, person_id="low-guy", scale=1, category="economy", unit="USD")
    _approve(client, person_id="top-person", scale=100, category="economy", unit="USD")
    _approve(client, person_id="neg-one", scale=5, direction=-1, category="economy", unit="USD")
    sub(client, person_id="pending-only", scale=1000)  # pending counts 0 -> hidden
    r = client.get("/?tab=overall")
    assert r.status_code == 200
    h = r.text
    order = [h.index(f'data-person="{p}"') for p in ("top-person", "low-guy", "neg-one")]
    assert order == sorted(order) and 'data-person="pending-only"' not in h
    assert 'href="/people/top-person"' in h and "Top Person" in h


def test_home_placeholder_initials(client):
    _approve(client, person_id="jonas-salk")
    h = client.get("/?tab=overall").text
    assert '<div class="avatar placeholder">JS</div>' in h and "Jonas Salk" in h


def test_set_person_image(client):
    _approve(client, person_id="jonas-salk")
    r = client.post("/api/people/jonas-salk/image", json={"image_url": "https://img.example/js.jpg"})
    assert r.status_code == 200 and r.json()["image_url"] == "https://img.example/js.jpg"
    client.post("/api/people/jonas-salk/image", json={"image_url": "https://img.example/2.jpg"})
    h = client.get("/?tab=overall").text
    assert '<img class="avatar" src="https://img.example/2.jpg"' in h and "avatar placeholder" not in h
    client.post("/people-image", data={"person_id": "jonas-salk", "image_url": ""})
    assert "avatar placeholder" in client.get("/?tab=overall").text


def test_submit_page_moved(client):
    h = client.get("/submit").text
    assert "Submit to queue" in h and "Import into queue" in h


def test_pct_100():
    assert score([e(5, 1), e(2.5, 1)])["positive_pct"] == 100


def test_pct_90():
    assert score([e(90, 1), e(10, -1)])["positive_pct"] == pytest.approx(90)


def test_pct_none_when_zero():
    assert score([])["positive_pct"] is None
    assert score([e(5, 1, approved=False)])["positive_pct"] is None
    from app.scoring import fmt_pct
    assert fmt_pct(None) == "\u2014" and fmt_pct(100.0) == "100%" and fmt_pct(90.0) == "90%"


def test_pct_display(client):
    a = _approve(client, person_id="jonas-salk", scale=9237.5, unit="lives")
    sub(client, person_id="nobody", scale=1)
    assert client.get("/api/people/jonas-salk").json()["positive_pct"] == 100
    assert client.get("/api/people/nobody").json()["positive_pct"] is None
    h = client.get("/?tab=overall").text
    assert '<span class="pos">100%</span> · <span class="neg">0%</span>' in h
    assert 'data-person="nobody"' not in h and "9,237.5" not in h and "bar" not in h
    t = client.get("/people/jonas-salk").text
    assert "Positive share: <span style='color:#1a8f4c'>100%</span>" in t
    assert "Negative share: <span style='color:#c93a3a'>0%</span>" in t


def test_negative_pct_90_10_and_none():
    r = score([e(90, 1), e(10, -1)])
    assert r["positive_pct"] == pytest.approx(90) and r["negative_pct"] == pytest.approx(10)
    assert score([])["negative_pct"] is None


def test_home_90_10_bar(client):
    _approve(client, person_id="mixed", scale=90, category="climate", unit="tCO2e")
    _approve(client, person_id="mixed", scale=10, direction=-1, category="climate", unit="tCO2e")
    assert client.get("/api/people/mixed").json()["overall"]["negative_pct"] == pytest.approx(10)
    h = client.get("/?tab=overall").text
    assert '<span class="pos">90%</span> · <span class="neg">10%</span>' in h


# ---------- credit sharing ----------
from app.scoring import credit_label


def test_share_math():
    es = [dict(e(100, 1), credit_share=0.6), dict(e(10, -1), credit_share=0.5), e(5, 1)]
    r = score(es)
    assert r["positive_total"] == pytest.approx(65) and r["negative_total"] == pytest.approx(5)
    assert r["net"] == pytest.approx(60) and r["positive_pct"] == pytest.approx(65 / 70 * 100)


def test_missing_share_defaults_to_one():
    assert score([dict(e(7, 1), credit_share=None)])["net"] == 7


def test_even_split_and_labels():
    x = dict(e(400, 1), credit_share=0.25, credit_basis="even_split")
    assert score([x])["net"] == pytest.approx(100)
    assert credit_label(x) == "Even split 1/4"
    assert credit_label(dict(x, credit_share=0.6, credit_basis="sourced_share")) == "Sourced share 60%"
    assert credit_label(e(1, 1)) == "Direct"


def test_estimate_excluded_by_default_pure():
    es = [e(10, 1), dict(e(90, 1), is_estimate=1)]
    assert score(es)["net"] == 10
    assert score(es, include_estimates=True)["net"] == 100


def test_submit_credit_fields_and_validation(client):
    r = sub(client, scale=400, credit_share=0.25, credit_basis="even_split", credit_note="Source lists 4 contributors")
    assert r.status_code == 201
    j = r.json()
    assert (j["credit_share"], j["credit_basis"], j["credit_note"], j["is_estimate"]) == (0.25, "even_split", "Source lists 4 contributors", 0)
    d = sub(client).json()
    assert (d["credit_share"], d["credit_basis"], d["is_estimate"]) == (1.0, "direct", 0)
    assert sub(client, credit_share=0).status_code == 422
    assert sub(client, credit_share=1.5).status_code == 422
    assert sub(client, credit_basis="vibes").status_code == 422


def test_estimate_toggle_api_and_pages(client):
    _approve(client, person_id="pp", scale=10)
    est = _approve(client, person_id="pp", scale=90, is_estimate=True)
    split = _approve(client, person_id="pp", scale=400, credit_share=0.25, credit_basis="even_split")
    p = client.get("/api/people/pp?include_estimates=0").json()
    assert p["net"] == 110 and [x["id"] for x in p["estimates"]] == [est]
    assert est not in [x["id"] for x in p["entries"]]
    p2 = client.get("/api/people/pp").json()
    assert p2["net"] == 200 and p2["estimates"] == [] and est in [x["id"] for x in p2["entries"]]
    assert [x for x in p2["entries"] if x["id"] == split][0]["effective_scale"] == 100
    h = client.get("/people/pp").text
    assert "Even split 1/4" in h and "est.</span>" in h and "Estimates (excluded)" not in h
    assert "include_estimates=0" in h
    assert "Estimates (excluded)" in client.get("/people/pp?include_estimates=0").text
    home = client.get("/?tab=overall").text
    assert 'href="/?tab=overall&include_estimates=0"' in home and "%" in home
    assert 'href="/people/pp?include_estimates=0"' in client.get("/?tab=overall&include_estimates=0").text


def test_approve_unproven_estimate(client):
    def mk(est):
        e = client.post("/api/entries", json={"person_id": "ue", "action": "a", "source": "https://x", "scale": 50,
                                              "direction": 1, "is_estimate": est}).json()["id"]
        client.post(f"/api/entries/{e}/mark-unproven", json={"reviewer_id": "r"})
        return e
    e, n = mk(True), mk(False)
    r = client.post(f"/api/entries/{e}/approve-estimate", json={"reviewer_id": "r"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.post(f"/api/entries/{n}/approve-estimate", json={"reviewer_id": "r"}).status_code == 409
    assert client.post(f"/api/entries/{e}/approve-estimate", json={"reviewer_id": "r"}).status_code == 409
    client.post("/api/people/ue/publish", json={"published": True})
    assert client.get("/api/people/ue").json()["net"] == 50

def test_credit_endpoint(client):
    i = _approve(client, person_id="cc", scale=100)
    r = client.post(f"/api/entries/{i}/credit", json={"credit_share": 0.6, "credit_basis": "sourced_share",
                                                      "credit_note": "Paper credits 60%", "is_estimate": False,
                                                      "reviewer_id": "many"})
    assert r.status_code == 200 and r.json()["credit_share"] == 0.6 and r.json()["reviewer_id"] == "many"
    assert client.get("/api/people/cc").json()["net"] == pytest.approx(60)
    client.post(f"/api/entries/{i}/credit", json={"credit_share": 1, "credit_basis": "direct", "is_estimate": True, "reviewer_id": "m"})
    assert client.get("/api/people/cc?include_estimates=0").json()["net"] == 0
    assert client.post("/api/entries/999/credit", json={"credit_share": 1, "credit_basis": "direct", "reviewer_id": "m"}).status_code == 404
    assert client.post(f"/api/entries/{i}/credit", json={"credit_share": 2, "credit_basis": "direct", "reviewer_id": "m"}).status_code == 422


def test_ui_submit_and_import_credit(client):
    client.post("/submit", data={"person_id": "u", "action": "a", "source": "s", "scale": "8", "direction": "1",
                                 "credit_share": "0.5", "credit_basis": "sourced_share", "credit_note": "", "is_estimate": "1"})
    client.post("/import", files={"csv_file": ("x.csv", b"person_id,action,source,scale,direction,credit_share,credit_basis\nu,c,s,4,1,0.25,even_split\nu,d,s,1,1,,\n", "text/csv")})
    q = client.get("/api/queue").json()
    assert [(x["credit_share"], x["credit_basis"], x["is_estimate"]) for x in q] == [(0.5, "sourced_share", 1), (0.25, "even_split", 0), (1.0, "direct", 0)]


def test_credit_migration_keeps_scores(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    path = str(tmp_path / "old2.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, action TEXT NOT NULL, "
              "source TEXT NOT NULL, scale REAL NOT NULL, direction INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, "
              "reviewed_at TIMESTAMP NULL, reviewer_id TEXT NULL, status TEXT NOT NULL DEFAULT 'pending')")
    c.execute("INSERT INTO entries VALUES (1,'p','a','s',9237.5,1,'t','t','r','approved')")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    e1 = db.get_entry(1)
    assert (e1["credit_share"], e1["credit_basis"], e1["credit_note"], e1["is_estimate"]) == (1.0, "direct", None, 0)
    assert score(db.person_entries("p"))["net"] == 9237.5


def test_person_image_credit(client):
    _approve(client, person_id="jonas-salk")
    r = client.post("/api/people/jonas-salk/image", json={"image_url": "https://img.example/js.jpg",
                    "image_credit": "Jane <Doe>", "image_license_url": "https://creativecommons.org/licenses/by-sa/3.0"})
    assert r.json()["image_credit"] == "Jane <Doe>"
    h = client.get("/people/jonas-salk").text
    assert 'class="photo-credit"' in h and "Photo: Jane &lt;Doe&gt;" in h
    assert 'href="https://creativecommons.org/licenses/by-sa/3.0"' in h


def test_person_page_no_credit_line(client):
    _approve(client, person_id="jonas-salk")
    client.post("/api/people/jonas-salk/image", json={"image_url": "https://img.example/js.jpg"})
    h = client.get("/people/jonas-salk").text
    assert '<img src="https://img.example/js.jpg"' in h and "photo-credit" not in h


def test_extra_sources(client):
    i = sub(client, person_id="p1").json()["id"]
    r = client.post(f"/api/entries/{i}/sources", json={"url": "https://two.example/a", "quote": "said <x>", "reviewer_id": "r"})
    assert r.status_code == 201 and r.json()["extra_sources"][0]["url"] == "https://two.example/a"
    assert client.get("/api/queue").json()[0]["extra_sources"][0]["quote"] == "said <x>"
    assert "https://two.example/a" in client.get("/queue").text and "said &lt;x&gt;" in client.get("/queue").text
    client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"})
    h = client.get("/people/p1").text
    assert "extra-source" in h and "https://two.example/a" in h
    assert client.get("/api/people/p1").json()["positive_total"] == 1
    assert client.post("/api/entries/999/sources", json={"url": "u", "reviewer_id": "r"}).status_code == 404


def test_correct_source_quote(client):
    i = sub(client).json()["id"]
    sid = client.post(f"/api/entries/{i}/sources", json={"url": "https://a.example", "quote": "old", "reviewer_id": "r"}).json()["extra_sources"][0]["id"]
    r = client.post(f"/api/entries/{i}/sources/{sid}/quote", json={"quote": "new"})
    assert r.json()["extra_sources"][0]["quote"] == "new"
    assert client.post(f"/api/entries/{i+1}/sources/{sid}/quote", json={"quote": "x"}).status_code == 404


def test_make_primary_source(client):
    i = sub(client).json()["id"]
    r = client.post(f"/api/entries/{i}/sources", json={"url": "https://better.example", "reviewer_id": "r", "make_primary": True})
    assert r.json()["source"] == "https://better.example" and r.json()["extra_sources"] == []
    assert client.get(f"/api/entries/{i}/sources").json()["source"] == "https://better.example"


def test_home_hides_zero_uncounted_and_org_reapprove(client):
    sub(client, person_id="nobody", scale=5)
    _approve(client, person_id="somebody", scale=5)
    h = client.get("/?tab=overall").text
    assert 'data-person="nobody"' not in h and 'data-person="somebody"' in h
    assert client.get("/api/people/nobody").status_code == 200
    e = sub(client, person_id="nobody", scale=30).json()["id"]
    client.post(f"/api/entries/{e}/mark-unproven", json={"reviewer_id": "r"})
    r = client.post(f"/api/entries/{e}/credit", json={"credit_share": 1, "credit_basis": "organization",
                                                       "is_estimate": False, "reviewer_id": "r"})
    assert r.status_code == 200
    assert client.post(f"/api/entries/{e}/reapprove", json={"reviewer_id": "r"}).json()["status"] == "approved"
    assert client.post(f"/api/entries/{e}/reapprove", json={"reviewer_id": "r"}).status_code == 409
    assert 'data-person="nobody"' in client.get("/?tab=overall").text
    assert "Organization" in client.get("/people/nobody").text



def test_categories_and_overall(client):
    _approve(client, person_id="cat", scale=2, unit="lives")                       # 2 x VSL
    _approve(client, person_id="cat", scale=1000, direction=-1, category="climate", unit="tCO2e")
    _approve(client, person_id="cat", scale=500, category="jobs", unit="jobs")     # not convertible
    _approve(client, person_id="cat", scale=50)                                    # people: not convertible
    v = client.get("/api/people/cat").json()
    assert {(c["category"], c["unit"]) for c in v["categories"]} == {("people", "lives"), ("climate", "tCO2e"),
                                                                     ("jobs", "jobs"), ("people", "people")}
    ov = v["overall"]
    assert ov["overall"] == pytest.approx(2 * 14_200_000 - 1000 * 190)
    assert set(ov["not_convertible"]) == {"jobs (jobs)", "people (people)"}
    assert sub(client, category="climate", unit="USD").status_code == 422
    h = client.get("/people/cat").text
    assert "By category" in h and "not convertible" in h
    assert "$28" in client.get("/?tab=overall").text
    assert {c["unit"] for c in client.get("/api/conversions").json()} == {"lives", "tCO2e", "USD"} | {f"EUR-{y}" for y in (2004, 2008, 2013, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025)}
    i = _approve(client, person_id="cat2", scale=7)
    r = client.post(f"/api/entries/{i}/category", json={"category": "economy", "unit": "USD", "reviewer_id": "r"})
    assert r.json()["category"] == "economy" and client.get("/api/people/cat2").json()["overall"]["overall"] == 7
    assert client.post(f"/api/entries/{i}/category", json={"category": "jobs", "unit": "x", "reviewer_id": "r"}).status_code == 422


def test_home_shows_nonconvertible_only_person(client):
    _approve(client, person_id="jobsonly", scale=10, category="jobs", unit="jobs")
    assert 'data-person="jobsonly"' in client.get("/?tab=overall").text


def test_usd_format():
    from app.main import usd
    assert usd(1.42e16) == "+$14,200T" and usd(-3.05e9) == "-$3.05B" and usd(0) == "$0"


def test_estimate_label_and_currency(client):
    _approve(client, person_id="estp", scale=3, unit="lives", is_estimate=True)
    v = client.get("/api/people/estp").json()["overall"]
    assert v["estimated"] is True
    assert "estimate</span>" in client.get("/?tab=overall").text and "driven by estimate" in client.get("/people/estp").text
    _approve(client, person_id="eur", scale=1000, direction=-1, category="economy", unit="EUR-2019")
    assert client.get("/api/people/eur").json()["overall"]["overall"] == pytest.approx(-1119.4)
    assert client.get("/api/people/eur").json()["overall"]["estimated"] is False
    assert sub(client, category="economy", unit="XYZ-1900").status_code == 422


def test_nonconvertible_lines_shown(client):
    _approve(client, person_id="mix2", scale=2, unit="lives")
    _approve(client, person_id="mix2", scale=78000, direction=-1)
    _approve(client, person_id="mix2", scale=500, category="jobs", unit="jobs")
    _approve(client, person_id="zero-but-counted", scale=50)
    h = client.get("/?tab=overall").text
    assert '<div class="other neg">-78,000 people</div>' in h and '<div class="other pos">+500 jobs</div>' in h
    assert 'data-person="zero-but-counted"' in h and "+50 people" in h
    assert "-78,000 people" in client.get("/people/mix2").text
    assert ["-", "-78,000 people"] in client.get("/api/people/mix2").json()["other"]


def test_category_tabs(client):
    _approve(client, person_id="saver", scale=10, direction=1, category="people", unit="lives")
    _approve(client, person_id="killer", scale=3, direction=-1, category="people", unit="lives")
    _approve(client, person_id="employer", scale=500, direction=1, category="jobs", unit="jobs")
    h = client.get("/").text  # default tab: lives
    assert 'class="tab active" href="/?tab=lives"' in h
    assert h.index('data-person="saver"') < h.index('data-person="killer"')
    assert 'data-person="employer"' not in h and "+10 lives" in h and "-3 lives" in h
    j = client.get("/?tab=jobs").text
    assert 'data-person="employer"' in j and 'data-person="saver"' not in j and "+500 jobs" in j
    assert [p["person_id"] for p in client.get("/api/ranking?tab=lives").json()] == ["saver", "killer"]
    assert 'data-person="employer"' in client.get("/?tab=overall").text
    assert client.get("/api/ranking?tab=bogus").status_code == 422


def test_culture_tab_ranks_by_certified_units(client):
    _approve(client, person_id="winner", scale=30, direction=1, category="culture", unit="grammys")
    _approve(client, person_id="seller", scale=2, direction=1, category="culture", unit="grammys")
    assert [p["net"] for p in client.get("/api/ranking?tab=culture").json()] == [30, 2]  # Grammys until certified units exist
    _approve(client, person_id="seller", scale=100, direction=1, category="culture", unit="certified-units")
    _approve(client, person_id="winner", scale=50, direction=1, category="culture", unit="certified-units")
    r = client.get("/api/ranking?tab=culture").json()
    assert [p["person_id"] for p in r] == ["seller", "winner"] and r[1]["net"] == 50
    assert "+30 grammys" in client.get("/?tab=culture").text
    _approve(client, person_id="recordman", scale=1, direction=1, category="culture", unit="guinness-records")
    assert "recordman" not in [p["person_id"] for p in client.get("/api/ranking?tab=culture").json()]
    assert client.get("/api/people/recordman").json()["categories"][0]["unit"] == "guinness-records"
    bad = client.post("/api/entries", json={"person_id": "x", "action": "a", "source": "https://e.org", "scale": 1,
                                            "direction": 1, "category": "culture", "unit": "influence"})
    assert bad.status_code == 422


def test_unpublished_people_hidden(client):
    body = {"person_id": "draft-guy", "action": "x", "source": "https://ex.com", "scale": 5, "direction": 1,
            "category": "jobs", "unit": "jobs"}
    i = client.post("/api/entries", json=body).json()["id"]
    client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"})
    assert client.get("/api/ranking?tab=jobs").json() == []
    assert 'data-person="draft-guy"' not in client.get("/?tab=overall").text
    assert client.get("/people/draft-guy").status_code == 404
    assert client.get("/api/people/draft-guy").status_code == 404
    assert client.get("/api/people/draft-guy?preview=1").status_code == 200
    assert client.post("/api/people/draft-guy/publish", json={"published": True}).json()["published"] is True
    assert [p["person_id"] for p in client.get("/api/ranking?tab=jobs").json()] == ["draft-guy"]
    assert client.get("/people/draft-guy").status_code == 200
    client.post("/api/people/draft-guy/publish", json={"published": False})
    assert client.get("/api/ranking?tab=jobs").json() == []


def test_migration_publishes_existing_people(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript(db.SQLITE_SCHEMA + "CREATE TABLE people (person_id TEXT PRIMARY KEY, image_url TEXT NULL);"
                      "INSERT INTO entries (person_id, action, source, scale, direction, created_at) VALUES ('old-guy','a','https://e.org',1,1,'2026-01-01');")
    con.commit(); con.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db._initialized.discard(path)
    db.init()
    assert "old-guy" in db.published_ids()


def test_admin_key_guard(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "k1")
    i = sub(client, person_id="guarded").json()["id"]  # submitting pending entries stays open
    assert client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"}).status_code == 401
    assert client.post("/api/people/guarded/publish", json={"published": True}).status_code == 401
    assert client.get("/api/people/guarded?preview=1").status_code == 401
    assert client.post(f"/api/entries/{i}/approve", json={"reviewer_id": "r"}, headers={"X-Admin-Key": "k1"}).status_code == 200
    assert client.get("/api/people/guarded?preview=1&admin_key=k1").status_code == 200
    j = sub(client, person_id="guarded").json()["id"]
    r = client.post(f"/queue/{j}/approve?admin_key=k1", data={"reviewer_id": "r"}, follow_redirects=False)
    assert r.status_code == 303 and "admin_key=k1" in r.headers["location"]
