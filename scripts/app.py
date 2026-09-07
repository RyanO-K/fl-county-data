"""
Local read-only web UI for the FL county data pipeline.

Serves a small JSON API plus a single-page HTML/JS frontend on top of the
SQLite database produced by etl.py. This process never writes to the
database and only ever opens short-lived, read-only connections, because
etl.py (run manually or via the daily Task Scheduler job) writes to the
same file concurrently.

Usage:
    .venv\\Scripts\\python.exe scripts\\app.py

Then open http://127.0.0.1:5000/ in a browser.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent.parent
import etl  # noqa: E402  (same folder)
DB_PATH = etl.DB_PATH
SOURCES_PATH = BASE_DIR / "scripts" / "sources.json"
DOR_LAYER_URL = ("https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
                 "Florida_Statewide_Cadastral/FeatureServer/0")

app = Flask(__name__)

# Columns returned for table rows. geometry_geojson and attributes_json are
# large and only fetched when explicitly requested (map view / detail).
LIGHT_COLUMNS = [
    "id", "county", "dataset_type", "feature_key", "acreage",
    "land_use_code", "land_use_desc", "zoning_code", "zoning_desc",
    "land_value", "building_value", "total_value", "last_synced_at",
    "just_value", "sale_price", "sale_date", "sale_qual", "dor_use_code",
    "site_address", "acreage_source", "city",
]

MAX_PER_PAGE = 500
MAX_MAP_FEATURES = 2000

# Columns returned for parcel_values table rows/list view.
VALUES_COLUMNS = [
    "county", "co_no", "parcel_id", "parcel_key", "assessment_year",
    "dor_use_code", "just_value", "assessed_value", "taxable_value",
    "land_value", "land_sqft", "building_count", "year_built", "living_area",
    "sale_price", "sale_year", "sale_month", "sale_qual",
    "sale2_price", "sale2_year", "sale2_month", "sale2_qual",
    "site_address", "site_city", "site_zip", "last_synced_at",
]

# Allowed sort keys for /api/values -> actual ORDER BY expression.
VALUES_SORT_COLUMNS = {
    "just_value": "just_value",
    "sale_price": "sale_price",
    "sale_year": "sale_year, sale_month",
    "land_value": "land_value",
    "parcel_id": "parcel_id",
}


def get_db():
    """Open a fresh, short-lived, read-only connection for this request.

    Using mode=ro (rather than a long-lived read/write handle) means this
    process never takes a lock that could block etl.py's writes, and never
    caches a stale schema/file handle across an ETL run that replaces the
    file's contents.
    """
    if "db" not in g:
        uri = f"file:{DB_PATH.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def build_filters(args):
    """Translate query-string filters into a WHERE clause + params list."""
    clauses = []
    params = []

    def add(col, value, op="=", cast=None):
        if value is None or value == "":
            return
        if cast:
            try:
                value = cast(value)
            except (TypeError, ValueError):
                return
        clauses.append(f"{col} {op} ?")
        params.append(value)

    add("county", args.get("county"))
    add("dataset_type", args.get("dataset_type"))
    add("zoning_code", args.get("zoning_code"))
    add("land_use_code", args.get("land_use_code"))
    add("acreage", args.get("min_acreage"), ">=", float)
    add("acreage", args.get("max_acreage"), "<=", float)
    add("total_value", args.get("min_value"), ">=", float)
    add("total_value", args.get("max_value"), "<=", float)

    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(land_use_desc LIKE ? OR zoning_desc LIKE ? OR zoning_code LIKE ? "
            "OR land_use_code LIKE ? OR feature_key LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def build_values_filters(args):
    """Translate query-string filters into a WHERE clause + params list for
    the parcel_values table. County is added first so the query can use the
    (county, ...) composite indexes."""
    clauses = []
    params = []

    def add(col, value, op="=", cast=None):
        if value is None or value == "":
            return
        if cast:
            try:
                value = cast(value)
            except (TypeError, ValueError):
                return
        clauses.append(f"{col} {op} ?")
        params.append(value)

    add("county", args.get("county"))
    add("dor_use_code", args.get("use_code"))
    add("just_value", args.get("min_jv"), ">=", float)
    add("just_value", args.get("max_jv"), "<=", float)
    add("sale_price", args.get("min_sale"), ">=", float)
    add("sale_price", args.get("max_sale"), "<=", float)

    q = (args.get("q") or "").strip()
    if q:
        clauses.append("(parcel_id LIKE ? OR site_address LIKE ?)")
        params.extend([f"{q}%", f"%{q}%"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/")
def index():
    demo = None
    if os.environ.get("DEMO_MODE") == "1":
        demo = {"counties": []}
        try:
            conn = get_db()
            row = conn.execute("SELECT value FROM demo_info WHERE key='counties'").fetchone()
            if row:
                demo["counties"] = json.loads(row[0])
        except sqlite3.Error:
            pass
    return render_template("index.html", demo=demo)


@app.route("/api/sources")
def api_sources():
    """Provenance for the UI: which public layer (and which field) each
    county/dataset comes from, so a number can be cited on hover."""
    import json
    try:
        sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sources = {}
    out = {}
    for county, datasets in sources.items():
        out[county] = {}
        for dataset_type, src in datasets.items():
            if src.get("type") == "manual":
                out[county][dataset_type] = {"manual": True, "note": src.get("note")}
                continue
            fm = src.get("field_map", {}) or {}
            out[county][dataset_type] = {
                "url": src.get("url"),
                "key_field": src.get("key_field"),
                "acreage_field": fm.get("acreage"),
                "fields": fm,
            }
    return jsonify({"counties": out, "dor_values": {"url": DOR_LAYER_URL,
                    "name": "Florida Statewide Cadastral (FDOR tax roll), State Geographic Information Office"}})


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/api/counties")
def api_counties():
    """Distinct (county, dataset_type) combinations currently in the table,
    with row counts, so the frontend can build filter dropdowns generically
    without any hardcoded list of counties/datasets."""
    conn = get_db()
    rows = conn.execute(
        "SELECT county, dataset_type, COUNT(*) AS row_count "
        "FROM features GROUP BY county, dataset_type ORDER BY county, dataset_type"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


FACETS_TTL = 600  # seconds; the dropdown value sets change only when the ETL runs
_facets_cache = {}
_facets_lock = threading.Lock()


def compute_facets(conn, args):
    """Distinct zoning/land-use codes for a filter selection."""
    base_where, base_params = build_filters(args)

    def nonempty_clause(col):
        extra = f"{col} IS NOT NULL AND {col} != ''"
        return (base_where + " AND " + extra) if base_where else ("WHERE " + extra)

    zoning_rows = conn.execute(
        f"SELECT DISTINCT zoning_code, zoning_desc FROM features "
        f"{nonempty_clause('zoning_code')} ORDER BY zoning_code LIMIT 500",
        base_params,
    ).fetchall()
    land_use_rows = conn.execute(
        f"SELECT DISTINCT land_use_code, land_use_desc FROM features "
        f"{nonempty_clause('land_use_code')} ORDER BY land_use_code LIMIT 500",
        base_params,
    ).fetchall()
    return {
        "zoning_codes": [dict(r) for r in zoning_rows],
        "land_use_codes": [dict(r) for r in land_use_rows],
    }


def cached_facets(conn, args):
    key = tuple(sorted((k, v) for k, v in args.items() if v not in (None, "")))
    now = time.time()
    with _facets_lock:
        hit = _facets_cache.get(key)
        if hit and now - hit[0] < FACETS_TTL:
            return hit[1]
    data = compute_facets(conn, args)
    with _facets_lock:
        _facets_cache[key] = (now, data)
    return data


def warm_facets():
    """The 'all counties' facets scan the whole features table (50 s on a
    small host), so compute them once in the background at startup."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        for dt in ("", "parcels", "zoning", "land_use", "future_land_use"):
            args = {"dataset_type": dt} if dt else {}
            cached_facets(conn, args)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"facets warm-up failed: {exc}")


threading.Thread(target=warm_facets, name="facets-warmup", daemon=True).start()


@app.route("/api/facets")
def api_facets():
    """Distinct zoning/land-use codes for the current county+dataset_type
    selection, to populate filter dropdowns with real values. Cached per
    filter combination for FACETS_TTL seconds."""
    return jsonify(cached_facets(get_db(), request.args))


@app.route("/api/features")
def api_features():
    conn = get_db()
    where, params = build_filters(request.args)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except ValueError:
        per_page = 50
    per_page = max(1, min(per_page, MAX_PER_PAGE))

    include_geometry = request.args.get("geometry") == "1"

    total = conn.execute(
        f"SELECT COUNT(*) FROM features {where}", params
    ).fetchone()[0]

    if include_geometry:
        # Map view: cap the number of features regardless of requested
        # per_page/page so we never try to render hundreds of thousands of
        # polygons in the browser at once.
        limit = min(per_page, MAX_MAP_FEATURES)
        cols = ", ".join(LIGHT_COLUMNS + ["geometry_geojson"])
        offset = 0
    else:
        limit = per_page
        offset = (page - 1) * per_page
        cols = ", ".join(LIGHT_COLUMNS)

    rows = conn.execute(
        f"SELECT {cols} FROM features {where} "
        f"ORDER BY id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return jsonify({
        "rows": [row_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })



JSON_COLUMNS = ("geometry_geojson", "attributes_json")


def row_dict(row):
    """sqlite3.Row -> dict with the compressed JSON columns decoded to text,
    which is what the front end has always received."""
    d = dict(row)
    for k in JSON_COLUMNS:
        if k in d:
            d[k] = etl.decode_json(d[k])
    return d


MAX_GEOMETRY_CHUNK = 5000


@app.route("/api/features/geometry")
def api_features_geometry():
    """Uncapped map feed: the client walks the full result set in chunks using
    keyset paging on id (fast at any depth, unlike OFFSET). `total` is only
    computed on the first chunk (after_id=0)."""
    conn = get_db()
    where, params = build_filters(request.args)
    try:
        after_id = max(0, int(request.args.get("after_id", 0)))
    except ValueError:
        after_id = 0
    try:
        limit = int(request.args.get("limit", 2000))
    except ValueError:
        limit = 2000
    limit = max(1, min(limit, MAX_GEOMETRY_CHUNK))

    total = None
    if after_id == 0:
        total = conn.execute(f"SELECT COUNT(*) FROM features {where}", params).fetchone()[0]

    clause = f"{where} AND id > ?" if where else "WHERE id > ?"
    cols = ", ".join(LIGHT_COLUMNS + ["geometry_geojson"])
    rows = conn.execute(
        f"SELECT {cols} FROM features {clause} ORDER BY id LIMIT ?",
        params + [after_id, limit],
    ).fetchall()
    rows = [row_dict(r) for r in rows]
    return jsonify({
        "rows": rows,
        "total": total,
        "next_after": rows[-1]["id"] if rows else after_id,
        "has_more": len(rows) == limit,
    })


@app.route("/api/feature/<int:feature_id>")
def api_feature_detail(feature_id):
    """Full record including raw attributes_json, for a detail popup."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM features WHERE id = ?", (feature_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_dict(row))


@app.route("/api/values")
def api_values():
    """Paginated statewide FL DOR tax-roll parcel values."""
    conn = get_db()
    where, params = build_values_filters(request.args)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except ValueError:
        per_page = 50
    per_page = max(1, min(per_page, MAX_PER_PAGE))

    sort = request.args.get("sort", "just_value")
    sort_col = VALUES_SORT_COLUMNS.get(sort, VALUES_SORT_COLUMNS["just_value"])
    direction = "ASC" if (request.args.get("dir", "desc").lower() == "asc") else "DESC"

    total = conn.execute(
        f"SELECT COUNT(*) FROM parcel_values {where}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    cols = ", ".join(VALUES_COLUMNS)
    rows = conn.execute(
        f"SELECT {cols} FROM parcel_values {where} "
        f"ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return jsonify({
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })


@app.route("/api/values/counties")
def api_values_counties():
    """Distinct counties currently present in parcel_values, with row
    counts, for the Parcel Values tab's county dropdown."""
    conn = get_db()
    rows = conn.execute(
        "SELECT county, COUNT(*) AS row_count FROM parcel_values "
        "GROUP BY county ORDER BY county"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/values/use_codes")
def api_values_use_codes():
    """Distinct DOR use codes (optionally scoped to one county), with row
    counts, for the Parcel Values tab's use-code dropdown."""
    conn = get_db()
    county = (request.args.get("county") or "").strip()
    where = "WHERE dor_use_code IS NOT NULL AND dor_use_code != ''"
    params = []
    if county:
        where += " AND county = ?"
        params.append(county)
    rows = conn.execute(
        f"SELECT dor_use_code, COUNT(*) AS row_count FROM parcel_values {where} "
        f"GROUP BY dor_use_code ORDER BY row_count DESC LIMIT 300",
        params,
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/value/<county>/<parcel_id>")
def api_value_detail(county, parcel_id):
    """Full parcel_values row for one (county, parcel_id), for a detail popup."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM parcel_values WHERE county = ? AND parcel_id = ?",
        (county, parcel_id),
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    out = dict(row)
    # Boundary for the mini map: the county's parcel feature, if that layer
    # has been loaded. Exact key match uses the unique index; the normalized
    # comparison is the fallback for counties whose ids differ cosmetically.
    geom = conn.execute(
        "SELECT id, geometry_geojson FROM features "
        "WHERE county = ? AND dataset_type = 'parcels' AND feature_key = ? LIMIT 1",
        (county, parcel_id),
    ).fetchone()
    if geom is None:
        try:
            geom = conn.execute(
                "SELECT id, geometry_geojson FROM features "
                "WHERE county = ? AND dataset_type = 'parcels' AND feature_key_norm = ? LIMIT 1",
                (county, row["parcel_key"]),
            ).fetchone()
        except sqlite3.OperationalError:
            geom = None  # database built before feature_key_norm existed: exact match only
    out["feature_id"] = geom["id"] if geom else None
    out["geometry_geojson"] = etl.decode_json(geom["geometry_geojson"]) if geom else None
    return jsonify(out)


@app.route("/api/status")
def api_status():
    """Pipeline health: latest sync_log entry per (county, dataset_type),
    plus the current live row count in `features` for that combination."""
    conn = get_db()

    latest_runs = conn.execute(
        """
        SELECT sl.county, sl.dataset_type, sl.started_at, sl.finished_at,
               sl.status, sl.rows_fetched, sl.error
        FROM sync_log sl
        JOIN (
            SELECT county, dataset_type, MAX(id) AS max_id
            FROM sync_log
            GROUP BY county, dataset_type
        ) latest
        ON sl.id = latest.max_id
        ORDER BY sl.county, sl.dataset_type
        """
    ).fetchall()

    counts = conn.execute(
        "SELECT county, dataset_type, COUNT(*) AS row_count "
        "FROM features GROUP BY county, dataset_type"
    ).fetchall()
    count_map = {(r["county"], r["dataset_type"]): r["row_count"] for r in counts}

    value_counts = conn.execute(
        "SELECT county, COUNT(*) AS row_count FROM parcel_values GROUP BY county"
    ).fetchall()
    value_count_map = {r["county"]: r["row_count"] for r in value_counts}

    sources = []
    seen = set()
    for r in latest_runs:
        key = (r["county"], r["dataset_type"])
        seen.add(key)
        if r["dataset_type"] == "dor_values":
            current_row_count = value_count_map.get(r["county"], 0)
        else:
            current_row_count = count_map.get(key, 0)
        sources.append({
            "county": r["county"],
            "dataset_type": r["dataset_type"],
            "status": r["status"],
            "rows_fetched": r["rows_fetched"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "error": r["error"],
            "current_row_count": current_row_count,
        })

    # Include any county/dataset present in features but with no sync_log
    # row yet (shouldn't normally happen, but keep it generic/defensive).
    for key, row_count in count_map.items():
        if key not in seen:
            sources.append({
                "county": key[0],
                "dataset_type": key[1],
                "status": "unknown",
                "rows_fetched": None,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "current_row_count": row_count,
            })

    total_rows = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    last_success = conn.execute(
        "SELECT MAX(finished_at) FROM sync_log WHERE status = 'success'"
    ).fetchone()[0]
    failing = sum(1 for s in sources if s["status"] == "failed")

    return jsonify({
        "sources": sources,
        "summary": {
            "total_rows": total_rows,
            "county_count": len(set(s["county"] for s in sources)),
            "last_success_at": last_success,
            "failing_count": failing,
        },
    })


# ---------------------------------------------------------------------------
# Pipeline status, one row per county
#
# /api/status (above) is kept as-is for backward compatibility; the Pipeline
# Status page uses /api/status/counties, which pivots the same sync_log data
# into one row per configured county with a cell per dataset.
# ---------------------------------------------------------------------------

# Per-county dataset columns, in display order. Matches load_all.NON_PARCEL_TYPES
# plus parcels; DOR values are handled separately (a single statewide sync).
STATUS_DATASETS = ("parcels", "zoning", "land_use", "future_land_use")

STATUS_TTL = 60      # s; the payload costs ~1.3 s to build (two GROUP BYs)
JOIN_TTL = 600       # s; the join-rate scan costs ~9 s (see _compute_join_rates)

_status_lock = threading.Lock()
_status_cache = {"data": None, "at": 0.0}
_join_lock = threading.Lock()
_join_cache = {"data": None, "at": 0.0, "running": False}


def read_sources():
    try:
        return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _ro_conn():
    """A standalone read-only connection (for work off the request thread)."""
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=60)


def _compute_join_rates():
    """{county: {"total": n, "joined": n}} over dataset_type='parcels'.

    How many parcel features carry a DOR value. Unlike the plain per-county
    counts (which ride the covering index idx_features_norm in ~0.4 s), this
    has to touch total_value, so it is a full scan of ~2.2M parcel rows that
    each carry compressed geometry: ~9 s. Far too slow to run per request, so
    it runs on a background thread and the page fills the number in on a later
    poll.
    """
    conn = _ro_conn()
    try:
        rows = conn.execute(
            "SELECT county, COUNT(*) AS total, "
            "SUM(CASE WHEN total_value IS NOT NULL THEN 1 ELSE 0 END) AS joined "
            "FROM features WHERE dataset_type = 'parcels' GROUP BY county"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: {"total": r[1], "joined": r[2] or 0} for r in rows}


def _refresh_join_rates():
    data = None
    try:
        data = _compute_join_rates()
    except sqlite3.Error:
        pass  # keep the previous numbers; try again after the TTL
    with _join_lock:
        if data is not None:
            _join_cache["data"] = data
        _join_cache["at"] = time.time()
        _join_cache["running"] = False


def join_rates():
    """Cached join rates, kicking off a background refresh when stale.

    Returns (map_or_None, pending) - pending is True only before the very
    first scan finishes, which is when the UI has nothing to show yet.
    """
    start = False
    with _join_lock:
        data = _join_cache["data"]
        if not _join_cache["running"] and time.time() - _join_cache["at"] > JOIN_TTL:
            _join_cache["running"] = True
            start = True
    if start:
        threading.Thread(target=_refresh_join_rates, daemon=True).start()
    return data, data is None


def _cell(source, latest, last_ok_at, row_count):
    """One dataset cell for one county.

    state is what the UI paints:
      ok             - latest run succeeded
      failed         - latest run failed (error + last successful time kept)
      running        - a sync_log row with no finished_at
      manual         - sources.json says this layer has no public REST service
      not_loaded     - configured, never synced
      not_configured - no entry in sources.json for this county/dataset
    """
    manual = bool(source) and source.get("type") == "manual"
    cell = {
        "configured": bool(source),
        "manual": manual,
        "source_type": (source or {}).get("type") or ("rest" if source else None),
        "note": (source or {}).get("note") if manual else None,
        "row_count": row_count,
        "last_success_at": last_ok_at,
        "last_attempt_at": None,
        "rows_fetched": None,
        "error": None,
    }
    if latest is None:
        cell["state"] = "manual" if manual else ("not_loaded" if source else "not_configured")
        return cell
    cell["last_attempt_at"] = latest["finished_at"] or latest["started_at"]
    cell["rows_fetched"] = latest["rows_fetched"]
    if latest["finished_at"] is None:
        cell["state"] = "running"
    elif latest["status"] == "success":
        cell["state"] = "ok"
    else:
        cell["state"] = "failed"
        cell["error"] = latest["error"]
    return cell


def _build_status_counties(conn):
    """The whole payload except join rates (which are merged in per request)."""
    sources = read_sources()

    latest = {}
    for r in conn.execute(
        """
        SELECT sl.county, sl.dataset_type, sl.started_at, sl.finished_at,
               sl.status, sl.rows_fetched, sl.error
        FROM sync_log sl
        JOIN (SELECT county, dataset_type, MAX(id) AS max_id
              FROM sync_log GROUP BY county, dataset_type) l
          ON sl.id = l.max_id
        """
    ):
        latest[(r["county"], r["dataset_type"])] = r

    last_ok = {}
    for r in conn.execute(
        "SELECT county, dataset_type, MAX(finished_at) AS finished_at FROM sync_log "
        "WHERE status = 'success' GROUP BY county, dataset_type"
    ):
        last_ok[(r["county"], r["dataset_type"])] = r["finished_at"]

    counts = {(r["county"], r["dataset_type"]): r["row_count"] for r in conn.execute(
        "SELECT county, dataset_type, COUNT(*) AS row_count FROM features "
        "GROUP BY county, dataset_type"
    )}
    value_counts = {r["county"]: r["row_count"] for r in conn.execute(
        "SELECT county, COUNT(*) AS row_count FROM parcel_values GROUP BY county"
    )}

    # DOR values are one statewide sync (dor_values.py) that writes a
    # county='statewide' sync_log row plus per-county rows; the statewide row is
    # the authoritative "when did the tax roll last land" time for every county.
    dor_latest = latest.get(("statewide", "dor_values"))
    dor_ok_at = last_ok.get(("statewide", "dor_values"))

    known = set(sources) | {c for c, _ in counts} | set(value_counts)
    known.discard("statewide")
    priority = list(getattr(etl, "PRIORITY_COUNTIES", []))

    rows = []
    failing = 0
    for county in sorted(known):
        cfg = sources.get(county) or {}
        datasets = {}
        for dataset_type in STATUS_DATASETS:
            datasets[dataset_type] = _cell(
                cfg.get(dataset_type),
                latest.get((county, dataset_type)),
                last_ok.get((county, dataset_type)),
                counts.get((county, dataset_type), 0),
            )
        # The per-county dor_values sync_log row (written by the same statewide
        # run) is the fallback when a database predates the statewide row.
        dor_cell = _cell(
            {"type": "statewide"},
            dor_latest or latest.get((county, "dor_values")),
            dor_ok_at or last_ok.get((county, "dor_values")),
            value_counts.get(county, 0),
        )
        dor_cell["rows_fetched"] = None  # statewide total; meaningless per county
        dor_cell["scope"] = "statewide"
        datasets["dor_values"] = dor_cell
        failing += sum(1 for c in datasets.values() if c["state"] == "failed")
        rows.append({
            "county": county,
            "in_sources": county in sources,
            "priority": county in priority,
            "datasets": datasets,
        })

    total_features = sum(counts.values())
    return {
        "counties": rows,
        "dataset_types": ["dor_values"] + list(STATUS_DATASETS),
        "priority_counties": priority,
        "summary": {
            "total_features": total_features,
            "county_count": len(rows),
            "counties_with_parcels": sum(
                1 for r in rows if r["datasets"]["parcels"]["row_count"] > 0),
            "valued_parcels": sum(value_counts.values()),
            "counties_with_values": sum(1 for n in value_counts.values() if n > 0),
            "last_success_at": conn.execute(
                "SELECT MAX(finished_at) FROM sync_log WHERE status = 'success'"
            ).fetchone()[0],
            "failing_count": failing,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.route("/api/status/counties")
def api_status_counties():
    """One row per county with the most recent update per dataset.

    Cached in-process for STATUS_TTL seconds: the two GROUP BYs behind it cost
    ~1.3 s, and the page polls every 20 s (as does every open tab).
    """
    with _status_lock:
        cached = _status_cache["data"]
        fresh = cached is not None and time.time() - _status_cache["at"] < STATUS_TTL
    if not fresh:
        data = _build_status_counties(get_db())
        with _status_lock:
            _status_cache["data"] = data
            _status_cache["at"] = time.time()
        cached = data

    rates, pending = join_rates()
    for row in cached["counties"]:
        cell = row["datasets"]["parcels"]
        r = (rates or {}).get(row["county"])
        cell["joined_count"] = r["joined"] if r else None
        cell["join_rate"] = (r["joined"] / r["total"]) if r and r["total"] else None
    out = dict(cached)
    out["join_rates_pending"] = pending
    out["cached"] = fresh
    return jsonify(out)


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"WARNING: database not found at {DB_PATH} - run scripts\\etl.py first")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # threaded=True so a single slow query (e.g. an unfiltered, statewide
    # /api/values sort over parcel_values' ~10M+ rows, which can't use the
    # per-county composite indexes) doesn't serialize/block every other
    # concurrent request against Flask's single-threaded dev server.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
