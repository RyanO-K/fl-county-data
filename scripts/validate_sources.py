"""Smoke-test every configured source through the same code path etl.py
uses (layer metadata, format detection, cert flag, pagination mode), fetching
a few records each without writing to the database.

Usage:
    python validate_sources.py                  # checks scripts/sources.json
    python validate_sources.py other.json       # checks another sources file
"""
import json
import sys
from pathlib import Path

import etl


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else etl.SOURCES_PATH
    sources = json.loads(path.read_text(encoding="utf-8"))
    ok = failed = manual = 0
    for county, datasets in sources.items():
        for dataset_type, source in datasets.items():
            print(f"\n=== {county} / {dataset_type} ===")
            if source.get("type") == "manual":
                manual += 1
                print(f"  SKIP (manual): {source.get('note')}")
                continue
            try:
                sample = etl.fetch_sample(source)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAILED: {str(exc)[:220]}")
                continue
            ok += 1
            feats = sample["features"]
            info = sample["info"]
            print(f"  format={sample['format']} id_field={info['id_field']} "
                  f"max_records={info['max_records']} sample_rows={len(feats)}")
            if not feats:
                print("  WARNING: layer returned no features")
                continue
            props = feats[0].get("properties", {}) or {}
            key_field = source["key_field"]
            flag = "" if key_field in props else "   <-- FIELD NOT IN LAYER"
            print(f"  key_field '{key_field}' -> {props.get(key_field)!r}{flag}")
            for norm_field, raw_field in source.get("field_map", {}).items():
                flag = "" if raw_field in props else "   <-- FIELD NOT IN LAYER"
                print(f"  {norm_field} ('{raw_field}') -> {props.get(raw_field)!r}{flag}")
            print(f"  geometry present: {feats[0].get('geometry') is not None}")
    print(f"\nSUMMARY: ok={ok} failed={failed} manual={manual}")


if __name__ == "__main__":
    main()
