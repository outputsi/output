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
| reviewer_id | TEXT NULL | set on approval / mark-unproven |
| status | TEXT NOT NULL DEFAULT 'pending' | one of pending, approved, rejected, unproven |

## Table `people`

| field | type |
|---|---|
| person_id | TEXT PRIMARY KEY |
| image_url | TEXT NULL |

Used only for homepage avatars; created by `init()`.

## Scoring (pure function `app/scoring.py`, no stored totals)

Over approved entries only (`status = 'approved'`):

- positive_total = sum of scale where direction = +1
- negative_total = sum of scale where direction = −1
- net = positive_total − negative_total

Totals are computed on every read, so approval is reflected immediately.

## API

- `POST /api/entries` — `{person_id, action, source, scale, direction}` → queued (reviewed_at null)
- `POST /api/entries/{id}/approve` — `{reviewer_id}` → status approved, sets reviewed_at + reviewer_id (only from pending; else 409)
- `POST /api/entries/{id}/mark-unproven` — `{reviewer_id}` → status unproven, sets reviewed_at + reviewer_id (from pending or approved — an approved entry drops out of the totals; 404 unknown, 409 if already unproven/rejected). Source/scale/direction are kept but never counted.
- `POST /api/people/{person_id}/image` — `{image_url}` (null/empty clears) → sets the avatar image in `people`
- `GET /api/queue` — pending entries (status = pending)
- `GET /api/people/{person_id}` — positive_total, negative_total, net, approved entries with sources (`entries`), plus `not_counted` (unproven/rejected)

## Web UI

- `/` homepage: grid of circular avatars, one per person_id with any entry, showing the prettified name (jonas-salk → Jonas Salk) and net Output, sorted by net descending; initials placeholder when no image; small form to set an image
- `/submit` submit form + import (paste JSON object/list, or upload CSV with header `person_id,action,source,scale,direction`)
- `/queue` pending entries with Approve and Mark unproven buttons (reviewer_id field)
- `/people/{person_id}` three totals, 'Counted entries' (approved), and a separate 'Not counted / unproven' section

## Run

```
pip install -r requirements.txt
uvicorn app.main:app --reload
# Without DATABASE_URL: local SQLite file net_output.db (override with NET_OUTPUT_DB=/path/to.db)
pytest
```

## Database

If `DATABASE_URL` is set (e.g. Render's `postgres://user:pass@host:5432/db`; `postgres://` and `postgresql://` both accepted), the app uses Postgres via psycopg2. If it is unset, it falls back to a local SQLite file. The `entries` table is created automatically on startup with the same schema in both (Postgres: `SERIAL` id, `DOUBLE PRECISION` scale).

Migration: on startup `init()` adds the `status` column to an existing `entries` table if missing (Postgres `ADD COLUMN IF NOT EXISTS`; SQLite via `PRAGMA table_info`) and backfills `status='approved'` where `reviewed_at IS NOT NULL`.
