"""Build a compact demo database for the public deployment.

The full database (all 67 counties, ~10.5 M valued parcels, boundaries for
every county) is far too large for a free web host, so the public site runs
on a subset built by this script:

  * every table/column/index the app expects, same schema as the real DB;
  * whole counties, chosen smallest-first until a size budget is reached
    (so each included county is complete: parcels, zoning, land use, values);
  * geometry coordinates rounded to 6 decimals (~10 cm) and consecutive
    duplicate points dropped, which cuts boundary size several-fold with no
    visible change at map scale.

Usage:
    python scripts/make_demo_db.py --out demo/fl_county_demo.db --budget-mb 350
    python scripts/make_demo_db.py --counties orange,polk --out ...   # explicit

The output is meant to be gzipped and attached to a GitHub Release; the
Render build step downloads it (see fetch_demo_db.py).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402
import dor_values  # noqa: E402

ALWAYS_TABLES = ("sync_log",)


def simplify_geom(gj, places=6):
    """Round coordinates and drop consecutive duplicates. Pure Python, fast
    enough for a few hundred thousand polygons."""
    if not gj:
        return gj
    try:
        g = json.loads(etl.decode_json(gj))
    except (TypeError, ValueError):
        return gj

    def ring(coords):
        out = []
        last = None
        for pt in coords:
            p = (round(pt[0], places), round(pt[1], places))
            if p != last:
                out.append([p[0], p[1]])
                last = p
        if len(out) > 1 and out[0] != out[-1]:
            out.append(out[0])
        return out

    t = g.get("type")
    if t == "Polygon":
        g["coordinates"] = [ring(r) for r in g["coordinates"]]
    elif t == "MultiPolygon":
        g["coordinates"] = [[ring(r) for r in poly] for poly in g["coordinates"]]
    return etl.encode_json(g)


def recompress_attrs(aj):
    """Attributes: decode whatever the source row holds and store compressed."""
    return etl.encode_json(etl.decode_json(aj)) if aj is not None else None


def county_sizes(src, require_parcels=False):
    """Estimated on-disk bytes per county in the demo. Source rows may be plain
    text (pre-codec) or already compressed; measured ratios: rounded+zlib
    geometry ~4.5x, zlib attributes ~2.3x, plus ~1.3x SQLite page overhead;
    parcel_values rows are ~320 B each including their three indexes."""
    geo = {}
    attr = {}
    for county, g, a, gsz, asz in src.execute(
            "SELECT county, geometry_geojson, attributes_json, "
            "SUM(COALESCE(length(geometry_geojson),0)), SUM(COALESCE(length(attributes_json),0)) "
            "FROM features GROUP BY county"):
        # a blob sample tells us whether that county's rows are already compressed
        geo[county] = gsz if isinstance(g, bytes) else gsz / 4.5
        attr[county] = asz if isinstance(a, bytes) else asz / 2.3
    vals = dict(src.execute("SELECT county, COUNT(*)*320 FROM parcel_values GROUP BY county").fetchall())
    counties = set(geo) | set(vals)
    if require_parcels:
        # complete = at least 90% as many boundary rows as the county has valued parcels
        counties &= {c for (c,) in src.execute(
            "SELECT f.county FROM features f WHERE f.dataset_type='parcels' GROUP BY f.county "
            "HAVING COUNT(*) >= 0.9 * (SELECT COUNT(*) FROM parcel_values pv WHERE pv.county = f.county)")}
    return {c: (geo.get(c, 0) + attr.get(c, 0)) * 1.3 + vals.get(c, 0) for c in counties}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget-mb", type=float, default=350)
    ap.add_argument("--counties", help="comma-separated county keys (overrides budget selection)")
    ap.add_argument("--source", default=str(etl.DB_PATH))
    ap.add_argument("--require-parcels", action="store_true",
                    help="budget mode: only consider counties whose parcel boundaries are loaded")
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{Path(args.source).as_posix()}?mode=ro", uri=True, timeout=120)
    if args.counties:
        chosen = [c.strip() for c in args.counties.split(",") if c.strip()]
    else:
        sizes = county_sizes(src, args.require_parcels)
        budget = args.budget_mb * 1024 * 1024
        chosen, used = [], 0
        # Priority metros (Tampa Bay, Orlando) first in listed order, then smallest-first.
        rank = {c: i for i, c in enumerate(etl.PRIORITY_COUNTIES)}
        order = sorted(sizes.items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[1]))
        for c, est in order:
            if used + est > budget:
                print(f"  skip {c}: est {est/1048576:.0f} MB would exceed budget ({used/1048576:.0f} MB used)")
                continue
            chosen.append(c)
            used += est
        print(f"selected {len(chosen)} counties, est {used/1048576:.0f} MB: {', '.join(sorted(chosen))}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    dst = sqlite3.connect(str(out))
    dst.execute("PRAGMA journal_mode=OFF")
    dst.execute("PRAGMA synchronous=OFF")

    # Recreate schema exactly as the app expects (etl + dor_values own it).
    for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index') AND sql IS NOT NULL "
                              "AND name NOT LIKE 'sqlite_%'"):
        dst.execute(sql)

    ph = ",".join("?" * len(chosen))
    cols = [r[1] for r in src.execute("PRAGMA table_info(features)")]
    gi = cols.index("geometry_geojson")
    ai = cols.index("attributes_json")
    n = 0
    cur = src.execute(f"SELECT {', '.join(cols)} FROM features WHERE county IN ({ph})", chosen)
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        rows = [tuple(simplify_geom(v) if i == gi else recompress_attrs(v) if i == ai else v
                      for i, v in enumerate(r)) for r in rows]
        dst.executemany(f"INSERT INTO features ({', '.join(cols)}) VALUES ({','.join('?'*len(cols))})", rows)
        n += len(rows)
    print(f"features: {n:,}")

    vcols = [r[1] for r in src.execute("PRAGMA table_info(parcel_values)")]
    cur = src.execute(f"SELECT {', '.join(vcols)} FROM parcel_values WHERE county IN ({ph})", chosen)
    m = 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        dst.executemany(f"INSERT INTO parcel_values ({', '.join(vcols)}) VALUES ({','.join('?'*len(vcols))})", rows)
        m += len(rows)
    print(f"parcel_values: {m:,}")

    scols = [r[1] for r in src.execute("PRAGMA table_info(sync_log)")]
    rows = src.execute(f"SELECT {', '.join(scols)} FROM sync_log WHERE county IN ({ph}) OR county='statewide'", chosen).fetchall()
    dst.executemany(f"INSERT INTO sync_log ({', '.join(scols)}) VALUES ({','.join('?'*len(scols))})", rows)

    dst.execute("CREATE TABLE IF NOT EXISTS demo_info (key TEXT PRIMARY KEY, value TEXT)")
    dst.executemany("INSERT INTO demo_info VALUES (?,?)", [
        ("counties", json.dumps(sorted(chosen))),
        ("built_from", "full 67-county database"),
        ("note", "Demo subset: whole counties, geometry rounded to 6 decimals, JSON columns zlib-compressed."),
    ])
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    print(f"wrote {out} ({out.stat().st_size/1048576:.0f} MB)")


if __name__ == "__main__":
    main()
