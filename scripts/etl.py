"""
Florida county GIS ETL pilot.

Pulls parcel / zoning / land-use data from verified public county ArcGIS
REST endpoints (see sources.json) into a local SQLite database.

Usage:
    python etl.py                # sync all configured sources
    python etl.py miami_dade     # sync only one county
"""
import json
import re
import os
import math
import sqlite3
import sys
import time
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


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    # WAL lets the web UI keep reading while a sync is writing; without it
    # every commit briefly locks readers out ("database is locked" 500s).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    for col in ("acreage_source", "city"):
        if col not in have:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} TEXT")
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
            acres = geodesic_acres(json.loads(gj))
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
                    val = _clean_city((json.loads(aj) or {}).get(field))
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
    r"^(OWN|OWNER|OWNERS|OWNNAME|OWN_NAME|MAIL|MAILTO|MAILING|FIDU|TAXPAYER)(_|\d|$)"
    r"|^O(NAME|ADDR\d?|CITY|STATE|ZIP|ZIPCD)$", re.I)


def _public_props(props, exclude_fields=()):
    ex = {k.upper() for k in (exclude_fields or ())}
    return {k: v for k, v in props.items()
            if k.upper() not in ex and not PRIVATE_FIELD_RE.search(k)}


def _write_features(conn, county, dataset_type, key_field, field_map, feats, exclude_fields=()):
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
            county, dataset_type, key,
            acreage, acreage_source, _clean_city(props.get(field_map.get("city"))),
            props.get(field_map.get("land_use_code")),
            props.get(field_map.get("land_use_desc")),
            props.get(field_map.get("zoning_code")),
            props.get(field_map.get("zoning_desc")),
            _num(props.get(field_map.get("land_value"))),
            _num(props.get(field_map.get("building_value"))),
            _num(props.get(field_map.get("total_value"))),
            json.dumps(geom) if geom else None,
            json.dumps(_public_props(props, exclude_fields)),
            now,
        ))
    conn.executemany(
        """
        INSERT INTO features (county, dataset_type, feature_key, acreage, acreage_source, city,
            land_use_code, land_use_desc, zoning_code, zoning_desc,
            land_value, building_value, total_value, geometry_geojson,
            attributes_json, last_synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(county, dataset_type, feature_key) DO UPDATE SET
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

        for n, i in enumerate(range(0, len(ids), batch_size)):
            total += _write_features(conn, county, dataset_type, key_field, field_map,
                                     fetch_bisect(ids[i:i + batch_size]), exclude_fields)
            if n % 10 == 9 or i + batch_size >= len(ids):
                log(f"  {county}/{dataset_type}: fetched {total:,}/{len(ids):,} rows so far (statewide batch)")
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
            return _write_features(conn, county, dataset_type, key_field, field_map, feats, exclude_fields)

        use_id_batches = bool(source.get("no_offset_pagination"))
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

            for i in range(0, len(remaining), page_size):
                batch = remaining[i:i + page_size]
                total += write(fetch_ids_bisect(batch))
                log(f"  {county}/{dataset_type}: fetched {total}/{len(ids)} rows so far (id batch)")
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


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    only_county = args[0] if args else None
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
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
    log("=== ETL run finished ===")


if __name__ == "__main__":
    main()
