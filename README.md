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
| credit_share | REAL NULL DEFAULT 1.0 | 0 < share ≤ 1 |
| credit_basis | TEXT NULL DEFAULT 'direct' | direct, sourced_share, even_split |
| credit_note | TEXT NULL | e.g. "Source lists 4 contributors" |
| is_estimate | INTEGER NULL DEFAULT 0 | 1 = estimate |

## Table `people`

| field | type |
|---|---|
| person_id | TEXT PRIMARY KEY |
| image_url | TEXT NULL |
| image_credit | TEXT NULL |
| image_license_url | TEXT NULL |

Used only for homepage avatars; created by `init()`.

## Credit sharing

- Effective scale = scale × credit_share (missing share = 1.0).
- `direct` (default, share 1.0): the person is credited with the whole action.
- `sourced_share`: a partial share is counted only when the source itself states it (e.g. 60%). Labeled "Sourced share 60%".
- `even_split`: when no share is stated but the source lists N contributors, use 1/N. Labeled "Even split 1/N".
- Estimates (`is_estimate` = 1) are labeled "est." and counted in the score by default; add `?include_estimates=0` to the person page/API or homepage (toggle links provided) to exclude them.
- `POST /api/entries/{id}/approve-estimate` `{reviewer_id}` approves an unproven estimate entry that passes the estimate rule (re-check only).
- Existing entries are migrated to share 1.0 / direct / not estimate, so earlier scores do not change.
- `POST /api/entries/{id}/credit` with `{credit_share, credit_basis, credit_note, is_estimate, reviewer_id}` updates these on an existing entry (records reviewer_id).

## Verification policy

1. The primary / closest record wins.
2. An entry needs 2 independent, very trusted sources agreeing on the same number; a 3rd source is the tiebreaker.
3. Sources that show their method beat bare totals.
4. When sources disagree, use the lowest shared/documented number, with a note.
5. Otherwise the entry is unproven, with all sources shown.

Money counts only through the number of people a source says it affected.

Estimates count. An `is_estimate` entry may be approved when 2 independent very trusted sources directly attribute the effect to the individual and give an approximate people count; the lowest stated figure is used (rule 4). Still unproven: no number; effect credited to an organization, company or foundation, or shared with others; counterfactuals or projections; non-people units (beds, gifts, dollars); negatives without a court or official finding.

Very trusted = peer-reviewed journals, government/official bodies and inquiries, national archives, major museums/memorials, university presses, Nobel, main Britannica, major newspapers of record.

## Scoring (pure function `app/scoring.py`, no stored totals)

Over approved entries only (`status = 'approved'`), using effective scale, excluding estimates unless included:

- positive_total = sum of scale where direction = +1
- negative_total = sum of scale where direction = −1
- net = positive_total − negative_total
- negative_pct (derived, not stored) = negative_total / (positive_total + negative_total) × 100; null when both are 0.
- positive_pct (derived, not stored) = positive_total / (positive_total + negative_total) × 100; null (shown as —) when both are 0. Shown on homepage and person page; returned by the person API. Ranking still sorts by net.

Totals are computed on every read, so approval is reflected immediately.

## API

- `POST /api/entries` — `{person_id, action, source, scale, direction}` → queued (reviewed_at null)
- `POST /api/entries/{id}/approve` — `{reviewer_id}` → status approved, sets reviewed_at + reviewer_id (only from pending; else 409)
- `POST /api/entries/{id}/mark-unproven` — `{reviewer_id}` → status unproven, sets reviewed_at + reviewer_id (from pending or approved — an approved entry drops out of the totals; 404 unknown, 409 if already unproven/rejected). Source/scale/direction are kept but never counted.
- `POST /api/people/{person_id}/image` — `{image_url, image_credit?, image_license_url?}` (null/empty clears) → sets the avatar image and optional photo credit in `people`; credit shown as a small line under the photo on the person page
- `POST /api/entries/{id}/sources` — `{url, quote?, reviewer_id, make_primary?}` → attaches an additional source (table `entry_sources`); `make_primary: true` replaces the entry's main `source` instead. `GET /api/entries/{id}/sources` lists them. Extra sources are shown on the person page and queue; scoring and statuses are unaffected.
- `GET /api/queue` — pending entries (status = pending)
- `GET /api/people/{person_id}` — positive_total, negative_total, net, approved entries with sources (`entries`), plus `not_counted` (unproven/rejected)

## Web UI

- `/` homepage: grid of circular avatars, one per person_id with any entry, showing the prettified name (jonas-salk → Jonas Salk) and positive and negative share (e.g. green 91% · red 9%); grey — when nothing is counted, sorted by net descending; initials placeholder when no image; small form to set an image
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
