"""Agricultural encroachment: which agricultural parcels are hemmed in by
residential, urban and industrial development?

For every agricultural parcel, look at a ring of land around it and measure
how much of that ring is developed. A parcel is "surrounded" when the
developed share of the ring is at or above the threshold (default 75%).

Definitions (defaults; each can be changed with a CLI flag)
-----------------------------------------------------------
Land-use category. Every parcel is reduced to a 2-digit Florida DOR use-code
category (Fla. Admin. Code R. 12D-8.008; the table is DOR_CATEGORIES in
fetch_code_descriptions.py). Two codes are available per parcel:
  * features.dor_use_code - the statewide DOR tax roll ("001", "066", ...),
    joined on by dor_values.py. Uniform statewide, one year behind.
  * features.land_use_code - the county layer's own code, in a county-
    specific format (see COUNTY_CODE_FORMATS / normalize_code).
The DOR roll wins by default (--code-priority dor); the county code fills
in where the roll is missing (--code-priority county reverses that).

Agricultural parcel: a dataset_type='parcels' feature whose DOR category is
50-69, OR whose zoning is agricultural per scripts/ag_zoning_codes.json
({"counties": {county: {zoning_code: description}}}). Zoning is taken from
the parcel's own zoning_code; when that is blank and the county has a
'zoning' dataset, from the zoning polygon containing the parcel's
representative point. results.ag_reason records which test(s) matched
("land_use", "zoning" or "land_use+zoning"), results.zoning_source whether
zoning came from the parcel ("parcel") or the zoning layer ("layer").
When the JSON file is missing the zoning test is skipped with a warning.

Surroundings (the ring): the parcel buffered outward by --ring-ft (660 ft,
an eighth of a mile) minus the parcel itself. All geometry is projected
from the stored WGS84 GeoJSON to EPSG:3086 (Florida GDL Albers, metres)
before any buffer or area is computed.

Neighbours: every OTHER parcel intersecting the ring, clipped to the ring,
classified by its DOR category:
    residential   01-09
    urban         11-39, 71-79, 81, 83-89, 91  (commercial, institutional,
                  government, utilities - improved)
    industrial    41-49, 92 (mining)
    agricultural  50-69   (includes neighbouring ag parcels)
    vacant        00, 10, 40, 70, 80, 99        (see --count-vacant-as-developed)
    open          82, 90, 96, 97  (parks, conservation, wetlands, leaseholds)
    excluded      93, 94, 95, 98  (subsurface rights, rights-of-way, water,
                  centrally assessed railroads/utilities)
    unknown       no usable code
Neighbours are classified on land use only (a neighbour's zoning is ignored).

Developed share = (residential + urban + industrial [+ vacant with
--count-vacant-as-developed]) / (residential + urban + industrial +
agricultural + vacant + open), all as clipped areas inside the ring. The
denominator deliberately leaves out excluded and unknown land AND the parts
of the ring no parcel covers at all: most roads are not parcels, just gaps
between parcels, and ROW/water parcels likewise say nothing about whether
the surroundings are built up. surrounded = developed share >= --threshold
(0.75); it is NULL when nothing classifiable lies in the ring.

coverage = area of the ring covered by any neighbour parcel / ring area.
low_coverage = coverage < --min-coverage (0.5): the ring is mostly
unmapped (county line, coast, missing data) and the result is weak.

Overlapping neighbours (condo stacks, duplicate polygons, parcels inside
parcels) are never double counted: parcels that overlap another are found
once per county (find_overlapping), and their clipped pieces in a ring are
unioned per class instead of summed. When pieces of different classes
overlap, the contested area goes to the first class in CLASS_PRIORITY
(agricultural, open, vacant, residential, urban, industrial, excluded,
unknown) - i.e. conservatively away from "developed". Digitizing slivers
thinner than 0.5 m between adjacent parcels are ignored.

Counties whose parcels have no DOR-based code for at least
--min-code-coverage (50%) of parcels are skipped with the reason logged
and recorded in the runs table (e.g. a county with neither a county code
nor a DOR roll match), rather than being misclassified.

Output
------
A separate SQLite file (default: ag_encroachment.db next to the source DB;
--out to change). The source database is opened read-only (mode=ro) and is
never written. Tables:
    results  one row per agricultural parcel, key (county, feature_id)
    runs     one row per county run: status, parameters, counts, timing
Re-running a county replaces only that county's rows in the OUTPUT db.
--resume skips counties whose latest run finished with the same parameters.

Usage
-----
    .venv\\Scripts\\python.exe scripts\\ag_encroachment.py                  # every county
    .venv\\Scripts\\python.exe scripts\\ag_encroachment.py --county orange --limit 200
    .venv\\Scripts\\python.exe scripts\\ag_encroachment.py --resume --workers 4 --csv out.csv
"""
import argparse
import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402
from fetch_code_descriptions import DOR_CATEGORIES  # noqa: E402

log = logging.getLogger("ag_encroachment")

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_AG_ZONING_PATH = SCRIPTS_DIR / "ag_zoning_codes.json"
PROJECTED_CRS = "EPSG:3086"          # Florida GDL Albers, metres
FT_TO_M = 0.3048
SQM_PER_ACRE = 4046.8564224
FETCH_ROWS = 20000                   # rows per fetchmany / bulk GeoJSON parse

# --- classes -----------------------------------------------------------------
CLASSES = ("residential", "urban", "industrial", "agricultural",
           "vacant", "open", "excluded", "unknown")
C = {name: i for i, name in enumerate(CLASSES)}
# Contested (cross-class overlapping) area goes to the first class listed.
CLASS_PRIORITY = ("agricultural", "open", "vacant", "residential", "urban",
                  "industrial", "excluded", "unknown")
DENOMINATOR_CLASSES = ("residential", "urban", "industrial", "agricultural", "vacant", "open")


def _cats(*spans):
    out = set()
    for s in spans:
        lo, hi = (s, s) if isinstance(s, int) else s
        out.update(range(lo, hi + 1))
    return out


CATEGORY_CLASS_SPANS = {
    "residential": _cats((1, 9)),
    "urban": _cats((11, 39), (71, 79), 81, (83, 89), 91),
    "industrial": _cats((41, 49), 92),
    "agricultural": _cats((50, 69)),
    "vacant": _cats(0, 10, 40, 70, 80, 99),
    "open": _cats(82, 90, 96, 97),
    "excluded": _cats(93, 94, 95, 98),
}
CATEGORY_CLASS = {f"{n:02d}": cls for cls, ns in CATEGORY_CLASS_SPANS.items() for n in ns}
AG_CATEGORIES = frozenset(f"{n:02d}" for n in range(50, 70))


def category_class(cat):
    """2-digit DOR category (or None) -> class name."""
    return CATEGORY_CLASS.get(cat, "unknown") if cat else "unknown"


# --- code normalization --------------------------------------------------------
# The per-county formats and normalize_code live in dor_codes.py (stdlib only)
# so the web app can share them; re-exported here for existing callers.
from dor_codes import (  # noqa: E402,F401
    COUNTY_CODE_FORMATS, DOR_ROLL_FORMAT, county_code_format, normalize_code,
)


def resolve_category(county_code, dor_code, county_fmt, priority="dor"):
    """(category, source) from the two candidate codes, in priority order."""
    tries = [("dor_roll", dor_code, DOR_ROLL_FORMAT), ("county", county_code, county_fmt)]
    if priority == "county":
        tries.reverse()
    for source, code, fmt in tries:
        cat = normalize_code(code, fmt)
        if cat is not None:
            return cat, source
    return None, None


# --- agricultural zoning ------------------------------------------------------
def norm_zoning(code):
    return " ".join(str(code).split()).upper() if code is not None else ""


def load_ag_zoning(path):
    """county -> frozenset of normalized agricultural zoning codes. A missing
    file yields {} (zoning test disabled) with a warning."""
    path = Path(path)
    if not path.exists():
        log.warning("ag zoning file %s not found: agricultural zoning test disabled", path)
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    counties = data.get("counties", {}) if isinstance(data, dict) else {}
    return {c: frozenset(norm_zoning(k) for k in codes if norm_zoning(k))
            for c, codes in counties.items() if isinstance(codes, dict)}


def is_ag_zoning(code, ag_codes):
    """True when the zoning code (or any part of a comma-joined list such as
    Manatee's "A,A-1,PD-R") is an agricultural code for the county."""
    if not ag_codes or code is None:
        return False
    n = norm_zoning(code)
    if not n:
        return False
    if n in ag_codes:
        return True
    return any(norm_zoning(p) in ag_codes for p in n.split(",") if "," in n)


# --- database -----------------------------------------------------------------
def open_source(path):
    """Read-only connection to the features database. mode=ro means SQLite
    refuses every write, and query_only makes that doubly sure."""
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"source database not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only = 1")
    return conn


OUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    county TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | done | skipped | failed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    seconds REAL,
    source_db TEXT,
    params_json TEXT,
    n_parcels INTEGER,
    n_parcels_coded INTEGER,
    n_ag INTEGER,
    n_ag_land_use INTEGER,
    n_ag_zoning INTEGER,
    n_evaluated INTEGER,
    n_surrounded INTEGER,
    n_low_coverage INTEGER,
    code_sources_json TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_county ON runs(county, run_id);
CREATE TABLE IF NOT EXISTS results (
    county TEXT NOT NULL,
    feature_id INTEGER NOT NULL,
    feature_key TEXT,
    land_use_code TEXT,
    dor_use_code TEXT,
    dor_category TEXT,
    code_source TEXT,
    zoning_code TEXT,
    zoning_source TEXT,
    ag_by_land_use INTEGER,
    ag_by_zoning INTEGER,
    ag_reason TEXT,
    acreage REAL,
    parcel_acres REAL,
    ring_acres REAL,
    coverage REAL,
    classified_frac REAL,
    share_residential REAL,
    share_urban REAL,
    share_industrial REAL,
    share_agricultural REAL,
    share_vacant REAL,
    share_open REAL,
    excluded_frac REAL,
    unknown_frac REAL,
    developed_share REAL,
    surrounded INTEGER,
    low_coverage INTEGER,
    n_neighbors INTEGER,
    status TEXT,
    lon REAL,
    lat REAL,
    run_id INTEGER,
    PRIMARY KEY (county, feature_id)
);
CREATE INDEX IF NOT EXISTS idx_results_surrounded ON results(county, surrounded);
"""
RESULT_COLUMNS = (
    "county", "feature_id", "feature_key", "land_use_code", "dor_use_code", "dor_category",
    "code_source", "zoning_code", "zoning_source", "ag_by_land_use", "ag_by_zoning", "ag_reason",
    "acreage", "parcel_acres", "ring_acres", "coverage", "classified_frac",
    "share_residential", "share_urban", "share_industrial", "share_agricultural",
    "share_vacant", "share_open", "excluded_frac", "unknown_frac", "developed_share",
    "surrounded", "low_coverage", "n_neighbors", "status", "lon", "lat", "run_id",
)


def open_output(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(OUT_SCHEMA)
    conn.commit()
    return conn


def list_counties(src):
    """Counties with parcels, via an index skip-scan (one seek per county)
    rather than a DISTINCT over ~10M rows."""
    rows = src.execute("""
        WITH RECURSIVE c(x) AS (
            SELECT MIN(county) FROM features
            UNION ALL
            SELECT (SELECT MIN(county) FROM features WHERE county > c.x) FROM c WHERE c.x IS NOT NULL)
        SELECT x FROM c WHERE x IS NOT NULL""").fetchall()
    return [r[0] for r in rows if has_dataset(src, r[0], "parcels")]


def has_dataset(src, county, dataset_type):
    return src.execute("SELECT 1 FROM features WHERE county = ? AND dataset_type = ? LIMIT 1",
                       (county, dataset_type)).fetchone() is not None


def feature_columns(src):
    return {r[1] for r in src.execute("PRAGMA table_info(features)")}


# --- geometry -----------------------------------------------------------------
_TO_PROJ = Transformer.from_crs("EPSG:4326", PROJECTED_CRS, always_xy=True)
_TO_WGS = Transformer.from_crs(PROJECTED_CRS, "EPSG:4326", always_xy=True)


def _apply(transformer):
    def f(xy):
        x, y = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])
    return f


def parse_geometries(cells):
    """Stored geometry cells (zlib bytes / JSON text / None) -> array of
    projected, valid, polygonal shapely geometries (None where unusable)."""
    texts = np.array([etl.decode_json(c) for c in cells], dtype=object)
    geoms = shapely.from_geojson(texts, on_invalid="ignore")
    geoms = shapely.transform(geoms, _apply(_TO_PROJ))
    return clean_geometries(geoms)


def clean_geometries(geoms):
    present = ~shapely.is_missing(geoms)
    bad = present & ~shapely.is_valid(geoms)
    if bad.any():
        # make_valid can return collections holding stray lines; buffer(0)
        # keeps only the polygonal area.
        geoms[bad] = shapely.buffer(shapely.make_valid(geoms[bad]), 0)
    polygonal = np.isin(shapely.get_type_id(geoms), (3, 6, 7))  # Polygon, MultiPolygon, Collection
    area = shapely.area(geoms)
    geoms[~(polygonal & (area > 0))] = None
    return geoms


# --- per-county processing ------------------------------------------------------
def load_parcels(src, county, params, has_dor_col):
    """Stream the county's parcels (only the needed columns, no ORDER BY)."""
    dor_col = "dor_use_code" if has_dor_col else "NULL"
    cur = src.execute(
        f"SELECT id, feature_key, land_use_code, {dor_col}, zoning_code, acreage, geometry_geojson "
        "FROM features WHERE county = ? AND dataset_type = 'parcels'", (county,))
    fmt = county_code_format(county)
    ids, keys, lu, dor, zc, acres, cats, srcs, parts = [], [], [], [], [], [], [], [], []
    while True:
        rows = cur.fetchmany(FETCH_ROWS)
        if not rows:
            break
        for r in rows:
            ids.append(r[0]); keys.append(r[1]); lu.append(r[2]); dor.append(r[3])
            zc.append(r[4]); acres.append(r[5])
            cat, source = resolve_category(r[2], r[3], fmt, params["code_priority"])
            cats.append(cat); srcs.append(source)
        parts.append(parse_geometries([r[6] for r in rows]))
    geoms = np.concatenate(parts) if parts else np.array([], dtype=object)
    return dict(ids=np.array(ids, dtype=np.int64), keys=keys, lu=lu, dor=dor, zc=zc, acres=acres,
                cats=cats, srcs=srcs, geoms=geoms)


def zoning_from_layer(src, county, ag_codes, geoms, need):
    """For parcels flagged in `need`, the agricultural zoning code of the
    zoning polygon containing each parcel's representative point, or None.
    Only agricultural polygons are decoded (nothing else can matter)."""
    out = [None] * len(geoms)
    idx = np.flatnonzero(need & ~shapely.is_missing(geoms))
    if not len(idx):
        return out
    codes, cells = [], []
    cur = src.execute("SELECT zoning_code, geometry_geojson FROM features "
                      "WHERE county = ? AND dataset_type = 'zoning'", (county,))
    for code, cell in cur:
        if is_ag_zoning(code, ag_codes):
            codes.append(code)
            cells.append(cell)
    if not codes:
        return out
    zgeoms = parse_geometries(cells)
    tree = shapely.STRtree(zgeoms)
    pts = shapely.point_on_surface(geoms[idx])
    hit_in, hit_tree = tree.query(pts, predicate="intersects")
    for i, t in zip(hit_in, hit_tree):
        j = idx[i]
        if out[j] is None:
            out[j] = codes[t]
    return out


OVERLAP_TOLERANCE_M = 0.5   # digitizing slivers thinner than this are ignored


def find_overlapping(geoms, tree):
    """Boolean mask of parcels whose interior overlaps another parcel:
    stacked condo/duplicate polygons, parcels inside parcels. Only these
    need a geometric union in the ring; everything else is simply summed.

    Each parcel is shrunk by OVERLAP_TOLERANCE_M and tested against the
    unshrunk parcels with 'intersects': neighbours that merely share an edge
    (or overlap by a digitizing sliver) no longer touch, real overlaps
    still do. ~7x faster than the 'overlaps' + 'contains' predicates."""
    shrunk = shapely.buffer(geoms, -OVERLAP_TOLERANCE_M, quad_segs=1, join_style="mitre")
    a, b = tree.query(shrunk, predicate="intersects")
    m = a != b
    flag = np.zeros(len(geoms), dtype=bool)
    flag[a[m]] = True
    flag[b[m]] = True
    return flag


def ring_stats(i, geoms, areas_all, cls, overlapping, tree, ring_m):
    """Ring area, covered area and per-class areas (m^2, overlap-free) of
    the neighbours in parcel i's ring, and the neighbour count."""
    g = geoms[i]
    ring = shapely.difference(shapely.buffer(g, ring_m, quad_segs=8), g)
    ring_area = ring.area
    areas_by_class = np.zeros(len(CLASSES))
    cand = tree.query(ring, predicate="intersects")
    cand = cand[cand != i]
    if not len(cand):
        return ring_area, 0.0, areas_by_class, 0
    # neighbours wholly inside the ring keep their own geometry and area;
    # only those crossing its edge are clipped
    shapely.prepare(ring)
    inside = shapely.contains(ring, geoms[cand])
    pieces = geoms[cand].copy()
    areas = areas_all[cand].copy()
    edge = ~inside
    if edge.any():
        pieces[edge] = shapely.intersection(geoms[cand[edge]], ring)
        areas[edge] = shapely.area(pieces[edge])
    keep = areas > 1e-3                      # touching-only neighbours
    cand, pieces, areas = cand[keep], pieces[keep], areas[keep]
    if not len(cand):
        return ring_area, 0.0, areas_by_class, 0
    ccls = cls[cand]
    stacked = overlapping[cand]
    np.add.at(areas_by_class, ccls[~stacked], areas[~stacked])
    covered = float(areas[~stacked].sum())
    if stacked.any():
        # overlapping pieces: union within each class, and hand area
        # contested between classes to the first in CLASS_PRIORITY
        sp, sc = pieces[stacked], ccls[stacked]
        taken = None
        for name in CLASS_PRIORITY:
            sel = sp[sc == C[name]]
            if not len(sel):
                continue
            u = shapely.union_all(sel)
            areas_by_class[C[name]] += (u if taken is None else shapely.difference(u, taken)).area
            taken = u if taken is None else shapely.union(taken, u)
        covered += taken.area
    return ring_area, min(covered, ring_area), areas_by_class, int(len(cand))


def evaluate(ring_area, covered, areas, n_neighbors, params):
    denom = sum(areas[C[n]] for n in DENOMINATOR_CLASSES)
    developed_classes = ["residential", "urban", "industrial"]
    if params["count_vacant_as_developed"]:
        developed_classes.append("vacant")
    developed = sum(areas[C[n]] for n in developed_classes)
    out = {
        "ring_acres": ring_area / SQM_PER_ACRE,
        "coverage": covered / ring_area if ring_area > 0 else None,
        "classified_frac": denom / ring_area if ring_area > 0 else None,
        "excluded_frac": areas[C["excluded"]] / ring_area if ring_area > 0 else None,
        "unknown_frac": areas[C["unknown"]] / ring_area if ring_area > 0 else None,
        "n_neighbors": n_neighbors,
    }
    for n in DENOMINATOR_CLASSES:
        out[f"share_{n}"] = areas[C[n]] / denom if denom > 0 else None
    share = developed / denom if denom > 0 else None
    out["developed_share"] = share
    out["surrounded"] = None if share is None else int(share >= params["threshold"] - 1e-12)
    cov = out["coverage"] or 0.0
    out["low_coverage"] = int(cov < params["min_coverage"])
    out["status"] = "ok" if denom > 0 else "no_classified_neighbors"
    return out


def county_params(args, county, ag_zoning):
    codes = sorted(ag_zoning.get(county, ()))
    return {
        "ring_ft": args.ring_ft, "threshold": args.threshold,
        "count_vacant_as_developed": bool(args.count_vacant_as_developed),
        "min_coverage": args.min_coverage, "min_code_coverage": args.min_code_coverage,
        "code_priority": args.code_priority, "limit": args.limit,
        "code_format": county_code_format(county),
        "ag_zoning_codes_sha1": hashlib.sha1("\n".join(codes).encode()).hexdigest() if codes else None,
    }


def process_county(src_path, county, params, ag_codes):
    """Evaluate one county. Pure function of its inputs (runs in a worker
    process with --workers); returns (result rows as dicts, stats dict)."""
    setup_logging()  # worker processes start without handlers
    t0 = time.time()
    src = open_source(src_path)
    try:
        cols = feature_columns(src)
        p = load_parcels(src, county, params, "dor_use_code" in cols)
        n = len(p["ids"])
        t_load = time.time() - t0
        code_sources = {}
        for s in p["srcs"]:
            code_sources[s or "none"] = code_sources.get(s or "none", 0) + 1
        n_coded = n - code_sources.get("none", 0)
        stats = dict(n_parcels=n, n_parcels_coded=n_coded, code_sources=code_sources,
                     n_ag=0, n_ag_land_use=0, n_ag_zoning=0, n_evaluated=0, n_surrounded=0,
                     n_low_coverage=0, status="done", note=None)
        if n == 0 or n_coded / n < params["min_code_coverage"]:
            samples = sorted({str(x) for x in p["lu"][:5000] if x is not None})[:10]
            stats["status"] = "skipped"
            stats["note"] = (f"only {n_coded:,} of {n:,} parcels have a DOR-based land-use code "
                             f"(county format {params['code_format']!r}, sample county codes {samples}); "
                             "neighbours cannot be classified")
            log.warning("%s: skipped - %s", county, stats["note"])
            return [], stats
        log.info("%s: loaded %s parcels in %.1fs (%s)", county, f"{n:,}", t_load, code_sources)

        geoms = p["geoms"]
        cats = p["cats"]
        cls = np.array([C[category_class(c)] for c in cats], dtype=np.int64)
        ag_lu = np.array([c in AG_CATEGORIES for c in cats], dtype=bool)

        # zoning test
        zoning_code = list(p["zc"])
        zoning_source = [("parcel" if norm_zoning(z) else None) for z in zoning_code]
        ag_zone = np.zeros(n, dtype=bool)
        if ag_codes:
            ag_zone = np.array([is_ag_zoning(z, ag_codes) for z in zoning_code], dtype=bool)
            blank = np.array([not norm_zoning(z) for z in zoning_code], dtype=bool)
            if blank.any() and has_dataset(src, county, "zoning"):
                tz = time.time()
                from_layer = zoning_from_layer(src, county, ag_codes, geoms, blank)
                for j, code in enumerate(from_layer):
                    if code is not None:
                        zoning_code[j] = code
                        zoning_source[j] = "layer"
                        ag_zone[j] = True
                log.info("%s: zoning layer lookup for %s parcels in %.1fs", county,
                         f"{int(blank.sum()):,}", time.time() - tz)

        ag_idx = np.flatnonzero(ag_lu | ag_zone)
        stats.update(n_ag=int(len(ag_idx)), n_ag_land_use=int(ag_lu.sum()), n_ag_zoning=int(ag_zone.sum()))
        if params["limit"]:
            ag_idx = ag_idx[:params["limit"]]
        tree = shapely.STRtree(geoms)
        ring_m = params["ring_ft"] * FT_TO_M
        areas_all = np.nan_to_num(shapely.area(geoms))
        overlapping = np.zeros(n, dtype=bool)
        if len(ag_idx):
            to = time.time()
            overlapping = find_overlapping(geoms, tree)
            log.info("%s: %s parcels overlap another (stacked/duplicate) - found in %.1fs", county,
                     f"{int(overlapping.sum()):,}", time.time() - to)
        # representative point of each ag parcel, back in WGS84 for mapping
        lonlat = np.full((len(ag_idx), 2), np.nan)
        if len(ag_idx):
            reps = shapely.point_on_surface(geoms[ag_idx])
            present = ~shapely.is_missing(reps)
            if present.any():
                lon, lat = _TO_WGS.transform(shapely.get_x(reps[present]), shapely.get_y(reps[present]))
                lonlat[present] = np.column_stack([lon, lat])

        rows = []
        t_eval, last_log = time.time(), time.time()
        for k, i in enumerate(ag_idx):
            reason = "+".join(r for r, on in (("land_use", ag_lu[i]), ("zoning", ag_zone[i])) if on)
            row = {
                "county": county, "feature_id": int(p["ids"][i]), "feature_key": p["keys"][i],
                "land_use_code": p["lu"][i], "dor_use_code": p["dor"][i], "dor_category": cats[i],
                "code_source": p["srcs"][i], "zoning_code": zoning_code[i],
                "zoning_source": zoning_source[i], "ag_by_land_use": int(ag_lu[i]),
                "ag_by_zoning": int(ag_zone[i]), "ag_reason": reason, "acreage": p["acres"][i],
                "lon": None if np.isnan(lonlat[k, 0]) else float(lonlat[k, 0]),
                "lat": None if np.isnan(lonlat[k, 1]) else float(lonlat[k, 1]),
            }
            g = geoms[i]
            if g is None:
                row.update(status="no_geometry", low_coverage=1)
            else:
                row["parcel_acres"] = g.area / SQM_PER_ACRE
                row.update(evaluate(*ring_stats(i, geoms, areas_all, cls, overlapping, tree, ring_m),
                                    params))
                stats["n_evaluated"] += 1
                stats["n_surrounded"] += int(row["surrounded"] == 1)
                stats["n_low_coverage"] += row["low_coverage"]
            rows.append(row)
            if time.time() - last_log > 30:
                last_log = time.time()
                rate = (k + 1) / (last_log - t_eval)
                log.info("%s: %s/%s ag parcels (%.0f/s, ~%.0fs left)", county, f"{k + 1:,}",
                         f"{len(ag_idx):,}", rate, (len(ag_idx) - k - 1) / rate)
        log.info("%s: %s ag parcels (%s by land use, %s by zoning), %s evaluated in %.1fs, "
                 "%s surrounded, %s low coverage", county, f"{stats['n_ag']:,}",
                 f"{stats['n_ag_land_use']:,}", f"{stats['n_ag_zoning']:,}",
                 f"{stats['n_evaluated']:,}", time.time() - t_eval,
                 f"{stats['n_surrounded']:,}", f"{stats['n_low_coverage']:,}")
        return rows, stats
    finally:
        src.close()


# --- orchestration ------------------------------------------------------------
def setup_logging():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_run(out, county):
    return out.execute("SELECT status, params_json FROM runs WHERE county = ? ORDER BY run_id DESC LIMIT 1",
                       (county,)).fetchone()


def start_run(out, county, params, src_path):
    cur = out.execute("INSERT INTO runs (county, status, started_at, source_db, params_json) "
                      "VALUES (?, 'running', ?, ?, ?)",
                      (county, _now(), str(src_path), json.dumps(params, sort_keys=True)))
    out.commit()
    return cur.lastrowid


def finish_run(out, run_id, county, rows, stats, seconds):
    """Replace this county's results (scoped to the county, output DB only)
    and close the run, in one transaction."""
    with out:
        # a skipped county also loses stale rows from an earlier run
        out.execute("DELETE FROM results WHERE county = ?", (county,))
        if rows:
            placeholders = ", ".join("?" * len(RESULT_COLUMNS))
            out.executemany(
                f"INSERT INTO results ({', '.join(RESULT_COLUMNS)}) VALUES ({placeholders})",
                ([{**r, "run_id": run_id}.get(c) for c in RESULT_COLUMNS] for r in rows))
        out.execute("""UPDATE runs SET status = ?, finished_at = ?, seconds = ?, n_parcels = ?,
            n_parcels_coded = ?, n_ag = ?, n_ag_land_use = ?, n_ag_zoning = ?, n_evaluated = ?,
            n_surrounded = ?, n_low_coverage = ?, code_sources_json = ?, note = ? WHERE run_id = ?""",
                    (stats["status"], _now(), round(seconds, 2), stats["n_parcels"],
                     stats["n_parcels_coded"], stats["n_ag"], stats["n_ag_land_use"],
                     stats["n_ag_zoning"], stats["n_evaluated"], stats["n_surrounded"],
                     stats["n_low_coverage"], json.dumps(stats["code_sources"], sort_keys=True),
                     stats["note"], run_id))


def fail_run(out, run_id, error):
    with out:
        out.execute("UPDATE runs SET status = 'failed', finished_at = ?, note = ? WHERE run_id = ?",
                    (_now(), str(error)[:2000], run_id))


def export_csv(out, path):
    cur = out.execute(f"SELECT {', '.join(RESULT_COLUMNS)} FROM results")
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RESULT_COLUMNS)
        while True:
            rows = cur.fetchmany(10000)
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
    log.info("wrote %s rows to %s", f"{n:,}", path)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="source features DB (default: FL_COUNTY_DB / etl.DB_PATH); opened read-only")
    ap.add_argument("--out", default=None, help="output SQLite (default: ag_encroachment.db next to the source DB)")
    ap.add_argument("--county", action="append", help="county to process (repeatable; default all)")
    ap.add_argument("--ring-ft", type=float, default=660.0, help="ring width in feet (default 660)")
    ap.add_argument("--threshold", type=float, default=0.75, help="developed share for 'surrounded' (default 0.75)")
    ap.add_argument("--count-vacant-as-developed", action="store_true",
                    help="count vacant land (DOR 00/10/40/70/80/99) as developed")
    ap.add_argument("--min-coverage", type=float, default=0.5,
                    help="flag low_coverage when less of the ring than this is covered by parcels (default 0.5)")
    ap.add_argument("--min-code-coverage", type=float, default=0.5,
                    help="skip a county when fewer of its parcels than this carry a DOR-based code (default 0.5)")
    ap.add_argument("--code-priority", choices=("dor", "county"), default="dor",
                    help="which land-use code wins when both exist: the DOR roll (default) or the county's")
    ap.add_argument("--ag-zoning-json", default=str(DEFAULT_AG_ZONING_PATH),
                    help="agricultural zoning codes file (default scripts/ag_zoning_codes.json)")
    ap.add_argument("--resume", action="store_true",
                    help="skip counties whose latest run finished with the same parameters")
    ap.add_argument("--limit", type=int, default=None, help="evaluate at most N ag parcels per county (trials)")
    ap.add_argument("--workers", type=int, default=1, help="counties processed in parallel (default 1)")
    ap.add_argument("--csv", default=None, help="also export the whole results table to this CSV")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging()
    src_path = Path(args.db or etl.DB_PATH).resolve()
    out_path = Path(args.out).resolve() if args.out else src_path.parent / "ag_encroachment.db"
    if out_path == src_path:
        raise SystemExit("--out must not be the source database")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # SQLite temp files (sorts, big transactions) go next to the output, not
    # to C:\...\Temp. Restored afterwards so callers (tests) are unaffected.
    saved_env = {k: os.environ.get(k) for k in ("TMP", "TEMP", "SQLITE_TMPDIR")}
    for k in saved_env:
        os.environ[k] = str(out_path.parent)
    try:
        return _run(args, src_path, out_path)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run(args, src_path, out_path):
    t_all = time.time()
    ag_zoning = load_ag_zoning(args.ag_zoning_json)
    src = open_source(src_path)
    try:
        counties = args.county or list_counties(src)
    finally:
        src.close()
    out = open_output(out_path)
    log.info("source %s (read-only) -> %s; %d counties", src_path, out_path, len(counties))

    todo = []
    for county in counties:
        params = county_params(args, county, ag_zoning)
        if args.resume:
            last = latest_run(out, county)
            if last and last[0] in ("done", "skipped") and json.loads(last[1]) == params:
                log.info("%s: already %s with these parameters - skipping (--resume)", county, last[0])
                continue
            if last and last[0] in ("done", "skipped"):
                log.info("%s: parameters changed since the last run - reprocessing", county)
        todo.append((county, params))

    summary = {}

    def record(county, run_id, t0, fut_result=None, error=None):
        if error is not None:
            log.error("%s: failed: %s", county, error)
            fail_run(out, run_id, error)
            summary[county] = "failed"
            return
        rows, stats = fut_result
        finish_run(out, run_id, county, rows, stats, time.time() - t0)
        summary[county] = stats["status"]

    if args.workers <= 1:
        for county, params in todo:
            run_id, t0 = start_run(out, county, params, src_path), time.time()
            try:
                res = process_county(src_path, county, params, ag_zoning.get(county, frozenset()))
            except Exception as exc:  # noqa: BLE001 - record and carry on with the next county
                record(county, run_id, t0, error=repr(exc))
                continue
            record(county, run_id, t0, res)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {}
            for county, params in todo:
                run_id = start_run(out, county, params, src_path)
                futs[pool.submit(process_county, src_path, county, params,
                                 ag_zoning.get(county, frozenset()))] = (county, run_id, time.time())
            for fut in as_completed(futs):
                county, run_id, t0 = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    record(county, run_id, t0, error=repr(exc))
                    continue
                record(county, run_id, t0, res)

    if args.csv:
        export_csv(out, args.csv)
    out.close()
    log.info("finished %d counties in %.0fs: %s", len(summary), time.time() - t_all,
             {s: sum(1 for v in summary.values() if v == s) for s in set(summary.values())})
    return summary


if __name__ == "__main__":
    main()
