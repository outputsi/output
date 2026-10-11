# Output

Minimal tracker of net output per person, based on sourced actions. Review is mandatory: submitted (including bot-submitted or imported) entries sit in a queue and do not affect any score until approved.

## Schema (table `entries`)

| field | type | notes |
|---|---|---|
| id | INTEGER PK | addressing only |
| person_id | TEXT | |
| action | TEXT | short description |
| source | TEXT | URL or citation |
| scale | REAL | people affected × magnitude (>= 0) |
| direction | INTEGER | +1 or −1 only |
| created_at | TIMESTAMP | set on submit (UTC ISO) |
| reviewed_at | TIMESTAMP NULL | null = pending |
| reviewer_id | TEXT NULL | set on approval |

## Scoring (pure function `app/scoring.py`, no stored totals)

Over approved entries only (`reviewed_at` not null):

- positive_total = sum of scale where direction = +1
- negative_total = sum of scale where direction = −1
- net = positive_total − negative_total

Totals are computed on every read, so approval is reflected immediately.

## API

- `POST /api/entries` — `{person_id, action, source, scale, direction}` → queued (reviewed_at null)
- `POST /api/entries/{id}/approve` — `{reviewer_id}` → sets reviewed_at + reviewer_id
- `GET /api/queue` — pending entries
- `GET /api/people/{person_id}` — positive_total, negative_total, net, approved entries with sources

## Web UI

- `/` submit form + import (paste JSON object/list, or upload CSV with header `person_id,action,source,scale,direction`)
- `/queue` pending entries with approve button (reviewer_id field)
- `/people/{person_id}` three totals + approved entries with source links

## Run

```
pip install -r requirements.txt
uvicorn app.main:app --reload
# Without DATABASE_URL: local SQLite file net_output.db (override with NET_OUTPUT_DB=/path/to.db)
pytest
```

## Database

If `DATABASE_URL` is set (e.g. Render's `postgres://user:pass@host:5432/db`; `postgres://` and `postgresql://` both accepted), the app uses Postgres via psycopg2. If it is unset, it falls back to a local SQLite file. The `entries` table is created automatically on startup with the same schema in both (Postgres: `SERIAL` id, `DOUBLE PRECISION` scale).
