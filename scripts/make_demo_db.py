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
        g = json.loads(gj)
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
    return json.dumps(g, separators=(",", ":"))


def county_sizes(src):
    rows = src.execute(
        "SELECT county, SUM(COALESCE(length(geometry_geojson),0) + COALESCE(length(attributes_json),0)) "
        "FROM features GROUP BY county").fetchall()
    feat = {c: n for c, n in rows}
    vals = dict(src.execute("SELECT county, COUNT(*)*110 FROM parcel_values GROUP BY county").fetchall())
    counties = set(feat) | set(vals)
    # geometry shrinks ~3x after rounding; attributes/values do not
    return {c: feat.get(c, 0) / 2.5 + vals.get(c, 0) for c in counties}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget-mb", type=float, default=350)
    ap.add_argument("--counties", help="comma-separated county keys (overrides budget selection)")
    ap.add_argument("--source", default=str(etl.DB_PATH))
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{Path(args.source).as_posix()}?mode=ro", uri=True, timeout=120)
    if args.counties:
        chosen = [c.strip() for c in args.counties.split(",") if c.strip()]
    else:
        sizes = county_sizes(src)
        budget = args.budget_mb * 1024 * 1024
        chosen, used = [], 0
        for c, est in sorted(sizes.items(), key=lambda kv: kv[1]):
            if used + est > budget:
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
    n = 0
    cur = src.execute(f"SELECT {', '.join(cols)} FROM features WHERE county IN ({ph})", chosen)
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        rows = [tuple(simplify_geom(v) if i == gi else v for i, v in enumerate(r)) for r in rows]
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
        ("note", "Demo subset: whole counties, geometry rounded to 6 decimals."),
    ])
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    print(f"wrote {out} ({out.stat().st_size/1048576:.0f} MB)")


if __name__ == "__main__":
    main()
