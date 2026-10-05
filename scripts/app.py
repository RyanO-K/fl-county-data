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
from werkzeug.exceptions import BadRequest, HTTPException

BASE_DIR = Path(__file__).resolve().parent.parent
import etl  # noqa: E402  (same folder)
import recordings  # noqa: E402
from dor_codes import (  # noqa: E402  (stdlib only; shared with ag_encroachment.py)
    DOR_ROLL_FORMAT, RESIDENTIAL_CATEGORIES, county_code_format, normalize_code,
)
DB_PATH = etl.DB_PATH
SOURCES_PATH = BASE_DIR / "scripts" / "sources.json"
# Descriptions for land-use codes whose source layer has none (Orange); built
# by scripts/fetch_code_descriptions.py, applied at display time only.
CODE_DESCRIPTIONS_PATH = BASE_DIR / "scripts" / "code_descriptions.json"
# Agricultural zoning codes per county, for the "Agricultural zoning" preset;
# built by scripts/build_ag_zoning_codes.py.
AG_ZONING_PATH = BASE_DIR / "scripts" / "ag_zoning_codes.json"
# Land-use preset buckets (sets of DOR use-code categories), hand-curated.
LAND_USE_PRESETS_PATH = BASE_DIR / "scripts" / "land_use_presets.json"
# Output of scripts/ag_encroachment.py, attached read-only for the "Ag enclaves"
# filter. The demo database carries its own ag_enclaves table instead.
AG_RESULTS_PATH = Path(os.environ.get("FL_AG_RESULTS") or etl.DB_PATH.with_name("ag_encroachment.db"))
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

# Sortable table columns: sort key (the ?sort= value) -> (columns, kind). Only
# these literal column names ever reach ORDER BY; the user's value is just a
# dict lookup. The first column decides where missing values go (see
# order_by_clause); any further columns break ties within it. kind:
#   "key"  - NOT NULL column, no missing values to place
#   "num"  - numeric, NULL counts as missing
#   "text" - text, NULL or '' counts as missing
NUM, TEXT, KEY = "num", "text", "key"

# Browse table (/api/features). Every visible column; "Zoning" and "Land use"
# show code + description and sort by the code, "Last sale" by its price.
FEATURES_SORT_COLUMNS = {
    "id": (("id",), KEY),  # the default order
    # county/dataset_type/feature_key is the table's UNIQUE index, so these
    # two orders are total on their own and county can walk that index.
    "county": (("county", "dataset_type", "feature_key"), KEY),
    "dataset_type": (("dataset_type", "county", "feature_key"), KEY),
    "feature_key": (("feature_key",), KEY),
    "city": (("city",), TEXT),
    "acreage": (("acreage",), NUM),
    "zoning_code": (("zoning_code", "zoning_desc"), TEXT),
    "land_use_code": (("land_use_code", "land_use_desc"), TEXT),
    "land_value": (("land_value",), NUM),
    "building_value": (("building_value",), NUM),
    "total_value": (("total_value",), NUM),
    "just_value": (("just_value",), NUM),
    "sale_price": (("sale_price",), NUM),
    "sale_date": (("sale_date",), TEXT),
    "last_synced_at": (("last_synced_at",), KEY),
}

# Parcel Values table (/api/values). The original five keys keep their names
# so existing ?sort= URLs still work.
VALUES_SORT_COLUMNS = {
    "county": (("county",), KEY),
    "parcel_id": (("parcel_id",), KEY),
    "site_address": (("site_address",), TEXT),
    "site_city": (("site_city",), TEXT),
    "dor_use_code": (("dor_use_code",), TEXT),
    "just_value": (("just_value",), NUM),
    "assessed_value": (("assessed_value",), NUM),
    "taxable_value": (("taxable_value",), NUM),
    "land_value": (("land_value",), NUM),
    "land_sqft": (("land_sqft",), NUM),
    "sale_price": (("sale_price",), NUM),
    "sale_year": (("sale_year", "sale_month"), NUM),
    "sale_qual": (("sale_qual",), TEXT),
    "year_built": (("year_built",), NUM),
}


def sort_direction(value, default="ASC"):
    """'asc'/'desc' (any case) -> SQL keyword; anything else -> default."""
    v = (value or "").strip().lower()
    return "ASC" if v == "asc" else "DESC" if v == "desc" else default


def order_by_clause(spec, direction, tiebreak):
    """ORDER BY body for a (columns, kind) entry of a *_SORT_COLUMNS table.

    Missing values (NULL, and '' for text) go last in both directions. DESC
    already puts them last in SQLite (NULL < '' < any other text), so the plain
    column is used and an index on it still serves the sort. ASC needs help:
    NULLS LAST for numbers (which SQLite can still serve from an index) and a
    blank flag for text. `tiebreak` columns follow in the same direction so
    every row has a fixed place and OFFSET paging never repeats or skips one.
    """
    cols, kind = spec
    first = cols[0]
    terms = []
    if direction == "ASC" and kind == TEXT:
        terms.append(f"({first} IS NULL OR {first} = '')")
    terms.append(f"{first} {direction}" + (" NULLS LAST" if direction == "ASC" and kind == NUM else ""))
    for c in list(cols[1:]) + [c for c in tiebreak if c not in cols]:
        terms.append(f"{c} {direction}")
    return ", ".join(terms)


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


@app.errorhandler(400)
@app.errorhandler(413)
@app.errorhandler(414)
def api_http_error(exc):
    """JSON {"error": ...} for a refused /api/* request (too many filter
    codes, a request too large), so the UI can show the reason; other pages
    keep Flask's HTML error page."""
    if not request.path.startswith("/api/") or not isinstance(exc, HTTPException):
        return exc
    return jsonify({"error": exc.description}), exc.code


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def has_table(conn, name):
    """True when this database has the named table. The private owner/
    recordings tables are absent from the demo database, and every feature
    that reads them is a no-op there."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# Most codes one multi-select filter accepts: the facets list at most 500 per
# field, so the UI never sends more, and the cap keeps a hand-built URL far
# below SQLite's bound-variable limit (32766 since SQLite 3.32). A request
# over it is refused with a 400 naming the parameter (code_args).
MAX_FILTER_CODES = 500


def arg_list(args, name, limit=None):
    """All non-empty values of a repeatable query parameter (?zoning_code=A&
    zoning_code=B), de-duplicated in order and cut to `limit`. Accepts a
    werkzeug MultiDict or a plain dict whose value is a string or a list."""
    if hasattr(args, "getlist"):
        values = args.getlist(name)
    else:
        v = args.get(name)
        values = v if isinstance(v, (list, tuple)) else [v]
    out, seen = [], set()
    for v in values:
        if v in (None, "") or v in seen:
            continue
        if limit is not None and len(out) >= limit:
            break
        seen.add(v)
        out.append(v)
    return out


def code_args(args, name):
    """arg_list for a code filter, refusing more than MAX_FILTER_CODES
    distinct values with a 400 (JSON on /api/*, see api_http_error) naming
    the parameter, rather than silently dropping the rest."""
    codes = arg_list(args, name)
    if len(codes) > MAX_FILTER_CODES:
        raise BadRequest(f"Too many {name} values: {len(codes):,} (at most {MAX_FILTER_CODES:,}). "
                         "Select fewer codes.")
    return codes


# Preset filters: a named selection with per-county meaning (?preset=ag_zoning).
# The same zoning code string means different districts in different counties
# (Volusia's "A" vs Palm Beach's "A (city)"), so a preset is applied as
# (county, zoning_code) pairs, never as a flat code list.
PRESETS = {"ag_zoning": "Agricultural zoning"}
# Land-use presets (?preset=lu_<bucket>) come from land_use_presets.json
# instead: each is a set of DOR use-code categories, matched the same way as
# hide_residential (see category_clause).
LAND_USE_PRESET_PREFIX = "lu_"
# Most codes a preset binds as parameters (SQLite's limit is 32766 since 3.32;
# the other filters bind at most ~1000). Beyond it the codes are inlined as
# quoted SQL literals, which is only ever reached by a hand-edited JSON file.
MAX_PRESET_BOUND = 20000

_ag_zoning_cache = {"key": None, "data": {}}


def load_ag_zoning():
    """{county: [zoning_code, ...]} from AG_ZONING_PATH's "counties" section,
    re-read only when its path or mtime changes. A missing, unreadable or
    malformed file (or a malformed county entry) yields no codes ({})."""
    path = AG_ZONING_PATH
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _ag_zoning_cache["key"] != key:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        counties = raw.get("counties") if isinstance(raw, dict) else None
        data = {}
        if isinstance(counties, dict):
            for county, codes in counties.items():
                if isinstance(county, str) and isinstance(codes, dict):
                    kept = sorted(c for c in codes if isinstance(c, str) and c != "")
                    if kept:
                        data[county] = kept
        _ag_zoning_cache.update(key=key, data=data)
    return _ag_zoning_cache["data"]


_land_use_presets_cache = {"key": None, "data": {}}


def load_land_use_presets():
    """{"lu_<bucket>": {"label": str, "categories": frozenset of "00".."99"}}
    from LAND_USE_PRESETS_PATH, re-read only when its path or mtime changes.
    A missing or malformed file (or bucket) yields no presets."""
    path = LAND_USE_PRESETS_PATH
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _land_use_presets_cache["key"] != key:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        buckets = raw.get("buckets") if isinstance(raw, dict) else None
        data = {}
        if isinstance(buckets, dict):
            for name, b in buckets.items():
                if not (isinstance(name, str) and isinstance(b, dict) and isinstance(b.get("categories"), list)):
                    continue
                cats = frozenset(c for c in b["categories"]
                                 if isinstance(c, str) and len(c) == 2 and c.isdigit())
                if cats:
                    data[LAND_USE_PRESET_PREFIX + name] = {
                        "label": str(b.get("label") or name), "categories": cats}
        _land_use_presets_cache.update(key=key, data=data)
    return _land_use_presets_cache["data"]


def all_presets():
    """{preset name: label} for every known preset, zoning first."""
    return {**PRESETS, **{k: v["label"] for k, v in load_land_use_presets().items()}}


def preset_codes(preset, county=None):
    """{county: [zoning codes]} a preset selects, limited to `county` when one
    is given. None for an unknown (ignored) preset."""
    if preset != "ag_zoning":
        return None
    codes = load_ag_zoning()
    if county:
        return {county: codes[county]} if county in codes else {}
    return codes


def _sql_literal(s):
    return "'" + str(s).replace("'", "''") + "'"


def county_codes_clause(per_county, col, extra=""):
    """(sql, params) matching rows whose (county, col) is one of the pairs in
    {county: [codes]}, each pair group optionally narrowed by the literal SQL
    `extra`. False ("0") for an empty mapping. Codes are bound as parameters
    up to MAX_PRESET_BOUND, beyond that inlined as quoted literals."""
    if not per_county:
        return "0", []
    bind = sum(len(c) for c in per_county.values()) + len(per_county) <= MAX_PRESET_BOUND
    parts, params = [], []
    for county in sorted(per_county):
        codes = per_county[county]
        if bind:
            parts.append(f"(county = ?{extra} AND {col} IN ({', '.join('?' * len(codes))}))")
            params.append(county)
            params.extend(codes)
        else:
            parts.append(f"(county = {_sql_literal(county)}{extra} AND {col} IN "
                         f"({', '.join(_sql_literal(c) for c in codes)}))")
    return "(" + " OR ".join(parts) + ")", params


def preset_clause(args, conn=None):
    """(sql, params) for the ?preset= filter, or None when no known preset is
    set. With nothing to match (the county has no agricultural codes, or the
    JSON is missing) the clause is false rather than absent: the preset means
    'only these districts', so it must not widen to every row. A land-use
    preset keeps rows whose DOR category is in its bucket; unknowns drop."""
    bucket = load_land_use_presets().get((args.get("preset") or "").strip())
    if bucket:
        sql, params = category_clause(conn, bucket["categories"], args.get("county") or None)
        return f"COALESCE({sql}, 0)", params
    per_county = preset_codes((args.get("preset") or "").strip(), args.get("county") or "")
    if per_county is None:
        return None
    return county_codes_clause(per_county, "zoning_code")


# Hide residential (?hide_residential=1): drop parcels whose DOR use-code
# category is residential (01-09, see dor_codes.RESIDENTIAL_CATEGORIES; 00,
# vacant residential, stays). The statewide roll's dor_use_code decides; where
# a row has none the county's own land_use_code does, when its county format
# (dor_codes.COUNTY_CODE_FORMATS) reads it as a DOR category. Rows with no
# usable code are kept. Every spelling the roll format accepts ("1", "01",
# "001"), from normalize_code itself; digits only, so safe to inline.
DOR_ROLL_CODES = sorted({s for n in range(100) for s in (str(n), f"{n:02d}", f"{n:03d}")
                         if normalize_code(s, DOR_ROLL_FORMAT) is not None})
DOR_ROLL_RESIDENTIAL = [s for s in DOR_ROLL_CODES
                        if normalize_code(s, DOR_ROLL_FORMAT) in RESIDENTIAL_CATEGORIES]


def category_land_use_codes(conn, categories, county=None):
    """{county: [parcels land_use_code values whose county format reads as one
    of `categories`]}, limited to `county` when given. The candidate
    codes come from the cached per-code counts (compute_code_counts); without
    a connection only an already cached copy is used, else {}."""
    if conn is not None:
        cc = _cached("code_counts", lambda: compute_code_counts(conn))
    else:
        with _facets_lock:
            hit = _facets_cache.get("code_counts")
        cc = hit[1] if hit else None
    out = {}
    for c, per_dataset in ((cc or {}).get("land_use") or {}).items():
        if county and c != county:
            continue
        fmt = county_code_format(c)
        codes = sorted(code for code in per_dataset.get("parcels", {})
                       if normalize_code(code, fmt) in categories)
        if codes:
            out[c] = codes
    return out


def residential_land_use_codes(conn, county=None):
    return category_land_use_codes(conn, RESIDENTIAL_CATEGORIES, county)


def category_clause(conn, categories, county=None):
    """(sql, params) for an expression that is 1 for rows whose DOR category
    is in `categories`, 0 for rows known to be outside them and NULL for
    unknown (a NULL land_use_code in the fallback); callers COALESCE it."""
    roll = ", ".join(f"'{s}'" for s in DOR_ROLL_CODES)
    roll_hit = ", ".join(f"'{s}'" for s in DOR_ROLL_CODES
                         if normalize_code(s, DOR_ROLL_FORMAT) in categories) or "NULL"
    fallback, params = county_codes_clause(
        category_land_use_codes(conn, categories, county), "land_use_code", " AND dataset_type = 'parcels'")
    return f"CASE WHEN dor_use_code IN ({roll}) THEN dor_use_code IN ({roll_hit}) ELSE {fallback} END", params


def residential_clause(conn, county=None):
    """(sql, params) keeping every row that is not known to be residential;
    NOT COALESCE(..., 0) keeps unknowns."""
    sql, params = category_clause(conn, RESIDENTIAL_CATEGORIES, county)
    return f"NOT COALESCE({sql}, 0)", params


# Ag enclaves (?ag_enclave=1): parcels in an agricultural zoning district
# (ag_zoning_codes.json) whose ring ag_encroachment.py found developed.
ENCLAVE_WHERE = "surrounded = 1 AND ag_by_zoning = 1"


def enclave_source(conn):
    """SELECT yielding the (county, feature_id) of every ag enclave, or None
    when there are no results. The demo database's ag_enclaves table wins;
    otherwise AG_RESULTS_PATH is attached read-only as `ag`."""
    if has_table(conn, "ag_enclaves"):
        return "SELECT county, feature_id FROM ag_enclaves"
    path = AG_RESULTS_PATH
    if not path.exists():
        return None
    try:
        if not any(r[1] == "ag" for r in conn.execute("PRAGMA database_list")):
            conn.execute("ATTACH DATABASE ? AS ag", (f"file:{path.as_posix()}?mode=ro",))
        conn.execute("SELECT 1 FROM ag.results LIMIT 0")
    except sqlite3.Error:
        return None
    return f"SELECT county, feature_id FROM ag.results WHERE {ENCLAVE_WHERE}"


def enclave_counts(conn):
    """{county: enclave count}, or None when there are no results."""
    src = enclave_source(conn)
    if src is None:
        return None
    return dict(conn.execute(f"SELECT county, COUNT(*) FROM ({src}) GROUP BY county").fetchall())


def build_filters(args, conn=None):
    """Translate query-string filters into a WHERE clause + params list.

    zoning_code and land_use_code are multi-select: repeat the parameter to
    match any of several codes (OR within a field, AND across fields).
    preset=ag_zoning keeps rows whose (county, zoning_code) is an agricultural
    district per ag_zoning_codes.json (see preset_clause); it is ANDed with
    everything else, including an explicit zoning_code selection.
    ag_enclave=1 keeps the ag enclaves (see enclave_source); with no results
    on file it matches nothing.
    exclude_land_use_code (repeatable) drops rows carrying any of the codes;
    rows with no land-use code stay. hide_residential=1 drops parcels whose
    DOR category is residential (see residential_clause); unknowns stay.
    `conn` is only needed for the recorded-instrument filters (has_mortgage,
    mortgage_since, mortgage_min, mortgage_max, has_lien) and ag_enclave,
    which are skipped when it is None or their tables are absent."""
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
    preset = preset_clause(args, conn)
    if preset:
        clauses.append(preset[0])
        params.extend(preset[1])
    # Code filters are multi-select: a row matches any of the chosen codes.
    for col in ("zoning_code", "land_use_code"):
        codes = code_args(args, col)
        if codes:
            clauses.append(f"{col} IN ({', '.join('?' * len(codes))})")
            params.extend(codes)
    # Excluded land-use codes: rows without a land-use code are kept (a bare
    # NOT IN would drop them, as NULL NOT IN (...) is NULL).
    excluded = code_args(args, "exclude_land_use_code")
    if excluded:
        clauses.append(f"(land_use_code IS NULL OR land_use_code NOT IN ({', '.join('?' * len(excluded))}))")
        params.extend(excluded)
    if args.get("hide_residential") == "1":
        sql, rparams = residential_clause(conn, args.get("county") or None)
        clauses.append(sql)
        params.extend(rparams)
    add("acreage", args.get("min_acreage"), ">=", float)
    add("acreage", args.get("max_acreage"), "<=", float)
    add("total_value", args.get("min_value"), ">=", float)
    add("total_value", args.get("max_value"), "<=", float)

    if conn is not None and args.get("ag_enclave") == "1":
        src = enclave_source(conn)
        clauses.append(f"(features.county, features.id) IN ({src})" if src else "0")

    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(land_use_desc LIKE ? OR zoning_desc LIKE ? OR zoning_code LIKE ? "
            "OR land_use_code LIKE ? OR feature_key LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    if conn is not None and has_table(conn, "instrument_parcels"):
        # Uncorrelated set of (county, parcel_key) matching the instrument
        # criteria: SQLite materializes it once and probes it per feature row.
        # A correlated EXISTS here made the planner drive from the instrument
        # date index and rescan every recent instrument for every parcel.
        link = ("(features.county, features.feature_key_norm) IN ("
                "SELECT ip.county, ip.parcel_key FROM recorded_instruments ri "
                "JOIN instrument_parcels ip ON ip.county = ri.county AND ip.instrument_no = ri.instrument_no "
                "WHERE {cond})")
        since = (args.get("mortgage_since") or "").strip()
        mmin, mmax = args.get("mortgage_min"), args.get("mortgage_max")
        conds, mparams = [], []
        # has_mortgage=1: any linked mortgage on file. has_mortgage=amount:
        # a linked mortgage whose dollar amount the clerk index carried
        # (rare: most feeds leave consideration NULL). Other values ignored.
        has_mtg = (args.get("has_mortgage") or "").strip().lower()
        if has_mtg == "amount":
            conds.append("AND ri.consideration IS NOT NULL")
        elif has_mtg == "1":
            conds.append("AND 1")
        if since:
            conds.append("AND ri.recorded_at >= ?")
            mparams.append(since)
        for v, op in ((mmin, ">="), (mmax, "<=")):
            if v not in (None, ""):
                try:
                    mparams.append(float(v))
                except ValueError:
                    continue
                conds.append(f"AND ri.consideration {op} ?")
        if conds:
            clauses.append(link.format(cond="ri.category = 'mortgage' " + " ".join(conds)))
            params.extend(mparams)
        if args.get("has_lien") == "1":
            # Open lien: the parcel's latest lien / lis pendens / judgment is
            # newer than its latest satisfaction or release (or it has none).
            # Two grouped passes over the instrument tables, materialized once.
            by_key = ("SELECT ip.county, ip.parcel_key, MAX(ri.recorded_at) AS last_at "
                      "FROM recorded_instruments ri JOIN instrument_parcels ip "
                      "ON ip.county = ri.county AND ip.instrument_no = ri.instrument_no "
                      "WHERE ri.category IN ({cats}) GROUP BY ip.county, ip.parcel_key")
            clauses.append(
                "(features.county, features.feature_key_norm) IN ("
                "SELECT l.county, l.parcel_key FROM (" + by_key.format(cats="'lien','lis_pendens','judgment'") + ") l "
                "LEFT JOIN (" + by_key.format(cats="'satisfaction','release'") + ") r "
                "ON r.county = l.county AND r.parcel_key = l.parcel_key "
                "WHERE r.last_at IS NULL OR r.last_at < l.last_at)")

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
    has_recordings = False
    try:
        conn = get_db()
        has_recordings = has_table(conn, "instrument_parcels")
        if os.environ.get("DEMO_MODE") == "1":
            demo = {"counties": []}
            row = conn.execute("SELECT value FROM demo_info WHERE key='counties'").fetchone()
            if row:
                demo["counties"] = json.loads(row[0])
    except sqlite3.Error:
        if os.environ.get("DEMO_MODE") == "1":
            demo = demo or {"counties": []}
    # The recording filters show whenever the instrument tables exist (the
    # demo database now carries them for its counties), not by demo flag.
    return render_template("index.html", demo=demo, has_recordings=has_recordings)


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


_code_desc_cache = {"key": None, "data": {}}


def load_code_descriptions():
    """Parsed CODE_DESCRIPTIONS_PATH, re-read only when its path or mtime
    changes. A missing or unreadable file means no descriptions ({})."""
    path = CODE_DESCRIPTIONS_PATH
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _code_desc_cache["key"] != key:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _code_desc_cache.update(key=key, data=data if isinstance(data, dict) else {})
    return _code_desc_cache["data"]


def describe_code(county, dataset_type, code, descriptions=None):
    """Plain-English meaning of a land-use code from code_descriptions.json,
    or None. With no dataset_type every dataset of the county is tried
    (parcels first). A dataset marked dor_fallback falls back to the statewide
    DOR category of the code's first two digits. Callers describing many rows
    pass `descriptions` (load_code_descriptions(), fetched once) to skip the
    per-call mtime check."""
    if not county or not code:
        return None
    data = load_code_descriptions() if descriptions is None else descriptions
    if county.startswith("_") or county == "dor_categories":
        return None
    datasets = data.get(county)
    if not isinstance(datasets, dict):
        return None
    if dataset_type:
        names = [dataset_type]
    else:
        names = sorted(datasets, key=lambda n: (n != "parcels", n))
    code = str(code)
    tried = []
    for name in names:
        ds = datasets.get(name)
        if not isinstance(ds, dict):
            continue
        desc = (ds.get("land_use") or {}).get(code)
        if desc:
            return desc
        tried.append(ds)
    # Only after no dataset matched exactly, so a named code always beats
    # a category guess.
    prefix = code[:2]
    if len(prefix) == 2 and prefix.isdigit():
        category = (data.get("dor_categories") or {}).get(prefix)
        if category and any(ds.get("dor_fallback") for ds in tried):
            return f"{category} (DOR category {prefix})"
    return None


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
_facets_cache = {}       # key -> (computed_at, data)
_facets_inflight = {}    # key -> Event set when a computation for it finishes
_facets_lock = threading.Lock()


def _facets_key(args):
    # Every value of a repeated parameter is part of the key (MultiDict.items()
    # would only yield the first); single values keep their old key form.
    items = []
    for k in (args.keys() if hasattr(args, "getlist") else args):
        vals = sorted(arg_list(args, k))
        if vals:
            items.append((k, vals[0] if len(vals) == 1 else vals))
    preset = (args.get("preset") or "").strip()
    if preset in all_presets():
        # The preset's codes live in a JSON file; a rebuilt file must not be
        # answered from facets computed with the old codes.
        path = LAND_USE_PRESETS_PATH if preset.startswith(LAND_USE_PRESET_PREFIX) else AG_ZONING_PATH
        try:
            items.append(("_preset_file", path.stat().st_mtime_ns))
        except OSError:
            items.append(("_preset_file", None))
    return json.dumps(sorted(items, key=lambda kv: kv[0]))


def _facets_disk_path():
    """Sidecar next to the database (never the database itself) holding the
    last computed facets, so a restart doesn't repeat the full-table scan."""
    return DB_PATH.with_name(DB_PATH.stem + ".facets.json")


def _db_stamp():
    """Identity of the database contents: mtime and size of the main file and
    its WAL. Any write (an ETL run) changes it and invalidates the sidecar."""
    parts = []
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal")):
        try:
            st = p.stat()
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def _load_disk_facets():
    try:
        saved = json.loads(_facets_disk_path().read_text(encoding="utf-8"))
        if saved.get("stamp") == _db_stamp():
            return saved.get("entries", {})
    except (OSError, ValueError):
        pass
    return {}


def _save_disk_facets():
    with _facets_lock:
        entries = {k: v for k, (_t, v) in _facets_cache.items()}
    path = _facets_disk_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"stamp": _db_stamp(), "entries": entries}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"facets cache not saved: {exc}")


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


def compute_availability(conn):
    """Which recorded-instrument filters can match anything, per county: how
    many features are linked to a mortgage, to a mortgage with a known amount,
    and to a lien-type instrument, plus the dataset types those linked features
    belong to. The UI greys out filters this says would certainly return
    nothing; counts are upper bounds (a lien here may already be released)."""
    out = {}
    if not has_table(conn, "instrument_parcels"):
        return {"recordings": out}
    flags = ("SELECT ip.parcel_key, MAX(ri.category = 'mortgage') AS mtg, "
             "MAX(ri.category = 'mortgage' AND ri.consideration IS NOT NULL) AS amt, "
             "MAX(ri.category IN ('lien','lis_pendens','judgment')) AS lien "
             "FROM recorded_instruments ri JOIN instrument_parcels ip "
             "ON ip.county = ri.county AND ip.instrument_no = ri.instrument_no "
             "WHERE ri.county = ? GROUP BY ip.parcel_key")
    for (county,) in conn.execute("SELECT DISTINCT county FROM instrument_parcels").fetchall():
        per = {"mortgage": 0, "mortgage_amount": 0, "lien": 0, "datasets": {}}
        dtypes = [r[0] for r in conn.execute(
            "SELECT DISTINCT dataset_type FROM features WHERE county = ?", (county,))]
        for dt in dtypes:
            n, mtg, amt, lien = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(m.mtg), 0), COALESCE(SUM(m.amt), 0), COALESCE(SUM(m.lien), 0) "
                f"FROM ({flags}) m JOIN features f "
                "ON f.county = ? AND f.dataset_type = ? AND f.feature_key_norm = m.parcel_key",
                (county, county, dt)).fetchone()
            if n:
                per["datasets"][dt] = n
                per["mortgage"] += mtg
                per["mortgage_amount"] += amt
                per["lien"] += lien
        if per["datasets"]:
            out[county] = per
    return {"recordings": out}


def compute_code_counts(conn):
    """Row counts per county x dataset, and per county x dataset x zoning /
    land-use code, from the covering facet indexes (about a second each on the
    full database). filter_counts() answers from this in memory."""
    out = {"datasets": {}, "zoning": {}, "land_use": {}}
    for county, dt, n in conn.execute(
            "SELECT county, dataset_type, COUNT(*) FROM features GROUP BY county, dataset_type"):
        out["datasets"].setdefault(county, {})[dt] = n
    for key, col in (("zoning", "zoning_code"), ("land_use", "land_use_code")):
        for county, dt, code, n in conn.execute(
                f"SELECT county, dataset_type, {col}, COUNT(*) FROM features "
                f"WHERE {col} IS NOT NULL AND {col} != '' GROUP BY county, dataset_type, {col}"):
            out[key].setdefault(county, {}).setdefault(dt, {})[code] = n
    return out


def _recording_needs(args):
    """Which availability counters a recording filter selection needs > 0."""
    needs = set()
    mtg = (args.get("has_mortgage") or "").strip().lower()
    if mtg == "1" or (args.get("mortgage_since") or "").strip():
        needs.add("mortgage")
    if mtg == "amount" or args.get("mortgage_min") or args.get("mortgage_max"):
        needs.add("mortgage_amount")
    if args.get("has_lien") == "1":
        needs.add("lien")
    return needs


def filter_counts(conn, args):
    """Upper bound on matching rows per county (ignoring the county filter) and
    per dataset (ignoring the dataset filter) for the categorical filters:
    dataset, zoning code, land-use code (chosen or excluded), preset, ag
    enclaves and the recorded-instrument filters.
    A multi-code selection counts the rows of all its codes (each row has one
    code, so the sum is exact per dataset). Each constraint is applied
    independently and the minimum taken, so a
    zero is certain but a positive count may still over-estimate. Ranges and
    free text are not considered. The UI greys out zero choices."""
    cc = _cached("code_counts", lambda: compute_code_counts(conn))
    rec = _cached("availability", lambda: compute_availability(conn))["recordings"]
    dt_arg = args.get("dataset_type") or ""
    zoning = code_args(args, "zoning_code")
    land_use = code_args(args, "land_use_code")
    # Excluded codes: subtracting their rows is exact (rows with no code are
    # not counted under any code, and stay). hide_residential is not counted:
    # it turns on the DOR roll code, which these counts do not carry, so the
    # bound simply stays an upper bound.
    excluded = set(code_args(args, "exclude_land_use_code"))
    if excluded and land_use:
        land_use = [c for c in land_use if c not in excluded]
        if not land_use:
            land_use = None  # every chosen code is also excluded: nothing matches
    needs = _recording_needs(args)
    # Preset: per-county zoning codes (all counties; the county filter is
    # ignored here like everywhere in this function). A county missing from
    # the preset can match nothing.
    preset = preset_codes((args.get("preset") or "").strip())
    # Ag enclaves are parcels only; no results on file means none anywhere.
    enclaves = (enclave_counts(conn) or {}) if args.get("ag_enclave") == "1" else None

    def code_rows(key, county, d, codes):
        per_code = cc[key].get(county, {}).get(d, {})
        return sum(per_code.get(c, 0) for c in codes)

    def zoning_codes_for(county):
        """Codes a row of this county may carry, or None for no constraint."""
        if preset is None:
            return zoning or None
        allowed = preset.get(county, [])
        if zoning:
            allowed = [c for c in zoning if c in set(allowed)]
        return allowed

    def bound(county, dtype):
        ds = cc["datasets"].get(county, {})
        total = 0
        codes = zoning_codes_for(county)
        for d in ([dtype] if dtype else list(ds)):
            n = ds.get(d, 0)
            if codes is not None:
                n = min(n, code_rows("zoning", county, d, codes))
            if land_use is None:
                n = 0
            elif land_use:
                n = min(n, code_rows("land_use", county, d, land_use))
            if excluded:
                n = min(n, ds.get(d, 0) - code_rows("land_use", county, d, excluded))
            if enclaves is not None:
                n = min(n, enclaves.get(county, 0)) if d == "parcels" else 0
            if needs:
                r = rec.get(county)
                if not r or any(r[k] <= 0 for k in needs):
                    n = 0
                else:
                    n = min(n, r["datasets"].get(d, 0))
            total += n
        return total

    counties = {c: bound(c, dt_arg) for c in cc["datasets"]}
    county_arg = args.get("county") or ""
    all_dts = sorted({d for ds in cc["datasets"].values() for d in ds})
    datasets = {
        d: (bound(county_arg, d) if county_arg else sum(bound(c, d) for c in cc["datasets"]))
        for d in all_dts
    }
    return {"counties": counties, "datasets": datasets}


def cached_facets(conn, args):
    return _cached(_facets_key(args), lambda: compute_facets(conn, args))


def _cached(key, compute):
    """Memoized compute() with the shared facets cache. Concurrent callers for
    the same key (the startup warm-up and the first page load, typically) share
    one computation instead of each scanning the table."""
    while True:
        with _facets_lock:
            hit = _facets_cache.get(key)
            if hit and time.time() - hit[0] < FACETS_TTL:
                return hit[1]
            pending = _facets_inflight.get(key)
            if pending is None:
                pending = _facets_inflight[key] = threading.Event()
                break
        pending.wait()  # someone else is computing it; re-check the cache
    try:
        data = compute()
        with _facets_lock:
            _facets_cache[key] = (time.time(), data)
    finally:
        with _facets_lock:
            _facets_inflight.pop(key, None)
        pending.set()
    _save_disk_facets()
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
        _cached("availability", lambda: compute_availability(conn))
        _cached("code_counts", lambda: compute_code_counts(conn))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"facets warm-up failed: {exc}")


_facets_cache.update({k: (time.time(), v) for k, v in _load_disk_facets().items()})
threading.Thread(target=warm_facets, name="facets-warmup", daemon=True).start()


@app.route("/api/facets")
def api_facets():
    """Distinct zoning/land-use codes for the current county+dataset_type
    selection, to populate filter dropdowns with real values. Cached per
    filter combination for FACETS_TTL seconds.

    With a county selected, land-use codes the source left undescribed get a
    description from code_descriptions.json (see describe_code). That is done
    here on a copy rather than in compute_facets, whose results are persisted
    by DB stamp and would go stale when the JSON changes. Without a county
    codes from different counties can collide, so nothing is filled."""
    facets = cached_facets(get_db(), request.args)
    county = request.args.get("county") or ""
    if county:
        dataset_type = request.args.get("dataset_type") or ""
        descriptions = load_code_descriptions()
        codes = []
        for entry in facets.get("land_use_codes", []):
            if not entry.get("land_use_desc"):
                desc = describe_code(county, dataset_type, entry.get("land_use_code"), descriptions)
                if desc:
                    entry = {**entry, "land_use_desc": desc}
            codes.append(entry)
        facets = {**facets, "land_use_codes": codes}
    return jsonify(facets)


@app.route("/api/availability")
def api_availability():
    """Per-county counts telling the UI which recorded-instrument filters can
    match anything (see compute_availability). Cached like the facets."""
    conn = get_db()
    return jsonify(_cached("availability", lambda: compute_availability(conn)))


@app.route("/api/presets")
def api_presets():
    """The preset filters. The zoning preset carries how many zoning codes each
    county contributes (the UI's hint), from ag_zoning_codes.json; each
    land-use preset (kind "land_use") its DOR categories."""
    out = {}
    for name, label in PRESETS.items():
        per_county = preset_codes(name) or {}
        out[name] = {"label": label, "kind": "zoning",
                     "counties": {c: len(v) for c, v in per_county.items()}}
    for name, b in load_land_use_presets().items():
        out[name] = {"label": b["label"], "kind": "land_use", "categories": sorted(b["categories"])}
    return jsonify(out)


@app.route("/api/ag_enclaves")
def api_ag_enclaves():
    """Whether ag-enclave results exist and how many enclaves per county
    (the checkbox's availability and hint)."""
    counts = enclave_counts(get_db())
    return jsonify({"available": counts is not None, "counties": counts or {}})


@app.route("/api/filter_counts")
def api_filter_counts():
    """Per-county and per-dataset match bounds for the current categorical
    filters (see filter_counts); answered from cached aggregates."""
    return jsonify(filter_counts(get_db(), request.args))


@app.route("/api/features")
def api_features():
    conn = get_db()
    where, params = build_filters(request.args, conn)

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

    # Unknown or absent sort keys keep the original id order. The map feed
    # (geometry=1) always takes the first features by id.
    sort = request.args.get("sort", "")
    if sort in FEATURES_SORT_COLUMNS and not include_geometry:
        direction = sort_direction(request.args.get("dir"))
    else:
        sort, direction = "id", "ASC"
    order = order_by_clause(FEATURES_SORT_COLUMNS[sort], direction, ["id"])

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
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    descriptions = load_code_descriptions()
    return jsonify({
        "rows": [row_dict(r, descriptions) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "sort": sort,
        "dir": direction.lower(),
    })



JSON_COLUMNS = ("geometry_geojson", "attributes_json")


def row_dict(row, descriptions=None):
    """sqlite3.Row -> dict with the compressed JSON columns decoded to text,
    which is what the front end has always received. A blank land_use_desc
    is filled from code_descriptions.json (describe_code) when the row
    carries its county, dataset and code; a source description always wins.
    Pass `descriptions` when converting many rows (see describe_code)."""
    d = dict(row)
    for k in JSON_COLUMNS:
        if k in d:
            d[k] = etl.decode_json(d[k])
    if "land_use_desc" in d and not d["land_use_desc"] and d.get("land_use_code"):
        desc = describe_code(d.get("county"), d.get("dataset_type"), d["land_use_code"],
                             descriptions)
        if desc:
            d["land_use_desc"] = desc
    return d


MAX_GEOMETRY_CHUNK = 5000


@app.route("/api/features/geometry")
def api_features_geometry():
    """Uncapped map feed: the client walks the full result set in chunks using
    keyset paging on id (fast at any depth, unlike OFFSET). `total` is only
    computed on the first chunk (after_id=0)."""
    conn = get_db()
    where, params = build_filters(request.args, conn)
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
    descriptions = load_code_descriptions()
    rows = [row_dict(r, descriptions) for r in rows]
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


def _feature_parcel(conn, feature_id):
    """(county, parcel_key) for a parcel feature, or None. parcel_key is the
    normalized id both parcel_owners and instrument_parcels key on."""
    row = conn.execute("SELECT county, feature_key_norm FROM features WHERE id = ? AND dataset_type = 'parcels'",
                       (feature_id,)).fetchone()
    return (row["county"], row["feature_key_norm"]) if row else None


@app.route("/api/feature/<int:feature_id>/owner")
def api_feature_owner(feature_id):
    """Owner of record from the statewide DOR roll. 404 when the parcel has no
    owner row, or (demo database) when parcel_owners does not exist."""
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
    """Recorded instruments linked to this parcel, newest first. 404 when the
    instrument tables do not exist (demo database)."""
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
    # Party names stay out of the demo database, so that table may be absent.
    with_parties = has_table(conn, "instrument_parties")
    out = []
    for r in rows:
        d = dict(r)
        d["parties"] = [{"role": p["role"], "name": p["name"]} for p in conn.execute(
            "SELECT role, name FROM instrument_parties WHERE county = ? AND instrument_no = ? ORDER BY role, seq",
            (key[0], r["instrument_no"]))] if with_parties else []
        out.append(d)
    return jsonify({"instruments": out})


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

    # Defaults (just_value, high to low) are what this endpoint always did;
    # an unknown key falls back to just_value but keeps the asked direction.
    sort = request.args.get("sort", "just_value")
    if sort not in VALUES_SORT_COLUMNS:
        sort = "just_value"
    direction = sort_direction(request.args.get("dir"), "DESC")
    # (county, parcel_id) is the primary key: a total, index-friendly tiebreak.
    order = order_by_clause(VALUES_SORT_COLUMNS[sort], direction, ["county", "parcel_id"])

    total = conn.execute(
        f"SELECT COUNT(*) FROM parcel_values {where}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    cols = ", ".join(VALUES_COLUMNS)
    rows = conn.execute(
        f"SELECT {cols} FROM parcel_values {where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return jsonify({
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "sort": sort,
        "dir": direction.lower(),
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
    # Recorded instruments are a local-only table: absent from the demo database.
    rec_counts = {}
    if has_table(conn, "recorded_instruments"):
        rec_counts = {r["county"]: r["n"] for r in conn.execute(
            "SELECT county, COUNT(*) AS n FROM recorded_instruments GROUP BY county")}

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
        rec_src = recordings.SOURCES.get(county)
        datasets["recordings"] = _cell(
            {"type": "feed", "note": rec_src["note"]} if rec_src else None,
            latest.get((county, "recordings")),
            last_ok.get((county, "recordings")),
            rec_counts.get(county, 0),
        )
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
        "dataset_types": ["dor_values"] + list(STATUS_DATASETS) + ["recordings"],
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
