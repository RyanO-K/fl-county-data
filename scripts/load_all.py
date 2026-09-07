"""Bulk loader that drives the existing ETL (etl.sync_source) across all 67
counties, in two phases, without touching sources.json / etl.py / dor_values.py.

Usage:
    python -u scripts/load_all.py phase1        # all non-parcel datasets, all counties
    python -u scripts/load_all.py phase2        # parcels, smallest county first, disk-aware

This module only orchestrates: all HTTP/pagination/retry/format logic lives in
etl.sync_source, which is called unmodified.
"""
import ctypes
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl
import dor_values

SOURCES_PATH = Path(__file__).resolve().parent / "sources.json"

NON_PARCEL_TYPES = ("zoning", "land_use", "future_land_use")

MIN_FREE_BYTES = 2.0 * 1024 ** 3   # stop threshold: 2.0 GB free
BYTES_PER_PARCEL_EST = 1.5 * 1024  # conservative estimate for sizing decisions


def free_bytes(drive=None):
    drive = drive or (etl.DB_PATH.drive + "\\")
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(drive, ctypes.pointer(free), None, None)
    return free.value


def load_sources():
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))


def run_phase1():
    sources = load_sources()
    conn = etl.get_conn()
    etl.log("=== load_all phase1 (non-parcel datasets, all counties) started ===")
    t0 = time.time()
    ok = 0
    failed = 0
    skipped = 0
    total_rows = 0
    for county in sorted(sources.keys()):
        datasets = sources[county]
        county_did_anything = False
        for dataset_type in NON_PARCEL_TYPES:
            source = datasets.get(dataset_type)
            if not source:
                continue
            if source.get("type") == "manual":
                skipped += 1
                etl.log(f"[SKIP] {county}/{dataset_type}: manual source")
                continue
            county_did_anything = True
            before = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM sync_log"
            ).fetchone()[0]
            try:
                etl.sync_source(conn, county, dataset_type, source)
                # sync_source logs its own sync_log row (success or failed);
                # inspect the row it just wrote to tally rows/status here.
                row = conn.execute(
                    "SELECT status, rows_fetched FROM sync_log WHERE id > ? "
                    "AND county=? AND dataset_type=? ORDER BY id DESC LIMIT 1",
                    (before, county, dataset_type),
                ).fetchone()
                if row and row[0] == "success":
                    ok += 1
                    total_rows += row[1] or 0
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - never let one source stop the run
                failed += 1
                etl.log(f"[FAIL] {county}/{dataset_type}: unhandled exception {exc}")
        if county_did_anything:
            try:
                etl.backfill_acreage(conn, county)
            except Exception as exc:  # noqa: BLE001
                etl.log(f"[FAIL] {county}: backfill_acreage raised {exc}")
    conn.close()
    elapsed = (time.time() - t0) / 60
    etl.log(f"=== load_all phase1 finished: {ok} ok, {failed} failed, {skipped} skipped (manual), "
            f"{total_rows:,} rows, {elapsed:.1f} min ===")


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
    counts = []
    for county, source in parcel_sources.items():
        try:
            n = get_parcel_count(source)
            counts.append((county, n, source))
            etl.log(f"  {county}: {n:,} parcels")
        except Exception as exc:  # noqa: BLE001
            etl.log(f"[FAIL] {county}: could not get parcel count ({exc}); skipping from phase2 ordering")

    # Priority metros first (in their listed order), then everything else
    # smallest-first so many counties become usable early.
    rank = {c: i for i, c in enumerate(etl.PRIORITY_COUNTIES)}
    counts.sort(key=lambda t: (rank.get(t[0], len(rank)), t[1] if t[0] not in rank else 0))
    etl.log("  phase2 order: " + ", ".join(c for c, _, _ in counts))

    conn = etl.get_conn()
    t0 = time.time()
    ok = 0
    failed = 0
    total_rows = 0
    loaded = []
    skipped_disk = []

    stopped = False
    for idx, (county, n, source) in enumerate(counts):
        if stopped:
            skipped_disk.append((county, n))
            continue
        free = free_bytes()
        est_size = n * BYTES_PER_PARCEL_EST
        if free < MIN_FREE_BYTES or (free - est_size) < MIN_FREE_BYTES:
            skipped_disk.append((county, n))
            if county in etl.PRIORITY_COUNTIES:
                etl.log(f"[SKIP] phase2: free={free/1024**3:.2f}GB, priority county {county} "
                        f"({n:,} parcels, est {est_size/1024**3:.2f}GB) would breach the 2.0GB floor; "
                        "continuing with smaller counties")
                continue
            etl.log(f"[STOP] phase2: free={free/1024**3:.2f}GB, next county {county} "
                    f"({n:,} parcels, est {est_size/1024**3:.2f}GB) would breach the 2.0GB floor; stopping "
                    f"(remaining {len(counts) - idx} counties, ascending by size, will only get bigger)")
            stopped = True
            continue
        have = conn.execute(
            "SELECT COUNT(*) FROM features WHERE county=? AND dataset_type='parcels'", (county,)).fetchone()[0]
        if n and have >= 0.98 * n:
            etl.log(f"[SKIP] {county}/parcels: already loaded ({have:,} of {n:,} rows present)")
            ok += 1
            loaded.append((county, have))
            continue
        etl.log(f"--- phase2: loading {county} ({n:,} parcels), free={free/1024**3:.2f}GB ---")
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM sync_log").fetchone()[0]
        try:
            etl.sync_source(conn, county, "parcels", source)
            row = conn.execute(
                "SELECT status, rows_fetched FROM sync_log WHERE id > ? "
                "AND county=? AND dataset_type='parcels' ORDER BY id DESC LIMIT 1",
                (before, county),
            ).fetchone()
            if row and row[0] == "success":
                ok += 1
                total_rows += row[1] or 0
                loaded.append((county, row[1] or 0))
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
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

    conn.close()
    elapsed = (time.time() - t0) / 60
    free = free_bytes()
    etl.log(f"=== load_all phase2 finished: {ok} ok, {failed} failed, {total_rows:,} rows, "
            f"{elapsed:.1f} min, free={free/1024**3:.2f}GB ===")
    etl.log(f"    loaded counties: {loaded}")
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
