import pytest
from fastapi.testclient import TestClient
from app.scoring import score

A = "2026-01-01T00:00:00"


def e(scale, d, approved=True):
    return {"scale": scale, "direction": d, "reviewed_at": A if approved else None,
            "status": "approved" if approved else "pending"}


def test_empty():
    assert score([]) == {"positive_total": 0, "negative_total": 0, "net": 0}


def test_mixed():
    assert score([e(10, 1), e(3, -1), e(5, 1)]) == {"positive_total": 15, "negative_total": 3, "net": 12}


def test_all_negative():
    assert score([e(4, -1), e(6, -1)]) == {"positive_total": 0, "negative_total": 10, "net": -10}


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
    assert client.get("/").status_code == 200
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
