import pytest
import time
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
    from app import main
    main._rate.clear()
    main.clear_cache()
    return TestClient(main.app)


def sub(c, **kw):
    body = {"person_id": "p1", "action": "x", "source": "https://ex.com", "scale": 1, "direction": 1, **kw}
    c.post(f"/api/people/{body['person_id']}/publish", json={"published": True})
    return c.post("/api/entries", json=body)


def test_unapproved_excluded_api(client):
    r = sub(client, scale=50)
    assert r.status_code == 201 and r.json()["reviewed_at"] is None
    p = client.get("/api/people/p1?preview=1").json()
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
    assert "<td class='r'>+6</td>" in page and 'href="https://s.org"' in page and "Book p.4" in page


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
    assert "Not counted, positive" in html and "Not counted, negative" in html


def test_ui_mark_unproven(client):
    i = sub(client).json()["id"]
    assert "Mark unproven" in client.get("/queue").text
    client.post(f"/queue/{i}/mark-unproven", data={"reviewer_id": "r"})
    assert client.get("/api/people/p1?preview=1").json()["not_counted"][0]["status"] == "unproven"


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
    # one tab each -> all listed under "Not enough data yet", ordered by entries then name
    order = [h.index(f'data-person="{p}"') for p in ("top-person", "low-guy", "neg-one")]  # net impact order
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
    assert client.get("/api/people/nobody?preview=1").json()["positive_pct"] is None
    h = client.get("/?tab=overall").text
    assert 'data-person="nobody"' not in h and "9,237.5" not in h and "bar" not in h
    t = client.get("/people/jonas-salk").text
    assert "Positive:" not in t and "Positive share" not in t  # no mixed-unit headline
    assert "<td class='r pos'>+9,237.5</td>" in t


def test_negative_pct_90_10_and_none():
    r = score([e(90, 1), e(10, -1)])
    assert r["positive_pct"] == pytest.approx(90) and r["negative_pct"] == pytest.approx(10)
    assert score([])["negative_pct"] is None


def test_home_90_10_bar(client):
    _approve(client, person_id="mixed", scale=90, category="climate", unit="tCO2e")
    _approve(client, person_id="mixed", scale=10, direction=-1, category="climate", unit="tCO2e")
    assert client.get("/api/people/mixed").json()["overall"]["negative_pct"] == pytest.approx(10)
    assert '<div class="positions">Climate +100</div>' in client.get("/?tab=overall").text


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
    assert '<img src="https://img.example/js.jpg" alt="Jonas Salk"' in h and 'class="photo-credit"' not in h


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
    assert client.post(f"/api/entries/{i+1}/sources/{sid}/remove").status_code == 404
    assert client.post(f"/api/entries/{i}/sources/{sid}/remove").json()["extra_sources"] == []


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
    assert client.get("/api/people/nobody").status_code == 404 and client.get("/api/people/nobody?preview=1").status_code == 200
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
    assert "Counted totals by category" in h and "Overall score <b>" in h and "Lives +300" in h
    assert "$" not in client.get("/?tab=overall").text.split('<div class="grid">')[1]
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
    assert "estimate</span>" in client.get("/?tab=overall").text and "<span class='badge'>est.</span>" in client.get("/people/estp").text
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
    assert '<div class="positions">Jobs +100, Lives +300, People -100</div>' in h and "Score +300" in h  # harm subtracts
    assert 'data-person="zero-but-counted"' in h
    assert "78,000 people harmed" in client.get("/people/mix2").text
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
    _approve(client, person_id="actor", scale=1, direction=1, category="culture", unit="academy-awards")
    assert client.get("/api/people/actor").json()["categories"][0]["unit"] == "academy-awards"


def test_culture_unit_subtoggles(client):
    _approve(client, person_id="a", scale=2, direction=1, category="culture", unit="academy-awards")
    _approve(client, person_id="b", scale=1, direction=1, category="culture", unit="academy-awards")
    _approve(client, person_id="b", scale=9, direction=1, category="culture", unit="grammys")
    _approve(client, person_id="c", scale=3, direction=1, category="culture", unit="guinness-records")
    o = client.get("/api/ranking?tab=culture&unit=oscars").json()
    assert [(p["person_id"], p["net"]) for p in o] == [("a", 2), ("b", 1)]  # Grammys not summed in
    assert [p["person_id"] for p in client.get("/api/ranking?tab=culture&unit=guinness").json()] == ["c"]
    assert [p["person_id"] for p in client.get("/api/ranking?tab=culture&unit=grammys").json()] == ["b"]
    assert client.get("/api/ranking?tab=culture&unit=bogus").status_code == 422
    assert client.get("/api/ranking?tab=jobs&unit=oscars").status_code == 422
    h = client.get("/?tab=culture&unit=oscars").text
    assert 'href="/?tab=culture&unit=oscars"' in h and "Guinness records" in h and "Certified units" not in h
    assert 'data-person="a"' in h and 'data-person="c"' not in h and "/cards/tab/culture.png?unit=oscars&amp;size=x" in h
    r = client.get("/cards/tab/culture.png?unit=oscars")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    _approve(client, person_id="d", scale=5, direction=1, category="culture", unit="certified-units")
    assert "Certified units" in client.get("/?tab=culture").text
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


def test_share_cards_and_og(client):
    from PIL import Image
    import io as _io
    _approve(client, person_id="card-guy", scale=12, direction=1, category="jobs", unit="jobs")
    _approve(client, person_id="card-guy", scale=3, direction=-1, category="people", unit="lives", is_estimate=True)
    from app import main as m
    lines = m.person_card_lines("card-guy")
    assert ("+", "+12 jobs", False) in lines and ("-", "-3 lives", True) in lines
    for path, size in (("/cards/person/card-guy.png", (1080, 1350)), ("/cards/person/card-guy.png?size=x", (1200, 675)),
                       ("/cards/tab/jobs.png", (1080, 1350)), ("/cards/tab/overall.png?size=x", (1200, 675))):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert Image.open(_io.BytesIO(r.content)).size == size
    assert client.get("/cards/tab/nope.png").status_code == 404
    assert client.get("/cards/person/card-guy.png?size=huge").status_code == 422
    h = client.get("/people/card-guy").text
    assert 'property="og:image" content="http://testserver/cards/person/card-guy.png?size=x"' in h
    assert 'name="twitter:card" content="summary_large_image"' in h
    assert 'og:image" content="http://testserver/cards/tab/jobs.png?size=x"' in client.get("/?tab=jobs").text
    body = {"person_id": "hidden-card", "action": "x", "source": "https://ex.com", "scale": 5, "direction": 1}
    client.post("/api/entries", json=body)
    assert client.get("/cards/person/hidden-card.png").status_code == 404


def test_display_name_override(client):
    _approve(client, person_id="doug-mcmillon")
    client.post("/api/people/doug-mcmillon/publish", json={"published": True})
    assert client.get("/api/ranking?tab=overall").json()[0]["name"] == "Doug Mcmillon"
    r = client.post("/api/people/doug-mcmillon/name", json={"display_name": " Doug McMillon "})
    assert r.json()["display_name"] == "Doug McMillon"
    assert client.get("/api/ranking?tab=overall").json()[0]["name"] == "Doug McMillon"
    client.post("/api/people/doug-mcmillon/name", json={"display_name": ""})
    assert client.get("/api/ranking?tab=overall").json()[0]["name"] == "Doug Mcmillon"


def test_not_counted_split_by_sign(client):
    for d, act in ((1, "good-claim"), (-1, "bad-claim")):
        e = client.post("/api/entries", json={"person_id": "split", "action": act, "source": "https://e.org",
                                              "scale": 5, "direction": d}).json()
        client.post(f"/api/entries/{e['id']}/mark-unproven", json={"reviewer_id": "r"})
    _approve(client, person_id="split", scale=2, direction=1)
    h = client.get("/people/split").text
    pos, neg = h.index("Not counted, positive"), h.index("Not counted, negative")
    assert pos < h.index("good-claim") < neg < h.index("bad-claim")
    v = client.get("/api/people/split").json()
    assert v["positive_total"] == 2 and v["negative_total"] == 0  # unproven never counts


def test_tab_card_labels_and_counts(client):
    from app import main as m
    _approve(client, person_id="a", scale=2, direction=1, category="culture", unit="academy-awards")
    _approve(client, person_id="b", scale=1, direction=1, category="culture", unit="academy-awards")
    _approve(client, person_id="c", scale=3, direction=1, category="culture", unit="guinness-records")
    _approve(client, person_id="d", scale=1, direction=1, category="culture", unit="grammys")
    title, rows, _ = m.card_tab_data("culture", "oscars")
    assert title == "Top 2 \u00b7 Culture \u00b7 Oscars"
    assert [r[2] for r in rows] == ["+2 Oscars", "+1 Oscar"]
    title, rows, _ = m.card_tab_data("culture", "guinness")
    assert title.startswith("Top 1 ") and rows[0][2] == "+3 Guinness records"
    assert m.card_tab_data("culture", "grammys")[1][0][2] == "+1 Grammy"
    assert m.card_tab_data("culture")[1][0][2] == "+1 Grammy"
    assert "academy-awards" not in str(m.card_tab_data("culture", "oscars"))
    assert m.card_tab_data("climate")[0] == "Top \u00b7 Climate"


def test_overall_card_skips_zero_and_formats_big(client):
    from app import main as m
    _approve(client, person_id="singer", scale=5, direction=1, category="culture", unit="grammys")
    _approve(client, person_id="saver", scale=10, direction=1, category="people", unit="lives", is_estimate=True)
    title, rows, note = m.card_tab_data("overall")
    assert title.endswith("Overall") and {r[0] for r in rows} == {"Singer", "Saver"} and "$" not in note
    assert m.card_usd(1.42e16) == "+$14.2 quadrillion" and m.card_usd(1.56e10) == "+$15.6 billion"
    assert m.card_usd(-2.5e12) == "-$2.5 trillion" and m.card_usd(9.5e5) == "+$950K"
    assert m.card_usd(3e18) == "+$3,000 quadrillion"
    r = client.get("/cards/tab/overall.png?size=x")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_adsense_pages(client):
    r = client.get("/ads.txt")
    assert r.status_code == 200 and r.text.strip() == "google.com, pub-1880568133397191, DIRECT, f08c47fec0942fa0"
    h = client.get("/").text
    assert 'adsbygoogle.js?client=ca-pub-1880568133397191' in h.split("</head>")[0]
    assert '<meta name="google-adsense-account" content="ca-pub-1880568133397191">' in h
    assert 'href="/privacy"' in h and 'href="/about"' in h
    for path in ("/privacy", "/about", "/submit"):
        r = client.get(path)
        assert r.status_code == 200 and "google-adsense-account" in r.text
    assert "policies.google.com/technologies/ads" in client.get("/privacy").text


# ---------- audit fixes: public pages, SEO, cache, limits ----------
def _pub(client, pid="ada-lovelace", **kw):
    i = _approve(client, person_id=pid, **kw)
    client.post(f"/api/people/{pid}/publish", json={"published": True})
    return i


def test_person_page_public_view(client):
    _pub(client, scale=120_000_000, unit="lives")
    _pub(client, scale=14, direction=-1, unit="lives")
    client.post("/api/people/ada-lovelace/name", json={"display_name": "Ada Lovelace"})
    client.post("/api/people/ada-lovelace/image", json={"image_url": "https://img.example/a.jpg", "image_credit": "X"})
    h = client.get("/people/ada-lovelace").text
    assert 'name="viewport"' in h and "<title>Ada Lovelace: 120 million lives saved · Output</title>" in h
    assert "<h1>Ada Lovelace</h1>" in h
    assert 'alt="Ada Lovelace"' in h and "e+0" not in h and "120 million" in h
    assert "reviewer" not in h.lower() and "2020-01" not in h and "T00:" not in h
    assert "Positive:" not in h and "Not counted, positive" in h and "Not counted, negative" in h
    assert 'rel="canonical" href="http://testserver/people/ada-lovelace"' in h
    assert ('<meta name="description" content="Ada Lovelace: 120 million lives saved, 14 lives lost. '
            'Counted impact on Output, backed by 2 trusted sources."') in h
    assert 'property="og:title" content="Ada Lovelace: 120 million lives saved · Output"' in h
    assert 'name="twitter:title" content="Ada Lovelace: 120 million lives saved · Output"' in h
    assert "Ada Lovelace’s counted impact is 120 million lives saved, and also 14 lives lost." in h


def test_num_format():
    from app.main import num
    assert num(1.2e8) == "120 million" and num(1_250_000_000) == "1.25 billion" and num(9237.5) == "9,237.5"
    assert num(14, True) == "+14" and num(-14) == "-14" and num(0) == "0"


def test_public_nav_and_viewport(client):
    for path in ("/", "/about", "/privacy", "/?tab=jobs", "/submit", "/queue"):
        h = client.get(path).text
        assert 'name="viewport"' in h, path
    for path in ("/", "/about", "/privacy"):
        h = client.get(path).text
        assert 'href="/queue"' not in h and "Set a person" not in h and "/people-image" not in h
        assert '<nav><a href="/submit"' not in h
    assert client.get("/submit").status_code == 200 and client.get("/queue").status_code == 200


def test_tab_meta_unique(client):
    titles = set()
    for t in ("lives", "people", "jobs", "economy", "climate", "nature", "culture", "overall"):
        h = client.get(f"/?tab={t}").text
        titles.add(h.split("<title>")[1].split("</title>")[0])
        assert "<h1" in h and 'name="description"' in h and 'rel="canonical"' in h
    assert len(titles) == 8


def test_seo_files(client):
    _pub(client, scale=3, unit="lives")
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Allow: /" in r.text and "Sitemap: http://testserver/sitemap.xml" in r.text
    x = client.get("/sitemap.xml").text
    for u in ("http://testserver/<", "/?tab=overall", "/people/ada-lovelace", "/about", "/privacy"):
        assert u in x
    assert client.get("/favicon.ico").status_code == 200 and client.get("/favicon.svg").status_code == 200
    assert x.count("<lastmod>") == x.count("<loc>") and "<lastmod>" in x
    assert "T" not in x.split("<lastmod>")[1].split("</lastmod>")[0]


def test_docs_need_admin_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "k")
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404
        assert client.get(p + "?admin_key=k").status_code == 200


def test_security_headers_and_rate_limit(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff" and "max-age" in r.headers["strict-transport-security"]
    assert r.headers["referrer-policy"]
    form = {"person_id": "u", "action": "a", "source": "s", "scale": "1", "direction": "1"}
    codes = [client.post("/submit", data=form, follow_redirects=False).status_code for _ in range(11)]
    assert codes[:10] == [303] * 10 and codes[10] == 429
    assert client.post("/import", data={"json_text": "[]"}).status_code == 429


def test_cache_cleared_on_writes(client):
    i = _pub(client, scale=3, unit="lives")
    assert "+3 lives" in client.get("/").text
    j = sub(client, person_id="ada-lovelace", scale=5, unit="lives").json()["id"]
    assert "+3 lives" in client.get("/").text
    client.post(f"/api/entries/{j}/approve", json={"reviewer_id": "r"})
    assert "+8 lives" in client.get("/").text
    client.post(f"/api/entries/{i}/mark-unproven", json={"reviewer_id": "r"})
    assert "+5 lives" in client.get("/").text
    client.post("/api/people/ada-lovelace/name", json={"display_name": "Countess Ada"})
    assert "Countess Ada" in client.get("/").text and "<h1>Countess Ada</h1>" in client.get("/people/ada-lovelace").text
    client.post("/api/people/ada-lovelace/publish", json={"published": False})
    assert client.get("/people/ada-lovelace").status_code == 404


def test_local_photo_served(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "PHOTO_MANIFEST", {"ada-lovelace": "https://img.example/a.jpg"})
    monkeypatch.setattr(main, "local_photo_path", lambda pid, url: "x" if main.PHOTO_MANIFEST.get(pid) == url else None)
    _pub(client, scale=3, unit="lives")
    client.post("/api/people/ada-lovelace/image", json={"image_url": "https://img.example/a.jpg", "image_credit": "Jane"})
    h = client.get("/people/ada-lovelace").text
    assert 'src="/static/people/ada-lovelace.jpg"' in h and "Photo: Jane" in h
    client.post("/api/people/ada-lovelace/image", json={"image_url": "https://img.example/new.jpg", "image_credit": "Jane"})
    assert 'src="https://img.example/new.jpg"' in client.get("/people/ada-lovelace").text


def test_mirrored_photos_exist():
    from app import main
    assert main.PHOTO_MANIFEST and all(main.local_photo_path(p, u) for p, u in main.PHOTO_MANIFEST.items())


def test_about_company_filing_rule(client):
    h = client.get("/about").text
    assert "official SEC filing" in h and "Self-reported company figures don't count on their own" not in h


def test_published_needs_counted_entry(client, monkeypatch):
    sub(client, person_id="nocount", scale=1)  # published, only a pending entry
    i = sub(client, person_id="unp", scale=1).json()["id"]
    client.post(f"/api/entries/{i}/mark-unproven", json={"reviewer_id": "r"})
    _approve(client, person_id="yes", scale=2, unit="lives")
    for pid in ("nocount", "unp"):
        assert client.get(f"/people/{pid}").status_code == 404
        assert client.get(f"/cards/person/{pid}.png").status_code == 404
        assert client.get(f"/api/people/{pid}").status_code == 404
        assert client.get(f"/people/{pid}?preview=1").status_code == 200
    x = client.get("/sitemap.xml").text
    assert "/people/yes" in x and "/people/nocount" not in x and "/people/unp" not in x
    monkeypatch.setenv("ADMIN_KEY", "k")
    assert client.get("/people/nocount?preview=1").status_code == 401
    assert client.get("/people/nocount?preview=1&admin_key=k").status_code == 200


def test_influence_tab_neutral(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "INFLUENCE", {"period": "P", "languages_as_of": "D", "people": {
        "a": {"title": "A", "views": 5_000_000, "languages": 40, "wikidata": "Q1"},
        "b": {"title": "B", "views": 900, "languages": 120, "wikidata": "Q2"},
        "hidden": {"title": "H", "views": 10**9, "languages": 300}}})
    _approve(client, person_id="a", scale=1, unit="lives")
    _approve(client, person_id="a", scale=1, category="jobs", unit="jobs")
    _approve(client, person_id="b", scale=5, unit="lives")
    _approve(client, person_id="b", scale=5, category="jobs", unit="jobs")
    client.post("/api/people/hidden/publish", json={"published": True})  # published, nothing counted
    before = client.get("/api/ranking?tab=overall").json()
    r = client.get("/api/ranking?tab=influence").json()  # default sub-tab: languages
    assert [p["person_id"] for p in r] == ["b", "a"] and r[0]["label"] == "120 languages"
    r = client.get("/api/ranking?tab=influence&unit=attention").json()
    assert [p["person_id"] for p in r] == ["a", "b"] and r[0]["label"] == "5 million views"
    assert 'href="/?tab=influence&amp;unit=attention"' in client.get("/?tab=influence").text or \
        'href="/?tab=influence&unit=attention"' in client.get("/?tab=influence").text
    assert "Pantheon" in client.get("/?tab=influence").text and "page views" in client.get("/?tab=influence&unit=attention").text
    assert "/?tab=influence&amp;unit=attention" in client.get("/sitemap.xml").text
    assert client.get("/api/ranking?tab=overall").json() == before
    assert all(t["tab"] != "Influence" for p in before for t in p["tabs"])
    assert client.get("/people/hidden").status_code == 404  # influence doesn't make anyone public
    h = client.get("/?tab=influence").text
    assert "reach, not impact" in h and 'class="other' not in h.split('<div class="grid">')[1]
    assert "not impact" in client.get("/people/a").text and "Influence" in client.get("/about").text
    title, rows, note = m.card_tab_data("influence")
    assert rows[0] == ("B", "=", "120 languages", False) and "not impact" in note and "Wikidata" in note
    assert m.card_tab_data("influence", "attention")[1][0] == ("A", "=", "5 million views", False)
    assert "40</b> languages" in client.get("/people/a").text
    assert client.get("/cards/tab/influence.png").status_code == 200


def test_broken_photo_falls_back_to_initials(client):
    _approve(client, person_id="ada-lovelace", scale=1, unit="lives")
    client.post("/api/people/ada-lovelace/image", json={"image_url": "https://img.example/broken.jpg"})
    h = client.get("/").text
    assert "onerror=" in h and "initials-fallback" in h.split("onerror=")[1][:120] and "AL" in h.split("onerror=")[1][:160]
    assert "onerror=" in client.get("/people/ada-lovelace").text
    client.post("/api/people/ada-lovelace/image", json={"image_url": ""})
    assert '<div class="avatar placeholder">AL</div>' in client.get("/").text


HUMAN = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"}


def _beacon(client, path, headers=None, ref="", **extra):
    h = {**HUMAN, "origin": "http://testserver", **(headers or {})}
    return client.post("/v", json={"p": path, "r": ref, **extra}, headers=h)


def test_pageview_stats(client, monkeypatch):
    from app import main as m
    m._views.clear()
    m._beacon_hits.clear()
    _approve(client, person_id="ada-lovelace", scale=1, unit="lives")
    monkeypatch.setenv("ADMIN_KEY", "k")
    page = client.get("/?tab=jobs", headers=HUMAN)
    assert "sendBeacon('/v'" in page.text and "googletagmanager" not in page.text and "plausible" not in page.text
    assert "set-cookie" not in page.headers
    # Fetching the HTML is not a visit, and neither is a bot, a preview, a headless browser,
    # a hosting network, the team box, Tor, or a browser that opted out.
    _beacon(client, "/?tab=jobs", ref="https://t.co/abc", headers={"cf-ipcountry": "US"})
    _beacon(client, "/?tab=jobs", headers={"cf-ipcountry": "US"})
    _beacon(client, "/people/ada-lovelace", headers={"cf-ipcountry": "US", "x-forwarded-for": "203.0.113.50"})
    _beacon(client, "/", headers={"user-agent": "Googlebot/2.1"})
    _beacon(client, "/", headers={"user-agent": "curl/8.0"})
    _beacon(client, "/", headers={"user-agent": "Mozilla/5.0 HeadlessChrome/120.0"})
    _beacon(client, "/", headers={"user-agent": "Twitterbot/1.0"})
    _beacon(client, "/", headers={**HUMAN, "x-forwarded-for": "3.5.140.1"})          # AWS
    _beacon(client, "/", headers={**HUMAN, "x-forwarded-for": "104.28.208.14"})      # team box
    _beacon(client, "/", headers={**HUMAN, "cf-ipcountry": "T1"})                     # Tor
    _beacon(client, "/?preview=1")
    _beacon(client, "/api/ranking")
    assert client.post("/v", json={"p": "/"}, headers={**HUMAN, "origin": "https://evil.example"}).status_code == 204
    assert client.get("/admin/stats").status_code == 404
    h = client.get("/admin/stats?admin_key=k").text
    assert "<b>3</b> page views" in h and "t.co" in h and ">US<" in h and "/people/ada-lovelace" in h
    assert "/?tab=jobs" in h and "Googlebot" not in h and "203.0.113.50" not in h and "104.28.208.14" not in h
    assert "Unique visitors" in h and "Eastern" in h
    assert "datacenter" in h and "headless" in h and "team" in h and "tor" in h
    st = m.db.pageview_stats()
    assert dict(st["pages"]) == {"/?tab=jobs": 2} and dict(st["people"]) == {"/people/ada-lovelace": 1}
    assert sum(n for _, n in st["uniques_by_day"]) == 2  # testclient address, plus 203.0.113.50
    # The admin response set the opt-out cookie, so this browser is no longer counted.
    assert "out_skip" in client.get("/admin/stats?admin_key=k").headers.get("set-cookie", "")
    _beacon(client, "/about", headers={"cf-ipcountry": "US"})
    assert "<b>3</b> page views" in client.get("/admin/stats?admin_key=k").text


def test_visit_classification(monkeypatch):
    from datetime import datetime, timezone
    from app.visitors import classify_visit, eastern_day, ip_hash, is_datacenter, is_team_ip, ua_class
    assert eastern_day(datetime(2026, 10, 19, 3, 30, tzinfo=timezone.utc)) == "2026-10-18"
    assert ua_class("Mozilla/5.0 Safari/604.1") == "browser"
    assert ua_class("Mozilla/5.0 HeadlessChrome/120") == "headless"
    assert ua_class("Twitterbot/1.0") == "preview"
    assert ua_class("curl/8.0") == "script"
    assert ua_class("Googlebot/2.1") == "bot"
    assert is_datacenter("8.8.8.8") is False and is_datacenter("127.0.0.1") is False
    assert is_datacenter("3.5.140.1") and is_datacenter("34.1.1.1") and is_datacenter("167.99.1.1")
    assert is_datacenter("116.202.1.1") and is_datacenter("51.38.1.1") and is_datacenter("43.128.1.1")
    assert is_datacenter("104.28.208.14") is False
    monkeypatch.delenv("TEAM_IPS", raising=False)
    assert is_team_ip("104.28.208.14") and is_team_ip("203.0.113.9") is False
    monkeypatch.setenv("TEAM_IPS", "203.0.113.9, 10.0.0.0/8")
    assert is_team_ip("203.0.113.9") and is_team_ip("10.1.2.3") and is_team_ip("104.28.208.14") is False
    monkeypatch.setenv("TEAM_IPS", "")
    assert is_team_ip("104.28.208.14") is False
    assert classify_visit("203.0.113.8", "Mozilla/5.0", "US", False) == ("browser", True)
    assert classify_visit("3.5.140.1", "Mozilla/5.0", "US", False)[0] == "datacenter"
    assert classify_visit("203.0.113.8", "Mozilla/5.0", "T1", False)[0] == "tor"
    assert classify_visit("203.0.113.8", "Mozilla/5.0", "US", True)[0] == "optout"
    monkeypatch.setenv("VIEW_SALT", "s")
    assert ip_hash("203.0.113.8", "2026-10-18") != ip_hash("203.0.113.8", "2026-10-19")
    assert "203.0.113.8" not in ip_hash("203.0.113.8", "2026-10-18")


def test_feedback_form(client, monkeypatch):
    _approve(client, person_id="ada-lovelace", scale=1, unit="lives")
    monkeypatch.setenv("ADMIN_KEY", "k")
    assert "Suggest a person or feature" in client.get("/").text and "/feedback" in client.get("/about").text
    assert "/feedback?page=/people/ada-lovelace" in client.get("/people/ada-lovelace").text
    assert 'name="website"' in client.get("/feedback").text
    r = client.post("/feedback", data={"message": "Add <Marie Curie>", "email": "a@b.co", "page": "/people/ada-lovelace"},
                    follow_redirects=False)
    assert r.status_code == 303
    client.post("/feedback", data={"message": "spam", "website": "http://spam"})       # honeypot
    assert client.post("/feedback", data={"message": ""}).status_code == 422
    assert client.post("/feedback", data={"message": "x", "email": "nope"}).status_code == 422
    assert client.get("/admin/feedback").status_code == 404
    h = client.get("/admin/feedback?admin_key=k").text
    assert "Add &lt;Marie Curie&gt;" in h and "a@b.co" in h and "spam" not in h
    codes = [client.post("/feedback", data={"message": "m"}, follow_redirects=False).status_code for _ in range(10)]
    assert 429 in codes
    assert "anonymous" in client.get("/privacy").text.lower() and "Feedback" in client.get("/privacy").text


def test_search_copy_notes_and_canonical_host(client, monkeypatch):
    import json as _json
    import re
    from app import main

    people = dict(main.INFLUENCE.get("people") or {})
    people["norman-borlaug"] = {"title": "Norman Borlaug", "wikidata": "Q171973", "languages": 40, "views": 12}
    monkeypatch.setitem(main.INFLUENCE, "people", people)

    action = ("High-yield wheat credited with averting famine (Guardian) [CONFIRMS ~1 billion]; "
              "magnitude 1.0; counterfactual, shared credit")
    created = sub(client, person_id="norman-borlaug", scale=1_000_000_000, unit="lives", is_estimate=True,
                  action=action, credit_note="Rule 4: lowest documented number. Replaces entry #19.")
    queued = client.get("/queue").text
    assert "magnitude 1.0" in queued and "[CONFIRMS ~1 billion]" in queued and "Replaces entry" not in queued
    client.post(f"/api/entries/{created.json()['id']}/approve", json={"reviewer_id": "r"})
    client.post(f"/api/entries/{created.json()['id']}/sources", json={
        "url": "https://www.govinfo.gov/content/pkg/PLAW-109publ395/html/PLAW-109publ395.htm",
        "quote": "[CONFIRMS ~1 billion] Public Law 109-395: more than a billion people.",
        "reviewer_id": "r"})
    _approve(client, person_id="florence-nightingale", scale=10, unit="lives")
    _approve(client, person_id="recordman", scale=2, direction=1, category="culture", unit="guinness-records")
    _approve(client, person_id="spender", scale=250, direction=-1, category="economy", unit="USD")
    _approve(client, person_id="noted", scale=4, unit="people", credit_note="Source lists 4 contributors",
             action="Named alongside three others")
    client.post("/api/people/norman-borlaug/image",
                json={"image_url": "https://img.example/borlaug.jpg", "image_credit": "Photo desk"})

    h = client.get("/people/norman-borlaug").text
    head = h.split("</head>")[0]
    assert "<title>Norman Borlaug: ~1 billion lives saved (est.) · Output</title>" in head
    assert 'property="og:title" content="Norman Borlaug: ~1 billion lives saved (est.) · Output"' in head
    assert 'name="twitter:title" content="Norman Borlaug: ~1 billion lives saved (est.) · Output"' in head
    assert "guinness-records" not in head and "-$250 economy" not in head
    assert "High-yield wheat credited with averting famine (Guardian)" in h
    assert "https://ex.com" in h and "govinfo.gov" in h
    assert "Public Law 109-395: more than a billion people." in h
    for hidden in ("CONFIRMS", "magnitude", "counterfactual", "Rule 4", "Replaces entry"):
        assert hidden not in h, hidden
    assert "Norman Borlaug’s counted impact is ~1 billion lives saved (est.)" in h
    assert 'href="/people/florence-nightingale"' in h and "Florence Nightingale" in h
    assert 'href="/"' in h and "Lives ranking" in h
    api = client.get("/api/people/norman-borlaug").json()
    assert api["net"] == 1_000_000_000
    assert api["entries"][0]["credit_note"].startswith("Rule 4")
    assert "magnitude 1.0" in api["entries"][0]["action"]
    preview = client.get("/people/norman-borlaug?preview=1").text
    assert "Rule 4" in preview and "magnitude 1.0" in preview and "[CONFIRMS ~1 billion]" in preview

    noted = client.get("/people/noted").text
    assert "Source lists 4 contributors" in noted and "Named alongside three others" in noted

    rec = client.get("/people/recordman").text.split("</head>")[0]
    assert "2 Guinness World Records" in rec and "guinness-records" not in rec
    econ = client.get("/people/spender").text.split("</head>")[0]
    assert "$250 in economic losses" in econ and "-$250 economy" not in econ

    blobs = re.findall(r'<script type="application/ld\+json">(.*?)</script>', h)
    kinds = [_json.loads(b) for b in blobs]
    person = next(b for b in kinds if b["@type"] == "Person")
    crumbs = next(b for b in kinds if b["@type"] == "BreadcrumbList")
    assert person["name"] == "Norman Borlaug" and person["image"] == "https://img.example/borlaug.jpg"
    assert "https://en.wikipedia.org/wiki/Norman_Borlaug" in person["sameAs"]
    assert "https://www.wikidata.org/wiki/Q171973" in person["sameAs"]
    assert [c["name"] for c in crumbs["itemListElement"]] == ["Output", "Norman Borlaug"]
    assert crumbs["itemListElement"][0]["item"] == "http://testserver/"

    home = client.get("/").text
    title = home.split("<title>")[1].split("</title>")[0]
    assert title == "Output · Famous people ranked by their impact on Earth"
    assert f'property="og:title" content="{title}"' in home
    assert 'property="og:url" content="http://testserver/"' in home
    assert 'property="og:url" content="http://testserver/?tab=' not in home
    listing = _json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', home).group(1))
    assert listing["@type"] == "ItemList"
    assert [i["name"] for i in listing["itemListElement"]][:2] == ["Norman Borlaug", "Florence Nightingale"]
    assert listing["itemListElement"][0]["position"] == 1
    jobs = client.get("/?tab=jobs").text
    assert "<title>Jobs ranking · Output</title>" in jobs
    assert 'property="og:title" content="Jobs ranking · Output"' in jobs
    assert 'property="og:url" content="http://testserver/?tab=jobs"' in jobs

    gone = client.get("http://output-orrw.onrender.com/people/norman-borlaug?x=1", follow_redirects=False)
    assert gone.status_code == 301
    assert gone.headers["location"] == "https://outputrank.com/people/norman-borlaug?x=1"
    www = client.get("http://www.outputrank.com/about?q=1", follow_redirects=False)
    assert www.status_code == 301 and www.headers["location"] == "https://outputrank.com/about?q=1"
    assert client.get("/people/norman-borlaug").status_code == 200

    assert "google-site-verification" not in client.get("/about").text
    monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "token-abc")
    assert 'name="google-site-verification" content="token-abc"' in client.get("/about").text
    monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "")
    assert "google-site-verification" not in client.get("/privacy").text


PG_BIN = "/usr/lib/postgresql/18/bin"


@pytest.fixture
def pg_url(tmp_path):
    """Throwaway real Postgres cluster (skipped when the server binaries aren't installed)."""
    import os, shutil, subprocess, socket
    if not os.path.exists(f"{PG_BIN}/initdb") or os.geteuid() == 0:
        pytest.skip("postgres server not available")
    data, sock = tmp_path / "pg", tmp_path / "sock"
    sock.mkdir()
    subprocess.run([f"{PG_BIN}/initdb", "-D", str(data), "-U", "t", "--auth=trust"], check=True, capture_output=True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    subprocess.run([f"{PG_BIN}/pg_ctl", "-D", str(data), "-l", str(tmp_path / "pg.log"),
                    "-o", f"-p {port} -k {sock} -c listen_addresses=127.0.0.1", "-w", "start"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    try:
        yield f"postgresql://t@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([f"{PG_BIN}/pg_ctl", "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


def test_stats_and_feedback_on_real_postgres(pg_url, monkeypatch):
    from app import db, main as m
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("ADMIN_KEY", "k")
    m._rate.clear(); m.clear_cache(); m._views.clear()
    db.init()
    c = TestClient(m.app)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    db.add_pageviews({(day, "/?tab=jobs", "t.co", "US"): 2, (day, "/people/a", "", ""): 1})
    db.add_pageviews({(day, "/?tab=jobs", "t.co", "US"): 1})
    st = db.pageview_stats()
    assert dict(st["pages"]) == {"/?tab=jobs": 3} and dict(st["people"]) == {"/people/a": 1}
    r = c.get("/admin/stats?admin_key=k")
    assert r.status_code == 200 and "<b>4</b> page views" in r.text
    db.add_feedback("hello", None, "/people/a")
    assert c.get("/admin/feedback?admin_key=k").status_code == 200 and db.list_feedback()[0]["message"] == "hello"


def test_economy_harms_only_notice(client):
    _approve(client, person_id="small-fine", scale=250, direction=-1, category="economy", unit="USD")
    _approve(client, person_id="big-fine", scale=500000, direction=-1, category="economy", unit="USD")
    r = client.get("/api/ranking?tab=economy").json()
    assert [p["person_id"] for p in r] == ["small-fine", "big-fine"]  # net order, smallest harm first
    html = client.get("/?tab=economy").text
    assert "harms-only" in html and "not a win" in html
    _approve(client, person_id="creator", scale=1000, direction=1, category="economy", unit="USD")
    assert client.get("/api/ranking?tab=economy").json()[0]["person_id"] == "creator"
    assert "harms-only" not in client.get("/?tab=economy").text




def test_overall_total_net_impact(client):
    for pid, kw in [("borlaug", dict(scale=1e9, unit="lives")), ("saver", dict(scale=1000, unit="lives")),
                    ("killer", dict(scale=215, direction=-1, unit="lives")),
                    ("fined", dict(scale=250, direction=-1, category="economy", unit="USD")),
                    ("fined", dict(scale=3, category="culture", unit="grammys")),
                    ("midfine", dict(scale=14e6, direction=-1, category="economy", unit="USD")),
                    ("bigfine", dict(scale=6e9, direction=-1, category="economy", unit="USD"))]:
        _approve(client, person_id=pid, **kw)
    r = client.get("/api/ranking?tab=overall").json()
    by = {p["person_id"]: p["score"] for p in r}
    assert all(p["ranked"] for p in r) and len(r) == 6
    assert by["borlaug"] == 300 and [p["person_id"] for p in r][0] == "borlaug" and r[-1]["person_id"] == "killer"
    assert by["saver"] > 0 and by["killer"] < by["bigfine"] < by["midfine"] < 0  # no fine near deaths
    assert by["bigfine"] == -25 and by["midfine"] > -8 and abs(by["midfine"]) * 3 < abs(by["bigfine"])
    assert by["fined"] == 50 - 0.0 or by["fined"] > 49  # $250 fine barely registers


def test_overall_harm_never_raises_score(client):
    _approve(client, person_id="x", scale=10, category="jobs", unit="jobs")
    _approve(client, person_id="y", scale=5, category="jobs", unit="jobs")
    before = {p["person_id"]: p["score"] for p in client.get("/api/ranking?tab=overall").json()}
    _approve(client, person_id="x", scale=1, direction=-1, category="economy", unit="USD")
    after = {p["person_id"]: p["score"] for p in client.get("/api/ranking?tab=overall").json()}
    assert after["x"] < before["x"] and after["y"] == before["y"]


def test_log_score():
    from app.main import _log_score as ls
    assert ls(9, 9) == 1 and ls(-9, 9) == -1 and ls(0, 9) == 0 and 0 < ls(1, 99) < ls(9, 99) < 1
