"""Bulk loader that drives the existing ETL (etl.sync_source) across all 67
counties, in two phases, several counties at a time.

Usage:
    python -u scripts/load_all.py phase1        # all non-parcel datasets, all counties
    python -u scripts/load_all.py phase2        # parcels: priority metros first, then smallest first

Counties run in parallel processes (COUNTY_WORKERS, env FL_COUNTY_WORKERS):
every county is a different server, so the wall clock is dominated by each
server's own speed, not ours. Processes rather than threads because the
per-row work (decode, round, acreage, compress, insert) is CPU-bound Python
and the GIL caps a threaded loader at about one core (~4-5 counties).
SQLite serialises the writes (WAL, one writer at a time) but each write is a
short batch, and the 60 s busy timeout covers the longest one (a metro
county's value join, ~15 s). Each worker process opens its own connection.
Within a county, page/batch fetches are themselves parallel
(etl.FETCH_WORKERS), so total in-flight requests are COUNTY_WORKERS x
FETCH_WORKERS, spread over COUNTY_WORKERS different servers.

This module only orchestrates: all HTTP/pagination/retry/format logic lives in
etl.sync_source, which is called unmodified.
"""
import ctypes
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl
import dor_values

SOURCES_PATH = Path(__file__).resolve().parent / "sources.json"

NON_PARCEL_TYPES = ("zoning", "land_use", "future_land_use")

MIN_FREE_BYTES = 2.0 * 1024 ** 3   # stop threshold: 2.0 GB free
BYTES_PER_PARCEL_EST = 1.5 * 1024  # conservative estimate for sizing decisions
COUNTY_WORKERS = int(os.environ.get("FL_COUNTY_WORKERS", "8"))


def free_bytes(drive=None):
    drive = drive or (etl.DB_PATH.drive + "\\")
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(drive, ctypes.pointer(free), None, None)
    return free.value


def load_sources():
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))


def _last_sync(conn, before, county, dataset_type):
    """The sync_log row sync_source just wrote (it logs success or failure itself)."""
    return conn.execute(
        "SELECT status, rows_fetched FROM sync_log WHERE id > ? "
        "AND county=? AND dataset_type=? ORDER BY id DESC LIMIT 1",
        (before, county, dataset_type),
    ).fetchone()


def run_pool(label, jobs, fn, processes=True):
    """Run fn(job) for every job with COUNTY_WORKERS workers, in submission
    order (priority first). Returns the list of results in job order. fn must
    be a module-level function (it is pickled to the worker processes)."""
    etl.log(f"  {label}: {len(jobs)} counties, {COUNTY_WORKERS} at a time "
            f"({'processes' if processes else 'threads'})")
    Pool = ProcessPoolExecutor if processes else ThreadPoolExecutor
    with Pool(max_workers=COUNTY_WORKERS) as pool:
        return list(pool.map(fn, jobs))


# --------------------------------------------------------------------------- phase 1
def phase1_load_county(county):
    """Worker: every non-parcel dataset for one county (runs in a subprocess)."""
    datasets = load_sources()[county]
    tally = {"ok": 0, "failed": 0, "skipped": 0, "rows": 0}
    conn = etl.get_conn()
    try:
        did_anything = False
        for dataset_type in NON_PARCEL_TYPES:
            source = datasets.get(dataset_type)
            if not source:
                continue
            if source.get("type") == "manual":
                tally["skipped"] += 1
                etl.log(f"[SKIP] {county}/{dataset_type}: manual source")
                continue
            did_anything = True
            before = conn.execute("SELECT COALESCE(MAX(id),0) FROM sync_log").fetchone()[0]
            try:
                etl.sync_source(conn, county, dataset_type, source)
                row = _last_sync(conn, before, county, dataset_type)
                if row and row[0] == "success":
                    tally["ok"] += 1
                    tally["rows"] += row[1] or 0
                else:
                    tally["failed"] += 1
            except Exception as exc:  # noqa: BLE001 - never let one source stop the run
                tally["failed"] += 1
                etl.log(f"[FAIL] {county}/{dataset_type}: unhandled exception {exc}")
        if did_anything:
            try:
                etl.backfill_acreage(conn, county)
            except Exception as exc:  # noqa: BLE001
                etl.log(f"[FAIL] {county}: backfill_acreage raised {exc}")
    finally:
        conn.close()
    return tally


def run_phase1():
    sources = load_sources()
    etl.log("=== load_all phase1 (non-parcel datasets, all counties) started ===")
    t0 = time.time()
    results = run_pool("phase1", sorted(sources.keys()), phase1_load_county)
    ok = sum(r["ok"] for r in results)
    failed = sum(r["failed"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    total_rows = sum(r["rows"] for r in results)
    elapsed = (time.time() - t0) / 60
    etl.log(f"=== load_all phase1 finished: {ok} ok, {failed} failed, {skipped} skipped (manual), "
            f"{total_rows:,} rows, {elapsed:.1f} min ===")


# --------------------------------------------------------------------------- phase 2
def get_parcel_count(source):
    """returnCountOnly probe against the layer's /query endpoint, honoring
    verify_ssl:false the same way etl.py does."""
    if source.get("type") == "statewide_geometry":
        conn = etl.get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM parcel_values WHERE county=? AND source_objectid IS NOT NULL",
                                (source["_county"],)).fetchone()[0]
        finally:
            conn.close()
    layer_url = source["url"].rstrip("/")
    query_url = layer_url + "/query"
    verify = source.get("verify_ssl", True)
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = requests.post(
        query_url,
        data={"where": source.get("where") or "1=1", "returnCountOnly": "true", "f": "json"},
        timeout=60,
        verify=verify,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return int(data["count"])


def count_one(item):
    county, source = item
    try:
        n = get_parcel_count(source)
        etl.log(f"  {county}: {n:,} parcels")
        return (county, n, source)
    except Exception as exc:  # noqa: BLE001
        etl.log(f"[FAIL] {county}: could not get parcel count ({exc}); skipping from phase2 ordering")
        return None


def phase2_load_county(item):
    """Worker: parcels + post-load join for one county (runs in a subprocess)."""
    county, n, source = item
    result = {"county": county, "status": "failed", "rows": 0}
    est_size = n * BYTES_PER_PARCEL_EST
    free = free_bytes()
    if free < MIN_FREE_BYTES or (free - est_size) < MIN_FREE_BYTES:
        etl.log(f"[SKIP] phase2: free={free/1024**3:.2f}GB, {county} ({n:,} parcels, "
                f"est {est_size/1024**3:.2f}GB) would breach the 2.0GB floor")
        result["status"] = "skipped_disk"
        return result
    conn = etl.get_conn()
    try:
        have = conn.execute(
            "SELECT COUNT(*) FROM features WHERE county=? AND dataset_type='parcels'", (county,)).fetchone()[0]
        if n and have >= 0.98 * n:
            etl.log(f"[SKIP] {county}/parcels: already loaded ({have:,} of {n:,} rows present)")
            result.update(status="already", rows=have)
            return result
        etl.log(f"--- phase2: loading {county} ({n:,} parcels), free={free/1024**3:.2f}GB ---")
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM sync_log").fetchone()[0]
        try:
            etl.sync_source(conn, county, "parcels", source)
            row = _last_sync(conn, before, county, "parcels")
            if row and row[0] == "success":
                result.update(status="ok", rows=row[1] or 0)
        except Exception as exc:  # noqa: BLE001
            etl.log(f"[FAIL] {county}/parcels: unhandled exception {exc}")
        try:
            etl.backfill_acreage(conn, county)
            dor_values.apply_values_to_features(conn, county)
            have, valued = conn.execute(
                "SELECT COUNT(*), SUM(total_value IS NOT NULL) FROM features "
                "WHERE county=? AND dataset_type='parcels'", (county,)).fetchone()
            if have and (valued or 0) < 0.5 * have:
                # Almost always a wrong key_field (e.g. an internal id instead of the
                # parcel number): the boundaries load fine but nothing joins.
                etl.log(f"[WARN] {county}/parcels: only {valued or 0:,} of {have:,} parcels matched DOR values; "
                        f"check key_field in sources.json")
        except Exception as exc:  # noqa: BLE001
            etl.log(f"[FAIL] {county}: post-load backfill/apply_values raised {exc}")
    finally:
        conn.close()
    return result


def run_phase2():
    sources = load_sources()
    parcel_sources = {}
    for county, datasets in sources.items():
        source = datasets.get("parcels")
        if not source or source.get("type") == "manual":
            continue
        source["_county"] = county
        parcel_sources[county] = source

    etl.log(f"=== load_all phase2: counting parcels for {len(parcel_sources)} counties ===")
    counts = [c for c in run_pool("phase2 counts", list(parcel_sources.items()), count_one, processes=False) if c]

    # Priority metros first (in their listed order), then everything else
    # smallest-first so many counties become usable early.
    rank = {c: i for i, c in enumerate(etl.PRIORITY_COUNTIES)}
    counts.sort(key=lambda t: (rank.get(t[0], len(rank)), t[1] if t[0] not in rank else 0))
    etl.log("  phase2 order: " + ", ".join(c for c, _, _ in counts))

    t0 = time.time()
    results = run_pool("phase2", counts, phase2_load_county)
    ok = [r for r in results if r["status"] in ("ok", "already")]
    failed = [r["county"] for r in results if r["status"] == "failed"]
    skipped_disk = [r["county"] for r in results if r["status"] == "skipped_disk"]
    total_rows = sum(r["rows"] for r in results if r["status"] == "ok")
    elapsed = (time.time() - t0) / 60
    free = free_bytes()
    etl.log(f"=== load_all phase2 finished: {len(ok)} ok, {len(failed)} failed, {total_rows:,} rows, "
            f"{elapsed:.1f} min, free={free/1024**3:.2f}GB ===")
    etl.log(f"    loaded counties: {[(r['county'], r['rows']) for r in ok]}")
    etl.log(f"    failed: {failed}")
    etl.log(f"    skipped for disk: {skipped_disk}")


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else None
    if phase not in ("phase1", "phase2"):
        print("usage: load_all.py [phase1|phase2]")
        sys.exit(1)
    other = etl.acquire_run_lock(f"load_all {phase}")
    if other:
        etl.log(f"[SKIP] load_all {phase}: '{other.get('name')}' (pid {other.get('pid')}, "
                f"started {other.get('started')}) holds the database; stop it first")
        sys.exit(2)
    try:
        run_phase1() if phase == "phase1" else run_phase2()
    finally:
        etl.release_run_lock()


if __name__ == "__main__":
    main()
