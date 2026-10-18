"""Uncached person pages and the Influence tab must not fan out into one query per row.

Production was opening a new Postgres connection per query (~0.5s on Neon). A person
page did ~66 of those and /?tab=influence did ~56, almost all of them repeat reads of
the same small tables plus one display-name lookup per card.
"""
import json
import os
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db, main
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    main._rate.clear()
    main.clear_cache()
    return TestClient(main.app)


class _Cur:
    def __init__(self, cur, sink):
        self._cur = cur
        self._sink = sink

    def execute(self, sql, *args, **kwargs):
        self._sink.append(" ".join(sql.split()))
        return self._cur.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cur, name)


@pytest.fixture
def queries(monkeypatch):
    from app import db
    sink = {"connects": 0, "execs": []}
    orig = db.connect

    @contextmanager
    def wrapped():
        sink["connects"] += 1
        with orig() as (cur, ph):
            yield _Cur(cur, sink["execs"]), ph

    monkeypatch.setattr(db, "connect", wrapped)
    return sink


def _publish_influence(client):
    path = os.path.join(os.path.dirname(__file__), "..", "app", "influence.json")
    people = list(json.load(open(path))["people"])
    cats = [("people", "lives"), ("people", "people"), ("jobs", "jobs"), ("economy", "USD"),
            ("climate", "tCO2e"), ("nature", "hectares"), ("culture", "grammys"),
            ("culture", "academy-awards"), ("culture", "guinness-records")]
    for i, pid in enumerate(people):
        assert client.post(f"/api/people/{pid}/publish", json={"published": True}).status_code == 200
        cat, unit = cats[i % len(cats)]
        created = client.post("/api/entries", json={
            "person_id": pid, "action": f"act {i}", "source": "https://example.com/s",
            "scale": 10 + i, "direction": 1 if i % 7 else -1, "category": cat, "unit": unit,
            "is_estimate": i % 11 == 0,
        })
        assert created.status_code == 201, created.text
        assert client.post(f"/api/entries/{created.json()['id']}/approve",
                           json={"reviewer_id": "bench"}).status_code == 200
    return people


def _measure(client, queries, path):
    from app import main
    main.clear_cache()
    main._views_flushed[0] = time.time()
    queries["connects"] = 0
    queries["execs"] = []
    resp = client.get(path)
    return resp, queries["connects"], list(queries["execs"])


def test_person_and_influence_use_a_handful_of_queries(client, queries):
    from app import main
    people = _publish_influence(client)
    # one counted extra source, so the person page still renders sources
    entries = client.get("/api/people/jeff-bezos").json()["entries"]
    client.post(f"/api/entries/{entries[0]['id']}/sources",
                json={"url": "https://example.com/extra", "quote": "quoted", "reviewer_id": "bench"})
    client.post("/api/people/jeff-bezos/name", json={"display_name": "Jeff Bezos"})

    main._views_flushed[0] = time.time()
    for path, cards in (("/?tab=influence", len(people)), ("/?tab=overall", None),
                        ("/people/jeff-bezos", None), ("/", None)):
        resp, connects, execs = _measure(client, queries, path)
        assert resp.status_code == 200, path
        if cards is not None:
            assert resp.text.count('class="card"') == cards
        # Was ~56 (influence) and ~66 (person): one connection and one SELECT per repeated read.
        assert connects <= 2, (path, connects, execs)
        assert len(execs) <= 6, (path, connects, execs)
        assert any(e.startswith("SELECT") for e in execs)


def test_request_cache_sees_its_own_writes(client):
    from app import db
    token = db.begin_request_cache()
    try:
        assert db.published_ids() == set()
        assert db.all_entries() == []
        db.set_published("zoe-quinn", True)
        db.set_display_name("zoe-quinn", "Zoe")
        assert db.published_ids() == {"zoe-quinn"}
        assert db.display_names()["zoe-quinn"] == "Zoe"
        assert db.get_person("zoe-quinn")["person_id"] == "zoe-quinn"
        row = db.insert_entry("zoe-quinn", "did a thing", "https://example.com/z", 4, 1)
        assert row["person_id"] == "zoe-quinn"
        assert [e["id"] for e in db.person_entries("zoe-quinn")] == [row["id"]]
        assert db.all_entries()[0]["action"] == "did a thing"
    finally:
        db.end_request_cache(token)
