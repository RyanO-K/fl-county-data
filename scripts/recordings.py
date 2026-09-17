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


# --- Parsers -----------------------------------------------------------------
import csv as _csv
import io as _io
from datetime import datetime

_HILLS_NAME = re.compile(r"^([DP])(\d{8})\d{2}id\.29$")
_HERN_NAME = re.compile(r"^(\d{2})-(\d{2})-(\d{2}|\d{4})\.csv$")
_HERN_PLACEHOLDER_LEGAL = "L Blk Un Sub S T R"


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
        if re.match(r"^\d{1,2}:\d{2}$", t):
            h, m = t.split(":")
            return dt.strftime("%Y-%m-%d") + f"T{int(h):02d}:{m}"
    if "%I" in fmt:
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
        legal = _text(f[6])
        if legal == _HERN_PLACEHOLDER_LEGAL:
            legal = None
        if rec is None:
            rec = recs[inst] = {
                "instrument_no": inst,
                "doc_type": _text(f[5]),
                "doc_desc": re.sub(r"[0-9X]+$", "", (f[5] or "").strip()).strip() or None,
                "category": categorize(f[5]),
                "book": _text(f[3]),
                "page": _text(f[4]),
                "recorded_at": _iso_from_mdy(f[7]),
                "consideration": None,
                "legal_desc": None,
                "parties": [],
                "_seen": set(),
                "_legals": [],
            }
        # Rows repeat per grantor x grantee x legal line; keep each real legal once.
        if legal and legal not in rec["_legals"]:
            rec["_legals"].append(legal)
        for role, name in (("grantor", _text(f[0])), ("grantee", _text(f[1]))):
            if name and (role, name) not in rec["_seen"]:
                rec["_seen"].add((role, name))
                seq = 1 + sum(1 for r, _, _ in rec["parties"] if r == role)
                rec["parties"].append((role, seq, name))
    out = []
    for rec in recs.values():
        rec.pop("_seen")
        rec["legal_desc"] = "; ".join(rec.pop("_legals")) or None
        out.append(rec)
    return out


# --- Storage -------------------------------------------------------------------
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


# --- Feeds and sync ------------------------------------------------------------
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
_HREF = re.compile(r'href="([^"]+)"', re.I)


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


AMBIGUOUS_NAME_LIMIT = 5


def link_instruments(conn, county):
    """Fill instrument_parcels for one county. Requires parcel_owners
    (dor_values.sync_owners); when that table is absent nothing is linked.

    INDEXED BY is deliberate: SQLite otherwise joins parcel_owners through its
    WITHOUT ROWID primary key (county only) and scans every owner in the
    county per party, which turned a seconds-long link into hours (the same
    planner trap the DOR value join works around)."""
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
            JOIN parcel_owners po INDEXED BY idx_po_{col.replace("_no", "")} ON po.county = ri.county AND po.{col} = ri.instrument_no
            WHERE ri.county = ?""", (county,)).rowcount
    conn.execute("DROP TABLE IF EXISTS temp.ambiguous_names")
    conn.execute(f"""
        CREATE TEMP TABLE ambiguous_names AS
        SELECT owner_name_key AS name_key FROM parcel_owners INDEXED BY idx_po_name
        WHERE county = ? AND owner_name_key <> ''
        GROUP BY owner_name_key HAVING COUNT(*) > {AMBIGUOUS_NAME_LIMIT}""", (county,))
    n += conn.execute("""
        INSERT OR IGNORE INTO instrument_parcels (county, instrument_no, parcel_id, parcel_key, method)
        SELECT ip.county, ip.instrument_no, po.parcel_id, po.parcel_key, 'owner_name'
        FROM instrument_parties ip INDEXED BY idx_ip_name
        JOIN parcel_owners po INDEXED BY idx_po_name
          ON po.county = ip.county AND po.owner_name_key = ip.name_key
        WHERE ip.county = ? AND ip.name_key <> ''
          AND ip.name_key NOT IN (SELECT name_key FROM temp.ambiguous_names)""",
        (county,)).rowcount
    conn.execute("DROP TABLE IF EXISTS temp.ambiguous_names")
    conn.commit()
    log(f"  {county}: {n:,} instrument-parcel links added in {time.time() - t0:.0f}s")
    return n


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
