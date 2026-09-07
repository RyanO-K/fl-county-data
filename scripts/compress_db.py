"""One-off migration: compress geometry_geojson / attributes_json in place.

Rows written before the codec was introduced hold plain JSON text; this
rewrites them as zlib blobs (etl.encode_json) in id-ordered batches, then
optionally VACUUMs to hand the freed pages back to the filesystem. Takes the
run lock, so stop load_all / the daily refresh first.

    python scripts/compress_db.py            # compress, report
    python scripts/compress_db.py --vacuum   # ... then VACUUM (needs free space ~= DB size)
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402


def main():
    vacuum = "--vacuum" in sys.argv
    other = etl.acquire_run_lock("compress_db")
    if other:
        print(f"[SKIP] '{other.get('name')}' (pid {other.get('pid')}) holds the database; stop it first")
        sys.exit(2)
    try:
        conn = etl.get_conn()
        before = etl.DB_PATH.stat().st_size
        t0 = time.time()
        last = 0
        scanned = rewritten = 0
        in_bytes = out_bytes = 0
        while True:
            rows = conn.execute(
                "SELECT id, geometry_geojson, attributes_json FROM features WHERE id > ? ORDER BY id LIMIT 5000",
                (last,)).fetchall()
            if not rows:
                break
            ups = []
            for fid, gj, aj in rows:
                last = fid
                scanned += 1
                if not isinstance(gj, str) and not isinstance(aj, str):
                    continue  # already compressed (or NULL)
                g = etl.encode_json(gj) if isinstance(gj, str) else gj
                a = etl.encode_json(aj) if isinstance(aj, str) else aj
                in_bytes += len(gj or b"") + len(aj or b"")
                out_bytes += len(g or b"") + len(a or b"")
                ups.append((g, a, fid))
            if ups:
                conn.executemany("UPDATE features SET geometry_geojson=?, attributes_json=? WHERE id=?", ups)
                conn.commit()
                rewritten += len(ups)
            if scanned % 100000 < 5000:
                print(f"  {scanned:,} scanned, {rewritten:,} rewritten, {time.time()-t0:.0f}s", flush=True)
        print(f"compressed {rewritten:,} of {scanned:,} rows: {in_bytes/1048576:.0f} MB -> {out_bytes/1048576:.0f} MB "
              f"of JSON in {time.time()-t0:.0f}s", flush=True)
        if vacuum:
            t1 = time.time()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None
            conn.execute("VACUUM")
            print(f"VACUUM done in {time.time()-t1:.0f}s", flush=True)
        conn.close()
        print(f"file size {before/1048576:.0f} MB -> {etl.DB_PATH.stat().st_size/1048576:.0f} MB")
    finally:
        etl.release_run_lock()


if __name__ == "__main__":
    main()
