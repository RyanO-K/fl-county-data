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


@app.route("/api/facets")
def api_facets():
    """Distinct zoning/land-use codes for the current county+dataset_type
    selection, to populate filter dropdowns with real values."""
    conn = get_db()
    base_where, base_params = build_filters(request.args)

    def nonempty_clause(col):
        extra = f"{col} IS NOT NULL AND {col} != ''"
        if base_where:
            return base_where + " AND " + extra
        return "WHERE " + extra

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

    return jsonify({
        "zoning_codes": [dict(r) for r in zoning_rows],
        "land_use_codes": [dict(r) for r in land_use_rows],
    })


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
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })


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
    rows = [dict(r) for r in rows]
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
    return jsonify(dict(row))


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
        geom = conn.execute(
            "SELECT id, geometry_geojson FROM features "
            "WHERE county = ? AND dataset_type = 'parcels' AND "
            "upper(replace(replace(replace(replace(feature_key,'-',''),' ',''),'.',''),'/','')) = ? LIMIT 1",
            (county, row["parcel_key"]),
        ).fetchone()
    out["feature_id"] = geom["id"] if geom else None
    out["geometry_geojson"] = geom["geometry_geojson"] if geom else None
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


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"WARNING: database not found at {DB_PATH} - run scripts\\etl.py first")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # threaded=True so a single slow query (e.g. an unfiltered, statewide
    # /api/values sort over parcel_values' ~10M+ rows, which can't use the
    # per-county composite indexes) doesn't serialize/block every other
    # concurrent request against Flask's single-threaded dev server.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
