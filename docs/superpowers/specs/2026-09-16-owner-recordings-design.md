# Parcel owners and recorded-instrument feeds

Date: 2026-09-16. Status: approved in conversation; implementation follows the
plan in `docs/superpowers/plans/`.

## Goal

Answer, per parcel, "who owns it, when did they buy, and what has been
recorded against it (mortgages, satisfactions, liens, lis pendens,
judgments)" from official sources only, starting with the clerk index feeds
that are free and need no agreement, and ordering the work so Orange County
(Orlando) is populated first.

## Sources verified 2026-09-16

| Source | Access | Cadence / retention | Fields |
|---|---|---|---|
| FDOR statewide cadastral layer (already pulled by `dor_values.py`) | ArcGIS REST, no key | annual roll | `OWN_NAME`, `OWN_ADDR1/2`, `OWN_CITY`, `OWN_STATE`, `OWN_ZIPCD`, `OWN_STATE_` (domicile), `OR_BOOK1/2`, `OR_PAGE1/2`, `CLERK_NO1/2` |
| Hillsborough Clerk daily index, `https://publicrec.hillsclerk.com/OfficialRecords/DailyIndexes/` | HTTPS directory listing, no login | one D/P/M file set per business day, ~2 months online | D: instrument no, doc type + description, legal text, book, page, page count, recorded date/time, consideration. P: party name per instrument with FRM/TO role. M: doc type to FACC standard type |
| Hernando Clerk weekly index, `https://subscriber.hernandoclerk.com/data_files/official_records/` | HTTPS directory listing, no login | weekly CSV since 2024-01-05, all kept online | grantor, grantee, clerk file no, book, page, doc type description, legal text, recorded date; one row per grantor x grantee pair |
| Broward Recorder FTPS, `bcftp.broward.org` | needs an account issued by the clerk (954-831-4000) | rolling 10 days | index + images (layout to be confirmed once access exists) |
| Lake, Palm Beach, Miami-Dade | paid subscription, notarized agreement | daily | not built; noted for later |
| Orange County | no feed. Clerk site is Tyler Eagle Recorder (guest login, no automation clause, JavaScript search). Property Appraiser sells the PARBA204S extract ($300) with owners, 5 sales with instrument numbers, mortgage company id | — | out of scope for recordings; owners and sale dates come from the statewide layer and the county parcel layer |

Gotchas established from samples:

- Neither clerk feed carries a parcel number. Instruments link to parcels by
  party name or by the clerk instrument number the DOR roll stores for the
  last two sales.
- Mortgage amounts are essentially absent: Hillsborough fills consideration
  on 1 to 3 of roughly 150 mortgages per day; Hernando has no amount column.
  Deed consideration (sale price) is filled on about half of Hillsborough
  deeds. A mortgage-value filter can only be complete once a paid source or
  document images fill the amount.
- The DOR roll's `SALE_YR1` only covers sales in the previous or current
  calendar year, so `CLERK_NO1/2` exists for about 13% of parcels.
- Hernando file names mix `MM-DD-YY.csv` and `MM-DD-YYYY.csv`.
- Hillsborough M files are named `MYYYYMMDD01d.29` (not `id`).
- Orange's parcels occupy one contiguous OBJECTID block in the statewide
  layer (6,429,451 to 6,921,026 in the 2025 export), so a county-first pull
  is a range query.

## Data model

New tables, all in the main database, none copied into the demo database.

```
parcel_owners (
  county TEXT NOT NULL, parcel_id TEXT NOT NULL, parcel_key TEXT NOT NULL,
  owner_name TEXT, owner_name_norm TEXT,
  mail_addr1 TEXT, mail_addr2 TEXT, mail_city TEXT, mail_state TEXT, mail_zip TEXT,
  owner_state_dom TEXT,
  or_book1 TEXT, or_page1 TEXT, clerk_no1 TEXT,
  or_book2 TEXT, or_page2 TEXT, clerk_no2 TEXT,
  last_synced_at TEXT NOT NULL,
  PRIMARY KEY (county, parcel_id)) WITHOUT ROWID
  index (county, owner_name_norm); index (county, clerk_no1); index (county, clerk_no2)

recorded_instruments (
  county TEXT NOT NULL, instrument_no TEXT NOT NULL,
  doc_type TEXT, doc_desc TEXT, category TEXT,      -- deed|mortgage|satisfaction|assignment|lien|lis_pendens|judgment|release|other
  book TEXT, page TEXT, recorded_at TEXT,           -- ISO date or datetime
  consideration REAL,                               -- sale price on deeds, mortgage amount when the source has it
  legal_desc TEXT, source_file TEXT, last_synced_at TEXT NOT NULL,
  PRIMARY KEY (county, instrument_no)) WITHOUT ROWID
  index (county, category, recorded_at); index (county, recorded_at)

instrument_parties (
  county TEXT NOT NULL, instrument_no TEXT NOT NULL,
  role TEXT NOT NULL,                               -- grantor|grantee
  seq INTEGER NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  PRIMARY KEY (county, instrument_no, role, seq)) WITHOUT ROWID
  index (county, name_norm)

instrument_parcels (
  county TEXT NOT NULL, instrument_no TEXT NOT NULL, parcel_id TEXT NOT NULL,
  method TEXT NOT NULL,                             -- clerk_no|owner_name
  PRIMARY KEY (county, instrument_no, parcel_id)) WITHOUT ROWID
  index (county, parcel_id)

recording_files (
  county TEXT NOT NULL, file_name TEXT NOT NULL, loaded_at TEXT NOT NULL,
  rows INTEGER, PRIMARY KEY (county, file_name))
```

`owner_name_norm` and `name_norm` use one function: uppercase, strip
punctuation, collapse whitespace, drop a trailing `ET AL`, `ET UX`, `ET VIR`,
`TRUSTEE`, `TR`, `H/W`, `JR`/`SR` suffix tokens are kept. Hernando names are
already `LAST FIRST` like the DOR roll; Hillsborough names are too. No
first/last reordering.

Category mapping is a small table keyed on the source doc type (Hillsborough
FACC type from the M file; Hernando description with trailing digits and `X`
stripped, e.g. `MORTGAGE2` -> mortgage, `LIENX` -> lien, `DEED1` -> deed).

## Components

### 1. `dor_values.py` extension: owners

- `FIELDS` gains the owner and deed-reference columns; `row_from_attrs`
  returns a second tuple for `parcel_owners`; `sync_statewide_values` writes
  both tables in the same transaction batches. Stale owner rows are removed
  with the same `last_synced_at < started` sweep.
- A new `sync_county_owners(conn, county)` pulls only that county's OBJECTID
  block (min/max `source_objectid` from `parcel_values`) and writes
  `parcel_owners` for it. `python dor_values.py --owners orange` runs it.
  This is how Orange loads first without waiting for the 15-minute statewide
  pass.
- `apply_values_to_features` is unchanged; owners are read by the API from
  `parcel_owners` directly.

### 2. `scripts/recordings.py`: feed loader

- `SOURCES` dict: `hillsborough` (daily D/P/M), `hernando` (weekly CSV),
  `broward` (FTPS; active only when `FL_BROWARD_FTP_USER` and
  `FL_BROWARD_FTP_PASS` are set; parser written once the layout is known and
  until then the source logs `[SKIP] broward: no credentials`).
- Each source implements `list_files()` -> names, `fetch(name)` -> bytes,
  `parse(name, data)` -> iterator of `(instrument, parties)` records. Files
  already present in `recording_files` are skipped; a file that fails leaves
  no row so it is retried next run.
- Writes are upserts; the same instrument appearing in a later file (a
  Hillsborough "modified" record) replaces the earlier row and its parties.
- After loading, `link_instruments(conn, county)` fills `instrument_parcels`:
  first `clerk_no` matches against `parcel_owners.clerk_no1/2`, then
  `owner_name` matches for parties whose `name_norm` equals an
  `owner_name_norm` in the same county, skipping names that match more than
  5 parcels (`AMBIGUOUS_NAME_LIMIT`).
- `sync_log` rows use `dataset_type = 'recordings'`, one per county per run,
  `rows_fetched` = instruments written.
- Runs from `etl.py` after the DOR step (`--no-recordings` skips it) and
  standalone via `python recordings.py [county]`.

### 3. API (`app.py`)

- `GET /api/parcel/<county>/<parcel_id>/owner` -> owner row or 404.
- `GET /api/parcel/<county>/<parcel_id>/instruments` -> linked instruments
  with parties, newest first.
- `/api/features` and `/api/features/geometry` accept
  `mortgage_since=YYYY-MM-DD` (parcel has a linked mortgage recorded on or
  after that date), `mortgage_min` and `mortgage_max` (linked mortgage
  consideration in range; parcels whose mortgages have no amount are
  excluded when either bound is set), and `has_lien=1` (linked lien, lis
  pendens or judgment with no later satisfaction or release). Implemented as
  an `EXISTS` subquery on `instrument_parcels` joined to
  `recorded_instruments`.
- `/api/status/counties` gains a `recordings` cell (last run, row count,
  state `ok|failed|not_configured`).
- In `DEMO_MODE` the owner and instrument endpoints return 404 and the
  filters are ignored, since the demo database has none of these tables.

### 4. UI (`scripts/templates`, `scripts/static`)

- Parcel detail popup: an **Owner** block (name, mailing address, domicile)
  and a **Recorded instruments** block (date, category, description, amount,
  other party, book/page). Both render only when the endpoints return data.
- Browse Data filters: "Mortgage recorded since" date, "Mortgage amount"
  min/max with a hint that amounts are only known for some counties, and
  "Open lien / lis pendens" checkbox. Filters apply to the table and the
  map the same way the existing filters do.
- Pipeline Status: "Recordings" column.

### 5. Demo builder and docs

- `make_demo_db.py` copies only the tables it already lists; the new tables
  are never created in the demo database, and the README privacy section is
  rewritten to say the local database stores owners and recordings while
  the public demo does not.

## Load order

1. `parcel_owners` for Orange (county block pull, about a minute).
2. Hillsborough and Hernando recordings backfill from every file online,
   then link.
3. Full statewide `parcel_owners` on the next `etl.py` run (or
   `python dor_values.py --owners all`).

## Error handling

- A feed directory that fails to list marks that county's `sync_log` row
  failed and leaves other counties running.
- Unparseable lines are counted and logged per file, never fatal; a file
  with more than 5% bad lines is not marked loaded.
- Upserts run in batches of 5,000 under the existing run lock so `etl.py`
  and `load_all.py` cannot write concurrently.

## Testing

- Unit tests (pytest, `tests/`): name normalization, category mapping,
  Hillsborough D/P/M parsing, Hernando CSV parsing including the two date
  formats and grantor x grantee dedupe, link resolution including the
  ambiguous-name cutoff, and the mortgage filter SQL on an in-memory
  database.
- Sample files captured on 2026-09-16 live in `tests/fixtures/recordings/`
  (a few hundred lines each, names as published).
- Manual check: after the Orange owner load, the popup for a known Orange
  parcel shows the owner; after the Hillsborough load, a parcel with a 2026
  deed shows the deed and its mortgage.

## Out of scope

- Broward parser until credentials exist; Lake, Palm Beach, Miami-Dade.
- Orange recordings (Eagle Web browser pull or the PARBA204S file).
- Filling mortgage amounts from document images or a paid API.
- Skip tracing, Sunbiz corporate resolution.
