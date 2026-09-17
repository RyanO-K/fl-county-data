"""One-off restore of the `features` table from scan_features.py output.

Background (2026-09-16): an unscoped DELETE emptied `features` in the live
database. The deleted pages stayed on SQLite's free list, and
scan_features.py carved the rows back out of a file snapshot into a scratch
database (table `features` with the live column order plus `src_page`).

This script:
  1. reports what the scan holds, by county and dataset type;
  2. inserts the rows into the target database's `features`, keeping one row
     per (county, dataset_type, feature_key): the latest last_synced_at, then
     the highest source page; test rows (last_synced_at = 't') are skipped;
  3. prints per-county counts for comparison with the pre-incident numbers.

Usage:
    python scripts/restore_features.py --scanned D:/fl-county-data/backup/scanned_features.db --report
    python scripts/restore_features.py --scanned ... --apply [--db path]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402

WHERE = "last_synced_at IS NOT 't' AND feature_key <> ''"


def feature_columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(features)")]


def report(rec):
    print("scanned rows:", rec.execute("SELECT COUNT(*) FROM features").fetchone()[0])
    print("distinct ids:", rec.execute("SELECT COUNT(DISTINCT id) FROM features").fetchone()[0])
    print("distinct (county, dataset_type, feature_key):",
          rec.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT county, dataset_type, feature_key FROM features WHERE {WHERE})").fetchone()[0])
    for county, dt, n in rec.execute(
            f"SELECT county, dataset_type, COUNT(DISTINCT feature_key) FROM features WHERE {WHERE} GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {county:14s} {dt:16s} {n:>9,}")


def apply(scanned_path, target):
    cols = feature_columns(target)
    assert cols[0] == "id" and cols[1] == "county", cols
    target.execute("ATTACH DATABASE ? AS rec", (str(scanned_path),))
    col_list = ", ".join(cols)
    t0 = time.time()
    total, distinct = target.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT county || '|' || dataset_type || '|' || feature_key) "
        f"FROM rec.features WHERE {WHERE}").fetchone()
    if total == distinct:
        # No older images to resolve: a straight insert avoids a 14 GB temp
        # sort (the window-function path filled the temp drive).
        src = f"SELECT {col_list} FROM rec.features WHERE {WHERE}"
    else:
        print(f"{total - distinct:,} duplicate images; resolving by last_synced_at then source page")
        src = f"""SELECT {col_list} FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY county, dataset_type, feature_key
                                         ORDER BY last_synced_at DESC, src_page DESC) AS rn
            FROM rec.features WHERE {WHERE}) WHERE rn = 1"""
    # INSERT OR IGNORE guards the unique (county, dataset_type, feature_key)
    # and the id primary key against anything already in the target.
    n = target.execute(f"INSERT OR IGNORE INTO features ({col_list}) {src}").rowcount
    target.commit()
    print(f"inserted {n:,} feature rows in {(time.time() - t0) / 60:.1f} min")
    target.execute("DETACH DATABASE rec")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scanned", required=True)
    ap.add_argument("--db", default=str(etl.DB_PATH))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rec = sqlite3.connect(f"file:{Path(a.scanned).as_posix()}?mode=ro", uri=True)
    if a.report:
        report(rec)
    rec.close()
    if a.apply:
        target = sqlite3.connect(a.db, timeout=120)
        print("target:", a.db, "features before:", target.execute("SELECT COUNT(*) FROM features").fetchone()[0])
        apply(a.scanned, target)
        print("features after:", target.execute("SELECT COUNT(*) FROM features").fetchone()[0])
        for county, dt, n in target.execute(
                "SELECT county, dataset_type, COUNT(*) FROM features GROUP BY 1, 2 ORDER BY 1, 2"):
            print(f"  {county:14s} {dt:16s} {n:>9,}")
        target.close()


if __name__ == "__main__":
    main()
