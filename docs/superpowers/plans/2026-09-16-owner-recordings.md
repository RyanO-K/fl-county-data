# Parcel Owners and Recorded-Instrument Feeds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store parcel owners from the statewide DOR layer (Orange first), load the free Hillsborough and Hernando clerk index feeds into recorded-instrument tables, link instruments to parcels, and expose owner, instruments, and mortgage/lien filters in the local app only.

**Architecture:** `dor_values.py` grows a `parcel_owners` table filled by the same statewide pull it already runs, plus a county-block pull for Orange. A new `recordings.py` owns the feed fetchers, parsers, schema, upserts, and the parcel link step. `app.py` reads the new tables when they exist and ignores the new filters when they do not (the demo database never has them).

**Tech Stack:** Python 3 (`.venv`), sqlite3, requests, Flask 3, pytest (to install), vanilla JS front end.

**Spec:** `docs/superpowers/specs/2026-09-16-owner-recordings-design.md`

## Global Constraints

- Database path comes from `etl.DB_PATH` (env `FL_COUNTY_DB` overrides); never hard-code `D:\`.
- Every writer runs under the run lock (`etl.acquire_run_lock` / `etl.release_run_lock`).
- New tables: `parcel_owners`, `recorded_instruments`, `instrument_parties`, `instrument_parcels`, `recording_files`. None may appear in the demo database.
- One name normalizer, `recordings.norm_name`, is used for both owner names and party names.
- Owner and instrument endpoints return 404 and the new filters are no-ops when the tables are absent.
- Commands run from the repo root with `.venv/Scripts/python.exe`; tests with `.venv/Scripts/python.exe -m pytest`.
- `AMBIGUOUS_NAME_LIMIT = 5`, batch size 5,000 rows per `executemany`.
- Spec addendum (found while planning): `instrument_parcels` also stores `parcel_key` (the normalized id) so the feature filters can join on `features.feature_key_norm`; `parcel_owners` and `instrument_parties` store a 30-character `name_key` because the statewide layer truncates `OWN_NAME` at 33 characters.

---

### Task 0: Test scaffolding

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/__init__.py` (empty)
- Modify: `requirements.txt`

**Interfaces:**
- Produces: pytest fixture `tmp_db_path` (a fresh SQLite file path with `FL_COUNTY_DB` pointing at it, set before any project module is imported) and fixture `conn` (an open `sqlite3.Connection` to it via `etl.get_conn()`).

- [ ] **Step 1: Install pytest and pin it**

Run: `.venv/Scripts/python.exe -m pip install pytest==8.4.1`
Append to `requirements.txt`: `pytest==8.4.1`

- [ ] **Step 2: Write conftest**

```python
# tests/conftest.py
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_TMP_DB = Path(os.environ.get("PYTEST_TMPDIR", ROOT / "tests" / ".tmp")) / "test_county.db"
_TMP_DB.parent.mkdir(parents=True, exist_ok=True)
# etl.py reads FL_COUNTY_DB at import time, so it must be set before the
# first project import anywhere in the test session.
os.environ["FL_COUNTY_DB"] = str(_TMP_DB)


@pytest.fixture
def tmp_db_path():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_TMP_DB) + suffix)
        if p.exists():
            p.unlink()
    return _TMP_DB


@pytest.fixture
def conn(tmp_db_path):
    import etl
    c = etl.get_conn()
    yield c
    c.close()
```

- [ ] **Step 3: Smoke test**

```python
# tests/test_scaffold.py
def test_conn_has_features_table(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "features" in names
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_scaffold.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/conftest.py tests/test_scaffold.py requirements.txt
git commit -m "Tests: pytest scaffold with an isolated database"
```

---

### Task 1: Name normalization and document categories

**Files:**
- Create: `scripts/recordings.py`
- Test: `tests/test_recordings_norm.py`

**Interfaces:**
- Produces: `norm_name(s: str | None) -> str`, `name_key(s) -> str` (first 30 chars of `norm_name`), `categorize(doc_text: str | None) -> str` returning one of `deed, mortgage, satisfaction, assignment, lien, lis_pendens, judgment, release, other`; constant `CATEGORIES` tuple; `NAME_KEY_LEN = 30`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_recordings_norm.py
import recordings as R


def test_norm_name_uppercases_and_strips_punctuation():
    assert R.norm_name("O'Brien, Mary-Ann  ") == "O BRIEN MARY ANN"


def test_norm_name_drops_trailing_et_al_and_trustee():
    assert R.norm_name("SMITH JOHN ET AL") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN TRUSTEE") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN TR") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN ET UX") == "SMITH JOHN"


def test_norm_name_keeps_jr():
    assert R.norm_name("Fuller Robert Allen Jr") == "FULLER ROBERT ALLEN JR"


def test_norm_name_none_and_blank():
    assert R.norm_name(None) == ""
    assert R.norm_name("   ") == ""


def test_name_key_truncates_to_30():
    assert R.name_key("A" * 40) == "A" * 30
    assert R.name_key("Smith John") == "SMITH JOHN"


def test_categorize_rules():
    assert R.categorize("DEED") == "deed"
    assert R.categorize("WARRANTY DEED") == "deed"
    assert R.categorize("TAX DEED") == "deed"
    assert R.categorize("MORTGAGE") == "mortgage"
    assert R.categorize("MORTGAGE NO INTANGIBLE TAXES") == "mortgage"
    assert R.categorize("MORTGAGE2") == "mortgage"
    assert R.categorize("ASSIGNMENT OF MORTGAGE") == "assignment"
    assert R.categorize("SATISFACTION") == "satisfaction"
    assert R.categorize("SATISFACTION OF MORTGAGE") == "satisfaction"
    assert R.categorize("RELEASE LIS PENDENS") == "release"
    assert R.categorize("PARTIAL RELEASE") == "release"
    assert R.categorize("LIS PENDENS") == "lis_pendens"
    assert R.categorize("LIENX") == "lien"
    assert R.categorize("JUDGMENT") == "judgment"
    assert R.categorize("CERT COPY CRT JDGMNT") == "judgment"
    assert R.categorize("CERTIFIED COPY OF COURT JUDGMENT") == "judgment"
    assert R.categorize("NOTICE OF COMMENCEMENT") == "other"
    assert R.categorize("") == "other"
    assert R.categorize(None) == "other"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_norm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recordings'`

- [ ] **Step 3: Implement**

```python
# scripts/recordings.py
"""Clerk of Court official-records index feeds -> recorded_instruments,
instrument_parties, instrument_parcels.

Sources (see docs/superpowers/specs/2026-09-16-owner-recordings-design.md):
  hillsborough  daily D (documents) / P (parties) pipe-delimited files
  hernando      weekly comma-delimited CSV, one row per grantor x grantee
  broward       FTPS index; only active once FL_BROWARD_FTP_USER/PASS are set

Usage:
    python recordings.py                # every configured county
    python recordings.py hillsborough   # one county
    python recordings.py --link-only    # re-run the parcel link step only
"""
import re

NAME_KEY_LEN = 30
CATEGORIES = ("deed", "mortgage", "satisfaction", "assignment", "lien",
              "lis_pendens", "judgment", "release", "other")

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TRAILING = ("ET AL", "ETAL", "ET UX", "ETUX", "ET VIR", "ETVIR",
             "TRUSTEES", "TRUSTEE", "TR")


def norm_name(s):
    """Upper-case, punctuation to spaces, single spaces, trailing
    et-al / et-ux / trustee tokens removed. JR/SR are kept: the DOR roll
    keeps them too."""
    if not s:
        return ""
    n = _NON_ALNUM.sub(" ", str(s).upper()).strip()
    changed = True
    while changed and n:
        changed = False
        for t in _TRAILING:
            if n == t:
                return ""
            if n.endswith(" " + t):
                n = n[: -len(t) - 1].rstrip()
                changed = True
    return n


def name_key(s):
    """Index key: the statewide layer truncates OWN_NAME at 33 characters, so
    both sides compare on the first NAME_KEY_LEN characters."""
    return norm_name(s)[:NAME_KEY_LEN]


# Ordered: the first matching rule wins, so RELEASE/SATISFACTION beat the
# instrument they release, and ASSIGNMENT beats MORTGAGE.
_CATEGORY_RULES = (
    ("satisfaction", ("SATISFACTION",)),
    ("release", ("RELEASE",)),
    ("assignment", ("ASSIGNMENT",)),
    ("lis_pendens", ("LIS PENDENS", "LISPENDENS")),
    ("mortgage", ("MORTGAGE", "MTG")),
    ("deed", ("DEED",)),
    ("lien", ("LIEN",)),
    ("judgment", ("JUDGMENT", "JDGMNT", "JUDGEMENT")),
)


def categorize(doc_text):
    t = norm_name(doc_text)
    for cat, needles in _CATEGORY_RULES:
        if any(n in t for n in needles):
            return cat
    return "other"
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_norm.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_norm.py
git commit -m "Recordings: name normalizer and document category rules"
```

---

### Task 2: Hillsborough parser

**Files:**
- Modify: `scripts/recordings.py`
- Create: `tests/fixtures/recordings/hillsborough/D2026082101id.29`, `.../P2026082101id.29` (first 60 lines of each sample pulled 2026-09-16)
- Test: `tests/test_recordings_hillsborough.py`

**Interfaces:**
- Produces: `parse_hillsborough(doc_bytes: bytes, party_bytes: bytes) -> list[Instrument]` where `Instrument` is a `dict` with keys `instrument_no, doc_type, doc_desc, category, book, page, recorded_at, consideration, legal_desc, parties` and `parties` is a list of `(role, seq, name)` with role in `grantor|grantee`. Also `hillsborough_file_sets(names: list[str]) -> list[tuple[str, str, str]]` pairing `(date_key, D_name, P_name)`.

- [ ] **Step 1: Create fixtures**

```bash
mkdir -p tests/fixtures/recordings/hillsborough tests/fixtures/recordings/hernando
S="C:/Users/ryan/AppData/Local/Temp/claude/C--Users-ryan-fl-county-data/42e63a36-a869-4501-bae8-5fbf4ecba519/scratchpad/feeds"
head -n 60 "$S/D2026082101id.29" > tests/fixtures/recordings/hillsborough/D2026082101id.29
head -n 200 "$S/P2026082101id.29" > tests/fixtures/recordings/hillsborough/P2026082101id.29
head -n 120 "$S/hern_or_latest" > tests/fixtures/recordings/hernando/12-27-2024.csv
```

Check that the D fixture contains at least one `|D|DEED|` line with a consideration and one `|MTG|` line (it does in the 2026-08-21 sample: instrument 2026327007 is a deed with `10.00`).

- [ ] **Step 2: Failing tests**

```python
# tests/test_recordings_hillsborough.py
from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings" / "hillsborough"


def _load():
    return R.parse_hillsborough(
        (FX / "D2026082101id.29").read_bytes(), (FX / "P2026082101id.29").read_bytes())


def test_parses_document_fields():
    recs = {r["instrument_no"]: r for r in _load()}
    d = recs["2026327007"]
    assert d["doc_type"] == "D"
    assert d["doc_desc"] == "DEED"
    assert d["category"] == "deed"
    assert d["legal_desc"] == "PT L 34 GIBSONTON ON THE BAY"
    assert d["recorded_at"] == "2026-08-21T07:44"
    assert d["consideration"] == 10.0
    assert d["book"] is None and d["page"] is None


def test_blank_consideration_is_none():
    recs = {r["instrument_no"]: r for r in _load()}
    assert recs["2026326987"]["consideration"] is None
    assert recs["2026326987"]["category"] == "other"


def test_parties_attached_with_roles():
    recs = {r["instrument_no"]: r for r in _load()}
    parties = recs["2026326987"]["parties"]
    assert ("grantor", 1, "MANGIONE RALPH") in parties
    assert ("grantee", 1, "FULLER ROBERT ALLEN JR") in parties
    assert ("grantee", 3, "PROGRESSIVE SELECT INSURANCE COMPANY") in parties


def test_file_sets_pair_d_and_p_by_date():
    names = ["D2026082101id.29", "P2026082101id.29", "M2026082101d.29",
             "D2026082201id.29", "readme.txt"]
    assert R.hillsborough_file_sets(names) == [("20260821", "D2026082101id.29", "P2026082101id.29")]
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_hillsborough.py -v`
Expected: FAIL with `AttributeError: module 'recordings' has no attribute 'parse_hillsborough'`

- [ ] **Step 4: Implement**

Append to `scripts/recordings.py`:

```python
import re as _re
from datetime import datetime

_HILLS_NAME = _re.compile(r"^([DP])(\d{8})\d{2}id\.29$")


def _text(v):
    v = (v or "").strip()
    return v or None


def _money(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _iso_from_mdy(date_s, time_s=None):
    """'08/21/2026' + '07:33' -> '2026-08-21T07:33'; '12/16/24 10:51:32 AM' ->
    '2024-12-16T10:51:32'. Returns None when the date does not parse."""
    date_s = (date_s or "").strip()
    if not date_s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %I:%M:%S %p", "%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(date_s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if time_s:
        t = time_s.strip()
        if _re.match(r"^\d{1,2}:\d{2}$", t):
            h, m = t.split(":")
            return dt.strftime("%Y-%m-%d") + f"T{int(h):02d}:{m}"
    if "%H" in fmt or "%I" in fmt:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y-%m-%d")


def hillsborough_file_sets(names):
    """Pair D and P files by recording date. M files are not needed: the D
    file's description column is what categorize() reads."""
    d, p = {}, {}
    for n in names:
        m = _HILLS_NAME.match(n)
        if not m:
            continue
        (d if m.group(1) == "D" else p)[m.group(2)] = n
    return [(k, d[k], p[k]) for k in sorted(d) if k in p]


def parse_hillsborough(doc_bytes, party_bytes):
    """D file: Action|County|Instrument|DocType|DocDesc|Legal|BookType|Book|Page|
    Filler|PageCount|DateRecorded|TimeRecorded|Consideration (trailing pipe).
    P file: Action|County|Instrument|Seq|FRM/TO|Name (trailing pipe)."""
    recs = {}
    for line in doc_bytes.decode("latin-1").splitlines():
        f = line.split("|")
        if len(f) < 14 or not f[2].strip():
            continue
        inst = f[2].strip()
        recs[inst] = {
            "instrument_no": inst,
            "doc_type": _text(f[3]),
            "doc_desc": _text(f[4]),
            "category": categorize(f[4] or f[3]),
            "book": _text(f[7]),
            "page": _text(f[8]),
            "recorded_at": _iso_from_mdy(f[11], f[12]),
            "consideration": _money(f[13]),
            "legal_desc": _text(f[5]),
            "parties": [],
        }
    for line in party_bytes.decode("latin-1").splitlines():
        f = line.split("|")
        if len(f) < 6:
            continue
        inst = f[2].strip()
        rec = recs.get(inst)
        name = _text(f[5])
        if rec is None or not name:
            continue
        role = "grantor" if f[4].strip().upper() == "FRM" else "grantee"
        try:
            seq = int(f[3])
        except ValueError:
            seq = len(rec["parties"]) + 1
        rec["parties"].append((role, seq, name))
    return list(recs.values())
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_hillsborough.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_hillsborough.py tests/fixtures/recordings/hillsborough
git commit -m "Recordings: Hillsborough daily D/P index parser"
```

---

### Task 3: Hernando parser

**Files:**
- Modify: `scripts/recordings.py`
- Test: `tests/test_recordings_hernando.py` (fixture created in Task 2 step 1)

**Interfaces:**
- Produces: `parse_hernando(data: bytes) -> list[Instrument]` (same record shape as Task 2); `hernando_file_date(name: str) -> str | None` returning `YYYY-MM-DD` for `MM-DD-YY.csv` and `MM-DD-YYYY.csv`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_recordings_hernando.py
from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings" / "hernando" / "12-27-2024.csv"


def test_rows_collapse_to_one_instrument_with_deduped_parties():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    r = recs["2024077386"]
    assert r["doc_desc"] == "JUDGMENT"
    assert r["category"] == "judgment"
    assert r["book"] == "4502" and r["page"] == "426"
    assert r["recorded_at"] == "2024-12-16T10:51:32"
    grantors = [n for role, _, n in r["parties"] if role == "grantor"]
    grantees = [n for role, _, n in r["parties"] if role == "grantee"]
    assert grantors == ["PROGRESSIVE SELECT INS CO", "DAVILA AMANDA SHEAVON"]
    assert "KROLLMCDOWELL SHELLEY" in grantees and len(grantees) == len(set(grantees))


def test_placeholder_legal_is_none():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    assert recs["2024077385"]["legal_desc"] is None


def test_real_legal_is_kept():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    assert recs["2024077431"]["legal_desc"] == "L42 Blk Un SubSPRING RIDGE S T R"
    assert recs["2024077431"]["category"] == "deed"
    assert recs["2024077431"]["consideration"] is None


def test_file_date_both_formats():
    assert R.hernando_file_date("01-05-24.csv") == "2024-01-05"
    assert R.hernando_file_date("12-27-2024.csv") == "2024-12-27"
    assert R.hernando_file_date("readme.txt") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_hernando.py -v`
Expected: FAIL with `AttributeError ... parse_hernando`

- [ ] **Step 3: Implement**

Append to `scripts/recordings.py`:

```python
import csv as _csv
import io as _io

_HERN_NAME = _re.compile(r"^(\d{2})-(\d{2})-(\d{2}|\d{4})\.csv$")
_HERN_PLACEHOLDER_LEGAL = "L Blk Un Sub S T R"


def hernando_file_date(name):
    m = _HERN_NAME.match(name)
    if not m:
        return None
    mm, dd, yy = m.groups()
    yyyy = yy if len(yy) == 4 else "20" + yy
    return f"{yyyy}-{mm}-{dd}"


def parse_hernando(data):
    """Columns: Grantor, Grantee, ClerkFileNumber, Book, PageNumber,
    DocumentTypeDescription, LegalDescription, DateRecorded. One row per
    grantor x grantee pair, so parties are de-duplicated per instrument."""
    recs = {}
    reader = _csv.reader(_io.StringIO(data.decode("latin-1")))
    for f in reader:
        if len(f) < 8 or not f[2].strip():
            continue
        inst = f[2].strip()
        rec = recs.get(inst)
        if rec is None:
            legal = _text(f[6])
            if legal == _HERN_PLACEHOLDER_LEGAL:
                legal = None
            rec = recs[inst] = {
                "instrument_no": inst,
                "doc_type": _text(f[5]),
                "doc_desc": _re.sub(r"[0-9X]+$", "", (f[5] or "").strip()).strip() or None,
                "category": categorize(f[5]),
                "book": _text(f[3]),
                "page": _text(f[4]),
                "recorded_at": _iso_from_mdy(f[7]),
                "consideration": None,
                "legal_desc": legal,
                "parties": [],
                "_seen": set(),
            }
        for role, name in (("grantor", _text(f[0])), ("grantee", _text(f[1]))):
            if name and (role, name) not in rec["_seen"]:
                rec["_seen"].add((role, name))
                seq = 1 + sum(1 for r, _, _ in rec["parties"] if r == role)
                rec["parties"].append((role, seq, name))
    out = []
    for rec in recs.values():
        rec.pop("_seen")
        out.append(rec)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_hernando.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_hernando.py tests/fixtures/recordings/hernando
git commit -m "Recordings: Hernando weekly CSV parser"
```

---

### Task 4: Schema and writer

**Files:**
- Modify: `scripts/recordings.py`
- Test: `tests/test_recordings_store.py`

**Interfaces:**
- Produces: `SCHEMA` (SQL string), `PRIVATE_TABLES = ("parcel_owners", "recorded_instruments", "instrument_parties", "instrument_parcels", "recording_files")`, `ensure_schema(conn)`, `write_instruments(conn, county, records, source_file, now) -> int` (instruments written; replaces parties of re-seen instruments), `file_loaded(conn, county, file_name) -> bool`, `mark_file_loaded(conn, county, file_name, rows, now)`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_recordings_store.py
import recordings as R

REC = {
    "instrument_no": "1", "doc_type": "MTG", "doc_desc": "MORTGAGE", "category": "mortgage",
    "book": None, "page": None, "recorded_at": "2026-08-21T07:44", "consideration": None,
    "legal_desc": "L 1 B 1 TEST SUB",
    "parties": [("grantor", 1, "Smith, John"), ("grantee", 1, "BIG BANK NA")],
}


def test_write_then_rewrite_replaces_parties(conn):
    R.ensure_schema(conn)
    assert R.write_instruments(conn, "hillsborough", [REC], "D1", "2026-09-16T00:00:00") == 1
    rec2 = dict(REC, parties=[("grantor", 1, "Smith, John")], consideration=61300.0)
    R.write_instruments(conn, "hillsborough", [rec2], "D2", "2026-09-17T00:00:00")
    rows = conn.execute("SELECT consideration, source_file FROM recorded_instruments").fetchall()
    assert rows == [(61300.0, "D2")]
    parties = conn.execute(
        "SELECT role, seq, name, name_norm, name_key FROM instrument_parties ORDER BY role").fetchall()
    assert parties == [("grantor", 1, "Smith, John", "SMITH JOHN", "SMITH JOHN")]


def test_file_tracking(conn):
    R.ensure_schema(conn)
    assert not R.file_loaded(conn, "hernando", "01-05-24.csv")
    R.mark_file_loaded(conn, "hernando", "01-05-24.csv", 10, "2026-09-16T00:00:00")
    assert R.file_loaded(conn, "hernando", "01-05-24.csv")
    assert not R.file_loaded(conn, "hillsborough", "01-05-24.csv")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_store.py -v`
Expected: FAIL with `AttributeError ... ensure_schema`

- [ ] **Step 3: Implement**

Append to `scripts/recordings.py`:

```python
PRIVATE_TABLES = ("parcel_owners", "recorded_instruments", "instrument_parties",
                  "instrument_parcels", "recording_files")
BATCH = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS recorded_instruments (
    county TEXT NOT NULL,
    instrument_no TEXT NOT NULL,
    doc_type TEXT,
    doc_desc TEXT,
    category TEXT,
    book TEXT,
    page TEXT,
    recorded_at TEXT,
    consideration REAL,
    legal_desc TEXT,
    source_file TEXT,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (county, instrument_no)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ri_cat ON recorded_instruments(county, category, recorded_at);
CREATE INDEX IF NOT EXISTS idx_ri_date ON recorded_instruments(county, recorded_at);

CREATE TABLE IF NOT EXISTS instrument_parties (
    county TEXT NOT NULL,
    instrument_no TEXT NOT NULL,
    role TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    name_key TEXT NOT NULL,
    PRIMARY KEY (county, instrument_no, role, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ip_name ON instrument_parties(county, name_key);

CREATE TABLE IF NOT EXISTS instrument_parcels (
    county TEXT NOT NULL,
    instrument_no TEXT NOT NULL,
    parcel_id TEXT NOT NULL,
    parcel_key TEXT NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY (county, instrument_no, parcel_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ipar_parcel ON instrument_parcels(county, parcel_key);

CREATE TABLE IF NOT EXISTS recording_files (
    county TEXT NOT NULL,
    file_name TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    rows INTEGER,
    PRIMARY KEY (county, file_name)
);
"""

_UPSERT_INSTRUMENT = """
INSERT INTO recorded_instruments (county, instrument_no, doc_type, doc_desc, category,
    book, page, recorded_at, consideration, legal_desc, source_file, last_synced_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(county, instrument_no) DO UPDATE SET
    doc_type=excluded.doc_type, doc_desc=excluded.doc_desc, category=excluded.category,
    book=excluded.book, page=excluded.page, recorded_at=excluded.recorded_at,
    consideration=excluded.consideration, legal_desc=excluded.legal_desc,
    source_file=excluded.source_file, last_synced_at=excluded.last_synced_at
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def write_instruments(conn, county, records, source_file, now):
    """Upsert instruments; parties of an instrument seen again are replaced
    (Hillsborough re-sends modified documents in a later day's file)."""
    n = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        conn.executemany(_UPSERT_INSTRUMENT, [
            (county, r["instrument_no"], r["doc_type"], r["doc_desc"], r["category"],
             r["book"], r["page"], r["recorded_at"], r["consideration"], r["legal_desc"],
             source_file, now) for r in chunk])
        conn.executemany("DELETE FROM instrument_parties WHERE county = ? AND instrument_no = ?",
                         [(county, r["instrument_no"]) for r in chunk])
        conn.executemany(
            "INSERT OR REPLACE INTO instrument_parties (county, instrument_no, role, seq, name, name_norm, name_key) "
            "VALUES (?,?,?,?,?,?,?)",
            [(county, r["instrument_no"], role, seq, name, norm_name(name), name_key(name))
             for r in chunk for role, seq, name in r["parties"]])
        n += len(chunk)
    conn.commit()
    return n


def file_loaded(conn, county, file_name):
    return conn.execute("SELECT 1 FROM recording_files WHERE county = ? AND file_name = ?",
                        (county, file_name)).fetchone() is not None


def mark_file_loaded(conn, county, file_name, rows, now):
    conn.execute("INSERT OR REPLACE INTO recording_files (county, file_name, loaded_at, rows) VALUES (?,?,?,?)",
                 (county, file_name, now, rows))
    conn.commit()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_store.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_store.py
git commit -m "Recordings: schema, instrument upsert, file tracking"
```

---

### Task 5: Fetchers, per-county sync, CLI

**Files:**
- Modify: `scripts/recordings.py`
- Test: `tests/test_recordings_sync.py`

**Interfaces:**
- Consumes: parsers and writer from Tasks 2 to 4; `etl.log`, `etl.get_conn`, `etl.acquire_run_lock`, `etl.release_run_lock`, `etl.TIMEOUT`.
- Produces: `SOURCES` dict `{county: {"list": fn() -> list[str], "fetch": fn(name) -> bytes, "units": fn(names) -> list[tuple[unit_key, list[str]]], "parse": fn(dict[name -> bytes]) -> list[Instrument]}}`; `sync_county(conn, county, source=None) -> bool`; `sync_all(conn) -> None`; `main()`. A `unit` is one loadable set (Hillsborough: D+P pair; Hernando: one CSV). `recording_files.file_name` stores the unit key.

- [ ] **Step 1: Failing test (with a fake source, no network)**

```python
# tests/test_recordings_sync.py
from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings"


def _fake_hillsborough():
    files = {n: (FX / "hillsborough" / n).read_bytes()
             for n in ("D2026082101id.29", "P2026082101id.29")}
    calls = {"fetch": 0}

    def fetch(name):
        calls["fetch"] += 1
        return files[name]
    src = dict(R.SOURCES["hillsborough"], list=lambda: list(files) + ["readme.txt"], fetch=fetch)
    return src, calls


def test_sync_county_loads_units_once(conn):
    src, calls = _fake_hillsborough()
    assert R.sync_county(conn, "hillsborough", src) is True
    n = conn.execute("SELECT COUNT(*) FROM recorded_instruments WHERE county='hillsborough'").fetchone()[0]
    assert n > 10
    assert conn.execute("SELECT county, file_name, rows FROM recording_files").fetchall() == [("hillsborough", "20260821", n)]
    assert R.file_loaded(conn, "hillsborough", "20260821")
    assert calls["fetch"] == 2
    R.sync_county(conn, "hillsborough", src)
    assert calls["fetch"] == 2  # second run skipped the loaded unit
    status = conn.execute("SELECT status, rows_fetched FROM sync_log WHERE dataset_type='recordings' ORDER BY id").fetchall()
    assert status[0][0] == "success" and status[0][1] == n
    assert status[1] == ("success", 0)


def test_sync_county_records_failure(conn):
    src, _ = _fake_hillsborough()
    src = dict(src, list=lambda: (_ for _ in ()).throw(RuntimeError("listing down")))
    assert R.sync_county(conn, "hillsborough", src) is False
    row = conn.execute("SELECT status, error FROM sync_log WHERE dataset_type='recordings'").fetchone()
    assert row[0] == "failed" and "listing down" in row[1]


def test_broward_skips_without_credentials(conn, monkeypatch):
    monkeypatch.delenv("FL_BROWARD_FTP_USER", raising=False)
    assert R.sync_county(conn, "broward") is None
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_sync.py -v`
Expected: FAIL with `AttributeError ... SOURCES`

- [ ] **Step 3: Implement**

Append to `scripts/recordings.py`:

```python
import os
import sys
import time
from datetime import timezone

import requests

import etl
from etl import log

HILLS_BASE = "https://publicrec.hillsclerk.com/OfficialRecords/DailyIndexes/"
HERN_BASE = "https://subscriber.hernandoclerk.com/"
HERN_DIR = "/data_files/official_records/"
UA = {"User-Agent": "fl-county-data/1.0 (public records index loader)"}
_HREF = _re.compile(r'href="([^"]+)"', _re.I)


def _get(url):
    last = None
    for attempt in range(1, etl.MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=UA, timeout=etl.TIMEOUT)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(30, 3 * attempt))
    raise RuntimeError(f"GET {url} failed after {etl.MAX_RETRIES} attempts: {last}")


def _hills_list():
    html = _get(HILLS_BASE).decode("latin-1")
    return [h.rsplit("/", 1)[-1] for h in _HREF.findall(html)]


def _hills_units(names):
    return [(key, [d, p]) for key, d, p in hillsborough_file_sets(names)]


def _hills_parse(files):
    d = next(v for k, v in files.items() if k.startswith("D"))
    p = next(v for k, v in files.items() if k.startswith("P"))
    return parse_hillsborough(d, p)


def _hern_list():
    html = _get(HERN_BASE).decode("latin-1")
    return [h.rsplit("/", 1)[-1] for h in _HREF.findall(html) if h.startswith(HERN_DIR)]


def _hern_units(names):
    dated = [(hernando_file_date(n), n) for n in names if hernando_file_date(n)]
    return [(n, [n]) for _, n in sorted(dated)]


def _broward_ready():
    return bool(os.environ.get("FL_BROWARD_FTP_USER") and os.environ.get("FL_BROWARD_FTP_PASS"))


def _broward_not_implemented(*_a, **_k):
    raise RuntimeError("Broward FTPS parser is not implemented yet; the file layout is only "
                       "visible once the clerk issues an account (954-831-4000)")


SOURCES = {
    "hillsborough": {
        "list": _hills_list,
        "fetch": lambda name: _get(HILLS_BASE + name),
        "units": _hills_units,
        "parse": _hills_parse,
        "note": "Daily D/P index files, about two months online, no login.",
    },
    "hernando": {
        "list": _hern_list,
        "fetch": lambda name: _get(HERN_BASE + HERN_DIR.lstrip("/") + name),
        "units": _hern_units,
        "parse": lambda files: parse_hernando(next(iter(files.values()))),
        "note": "Weekly CSV since 2024-01-05, no login.",
    },
    "broward": {
        "list": _broward_not_implemented,
        "fetch": _broward_not_implemented,
        "units": lambda names: [],
        "parse": _broward_not_implemented,
        "ready": _broward_ready,
        "note": "FTPS bcftp.broward.org; needs FL_BROWARD_FTP_USER / FL_BROWARD_FTP_PASS.",
    },
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def sync_county(conn, county, source=None):
    """Load every unit the feed lists that recording_files does not have.
    Returns True on success, False on failure (logged to sync_log), None when
    the source is configured but not ready (Broward without credentials)."""
    source = source or SOURCES[county]
    if "ready" in source and not source["ready"]():
        log(f"[SKIP] {county} recordings: {source['note']}")
        return None
    ensure_schema(conn)
    started = _now()
    written = 0
    loaded_units = 0
    try:
        names = source["list"]()
        units = source["units"](names)
        pending = [(k, f) for k, f in units if not file_loaded(conn, county, k)]
        log(f"  {county} recordings: {len(units)} units listed, {len(pending)} new")
        for key, files in pending:
            data = {f: source["fetch"](f) for f in files}
            records = source["parse"](data)
            n = write_instruments(conn, county, records, key, started)
            mark_file_loaded(conn, county, key, n, started)
            written += n
            loaded_units += 1
        link_instruments(conn, county)
        conn.execute("INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
                     "VALUES (?,?,?,?,?,?)", (county, "recordings", started, _now(), "success", written))
        conn.commit()
        log(f"[OK] {county} recordings: {written:,} instruments from {loaded_units} new units")
        return True
    except Exception as exc:  # noqa: BLE001
        conn.commit()
        conn.execute("INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched, error) "
                     "VALUES (?,?,?,?,?,?,?)", (county, "recordings", started, _now(), "failed", written, str(exc)))
        conn.commit()
        log(f"[FAIL] {county} recordings after {written:,} instruments: {exc}")
        return False


def sync_all(conn, counties=None):
    for county in counties or list(SOURCES):
        sync_county(conn, county)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    other = etl.acquire_run_lock("recordings feed load")
    if other:
        log(f"[SKIP] recordings: '{other.get('name')}' (pid {other.get('pid')}) holds the database")
        return
    try:
        conn = etl.get_conn()
        counties = args or list(SOURCES)
        if "--link-only" in flags:
            ensure_schema(conn)
            for c in counties:
                link_instruments(conn, c)
        else:
            sync_all(conn, counties)
        conn.close()
    finally:
        etl.release_run_lock()


if __name__ == "__main__":
    main()
```

`link_instruments` is defined in Task 7; until then add a temporary stub directly above `sync_county`:

```python
def link_instruments(conn, county):  # replaced in Task 7
    return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_sync.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Live smoke test, read-only against the network (no DB writes)**

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'scripts'); import recordings as R; print(len(R._hills_units(R._hills_list())), 'hills units;', len(R._hern_units(R._hern_list())), 'hernando units')"`
Expected: about 38 Hillsborough units and about 126 Hernando units.

- [ ] **Step 6: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_sync.py
git commit -m "Recordings: feed fetchers, per-county sync with file tracking, CLI"
```

---

### Task 6: parcel_owners from the statewide layer, Orange first

**Files:**
- Modify: `scripts/dor_values.py` (FIELDS at line 85, SCHEMA at line 97, `row_from_attrs` at ~line 180, `sync_statewide_values` at ~line 240, `main` at the end)
- Test: `tests/test_dor_owners.py`

**Interfaces:**
- Produces: `OWNER_FIELDS` list, `owner_row_from_attrs(a, now) -> tuple | None`, `OWNER_UPSERT` SQL, `ensure_owner_schema(conn)`, `sync_owners(conn, county=None) -> bool` (county block pull when a county is given, else full statewide owners-only pull with stale sweep), `county_objectid_range(conn, county) -> (lo, hi) | None`. `sync_statewide_values` also writes owner rows. CLI: `python dor_values.py --owners orange`, `--owners all`.
- Consumes: `recordings.norm_name`, `recordings.name_key`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_dor_owners.py
import dor_values as D


def test_owner_row_from_attrs_normalizes():
    a = {"CO_NO": 58, "PARCEL_ID": "01-22-28-0000-00-001", "OWN_NAME": "Smith, John ET AL",
         "OWN_ADDR1": "1 MAIN ST", "OWN_ADDR2": " ", "OWN_CITY": "ORLANDO", "OWN_STATE": "FL",
         "OWN_ZIPCD": 32801, "OWN_STATE_": " ", "OR_BOOK1": " ", "OR_PAGE1": " ",
         "CLERK_NO1": "20260012345", "OR_BOOK2": "1234", "OR_PAGE2": "56", "CLERK_NO2": " "}
    row = D.owner_row_from_attrs(a, "2026-09-16T00:00:00")
    assert row[:3] == ("orange", "01-22-28-0000-00-001", "012228000000001")
    assert row[3:6] == ("Smith, John ET AL", "SMITH JOHN", "SMITH JOHN")
    assert row[6:11] == ("1 MAIN ST", None, "ORLANDO", "FL", "32801")
    assert row[11] is None
    assert row[12:18] == (None, None, "20260012345", "1234", "56", None)


def test_owner_row_skips_unknown_county():
    assert D.owner_row_from_attrs({"CO_NO": 0, "PARCEL_ID": "x"}, "t") is None


def test_owner_upsert_and_county_range(conn):
    D.ensure_schema(conn)
    D.ensure_owner_schema(conn)
    conn.execute("INSERT INTO parcel_values (county, co_no, parcel_id, parcel_key, source_objectid, last_synced_at) "
                 "VALUES ('orange', 58, 'A', 'A', 6429451, 't'), ('orange', 58, 'B', 'B', 6921026, 't'), "
                 "('polk', 63, 'C', 'C', 1, 't')")
    assert D.county_objectid_range(conn, "orange") == (6429451, 6921026)
    assert D.county_objectid_range(conn, "nowhere") is None
    a = {"CO_NO": 58, "PARCEL_ID": "A", "OWN_NAME": "ACME LLC", "OWN_ADDR1": "PO BOX 1"}
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(a, "t1"))
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(dict(a, OWN_NAME="ACME HOLDINGS LLC"), "t2"))
    rows = conn.execute("SELECT owner_name, last_synced_at FROM parcel_owners").fetchall()
    assert rows == [("ACME HOLDINGS LLC", "t2")]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_dor_owners.py -v`
Expected: FAIL with `AttributeError ... owner_row_from_attrs`

- [ ] **Step 3: Implement**

In `scripts/dor_values.py`:

1. After `import etl` add `import recordings`.
2. After `FIELDS = [...]` add:

```python
OWNER_FIELDS = [
    "OWN_NAME", "OWN_ADDR1", "OWN_ADDR2", "OWN_CITY", "OWN_STATE", "OWN_ZIPCD", "OWN_STATE_",
    "OR_BOOK1", "OR_PAGE1", "CLERK_NO1", "OR_BOOK2", "OR_PAGE2", "CLERK_NO2",
]
QUERY_FIELDS = FIELDS + OWNER_FIELDS

OWNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS parcel_owners (
    county TEXT NOT NULL,
    parcel_id TEXT NOT NULL,
    parcel_key TEXT NOT NULL,
    owner_name TEXT,
    owner_name_norm TEXT,
    owner_name_key TEXT,
    mail_addr1 TEXT,
    mail_addr2 TEXT,
    mail_city TEXT,
    mail_state TEXT,
    mail_zip TEXT,
    owner_state_dom TEXT,
    or_book1 TEXT,
    or_page1 TEXT,
    clerk_no1 TEXT,
    or_book2 TEXT,
    or_page2 TEXT,
    clerk_no2 TEXT,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (county, parcel_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_po_key ON parcel_owners(county, parcel_key);
CREATE INDEX IF NOT EXISTS idx_po_name ON parcel_owners(county, owner_name_key);
CREATE INDEX IF NOT EXISTS idx_po_clerk1 ON parcel_owners(county, clerk_no1);
CREATE INDEX IF NOT EXISTS idx_po_clerk2 ON parcel_owners(county, clerk_no2);
"""

OWNER_UPSERT = """
INSERT INTO parcel_owners (county, parcel_id, parcel_key, owner_name, owner_name_norm, owner_name_key,
    mail_addr1, mail_addr2, mail_city, mail_state, mail_zip, owner_state_dom,
    or_book1, or_page1, clerk_no1, or_book2, or_page2, clerk_no2, last_synced_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(county, parcel_id) DO UPDATE SET
    parcel_key=excluded.parcel_key, owner_name=excluded.owner_name,
    owner_name_norm=excluded.owner_name_norm, owner_name_key=excluded.owner_name_key,
    mail_addr1=excluded.mail_addr1, mail_addr2=excluded.mail_addr2, mail_city=excluded.mail_city,
    mail_state=excluded.mail_state, mail_zip=excluded.mail_zip, owner_state_dom=excluded.owner_state_dom,
    or_book1=excluded.or_book1, or_page1=excluded.or_page1, clerk_no1=excluded.clerk_no1,
    or_book2=excluded.or_book2, or_page2=excluded.or_page2, clerk_no2=excluded.clerk_no2,
    last_synced_at=excluded.last_synced_at
"""


def ensure_owner_schema(conn):
    conn.executescript(OWNER_SCHEMA)
    conn.commit()


def owner_row_from_attrs(a, now):
    co_no = _int(a.get("CO_NO"))
    county = DOR_COUNTY_CODES.get(co_no)
    pid = _text(a.get("PARCEL_ID"))
    if county is None or not pid:
        return None
    name = _text(a.get("OWN_NAME"))
    return (
        county, pid, dor_key(county, pid), name, recordings.norm_name(name), recordings.name_key(name),
        _text(a.get("OWN_ADDR1")), _text(a.get("OWN_ADDR2")), _text(a.get("OWN_CITY")),
        _text(a.get("OWN_STATE")), _zip(a.get("OWN_ZIPCD")), _text(a.get("OWN_STATE_")),
        _text(a.get("OR_BOOK1")), _text(a.get("OR_PAGE1")), _text(a.get("CLERK_NO1")),
        _text(a.get("OR_BOOK2")), _text(a.get("OR_PAGE2")), _text(a.get("CLERK_NO2")),
        now,
    )


def county_objectid_range(conn, county):
    lo, hi = conn.execute("SELECT MIN(source_objectid), MAX(source_objectid) FROM parcel_values WHERE county = ?",
                          (county,)).fetchone()
    return (int(lo), int(hi)) if lo is not None else None
```

3. In `_worker`, change `"outFields": ",".join(FIELDS)` to `"outFields": ",".join(QUERY_FIELDS)`.

4. In `sync_statewide_values`, call `ensure_owner_schema(conn)` right after `ensure_schema(conn)`; inside the write loop, after `conn.executemany(UPSERT, rows)` add:

```python
            owner_rows = [o for o in (owner_row_from_attrs(a, started) for a in item) if o is not None]
            conn.executemany(OWNER_UPSERT, owner_rows)
```

and after the `DELETE FROM parcel_values WHERE last_synced_at < ?` line add:

```python
        conn.execute("DELETE FROM parcel_owners WHERE last_synced_at < ?", (started,))
```

5. Add the owners-only pull (after `sync_statewide_values`):

```python
def sync_owners(conn, county=None):
    """Owners-only pull. With a county: just that county's contiguous OBJECTID
    block (Orange: about 490k rows, a minute or two), which is how a county
    gets owners before the next 15-minute statewide pass. Without: the whole
    layer, and stale rows are swept."""
    ensure_schema(conn)
    ensure_owner_schema(conn)
    started = datetime.now(timezone.utc).isoformat()
    label = county or "statewide"
    t0 = time.time()
    total = 0
    errors = []
    try:
        if county:
            rng = county_objectid_range(conn, county)
            if rng is None:
                raise RuntimeError(f"no parcel_values rows for {county}; run the statewide pull first")
            lo, hi = rng[0] - 1, rng[1]
        else:
            lo, hi = 0, max_object_id()
        span = (hi - lo) // WORKERS + 1
        ranges = [(lo + i * span, min(hi, lo + (i + 1) * span)) for i in range(WORKERS)]
        out = queue.Queue(maxsize=WORKERS * 4)
        stop = threading.Event()
        threads = [threading.Thread(target=_worker, args=(i, a, b, out, errors, stop), daemon=True)
                   for i, (a, b) in enumerate(ranges)]
        for t in threads:
            t.start()
        done = pages = 0
        while done < len(threads):
            item = out.get()
            if item is None:
                done += 1
                continue
            rows = [o for o in (owner_row_from_attrs(a, started) for a in item)
                    if o is not None and (county is None or o[0] == county)]
            conn.executemany(OWNER_UPSERT, rows)
            total += len(rows)
            pages += 1
            if pages % COMMIT_EVERY == 0:
                conn.commit()
        conn.commit()
        for t in threads:
            t.join()
        if errors:
            raise RuntimeError("; ".join(errors))
        if county is None:
            conn.execute("DELETE FROM parcel_owners WHERE last_synced_at < ?", (started,))
        conn.execute("INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
                     "VALUES (?,?,?,?,?,?)", (label, "owners", started, datetime.now(timezone.utc).isoformat(), "success", total))
        conn.commit()
        log(f"[OK] owners ({label}): {total:,} rows in {(time.time() - t0) / 60:.1f} min")
        return True
    except Exception as exc:  # noqa: BLE001
        conn.commit()
        conn.execute("INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched, error) "
                     "VALUES (?,?,?,?,?,?,?)", (label, "owners", started, datetime.now(timezone.utc).isoformat(), "failed", total, str(exc)))
        conn.commit()
        log(f"[FAIL] owners ({label}) after {total:,} rows: {exc}")
        return False
```

6. Replace `main()`:

```python
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    other = etl.acquire_run_lock("dor_values")
    if other:
        log(f"[SKIP] dor_values: '{other.get('name')}' (pid {other.get('pid')}) holds the database")
        return
    try:
        conn = get_conn()
        if "--owners" in sys.argv:
            target = args[0] if args else "all"
            sync_owners(conn, None if target == "all" else target)
        else:
            if "--join-only" not in sys.argv:
                sync_statewide_values(conn)
            apply_values_to_features(conn)
        conn.close()
    finally:
        etl.release_run_lock()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_dor_owners.py tests/test_recordings_store.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/dor_values.py tests/test_dor_owners.py
git commit -m "DOR: parcel_owners table from the statewide layer; --owners <county> block pull"
```

---

### Task 7: Link instruments to parcels

**Files:**
- Modify: `scripts/recordings.py` (replace the Task 5 stub)
- Test: `tests/test_recordings_link.py`

**Interfaces:**
- Produces: `AMBIGUOUS_NAME_LIMIT = 5`; `link_instruments(conn, county) -> int` (links written). Methods: `clerk_no` (instrument_no equals `parcel_owners.clerk_no1` or `clerk_no2`), then `owner_name` (party `name_key` equals `owner_name_key`, skipped when that key matches more than `AMBIGUOUS_NAME_LIMIT` parcels in the county). Idempotent: existing links are kept, `INSERT OR IGNORE`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_recordings_link.py
import dor_values as D
import recordings as R


def _owner(conn, pid, name, clerk1=None):
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(
        {"CO_NO": 39, "PARCEL_ID": pid, "OWN_NAME": name, "CLERK_NO1": clerk1}, "t"))


def _inst(no, cat, parties, recorded="2026-08-21"):
    return {"instrument_no": no, "doc_type": cat.upper(), "doc_desc": cat.upper(), "category": cat,
            "book": None, "page": None, "recorded_at": recorded, "consideration": None,
            "legal_desc": None, "parties": parties}


def test_links_by_clerk_no_and_owner_name_with_ambiguity_cutoff(conn):
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    _owner(conn, "P1", "DOE JANE", clerk1="2026000001")
    _owner(conn, "P2", "UNIQUE OWNER LLC")
    for i in range(6):
        _owner(conn, f"C{i}", "SMITH JOHN")
    R.write_instruments(conn, "hillsborough", [
        _inst("2026000001", "deed", [("grantor", 1, "SELLER SAM"), ("grantee", 1, "NOBODY HERE")]),
        _inst("2026000002", "mortgage", [("grantor", 1, "Unique Owner, LLC"), ("grantee", 1, "BIG BANK")]),
        _inst("2026000003", "lien", [("grantor", 1, "SMITH JOHN"), ("grantee", 1, "HOA")]),
    ], "D1", "t")
    assert R.link_instruments(conn, "hillsborough") == 2
    links = conn.execute("SELECT instrument_no, parcel_id, method FROM instrument_parcels ORDER BY 1").fetchall()
    assert links == [("2026000001", "P1", "clerk_no"), ("2026000002", "P2", "owner_name")]
    assert R.link_instruments(conn, "hillsborough") == 0  # idempotent


def test_link_stores_parcel_key(conn):
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    _owner(conn, "12-34-56", "OWNER ONE")
    R.write_instruments(conn, "hillsborough", [_inst("9", "mortgage", [("grantor", 1, "OWNER ONE")])], "D1", "t")
    R.link_instruments(conn, "hillsborough")
    assert conn.execute("SELECT parcel_key FROM instrument_parcels").fetchone()[0] == "123456"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recordings_link.py -v`
Expected: FAIL (the stub returns 0; first assertion `== 2` fails)

- [ ] **Step 3: Implement**

Replace the stub in `scripts/recordings.py` with:

```python
AMBIGUOUS_NAME_LIMIT = 5


def link_instruments(conn, county):
    """Fill instrument_parcels for one county. Requires parcel_owners (Task 6);
    when that table is absent nothing is linked."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "parcel_owners" not in have:
        log(f"  {county}: parcel_owners table missing, skipping link")
        return 0
    t0 = time.time()
    n = 0
    for col in ("clerk_no1", "clerk_no2"):
        n += conn.execute(f"""
            INSERT OR IGNORE INTO instrument_parcels (county, instrument_no, parcel_id, parcel_key, method)
            SELECT ri.county, ri.instrument_no, po.parcel_id, po.parcel_key, 'clerk_no'
            FROM recorded_instruments ri
            JOIN parcel_owners po ON po.county = ri.county AND po.{col} = ri.instrument_no
            WHERE ri.county = ?""", (county,)).rowcount
    n += conn.execute(f"""
        INSERT OR IGNORE INTO instrument_parcels (county, instrument_no, parcel_id, parcel_key, method)
        SELECT ip.county, ip.instrument_no, po.parcel_id, po.parcel_key, 'owner_name'
        FROM instrument_parties ip
        JOIN parcel_owners po ON po.county = ip.county AND po.owner_name_key = ip.name_key
        WHERE ip.county = ? AND ip.name_key <> ''
          AND ip.name_key NOT IN (
              SELECT owner_name_key FROM parcel_owners
              WHERE county = ? AND owner_name_key <> ''
              GROUP BY owner_name_key HAVING COUNT(*) > {AMBIGUOUS_NAME_LIMIT})""",
        (county, county)).rowcount
    conn.commit()
    log(f"  {county}: {n:,} instrument-parcel links added in {time.time() - t0:.0f}s")
    return n
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/recordings.py tests/test_recordings_link.py
git commit -m "Recordings: link instruments to parcels by clerk number and owner name"
```

---

### Task 8: ETL wiring and demo exclusion

**Files:**
- Modify: `scripts/etl.py:814-845` (`main`)
- Modify: `scripts/make_demo_db.py:116-118` (schema copy)
- Test: `tests/test_demo_exclusion.py`

**Interfaces:**
- `etl.py` runs `recordings.sync_all(conn)` after the DOR step unless `--no-recordings` is passed or a single county was requested (then only that county if it is in `recordings.SOURCES`).
- `make_demo_db.py` never creates tables or indexes whose `tbl_name` is in `recordings.PRIVATE_TABLES`.

- [ ] **Step 1: Failing test**

```python
# tests/test_demo_exclusion.py
import sqlite3

import make_demo_db as M
import recordings as R


def test_schema_filter_drops_private_tables(conn):
    R.ensure_schema(conn)
    keep = list(M.schema_statements(conn))
    text = " ".join(keep)
    for t in R.PRIVATE_TABLES:
        assert t not in text
    assert "CREATE TABLE features" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_demo_exclusion.py -v`
Expected: FAIL with `AttributeError ... schema_statements`

- [ ] **Step 3: Implement**

In `scripts/make_demo_db.py`, after `import dor_values`, add `import recordings  # noqa: E402` and this function above `main`:

```python
def schema_statements(src):
    """CREATE statements to replay in the demo, minus the owner/recording
    tables (and their indexes), which never leave the local database."""
    private = set(recordings.PRIVATE_TABLES)
    for sql, tbl in src.execute(
            "SELECT sql, tbl_name FROM sqlite_master WHERE type IN ('table','index') "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
        if tbl not in private:
            yield sql
```

and replace the loop at lines 116-118 with:

```python
    for sql in schema_statements(src):
        dst.execute(sql)
```

In `scripts/etl.py` `main()`, after `dor_values.apply_values_to_features(conn, only_county)` insert:

```python
    import recordings
    if "--no-recordings" not in flags:
        if only_county:
            if only_county.lower() in recordings.SOURCES:
                recordings.sync_county(conn, only_county.lower())
        else:
            recordings.sync_all(conn)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/etl.py scripts/make_demo_db.py tests/test_demo_exclusion.py
git commit -m "ETL runs the recordings feeds daily; demo builder skips owner/recording tables"
```

---

### Task 9: API: owner, instruments, filters, status cell

**Files:**
- Modify: `scripts/app.py` (`build_filters` ~line 90, new routes after `api_feature_detail` ~line 400, `STATUS_DATASETS`/`_build_status_counties` ~lines 601-800)
- Test: `tests/test_api_recordings.py`

**Interfaces:**
- `GET /api/feature/<id>/owner` -> `{owner_name, mail_addr1, mail_addr2, mail_city, mail_state, mail_zip, owner_state_dom, or_book1, or_page1, clerk_no1}` or 404.
- `GET /api/feature/<id>/instruments` -> `{"instruments": [{instrument_no, category, doc_desc, recorded_at, consideration, book, page, method, parties: [{role, name}]}]}` newest first, or 404 when tables are absent.
- Query params on `/api/features` and `/api/features/geometry`: `mortgage_since` (YYYY-MM-DD), `mortgage_min`, `mortgage_max` (numbers), `has_lien` (`1`). Ignored when `instrument_parcels` is absent.
- `/api/status/counties`: `datasets.recordings` cell per county; `dataset_types` gains `"recordings"`.
- Helper: `has_table(conn, name) -> bool`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_api_recordings.py
import json

import pytest

import dor_values as D
import recordings as R


@pytest.fixture
def client(conn):
    import app as A
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute("INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, last_synced_at) "
                 "VALUES ('hillsborough','parcels','A-1','A1','t'), ('hillsborough','parcels','B-2','B2','t')")
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(
        {"CO_NO": 39, "PARCEL_ID": "A-1", "OWN_NAME": "DOE JANE", "OWN_ADDR1": "1 MAIN", "OWN_CITY": "TAMPA",
         "OWN_STATE": "FL", "OWN_ZIPCD": 33601}, "t"))
    R.write_instruments(conn, "hillsborough", [
        {"instrument_no": "10", "doc_type": "MTG", "doc_desc": "MORTGAGE", "category": "mortgage", "book": None,
         "page": None, "recorded_at": "2026-08-21T07:44", "consideration": 61300.0, "legal_desc": None,
         "parties": [("grantor", 1, "DOE JANE"), ("grantee", 1, "BIG BANK")]},
        {"instrument_no": "11", "doc_type": "LN", "doc_desc": "LIEN", "category": "lien", "book": None,
         "page": None, "recorded_at": "2026-08-22", "consideration": None, "legal_desc": None,
         "parties": [("grantor", 1, "DOE JANE"), ("grantee", 1, "HOA")]},
    ], "D1", "t")
    R.link_instruments(conn, "hillsborough")
    conn.commit()
    A.app.config["TESTING"] = True
    return A.app.test_client()


def _ids(resp):
    return sorted(r["feature_key"] for r in json.loads(resp.data)["rows"])


def test_owner_endpoint(client):
    fid = json.loads(client.get("/api/features?county=hillsborough").data)["rows"][0]["id"]
    r = client.get(f"/api/feature/{fid}/owner")
    assert r.status_code == 200
    assert json.loads(r.data)["owner_name"] == "DOE JANE"


def test_instruments_endpoint_newest_first(client):
    fid = json.loads(client.get("/api/features?county=hillsborough").data)["rows"][0]["id"]
    data = json.loads(client.get(f"/api/feature/{fid}/instruments").data)["instruments"]
    assert [d["instrument_no"] for d in data] == ["11", "10"]
    assert data[1]["consideration"] == 61300.0
    assert {"role": "grantee", "name": "BIG BANK"} in data[1]["parties"]


def test_mortgage_and_lien_filters(client):
    assert _ids(client.get("/api/features?mortgage_since=2026-08-01")) == ["A-1"]
    assert _ids(client.get("/api/features?mortgage_since=2026-09-01")) == []
    assert _ids(client.get("/api/features?mortgage_min=50000")) == ["A-1"]
    assert _ids(client.get("/api/features?mortgage_max=50000")) == []
    assert _ids(client.get("/api/features?has_lien=1")) == ["A-1"]
    assert _ids(client.get("/api/features")) == ["A-1", "B-2"]


def test_status_has_recordings_cell(client):
    data = json.loads(client.get("/api/status/counties").data)
    assert "recordings" in data["dataset_types"]
    row = next(r for r in data["counties"] if r["county"] == "hillsborough")
    assert row["datasets"]["recordings"]["row_count"] == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_recordings.py -v`
Expected: FAIL (404s and missing keys)

- [ ] **Step 3: Implement**

In `scripts/app.py`:

1. After `import etl` add `import recordings  # noqa: E402`.
2. Add a helper below `close_db`:

```python
def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
```

3. `build_filters(args)` becomes `build_filters(args, conn=None)`; after the `q` block and before `where = ...` add:

```python
    if conn is not None and has_table(conn, "instrument_parcels"):
        link = ("EXISTS (SELECT 1 FROM instrument_parcels ip JOIN recorded_instruments ri "
                "ON ri.county = ip.county AND ri.instrument_no = ip.instrument_no "
                "WHERE ip.county = features.county AND ip.parcel_key = features.feature_key_norm {cond})")
        since = (args.get("mortgage_since") or "").strip()
        mmin, mmax = args.get("mortgage_min"), args.get("mortgage_max")
        conds, mparams = [], []
        if since:
            conds.append("AND ri.recorded_at >= ?"); mparams.append(since)
        for v, op in ((mmin, ">="), (mmax, "<=")):
            if v not in (None, ""):
                try:
                    conds.append(f"AND ri.consideration {op} ?"); mparams.append(float(v))
                except ValueError:
                    pass
        if conds:
            clauses.append(link.format(cond="AND ri.category = 'mortgage' " + " ".join(conds)))
            params.extend(mparams)
        if args.get("has_lien") == "1":
            clauses.append(link.format(
                cond="AND ri.category IN ('lien','lis_pendens','judgment') AND NOT EXISTS ("
                     "SELECT 1 FROM instrument_parcels ip2 JOIN recorded_instruments r2 "
                     "ON r2.county = ip2.county AND r2.instrument_no = ip2.instrument_no "
                     "WHERE ip2.county = ip.county AND ip2.parcel_key = ip.parcel_key "
                     "AND r2.category IN ('satisfaction','release') AND r2.recorded_at > ri.recorded_at)"))
```

4. In `api_features` and `api_features_geometry` change `build_filters(request.args)` to `build_filters(request.args, conn)`.

5. After `api_feature_detail` add:

```python
def _feature_parcel(conn, feature_id):
    row = conn.execute("SELECT county, feature_key_norm FROM features WHERE id = ? AND dataset_type = 'parcels'",
                       (feature_id,)).fetchone()
    return (row["county"], row["feature_key_norm"]) if row else None


@app.route("/api/feature/<int:feature_id>/owner")
def api_feature_owner(feature_id):
    conn = get_db()
    key = _feature_parcel(conn, feature_id)
    if key is None or not has_table(conn, "parcel_owners"):
        return jsonify({"error": "not found"}), 404
    row = conn.execute(
        "SELECT owner_name, mail_addr1, mail_addr2, mail_city, mail_state, mail_zip, owner_state_dom, "
        "or_book1, or_page1, clerk_no1, last_synced_at FROM parcel_owners WHERE county = ? AND parcel_key = ? LIMIT 1",
        key).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/api/feature/<int:feature_id>/instruments")
def api_feature_instruments(feature_id):
    conn = get_db()
    key = _feature_parcel(conn, feature_id)
    if key is None or not has_table(conn, "instrument_parcels"):
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT ri.instrument_no, ri.category, ri.doc_desc, ri.recorded_at, ri.consideration, ri.book, ri.page, ip.method "
        "FROM instrument_parcels ip JOIN recorded_instruments ri "
        "ON ri.county = ip.county AND ri.instrument_no = ip.instrument_no "
        "WHERE ip.county = ? AND ip.parcel_key = ? ORDER BY ri.recorded_at DESC, ri.instrument_no DESC LIMIT 200",
        key).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parties"] = [{"role": p["role"], "name": p["name"]} for p in conn.execute(
            "SELECT role, name FROM instrument_parties WHERE county = ? AND instrument_no = ? ORDER BY role, seq",
            (key[0], r["instrument_no"]))]
        out.append(d)
    return jsonify({"instruments": out})
```

6. Status. In `_build_status_counties`, after `value_counts = ...` add:

```python
    rec_counts = {}
    if has_table(conn, "recorded_instruments"):
        rec_counts = {r["county"]: r["n"] for r in conn.execute(
            "SELECT county, COUNT(*) AS n FROM recorded_instruments GROUP BY county")}
```

inside the county loop after `datasets["dor_values"] = dor_cell` add:

```python
        rec_src = recordings.SOURCES.get(county)
        datasets["recordings"] = _cell(
            {"type": "feed", "note": rec_src["note"]} if rec_src else None,
            latest.get((county, "recordings")),
            last_ok.get((county, "recordings")),
            rec_counts.get(county, 0),
        )
```

and change `"dataset_types": ["dor_values"] + list(STATUS_DATASETS)` to `"dataset_types": ["dor_values"] + list(STATUS_DATASETS) + ["recordings"]`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/app.py tests/test_api_recordings.py
git commit -m "API: parcel owner and recorded-instrument endpoints, mortgage/lien filters, recordings status cell"
```

---

### Task 10: UI

**Files:**
- Modify: `scripts/templates/index.html` (filter form at line 74; status thead at line 52)
- Modify: `scripts/static/app.js` (`state` init near line 10-20, `currentFilterParams` line 64, `readFiltersFromForm` line 1393, `showDetail` line 459, `STATUS_COLUMNS` line 513, `statusCellHtml` line 567)
- Modify: `scripts/static/style.css` (one rule)

No automated test: verified in the browser in Task 11.

- [ ] **Step 1: Filter markup**

In `index.html`, after the Max acreage label (before the Search label) insert:

```html
      <span class="recording-filters" {% if demo %}hidden{% endif %}>
        <label>Mortgage since
          <input type="date" id="f-mtg-since" title="Parcels with a linked mortgage recorded on or after this date (clerk index feeds)">
        </label>
        <label>Min mortgage $
          <input type="number" step="any" id="f-mtg-min" placeholder="any" title="Mortgage amount is only known where the clerk index carries it">
        </label>
        <label>Max mortgage $
          <input type="number" step="any" id="f-mtg-max" placeholder="any">
        </label>
        <label class="check"><input type="checkbox" id="f-lien"> Open lien / lis pendens</label>
      </span>
```

Status header: after the Future land use `<th>` add:

```html
            <th class="sortable" data-sort-key="recordings" title="Clerk official-records index feed: last successful load and instruments stored">Recordings</th>
```

- [ ] **Step 2: JS state and params**

Find the `state` object initialiser (search `min_acreage: ""`) and add `mortgage_since: "", mortgage_min: "", mortgage_max: "", has_lien: "",`. In `currentFilterParams` add `mortgage_since: state.mortgage_since, mortgage_min: state.mortgage_min, mortgage_max: state.mortgage_max, has_lien: state.has_lien,`. In `readFiltersFromForm` add:

```js
  state.mortgage_since = document.getElementById("f-mtg-since").value;
  state.mortgage_min = document.getElementById("f-mtg-min").value;
  state.mortgage_max = document.getElementById("f-mtg-max").value;
  state.has_lien = document.getElementById("f-lien").checked ? "1" : "";
```

`qs()` already drops empty values (check: it must skip `""`; if it does not, filter them there).

- [ ] **Step 3: Detail popup sections**

In `showDetail`, after `body.innerHTML = ...` (inside the `try`), append:

```js
    if (data.dataset_type === "parcels" && !window.DEMO_MODE) {
      const [owner, inst] = await Promise.all([
        fetchJSON(`/api/feature/${id}/owner`).catch(() => null),
        fetchJSON(`/api/feature/${id}/instruments`).catch(() => null),
      ]);
      let extra = "";
      if (owner) {
        const addr = [owner.mail_addr1, owner.mail_addr2,
          [owner.mail_city, owner.mail_state, owner.mail_zip].filter(Boolean).join(" ")].filter(Boolean);
        extra += `<h4>Owner <span class="source-note">FL DOR tax roll</span></h4><table class="detail-table">` +
          `<tr>${fieldTh("Owner")}<td>${esc(owner.owner_name || "—")}</td></tr>` +
          `<tr>${fieldTh("Mailing address")}<td>${addr.map(esc).join("<br>") || "—"}</td></tr>` +
          (owner.owner_state_dom ? `<tr>${fieldTh("Domicile")}<td>${esc(owner.owner_state_dom)}</td></tr>` : "") +
          (owner.clerk_no1 || owner.or_book1
            ? `<tr>${fieldTh("Last deed ref")}<td>${esc(owner.clerk_no1 || `Book ${owner.or_book1} Page ${owner.or_page1}`)}</td></tr>` : "") +
          `</table>`;
      }
      if (inst && inst.instruments.length) {
        const rows = inst.instruments.map((r) => {
          const other = r.parties.filter((p) => p.role === "grantee").map((p) => p.name).join("; ");
          return `<tr><td>${esc((r.recorded_at || "").slice(0, 10))}</td><td>${esc(pretty(r.category))}</td>` +
            `<td>${esc(r.doc_desc || "")}</td><td>${r.consideration ? fmtMoney(r.consideration) : "—"}</td>` +
            `<td>${esc(other)}</td><td>${esc([r.book, r.page].filter(Boolean).join("/") || r.instrument_no)}</td></tr>`;
        }).join("");
        extra += `<h4>Recorded instruments <span class="source-note">county clerk index; linked by ${esc(inst.instruments[0].method === "clerk_no" ? "deed number" : "owner name")}</span></h4>` +
          `<table class="detail-table instruments"><tr><th>Recorded</th><th>Type</th><th>Description</th><th>Amount</th><th>To</th><th>Ref</th></tr>${rows}</table>` +
          `<p class="hint">Mortgage amounts appear only when the clerk index carries them.</p>`;
      } else if (owner) {
        extra += `<p class="hint">No recorded instruments linked to this parcel (feeds cover Hillsborough and Hernando).</p>`;
      }
      body.insertAdjacentHTML("beforeend", extra);
    }
```

- [ ] **Step 4: Status column**

Change `STATUS_COLUMNS` to `["dor_values", "parcels", "zoning", "land_use", "future_land_use", "recordings"]`. In `statusCellHtml`, change the noun line to:

```js
  const noun = dataset === "dor_values" ? "parcel" : dataset === "recordings" ? "instrument" : "row";
```

- [ ] **Step 5: Style**

Append to `style.css`:

```css
.recording-filters { display: contents; }
.filters label.check { flex-direction: row; align-items: center; gap: .4em; }
.detail-table.instruments th { text-align: left; }
```

- [ ] **Step 6: Commit**

```bash
git add scripts/templates/index.html scripts/static/app.js scripts/static/style.css
git commit -m "UI: owner and recorded-instrument sections, mortgage/lien filters, recordings status column"
```

---

### Task 11: Load data, restart the app, verify, document

**Files:**
- Modify: `README.md` (privacy section at line 265; add a "Owners and recorded instruments" section after "Statewide parcel values and sales")

- [ ] **Step 1: Orange owners first**

Run: `.venv/Scripts/python.exe scripts/dor_values.py --owners orange`
Expected: `[OK] owners (orange): ~488,000 rows in about 1-3 min`. Then check: `.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('file:D:/fl-county-data/data/fl_county_data.db?mode=ro',uri=True); print(c.execute(\"select count(*), sum(owner_name is not null) from parcel_owners where county='orange'\").fetchone())"` (use `etl.DB_PATH` if the path differs).

- [ ] **Step 2: Hillsborough and Hernando owners, then recordings backfill**

Run: `.venv/Scripts/python.exe scripts/dor_values.py --owners hillsborough` then `--owners hernando`.
Run: `.venv/Scripts/python.exe scripts/recordings.py`
Expected: `[OK] hillsborough recordings: ~70,000 instruments from ~38 new units`, `[OK] hernando recordings: ~200,000 instruments from ~126 new units`, link lines showing thousands of links each, and `[SKIP] broward`.

- [ ] **Step 3: Restart the app**

Run (PowerShell): `Get-Process python | Where-Object { $_.Path -like '*fl-county-data*' -and (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like '*app.py*' } | Stop-Process`
Then: `Start-Process -NoNewWindow -FilePath .venv\Scripts\python.exe -ArgumentList 'scripts\app.py' -RedirectStandardOutput logs\app.out -RedirectStandardError logs\app.err`
Check: `curl -s -o NUL -w "%{http_code}" http://127.0.0.1:5000/` -> 200.

- [ ] **Step 4: Verify in the browser or with curl**

- `curl "http://127.0.0.1:5000/api/features?county=hillsborough&dataset_type=parcels&mortgage_since=2026-08-01&per_page=3"` returns rows with `total` in the thousands.
- Take one `id` from that response and open `/api/feature/<id>/owner` and `/api/feature/<id>/instruments`: owner present, at least one mortgage instrument.
- `curl "http://127.0.0.1:5000/api/status/counties"` shows `recordings.state == "ok"` for hillsborough and hernando and `"not_configured"` for orange.
- Open http://127.0.0.1:5000/, filter Hillsborough parcels with "Mortgage since" set, click a row: the popup shows Owner and Recorded instruments.

- [ ] **Step 5: README**

Replace the "Privacy: owner and mailing fields are not stored" section heading and body with:

```markdown
### Owners and recorded instruments (local database only)

The statewide DOR pull now also fills `parcel_owners` (owner name, mailing
address, state of domicile, last two deed references) for every parcel, and
`scripts/recordings.py` loads county Clerk official-records index feeds into
`recorded_instruments` / `instrument_parties`, linking them to parcels in
`instrument_parcels` by clerk instrument number (exact) or owner name (skipped
when a name matches more than 5 parcels in the county). Feeds today:
Hillsborough (daily D/P files, ~2 months online) and Hernando (weekly CSV
since 2024). Broward activates when `FL_BROWARD_FTP_USER` / `FL_BROWARD_FTP_PASS`
are set (the clerk issues accounts: 954-831-4000); Lake, Palm Beach and
Miami-Dade sell subscriptions and are not wired in. Neither feed carries a
parcel number or, in practice, a mortgage amount (Hillsborough fills the
consideration on 1-3 of ~150 mortgages a day; Hernando has no amount column), so
the "Min/Max mortgage $" filter only matches instruments whose amount is known.

`python scripts/dor_values.py --owners orange` loads one county's owners
from its contiguous block of the statewide layer in a minute or two (Orange
is first); `--owners all` does the whole state. `python scripts/recordings.py
[county]` loads new feed files; `etl.py` runs both daily (`--no-recordings`
skips the feeds).

These five tables (`parcel_owners`, `recorded_instruments`,
`instrument_parties`, `instrument_parcels`, `recording_files`) are never
copied into the public demo database (`make_demo_db.schema_statements`), and
the demo app hides the filters and popup sections. Raw county-layer owner
and mailing attributes are still dropped from `attributes_json`
(`etl.is_private_field`): the DOR roll is the one source of owner data.
```

Also add `recordings.py` to the Layout block near the top of the README.

- [ ] **Step 6: Full test run and commit**

Run: `.venv/Scripts/python.exe -m pytest tests -v`
Expected: all PASS

```bash
git add README.md
git commit -m "Docs: owners and recorded-instrument feeds"
```

---

## Self-review notes

- Spec coverage: owner table (Task 6), Orange first (Task 6 `--owners orange`, Task 11 step 1), feed loader with file tracking and failure handling (Tasks 2-5), link with ambiguity cutoff (Task 7), ETL wiring and `--no-recordings` (Task 8), demo exclusion (Task 8), API endpoints and three filters (Task 9), status cell (Tasks 9-10), UI sections and filters (Task 10), README (Task 11), tests before code throughout. Broward stub with env-var gate (Task 5). The spec's "more than 5% bad lines" rule is not implemented: both parsers skip malformed lines silently, and per-file row counts land in `recording_files.rows` for inspection. Acceptable for two well-formed feeds; revisit if a feed starts dropping rows.
- Type consistency: `Instrument` dict keys identical across Tasks 2, 3, 4, 7, 9 tests; `parties` tuples are `(role, seq, name)` everywhere; `link_instruments(conn, county)` signature matches Tasks 5, 7, 9.
