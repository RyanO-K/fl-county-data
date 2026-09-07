"""Statewide parcel values (all 67 counties) from the Florida Department of
Revenue tax roll, as published by the State Geographic Information Office's
"Florida Statewide Cadastral" ArcGIS layer.

Every parcel in the state carries just/assessed/taxable/land value, the DOR
use code, and the last two recorded sales (price, year, month, qualification
code). The layer is an annual snapshot (DOR collects county rolls each
April), so this is the value/sales backbone; county layers that publish
fresher values keep theirs (see apply_values_to_features).

Owner names and mailing addresses are deliberately not stored.

Usage:
    python dor_values.py              # full statewide pull + join into features
    python dor_values.py --join-only  # only re-run the join step
"""
import queue
import sys
import threading
import time
from datetime import datetime, timezone

import requests

from etl import MAX_RETRIES, TIMEOUT, get_conn, log

LAYER_URL = ("https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
             "Florida_Statewide_Cadastral/FeatureServer/0")
QUERY_URL = LAYER_URL + "/query"
PAGE = 2000          # the layer's maxRecordCount
WORKERS = 4          # parallel OBJECTID ranges; hosted services handle this fine
COMMIT_EVERY = 20    # pages per commit on the single writer thread

# DOR county numbers (alphabetical order + 10, with St. Johns / St. Lucie
# sorted under "St" ahead of Santa Rosa). Confirmed against the layer.
DOR_COUNTY_CODES = {
    11: "alachua", 12: "baker", 13: "bay", 14: "bradford", 15: "brevard",
    16: "broward", 17: "calhoun", 18: "charlotte", 19: "citrus", 20: "clay",
    21: "collier", 22: "columbia", 23: "miami_dade", 24: "desoto", 25: "dixie",
    26: "duval", 27: "escambia", 28: "flagler", 29: "franklin", 30: "gadsden",
    31: "gilchrist", 32: "glades", 33: "gulf", 34: "hamilton", 35: "hardee",
    36: "hendry", 37: "hernando", 38: "highlands", 39: "hillsborough",
    40: "holmes", 41: "indian_river", 42: "jackson", 43: "jefferson",
    44: "lafayette", 45: "lake", 46: "lee", 47: "leon", 48: "levy",
    49: "liberty", 50: "madison", 51: "manatee", 52: "marion", 53: "martin",
    54: "monroe", 55: "nassau", 56: "okaloosa", 57: "okeechobee", 58: "orange",
    59: "osceola", 60: "palm_beach", 61: "pasco", 62: "pinellas", 63: "polk",
    64: "putnam", 65: "st_johns", 66: "st_lucie", 67: "santa_rosa",
    68: "sarasota", 69: "seminole", 70: "sumter", 71: "suwannee", 72: "taylor",
    73: "union", 74: "volusia", 75: "wakulla", 76: "walton", 77: "washington",
}

FIELDS = [
    "OBJECTID", "CO_NO", "PARCEL_ID", "ASMNT_YR", "DOR_UC",
    "JV", "AV_NSD", "TV_NSD", "LND_VAL", "LND_SQFOOT",
    "NO_BULDNG", "ACT_YR_BLT", "TOT_LVG_AR",
    "SALE_PRC1", "SALE_YR1", "SALE_MO1", "QUAL_CD1",
    "SALE_PRC2", "SALE_YR2", "SALE_MO2", "QUAL_CD2",
    "PHY_ADDR1", "PHY_CITY", "PHY_ZIPCD",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcel_values (
    county TEXT NOT NULL,
    co_no INTEGER NOT NULL,
    parcel_id TEXT NOT NULL,
    parcel_key TEXT NOT NULL,
    assessment_year INTEGER,
    dor_use_code TEXT,
    just_value REAL,
    assessed_value REAL,
    taxable_value REAL,
    land_value REAL,
    land_sqft REAL,
    building_count INTEGER,
    year_built INTEGER,
    living_area REAL,
    sale_price REAL,
    sale_year INTEGER,
    sale_month INTEGER,
    sale_qual TEXT,
    sale2_price REAL,
    sale2_year INTEGER,
    sale2_month INTEGER,
    sale2_qual TEXT,
    site_address TEXT,
    site_city TEXT,
    site_zip TEXT,
    source_objectid INTEGER,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (county, parcel_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_pv_key ON parcel_values(county, parcel_key);
CREATE INDEX IF NOT EXISTS idx_pv_use ON parcel_values(county, dor_use_code);
CREATE INDEX IF NOT EXISTS idx_pv_jv ON parcel_values(county, just_value);
"""

FEATURE_COLUMNS = [
    ("acreage_source", "TEXT"), ("city", "TEXT"),
    ("just_value", "REAL"), ("assessed_value", "REAL"), ("taxable_value", "REAL"),
    ("sale_price", "REAL"), ("sale_date", "TEXT"), ("sale_qual", "TEXT"),
    ("dor_use_code", "TEXT"), ("site_address", "TEXT"),
]

UPSERT = """
INSERT INTO parcel_values (county, co_no, parcel_id, parcel_key, assessment_year,
    dor_use_code, just_value, assessed_value, taxable_value, land_value, land_sqft,
    building_count, year_built, living_area,
    sale_price, sale_year, sale_month, sale_qual,
    sale2_price, sale2_year, sale2_month, sale2_qual,
    site_address, site_city, site_zip, source_objectid, last_synced_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(county, parcel_id) DO UPDATE SET
    co_no=excluded.co_no, parcel_key=excluded.parcel_key,
    assessment_year=excluded.assessment_year, dor_use_code=excluded.dor_use_code,
    just_value=excluded.just_value, assessed_value=excluded.assessed_value,
    taxable_value=excluded.taxable_value, land_value=excluded.land_value,
    land_sqft=excluded.land_sqft, building_count=excluded.building_count,
    year_built=excluded.year_built, living_area=excluded.living_area,
    sale_price=excluded.sale_price, sale_year=excluded.sale_year,
    sale_month=excluded.sale_month, sale_qual=excluded.sale_qual,
    sale2_price=excluded.sale2_price, sale2_year=excluded.sale2_year,
    sale2_month=excluded.sale2_month, sale2_qual=excluded.sale2_qual,
    site_address=excluded.site_address, site_city=excluded.site_city,
    site_zip=excluded.site_zip, source_objectid=excluded.source_objectid,
    last_synced_at=excluded.last_synced_at
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    for name, typ in FEATURE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE features ADD COLUMN {name} {typ}")
    conn.commit()


def normalize_key(pid):
    """Parcel ids differ cosmetically between county layers and the DOR roll
    (spaces, dashes, dots). Compare on a stripped, upper-cased form."""
    if pid is None:
        return ""
    return "".join(ch for ch in str(pid).upper() if ch.isalnum())


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _zip(v):
    n = _int(v)
    return f"{n:05d}" if n else None


def _sale_price(v):
    # DOR uses 0 / blank for "no sale recorded"
    n = _num(v)
    return n if n and n > 0 else None


def row_from_attrs(a, now):
    co_no = _int(a.get("CO_NO"))
    county = DOR_COUNTY_CODES.get(co_no)
    pid = _text(a.get("PARCEL_ID"))
    if county is None or not pid:
        return None
    return (
        county, co_no, pid, normalize_key(pid), _int(a.get("ASMNT_YR")),
        _text(a.get("DOR_UC")), _num(a.get("JV")), _num(a.get("AV_NSD")),
        _num(a.get("TV_NSD")), _num(a.get("LND_VAL")), _num(a.get("LND_SQFOOT")),
        _int(a.get("NO_BULDNG")), _int(a.get("ACT_YR_BLT")) or None, _num(a.get("TOT_LVG_AR")),
        _sale_price(a.get("SALE_PRC1")), _int(a.get("SALE_YR1")) or None,
        _int(a.get("SALE_MO1")) or None, _text(a.get("QUAL_CD1")),
        _sale_price(a.get("SALE_PRC2")), _int(a.get("SALE_YR2")) or None,
        _int(a.get("SALE_MO2")) or None, _text(a.get("QUAL_CD2")),
        _text(a.get("PHY_ADDR1")), _text(a.get("PHY_CITY")), _zip(a.get("PHY_ZIPCD")),
        _int(a.get("OBJECTID")), now,
    )


def _post(params):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(QUERY_URL, data=params, timeout=TIMEOUT * 3)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(60, 3 * attempt))
    raise RuntimeError(f"statewide query failed after {MAX_RETRIES} attempts: {last}")


def max_object_id():
    data = _post({"where": "1=1", "outFields": "OBJECTID", "orderByFields": "OBJECTID DESC",
                  "resultRecordCount": 1, "returnGeometry": "false", "f": "json"})
    feats = data.get("features") or []
    if not feats:
        raise RuntimeError("statewide layer returned no features")
    return int(feats[0]["attributes"]["OBJECTID"])


def _worker(idx, lo, hi, out, errors, stop):
    """Keyset-paginate OBJECTIDs in (lo, hi]. resultOffset is unusable on a
    10.8M-row hosted layer (deep offsets time out) but an indexed
    OBJECTID > x ORDER BY OBJECTID walk is consistently fast."""
    last = lo
    try:
        while last < hi and not stop.is_set():
            data = _post({
                "where": f"OBJECTID > {last} AND OBJECTID <= {hi}",
                "orderByFields": "OBJECTID ASC",
                "outFields": ",".join(FIELDS),
                "returnGeometry": "false",
                "resultRecordCount": PAGE,
                "f": "json",
            })
            feats = data.get("features") or []
            if not feats:
                break
            out.put([f["attributes"] for f in feats])
            last = int(feats[-1]["attributes"]["OBJECTID"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"worker {idx} ({lo}, {hi}] stopped at {last}: {exc}")
        stop.set()
    finally:
        out.put(None)


def sync_statewide_values(conn):
    ensure_schema(conn)
    started = datetime.now(timezone.utc).isoformat()
    log("=== statewide DOR values sync started ===")
    t0 = time.time()
    total = 0
    written_by_county = {}
    errors = []
    try:
        top = max_object_id()
        log(f"  statewide: max OBJECTID {top:,}; {WORKERS} workers x pages of {PAGE}")
        span = top // WORKERS + 1
        ranges = [(i * span, min(top, (i + 1) * span)) for i in range(WORKERS)]
        out = queue.Queue(maxsize=WORKERS * 4)
        stop = threading.Event()
        threads = [threading.Thread(target=_worker, args=(i, lo, hi, out, errors, stop), daemon=True)
                   for i, (lo, hi) in enumerate(ranges)]
        for t in threads:
            t.start()

        done = 0
        pages = 0
        while done < len(threads):
            item = out.get()
            if item is None:
                done += 1
                continue
            rows = []
            for a in item:
                r = row_from_attrs(a, started)
                if r is not None:
                    rows.append(r)
                    written_by_county[r[0]] = written_by_county.get(r[0], 0) + 1
            conn.executemany(UPSERT, rows)
            total += len(rows)
            pages += 1
            if pages % COMMIT_EVERY == 0:
                conn.commit()
            if pages % 100 == 0:
                rate = total / max(1, time.time() - t0)
                log(f"  statewide: {total:,} rows written ({rate:,.0f} rows/s)")
        conn.commit()
        for t in threads:
            t.join()
        if errors:
            raise RuntimeError("; ".join(errors))

        # Full pass succeeded: parcels no longer in the roll are dropped.
        removed = conn.execute("DELETE FROM parcel_values WHERE last_synced_at < ?", (started,)).rowcount
        conn.commit()
        finished = datetime.now(timezone.utc).isoformat()
        for county, n in sorted(written_by_county.items()):
            conn.execute(
                "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
                "VALUES (?,?,?,?,?,?)", (county, "dor_values", started, finished, "success", n))
        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
            "VALUES (?,?,?,?,?,?)", ("statewide", "dor_values", started, finished, "success", total))
        conn.commit()
        log(f"[OK] statewide DOR values: {total:,} parcels across {len(written_by_county)} counties "
            f"in {(time.time() - t0) / 60:.1f} min; {removed} stale rows removed")
        return True
    except Exception as exc:  # noqa: BLE001
        conn.commit()  # keep whatever was written; it is all valid data
        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched, error) "
            "VALUES (?,?,?,?,?,?,?)",
            ("statewide", "dor_values", started, datetime.now(timezone.utc).isoformat(), "failed", total, str(exc)))
        conn.commit()
        log(f"[FAIL] statewide DOR values after {total:,} rows: {exc}")
        return False


def apply_values_to_features(conn, county=None):
    """Attach DOR values/sales to county parcel features, matched on the
    normalized parcel id. County-published land/total values and acreage win
    when the county layer provides them; DOR fills the rest (DOR land square
    footage beats geometry-derived acreage, which is the last resort)."""
    ensure_schema(conn)
    where_county = "AND features.county = ?" if county else ""
    params = [county] if county else []
    t0 = time.time()
    cur = conn.execute(f"""
        UPDATE features SET
            just_value = pv.just_value,
            assessed_value = pv.assessed_value,
            taxable_value = pv.taxable_value,
            sale_price = pv.sale_price,
            sale_date = CASE WHEN pv.sale_year IS NOT NULL
                             THEN printf('%04d-%02d', pv.sale_year, COALESCE(pv.sale_month, 1)) END,
            sale_qual = pv.sale_qual,
            dor_use_code = pv.dor_use_code,
            site_address = pv.site_address,
            city = COALESCE(features.city, pv.site_city),
            land_value = COALESCE(features.land_value, pv.land_value),
            total_value = COALESCE(features.total_value, pv.just_value),
            acreage = CASE
                WHEN features.acreage IS NOT NULL AND COALESCE(features.acreage_source, 'county') = 'county'
                    THEN features.acreage
                WHEN pv.land_sqft > 0 THEN round(pv.land_sqft / 43560.0, 4)
                ELSE features.acreage END,
            acreage_source = CASE
                WHEN features.acreage IS NOT NULL AND COALESCE(features.acreage_source, 'county') = 'county'
                    THEN COALESCE(features.acreage_source, 'county')
                WHEN pv.land_sqft > 0 THEN 'dor'
                ELSE features.acreage_source END
        FROM parcel_values AS pv
        WHERE features.dataset_type = 'parcels'
          AND pv.county = features.county
          AND pv.parcel_key = upper(replace(replace(replace(replace(features.feature_key, '-', ''), ' ', ''), '.', ''), '/', ''))
          {where_county}
        """, params)
    conn.commit()
    log(f"  DOR values joined onto {cur.rowcount:,} parcel features"
        f"{' for ' + county if county else ''} in {time.time() - t0:.0f}s")
    return cur.rowcount


def main():
    conn = get_conn()
    if "--join-only" not in sys.argv:
        sync_statewide_values(conn)
    apply_values_to_features(conn)
    conn.close()


if __name__ == "__main__":
    main()
