"""
Florida county GIS ETL pilot.

Pulls parcel / zoning / land-use data from verified public county ArcGIS
REST endpoints (see sources.json) into a local SQLite database.

Usage:
    python etl.py                # sync all configured sources
    python etl.py miami_dade     # sync only one county
"""
import json
import zlib
import re
import os
import math
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

BASE_DIR = Path(__file__).resolve().parent.parent
# The database lives on D: (C: is nearly full). Override with the FL_COUNTY_DB
# environment variable if it ever needs to move again.
DEFAULT_DB_PATH = Path("D:/fl-county-data/data/fl_county_data.db")
DB_PATH = Path(os.environ.get("FL_COUNTY_DB", DEFAULT_DB_PATH))
SOURCES_PATH = Path(__file__).resolve().parent / "sources.json"
LOG_DIR = BASE_DIR / "logs"

PAGE_SIZE = 1000
TIMEOUT = 60
MAX_RETRIES = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    county TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    feature_key TEXT NOT NULL,
    feature_key_norm TEXT,
    acreage REAL,
    acreage_source TEXT,
    city TEXT,
    land_use_code TEXT,
    land_use_desc TEXT,
    zoning_code TEXT,
    zoning_desc TEXT,
    land_value REAL,
    building_value REAL,
    total_value REAL,
    geometry_geojson TEXT,
    attributes_json TEXT,
    last_synced_at TEXT NOT NULL,
    UNIQUE(county, dataset_type, feature_key)
);
CREATE INDEX IF NOT EXISTS idx_features_norm ON features(county, dataset_type, feature_key_norm);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    county TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    rows_fetched INTEGER DEFAULT 0,
    error TEXT
);
"""


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "etl.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Covering indexes for the filter dropdowns (/api/facets: DISTINCT code+desc per
# dataset). Not in SCHEMA on purpose: creating them on the full table takes
# minutes and would hold the write lock against a running loader. The demo
# builder creates them; run ensure_facet_indexes() on the main DB when idle.
FACET_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_features_zoning ON features(dataset_type, zoning_code, zoning_desc)",
    "CREATE INDEX IF NOT EXISTS idx_features_landuse ON features(dataset_type, land_use_code, land_use_desc)",
    "CREATE INDEX IF NOT EXISTS idx_features_county_zoning ON features(county, dataset_type, zoning_code, zoning_desc)",
    "CREATE INDEX IF NOT EXISTS idx_features_county_landuse ON features(county, dataset_type, land_use_code, land_use_desc)",
)


def ensure_facet_indexes(conn):
    for sql in FACET_INDEXES:
        conn.execute(sql)
    conn.commit()


def normalize_key(pid):
    """Parcel ids differ cosmetically between county layers and the DOR roll
    (spaces, dashes, dots). Compare on a stripped, upper-cased form. Stored
    as features.feature_key_norm and parcel_values.parcel_key so the value
    join is an index lookup."""
    if pid is None:
        return ""
    return "".join(ch for ch in str(pid).upper() if ch.isalnum())


# Per-source key transforms (sources.json "key_transform"), applied before
# normalization so feature_key_norm lines up with the DOR roll's parcel_key
# while feature_key keeps the county's own spelling for display.
#   swap_sec_rng: county writes RR-TT-SS-..., DOR writes SS-TT-RR-... (Orange)
KEY_TRANSFORMS = {
    "swap_sec_rng": lambda k: (k[4:6] + k[2:4] + k[0:2] + k[6:]) if len(k) >= 6 else k,
}


def join_key(key, transform=None):
    """feature_key_norm for a county feature key."""
    k = normalize_key(key)
    if transform:
        k = KEY_TRANSFORMS[transform](k)
    return k


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # WAL lets the web UI keep reading while a sync is writing; without it
    # every commit briefly locks readers out ("database is locked" 500s).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.create_function("norm_key", 1, normalize_key, deterministic=True)
    have = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    if have:  # existing database: add columns before the schema's CREATE INDEX runs
        for col in ("acreage_source", "city", "feature_key_norm"):
            if col not in have:
                conn.execute(f"ALTER TABLE features ADD COLUMN {col} TEXT")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT 1 FROM features WHERE feature_key_norm IS NULL LIMIT 1").fetchone():
        t0 = time.time()
        n = conn.execute("UPDATE features SET feature_key_norm = norm_key(feature_key) "
                         "WHERE feature_key_norm IS NULL").rowcount
        log(f"  populated feature_key_norm on {n:,} rows in {time.time()-t0:.0f}s")
    conn.commit()
    return conn


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _query(url, params, verify=True):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, data=params, timeout=TIMEOUT, verify=verify)
            resp.raise_for_status()
            data = resp.json()
            # ArcGIS servers often return HTTP 200 with an "error" body on
            # transient failures (e.g. "Failed to execute query.") - treat
            # that the same as a network error and retry it too.
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    raise last_exc


def fetch_layer_info(layer_url, verify=True):
    data = _query(layer_url, {"f": "json"}, verify)
    id_field = data.get("objectIdField")
    if not id_field:
        oid_fields = [f["name"] for f in data.get("fields", []) if f.get("type") == "esriFieldTypeOID"]
        id_field = oid_fields[0] if oid_fields else "OBJECTID"
    return {"id_field": id_field, "max_records": data.get("maxRecordCount") or PAGE_SIZE}


def detect_format(query_url, verify=True, hint=None):
    """ArcGIS Server older than 10.4 rejects f=geojson outright, so probe once
    and fall back to Esri JSON (which every version supports) when needed."""
    if hint in ("json", "geojson"):
        return hint
    probe = {"where": "1=1", "outFields": "*", "f": "geojson",
             "resultRecordCount": 1, "returnGeometry": "false"}
    resp = requests.post(query_url, data=probe, timeout=TIMEOUT, verify=verify)
    if resp.status_code == 400 and "format not supported" in resp.text.lower():
        return "json"
    resp.raise_for_status()
    return "geojson"


def _signed_area(ring):
    area = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _esri_to_geojson_geometry(geom):
    if not geom:
        return None
    if "rings" in geom:
        rings = [[pt[:2] for pt in ring] for ring in geom["rings"]]
        # Esri: exterior rings are clockwise (negative signed area), holes are
        # counter-clockwise and follow their exterior ring.
        polygons, current = [], None
        for ring in rings:
            if current is None or _signed_area(ring) < 0:
                current = [ring]
                polygons.append(current)
            else:
                current.append(ring)
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}
    if "paths" in geom:
        paths = [[pt[:2] for pt in path] for path in geom["paths"]]
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}
        return {"type": "MultiLineString", "coordinates": paths}
    if "x" in geom and "y" in geom:
        return {"type": "Point", "coordinates": [geom["x"], geom["y"]]}
    return None


def _normalize_features(data, fmt):
    """Return GeoJSON-shaped features regardless of what the server sent.
    Some ArcGIS Enterprise servers (Putnam) accept f=geojson but answer with
    Esri JSON anyway, so decide per feature rather than trusting `fmt`."""
    out = []
    for f in data.get("features", []):
        if "properties" in f:
            out.append(f)
        else:
            out.append({"properties": f.get("attributes") or {},
                        "geometry": _esri_to_geojson_geometry(f.get("geometry"))})
    return out


def fetch_page(query_url, offset, page_size, fmt="geojson", verify=True, where="1=1"):
    params = {
        "where": where,
        "outFields": "*",
        "f": fmt,
        "resultOffset": offset,
        "resultRecordCount": page_size,
        "outSR": 4326,
        "returnGeometry": "true",
    }
    return _normalize_features(_query(query_url, params, verify), fmt)


def fetch_object_ids(query_url, verify=True, where="1=1"):
    params = {"where": where, "returnIdsOnly": "true", "f": "json"}
    data = _query(query_url, params, verify)
    id_field = data.get("objectIdFieldName")
    ids = sorted(data.get("objectIds") or [])
    return id_field, ids


def fetch_batch_by_ids(query_url, id_field, ids, fmt="geojson", verify=True):
    id_list = ",".join(str(i) for i in ids)
    params = {
        "where": f"{id_field} IN ({id_list})",
        "outFields": "*",
        "f": fmt,
        "outSR": 4326,
        "returnGeometry": "true",
    }
    return _normalize_features(_query(query_url, params, verify), fmt)


def fetch_sample(source, n=3):
    """Fetch a few records through exactly the code path a real sync uses."""
    layer_url = source["url"].rstrip("/")
    query_url = layer_url + "/query"
    verify = source.get("verify_ssl", True)
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    info = fetch_layer_info(layer_url, verify)
    fmt = detect_format(query_url, verify, source.get("format"))
    if source.get("no_offset_pagination"):
        id_field, ids = fetch_object_ids(query_url, verify)
        feats = fetch_batch_by_ids(query_url, id_field or info["id_field"], ids[:n], fmt, verify)
    else:
        feats = fetch_page(query_url, 0, n, fmt, verify)
    return {"info": info, "format": fmt, "features": feats}


EARTH_RADIUS_M = 6371008.8  # authalic mean radius: equal-area sphere for WGS84
SQM_PER_ACRE = 4046.8564224


def _ring_area_sqm(ring):
    """Spherical area of a lon/lat ring (Chamberlain & Duquette, as used by
    Turf.js). Accurate to well under 0.5% for parcel- and district-sized
    polygons, which is plenty for acreage."""
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0)


def geodesic_acres(geom):
    """Acreage computed from WGS84 GeoJSON geometry (Polygon/MultiPolygon):
    exterior rings minus holes. Returns None for non-areal geometry."""
    if not geom:
        return None
    gtype = geom.get("type")
    if gtype == "Polygon":
        polys = [geom.get("coordinates") or []]
    elif gtype == "MultiPolygon":
        polys = geom.get("coordinates") or []
    else:
        return None
    sqm = 0.0
    for rings in polys:
        for i, ring in enumerate(rings):
            a = _ring_area_sqm(ring)
            sqm += a if i == 0 else -a
    return round(max(sqm, 0.0) / SQM_PER_ACRE, 4) if sqm > 0 else None


def backfill_acreage(conn, county=None):
    """Fill acreage from stored geometry wherever the source layer had no
    acreage field (zoning / land-use districts, attribute-stripped parcels)."""
    where = "WHERE acreage IS NULL AND geometry_geojson IS NOT NULL"
    params = []
    if county:
        where += " AND county = ?"
        params.append(county)
    rows = conn.execute(f"SELECT id, geometry_geojson FROM features {where}", params).fetchall()
    updates = []
    for fid, gj in rows:
        try:
            acres = geodesic_acres(json.loads(decode_json(gj)))
        except (TypeError, ValueError):
            acres = None
        if acres is not None:
            updates.append((acres, fid))
    conn.executemany("UPDATE features SET acreage = ?, acreage_source = 'geometry' WHERE id = ?", updates)
    conn.commit()
    if rows:
        scope = f" in {county}" if county else ""
        log(f"  acreage computed from geometry for {len(updates):,} of {len(rows):,} features missing it{scope}")
    return len(updates)


def _clean_city(v):
    """County city fields are inconsistent (ALL CAPS, trailing spaces, codes
    like 'UNINCORPORATED'). Title-case them for display; keep None for blanks."""
    if v is None:
        return None
    t = str(v).strip()
    if not t or t.lower() in ("none", "null", "n/a"):
        return None
    if t.isupper() or t.islower():
        t = " ".join(w.capitalize() for w in t.split())
        # Keep common abbreviations readable
        for pre in ("St ", "Ft ", "Mt "):
            t = t.replace(pre, pre.strip() + ". ")
    return t


def backfill_city(conn, county=None):
    """Fill `city` from the raw attributes already stored, using each
    layer's field_map.city. Lets a mapping added later apply without a
    re-download, and lets rows written by an older ETL pick it up."""
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    total = 0
    for cty, datasets in sources.items():
        if county and cty != county:
            continue
        for dt, src in datasets.items():
            field = (src.get("field_map") or {}).get("city")
            if not field or src.get("type") == "manual":
                continue
            rows = conn.execute(
                "SELECT id, attributes_json FROM features WHERE county=? AND dataset_type=? AND city IS NULL",
                (cty, dt)).fetchall()
            updates = []
            for fid, aj in rows:
                try:
                    val = _clean_city((json.loads(decode_json(aj)) or {}).get(field))
                except (TypeError, ValueError):
                    val = None
                if val:
                    updates.append((val, fid))
            if updates:
                conn.executemany("UPDATE features SET city=? WHERE id=?", updates)
                conn.commit()
                total += len(updates)
    if total:
        log(f"  city filled from stored attributes on {total:,} features")
    return total


# Owner names and mailing addresses are never stored. Keys matching this
# pattern are dropped from every layer's raw attributes on ingest; a source can
# list extra layer-specific keys under "exclude_fields" in sources.json.
PRIVATE_FIELD_RE = re.compile(
    r"^(OWN|OWNER|MAIL|FIDU|TAXPAYER)"          # any spelling: OWNERNAME, OWNERADD1, OwnerCity, MAILADD, MAIL_ZIP ...
    r"|^[MO](NAME|ADDR\d?|ADD\d?|CITY|STATE|ZIP|ZIPCD|COUNTRY)$"  # DOR/FGDL/SWFWMD mailing shorthand (MCITY, OADDR1 ...)
    r"|^(CREATOR|EDITOR)_?NAME$|^(CASE_)?CONTACT$",  # staff/applicant names on planning layers
    re.I)
# Non-personal keys that happen to start with OWN: ownership class, not a person.
PRIVATE_FIELD_ALLOW = {"OWNTYPE", "OWN_TYPE", "OWNERTYPE", "OWNER_TYPE", "OWNERSHIP", "OWNERSHIP_TYPE"}


def is_private_field(key):
    k = key.upper()
    return k not in PRIVATE_FIELD_ALLOW and bool(PRIVATE_FIELD_RE.search(k))

# Metro counties that matter most for the public site: Tampa Bay and Orlando
# cores first, then the adjacent ring. The bulk loader takes these before the
# smallest-first sweep and the demo builder fills its budget with them first.
PRIORITY_COUNTIES = [
    "orange", "pinellas", "pasco", "polk", "osceola", "lake",          # Orlando + Tampa cores with county REST layers (fast)
    "hernando", "manatee", "sarasota", "volusia", "brevard", "citrus", "sumter",  # ring
    "hillsborough", "seminole",   # no county REST layer: statewide DOR cadastral by OBJECTID (hours each), so last
]


# --- JSON column codec ------------------------------------------------------
# geometry_geojson and attributes_json are stored zlib-compressed (level 6):
# ~2.3x on attributes, ~4.5x on rounded geometry, 0.07 ms/row. Values are
# self-describing: a zlib stream always starts with 0x78, JSON text never
# does, so plain-text rows written before this change decode unchanged.
def round_geometry(geom, places=6):
    """Round GeoJSON coordinates to `places` decimals (6 ~ 10 cm), drop any Z
    value and consecutive duplicate points. Servers hand out full doubles
    (and often a meaningless Z), which is 2-3x the storage for no map-scale
    difference."""
    if not geom:
        return geom

    def ring(coords):
        out, last = [], None
        for pt in coords:
            p = (round(pt[0], places), round(pt[1], places))
            if p != last:
                out.append([p[0], p[1]])
                last = p
        if len(out) > 1 and out[0] != out[-1]:
            out.append(out[0])
        return out

    t = geom.get("type")
    if t == "Polygon":
        geom["coordinates"] = [ring(r) for r in geom["coordinates"]]
    elif t == "MultiPolygon":
        geom["coordinates"] = [[ring(r) for r in poly] for poly in geom["coordinates"]]
    elif t == "Point":
        geom["coordinates"] = [round(geom["coordinates"][0], places), round(geom["coordinates"][1], places)]
    elif t in ("LineString", "MultiPoint"):
        geom["coordinates"] = [[round(x, places), round(y, places)] for x, y, *_ in geom["coordinates"]]
    elif t == "MultiLineString":
        geom["coordinates"] = [[[round(x, places), round(y, places)] for x, y, *_ in line] for line in geom["coordinates"]]
    return geom


def encode_json(value):
    """str/dict/list/None -> compressed bytes (or None)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8"), 6)


def decode_json(value):
    """Stored cell (bytes, str or None) -> JSON text (or None)."""
    if value is None or isinstance(value, str):
        return value
    if value[:1] == b"\x78":
        return zlib.decompress(value).decode("utf-8")
    return value.decode("utf-8")


def _public_props(props, exclude_fields=()):
    ex = {k.upper() for k in (exclude_fields or ())}
    return {k: v for k, v in props.items()
            if k.upper() not in ex and not is_private_field(k)}


def _write_features(conn, county, dataset_type, key_field, field_map, feats, exclude_fields=(), key_transform=None):
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for feat in feats:
        props = feat.get("properties", {}) or {}
        geom = feat.get("geometry")
        key = props.get(key_field)
        key = None if key is None else str(key).strip()
        if not key:
            continue  # unkeyed placeholder records (e.g. Putnam) cannot be upserted
        acreage = _num(props.get(field_map.get("acreage")))
        acreage_source = "county" if acreage is not None else None
        if acreage is None and geom:
            acreage = geodesic_acres(geom)
            acreage_source = "geometry" if acreage is not None else None
        rows.append((
            county, dataset_type, key, join_key(key, key_transform),
            acreage, acreage_source, _clean_city(props.get(field_map.get("city"))),
            props.get(field_map.get("land_use_code")),
            props.get(field_map.get("land_use_desc")),
            props.get(field_map.get("zoning_code")),
            props.get(field_map.get("zoning_desc")),
            _num(props.get(field_map.get("land_value"))),
            _num(props.get(field_map.get("building_value"))),
            _num(props.get(field_map.get("total_value"))),
            encode_json(round_geometry(geom)) if geom else None,
            encode_json(_public_props(props, exclude_fields)),
            now,
        ))
    conn.executemany(
        """
        INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, acreage, acreage_source, city,
            land_use_code, land_use_desc, zoning_code, zoning_desc,
            land_value, building_value, total_value, geometry_geojson,
            attributes_json, last_synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(county, dataset_type, feature_key) DO UPDATE SET
            feature_key_norm=excluded.feature_key_norm,
            acreage=excluded.acreage,
            acreage_source=excluded.acreage_source,
            city=COALESCE(excluded.city, features.city),
            land_use_code=excluded.land_use_code,
            land_use_desc=excluded.land_use_desc,
            zoning_code=excluded.zoning_code,
            zoning_desc=excluded.zoning_desc,
            land_value=excluded.land_value,
            building_value=excluded.building_value,
            total_value=excluded.total_value,
            geometry_geojson=excluded.geometry_geojson,
            attributes_json=excluded.attributes_json,
            last_synced_at=excluded.last_synced_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)

# Object-id batches are independent requests, so a few can be in flight at
# once. Results are yielded in submission order so writes stay deterministic.
# Four workers keeps us a polite client of public county servers.
FETCH_WORKERS = 4


def fetch_batches_parallel(batches, fetch_fn, workers=FETCH_WORKERS):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        window = []
        for batch in batches:
            window.append(pool.submit(fetch_fn, batch))
            if len(window) >= workers * 2:
                yield window.pop(0).result()
        for fut in window:
            yield fut.result()


def sync_statewide_geometry(conn, county, dataset_type, source):
    """Parcel boundaries for counties with no county-hosted parcel layer.
    Pulls geometry for exactly the parcels already in parcel_values (the
    statewide DOR roll, same layer) by OBJECTID in batches - CO_NO filters on
    that layer time out, OBJECTID IN (...) lookups do not."""
    query_url = source["url"].rstrip("/") + "/query"
    key_field = source["key_field"]
    field_map = source.get("field_map", {})
    exclude_fields = source.get("exclude_fields") or []
    batch_size = int(source.get("batch_size", 200))
    started = datetime.now(timezone.utc).isoformat()
    total = 0
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT source_objectid FROM parcel_values WHERE county=? AND source_objectid IS NOT NULL "
            "ORDER BY source_objectid", (county,))]
        if not ids:
            raise RuntimeError("no parcel_values rows for this county - run dor_values.py first")
        fmt = detect_format(query_url, True, source.get("format"))
        log(f"  {county}/{dataset_type}: statewide geometry for {len(ids):,} parcels by OBJECTID (batch {batch_size})")
        skipped = []

        def fetch_bisect(batch):
            try:
                return fetch_batch_by_ids(query_url, "OBJECTID", batch, fmt, True)
            except Exception as exc:  # noqa: BLE001
                if len(batch) == 1:
                    skipped.append(batch[0])
                    log(f"  {county}/{dataset_type}: skipping OBJECTID={batch[0]} ({exc})")
                    return []
                mid = len(batch) // 2
                return fetch_bisect(batch[:mid]) + fetch_bisect(batch[mid:])

        batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
        for n, feats in enumerate(fetch_batches_parallel(batches, fetch_bisect)):
            total += _write_features(conn, county, dataset_type, key_field, field_map, feats, exclude_fields)
            if n % 10 == 9 or n + 1 == len(batches):
                log(f"  {county}/{dataset_type}: fetched {total:,}/{len(ids):,} rows so far (statewide batch, {FETCH_WORKERS} workers)")
        if skipped:
            log(f"  {county}/{dataset_type}: WARNING skipped {len(skipped)} features: {skipped[:20]}")
        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
            "VALUES (?,?,?,?,?,?)",
            (county, dataset_type, started, datetime.now(timezone.utc).isoformat(), "success", total),
        )
        conn.commit()
        log(f"[OK] {county}/{dataset_type}: {total} rows synced")
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (county, dataset_type, started, datetime.now(timezone.utc).isoformat(), "failed", total, str(exc)),
        )
        conn.commit()
        log(f"[FAIL] {county}/{dataset_type}: {exc}")


def sync_source(conn, county, dataset_type, source):
    if source.get("type") == "manual":
        log(f"[SKIP] {county}/{dataset_type}: manual source - {source.get('note')}")
        return
    if source.get("type") == "statewide_geometry":
        return sync_statewide_geometry(conn, county, dataset_type, source)
    where = source.get("where") or "1=1"

    layer_url = source["url"].rstrip("/")
    query_url = layer_url + "/query"
    key_field = source["key_field"]
    field_map = source.get("field_map", {})
    exclude_fields = source.get("exclude_fields") or []
    verify = source.get("verify_ssl", True)
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    started = datetime.now(timezone.utc).isoformat()
    total = 0
    try:
        info = fetch_layer_info(layer_url, verify)
        id_field = info["id_field"]
        fmt = detect_format(query_url, verify, source.get("format"))
        # Servers silently cap each response at maxRecordCount, so asking for
        # more than that just wastes the round trip.
        page_size = max(1, min(PAGE_SIZE, int(info["max_records"])))
        log(f"  {county}/{dataset_type}: id_field={id_field} format={fmt} page_size={page_size}")
        seen_ids = set()

        def write(feats):
            for feat in feats:
                oid = (feat.get("properties") or {}).get(id_field)
                if oid is not None:
                    seen_ids.add(oid)
            return _write_features(conn, county, dataset_type, key_field, field_map, feats, exclude_fields,
                                   source.get("key_transform"))

        use_id_batches = bool(source.get("no_offset_pagination"))
        if not use_id_batches and page_size < 1000:
            # resultOffset paging on small-page servers slows down with depth
            # (Orange: 1.5 s/page at offset 0, 8 s/page past 80k rows). Object-id
            # batches cost the same at any depth, so prefer them whenever the
            # server hands out ids; keep offset paging as the fallback.
            try:
                probe_field, probe_ids = fetch_object_ids(query_url, verify, where)
                if probe_ids:
                    use_id_batches = True
                    log(f"  {county}/{dataset_type}: page_size {page_size} < 1000, using object-id batches "
                        f"({len(probe_ids):,} ids)")
            except Exception as exc:  # noqa: BLE001
                log(f"  {county}/{dataset_type}: returnIdsOnly failed ({exc}); using offset paging")
        if not use_id_batches:
            offset = 0
            try:
                while True:
                    feats = fetch_page(query_url, offset, page_size, fmt, verify, where)
                    if not feats:
                        break
                    total += write(feats)
                    log(f"  {county}/{dataset_type}: fetched {total} rows so far (offset {offset})")
                    offset += len(feats)
            except Exception as exc:  # noqa: BLE001
                # Some servers (Orange County, for one) reliably fail deep
                # offsets. Fall back to fetching the remainder by object ID,
                # which never depends on offset support.
                log(f"  {county}/{dataset_type}: offset paging failed at offset {offset} "
                    f"({exc}); switching to object-id batches for the remainder")
                use_id_batches = True

        if use_id_batches:
            reported_field, ids = fetch_object_ids(query_url, verify, where)
            if reported_field and reported_field != id_field:
                id_field, seen_ids = reported_field, set()
            remaining = [i for i in ids if i not in seen_ids]
            skipped = []

            def fetch_ids_bisect(batch):
                # Some servers (Orange County) fail a whole batch when it
                # contains one feature they cannot serialize. Split the batch
                # until the bad ids are isolated, then skip just those.
                try:
                    return fetch_batch_by_ids(query_url, id_field, batch, fmt, verify)
                except Exception as exc:  # noqa: BLE001
                    if len(batch) == 1:
                        skipped.append(batch[0])
                        log(f"  {county}/{dataset_type}: skipping {id_field}={batch[0]} ({exc})")
                        return []
                    mid = len(batch) // 2
                    return fetch_ids_bisect(batch[:mid]) + fetch_ids_bisect(batch[mid:])

            batches = [remaining[i:i + page_size] for i in range(0, len(remaining), page_size)]
            for n, feats in enumerate(fetch_batches_parallel(batches, fetch_ids_bisect)):
                total += write(feats)
                if n % 10 == 9 or n + 1 == len(batches):
                    log(f"  {county}/{dataset_type}: fetched {total:,}/{len(ids):,} rows so far (id batch, {FETCH_WORKERS} workers)")
            if skipped:
                log(f"  {county}/{dataset_type}: WARNING skipped {len(skipped)} features the server could not return: {skipped[:20]}")

        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched) "
            "VALUES (?,?,?,?,?,?)",
            (county, dataset_type, started, datetime.now(timezone.utc).isoformat(), "success", total),
        )
        conn.commit()
        log(f"[OK] {county}/{dataset_type}: {total} rows synced")
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "INSERT INTO sync_log (county, dataset_type, started_at, finished_at, status, rows_fetched, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (county, dataset_type, started, datetime.now(timezone.utc).isoformat(), "failed", total, str(exc)),
        )
        conn.commit()
        log(f"[FAIL] {county}/{dataset_type}: {exc}")

# --- run lock ---------------------------------------------------------------
# One writer at a time: the scheduled daily refresh (etl.py) and the initial
# bulk loader (load_all.py) both upsert into the same SQLite file, and running
# them together only produces lock contention. The lock is a small JSON file
# next to the database holding the owner's pid; a stale lock (dead pid) is
# ignored.
LOCK_PATH = DB_PATH.parent / "etl.lock"


def _pid_alive(pid):
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def active_run():
    """Return {'pid', 'name', 'started'} for a live run holding the lock, else None."""
    try:
        info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if _pid_alive(info.get("pid", -1)) else None


def acquire_run_lock(name):
    """Take the lock for this process, or return the conflicting run's info."""
    other = active_run()
    if other and other.get("pid") != os.getpid():
        return other
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "name": name,
                                     "started": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    return None


def release_run_lock():
    try:
        if json.loads(LOCK_PATH.read_text(encoding="utf-8")).get("pid") == os.getpid():
            LOCK_PATH.unlink()
    except (OSError, ValueError):
        pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    only_county = args[0] if args else None
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    other = acquire_run_lock("etl daily refresh")
    if other:
        log(f"[SKIP] ETL run: '{other.get('name')}' (pid {other.get('pid')}, started {other.get('started')}) "
            "is still writing to the database; try again after it finishes")
        return
    conn = get_conn()
    log(f"=== ETL run started (filter={only_county or 'ALL'}) ===")
    for county, datasets in sources.items():
        if only_county and county.lower() != only_county.lower():
            continue
        for dataset_type, source in datasets.items():
            sync_source(conn, county, dataset_type, source)

    # Statewide DOR values (all 67 counties, one layer). Skipped when a single
    # county was requested or --no-values is passed; the join runs regardless
    # so freshly synced parcels pick up values already in the database.
    import dor_values
    if not only_county and "--no-values" not in flags:
        dor_values.sync_statewide_values(conn)
    backfill_acreage(conn, only_county)
    backfill_city(conn, only_county)
    dor_values.apply_values_to_features(conn, only_county)
    conn.close()
    release_run_lock()
    log("=== ETL run finished ===")


if __name__ == "__main__":
    main()
