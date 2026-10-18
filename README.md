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
| display_name | TEXT NULL (overrides the name derived from person_id) |

Used only for homepage avatars; created by `init()`.

## Credit sharing

- Effective scale = scale × credit_share (missing share = 1.0).
- `direct` (default, share 1.0): the person is credited with the whole action.
- `sourced_share`: a partial share is counted only when the source itself states it (e.g. 60%). Labeled "Sourced share 60%".
- `organization`: the source credits an organization the person founded/leads/funds, with no share stated and no other named credited people; the person gets full credit, labeled "Organization".
- `even_split`: when no share is stated but the source lists N contributors, use 1/N. Labeled "Even split 1/N".
- Estimates (`is_estimate` = 1) are labeled "est." and counted in the score by default; add `?include_estimates=0` to the person page/API or homepage (toggle links provided) to exclude them.
- `POST /api/entries/{id}/approve-estimate` `{reviewer_id}` approves an unproven estimate entry that passes the estimate rule (re-check only).
- `POST /api/entries/{id}/reapprove` `{reviewer_id}` approves an unproven entry that passes the current policy (re-check only).
- The homepage hides people whose net is 0 and who have no counted entries; their person pages and API stay.
- Existing entries are migrated to share 1.0 / direct / not estimate, so earlier scores do not change.
- `POST /api/entries/{id}/credit` with `{credit_share, credit_basis, credit_note, is_estimate, reviewer_id}` updates these on an existing entry (records reviewer_id).

## Verification policy

1. The primary / closest record wins.
2. An entry needs 2 independent, very trusted sources agreeing on the same number; a 3rd source is the tiebreaker.
3. Sources that show their method beat bare totals.
4. When sources disagree, use the lowest shared/documented number, with a note.
5. Otherwise the entry is unproven, with all sources shown.

Money counts only through the number of people a source says it affected.

Estimates count. An `is_estimate` entry may be approved when 2 independent very trusted sources give an approximate people count; the lowest stated figure is used (rule 4). Counterfactual/projection figures (e.g. "saved X lives") stated by 2 very trusted sources count as estimates.

Everything tied to a person counts, including their company and foundation outcomes. Credit share (no judgment): a share the source states (ownership %, sole funder) -> `sourced_share`; otherwise split evenly among the named credited people (`even_split`); an organization credited alone, with the person as founder/leader/funder -> full credit, `organization`. Still unproven: no number; non-people units (dollars, beds, gifts, doses) unless a source converts them to people; fewer than 2 very trusted sources on the number; negatives without a court or official finding.

Very trusted = peer-reviewed journals, government/official bodies and inquiries, national archives, major museums/memorials, university presses, Nobel, main Britannica, major newspapers of record.

Lesser tier: sports dailies (AS, Marca, Mundo Deportivo, Sport, A Bola, Record, L'Équipe, Gazzetta and similar). A claim that relies on any of them needs 3 independent sources, at least one being a very trusted non-sports outlet or a primary statement (family, hospital, official), verified by full text. All other claims keep the 2-very-trusted-source rule.

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
- `POST /api/people/{person_id}/name` — `{display_name}` (null/empty clears) → display name used on rankings and cards instead of the id-derived one (e.g. `Doug McMillon`)
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

## Homepage tabs

The homepage ranks people per category in tabs: Lives (default, `people/lives`), People, Jobs, Economy $ (all economy units converted with the official rates), Climate, Nature, and Overall (average of the person's percentile positions across the category tabs: 100 = 1st, 0 = last, ties share a score; Culture is one tab averaged over its sub-tabs; needs 2+ tabs, others listed under "Not enough data yet"; ties by more tabs, more counted entries, then name; no money or cross-category conversion). Each tab sorts by that category's net in its own unit, shows green/red totals, and lists only people with counted entries in it. Links are shareable: `/?tab=jobs`; API: `/api/ranking?tab=jobs`. Lives is the default because it is the only unit with an official rate and the most counted people. Person pages keep all categories.

## Categories, units and the overall score

Output measures a person's total impact, not only people helped or harmed. Each entry has a `category` and `unit` (existing entries default to `people`/`people`):

| category | units |
|---|---|
| people | `people` (people affected), `lives` (deaths caused or lives saved, as the source states) |
| jobs | `jobs` |
| climate | `tCO2e` (tonnes CO2-equivalent) |
| nature | `animals`, `hectares` |
| economy | `USD` (nominal dollars of the year stated; no inflation adjustment) |
| tech, culture | any measurable unit the source states |

All sourcing rules above apply to every category. Economy: fines count as negative only with an official finding; settlements without one stay unproven. Guilty pleas are convictions and count. Deferred/non-prosecution agreements in which the company admitted the facts count, but only for conduct within the person's tenure; if the in-tenure share is not sourced, the entry stays unproven. Court-ordered civil damages count like fines. First-instance rulings count unless reversed or stayed (noted in the entry). Vaccine/aid lives-saved figures from a multi-donor body count for a funder only via a sourced funding share (`sourced_share`); otherwise unproven. Duplicates are never deleted: marked unproven with 'duplicate of #N'. Company outcomes count toward a person only while they are CEO, chairman/executive chair, or controlling owner, with role dates from a source (organization basis). Jobs count as positive when an official filing and a major outlet both state the number.

The person page shows per-category totals side by side. The overall score converts categories to USD **only** with officially published rates, stored in the `conversions` table with their source (`GET /api/conversions`):

- `people/lives`: US DOT value of a statistical life, $14.2M (2025 base year).
- `climate/tCO2e`: US EPA 2023 social cost of CO2, $190/tonne (2020 emissions, 2% discount rate, 2020 dollars).
- `economy/USD`: 1:1.
- `economy/EUR-YYYY`: Federal Reserve G.5A annual averages (USD per euro): 2004 1.2438, 2008 1.4726, 2013 1.3281, 2016 1.1072, 2019 1.1194, 2020 1.1410, 2021 1.1830, 2022 1.0534, 2023 1.0817, 2024 1.0820, 2025 1.1306. Non-USD amounts use unit `CUR-YYYY` (currency, year of the amount) and need an official Fed G.5A/H.10 or ECB rate for that year in `conversions`.

People rescued from death count as lives saved (`people/lives`) when the sources say rescued or saved from death. Scores driven by estimate entries carry an "estimate" label on the homepage and person page.

Categories without an official rate (people/people, jobs, nature, tech, culture) are shown but not included in the overall score, and are labeled "not convertible". The Overall tab does not use this USD figure: it ranks by average tab position (`score`, `tabs`, `positions`, `ranked` in `/api/ranking?tab=overall`).

API: entries accept `category` and `unit`; `POST /api/entries/{id}/category` `{category, unit, reviewer_id}` reclassifies an entry.

### Culture tab
Category `culture`, units limited to `certified-units` (RIAA Gold & Platinum certified units), `grammys` (Recording Academy wins), `academy-awards` (competitive Oscar wins only; honorary, special and scientific/technical awards excluded) and `guinness-records`. Culture enters Overall only as one tab position (sub-tabs averaged). The tab ranks by `grammys` until any `certified-units` entry is approved, then by `certified-units` only; other units are shown as lines but never summed with it. Sub-toggles rank one unit each, never summed: `?tab=culture&unit=grammys|oscars|guinness|certified` (certified shown once any are counted); same `unit` param on `/api/ranking` and `/cards/tab/culture.png`. No judgment metrics (e.g. "influence"). Same 2-source policy; the certifier's own database is the primary record. For `academy-awards` the Academy Awards database (awardsdatabase.oscars.org / oscars.org ceremony pages) is the primary record, plus one trusted outlet stating the win.

### Publishing
Each person has a `published` flag (`POST /api/people/{id}/publish {"published": true|false}`). Only published people appear on homepage tabs and `/api/ranking`; unpublished person pages return 404 unless `?preview=1` (reviewer preview; there is no login). Everyone who existed when the flag was added was marked published. New names stay unpublished until both sides are researched.

### Admin key
When the `ADMIN_KEY` env var is set, every POST except submitting pending entries (`/api/entries`, `/submit`, `/import`) and any `?preview=1` request needs the key, as header `X-Admin-Key` or query `admin_key=`. The review queue keeps the key in its form links when opened as `/queue?admin_key=...`.


### SEC filings and jobs
An official SEC filing (10-K, 8-K, etc.) alone is enough for a company's jobs/employee count. It is not enough for self-reported impact claims.

The same applies to equivalent legally required regulator filings outside the US (e.g. UK Companies House accounts, annual reports filed with an EU/national securities regulator), for jobs/employee counts only.

## Share cards
Server-rendered PNGs (Pillow, bundled DejaVu fonts): `/cards/person/{id}.png` and `/cards/tab/{tab}.png` (top 5).
`?size=portrait` (default, 1080x1350, IG/TikTok) or `?size=x` (1200x675, X/OG). Only approved, counted totals; economy
currency-years converted to USD with official rates, other categories in their own units; any estimate is labelled "est.".
Photos: freely licensed only (CC BY, CC BY-SA, CC0, public domain, UK Open Government Licence v3), always with a credit line and license URL.

Green = positive, red = negative; footer `outputrank.com`. A photo is drawn only when the person has an image with a credit line,
which is printed under it. Unpublished people 404 (preview with the admin key). Person and home/tab pages carry Open Graph and
Twitter tags pointing at the `?size=x` card; set `PUBLIC_URL` to force the absolute origin (e.g. https://outputrank.com).

## Public site notes

- Person photos are mirrored in `app/static/people/` (`tools/fetch_photos.py`, manifest maps person_id to source URL); the local copy is used only while the DB `image_url` matches.
- Public GET pages, cards and `/api/ranking` are cached in memory; any successful write clears the cache (10 min TTL as a safety net).
- `/submit` and `/import` POSTs: 10 per hour per IP. `/docs`, `/redoc`, `/openapi.json` need the admin key when `ADMIN_KEY` is set.

## Influence tab

Neutral reach/attention tab (`?tab=influence`, `/api/ranking?tab=influence`): English Wikipedia page views for the last 12 months from the Wikimedia Pageviews API, stored in `app/influence.json` (period, as-of date, article title). Never positive/negative, excluded from Overall and its 2-tab minimum, and does not make anyone public.
