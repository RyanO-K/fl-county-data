"""Download the demo database at build time (public deployment only).

Set DEMO_DB_URL to a .db or .db.gz URL (a GitHub Release asset). The file is
written to FL_COUNTY_DB (or etl.DB_PATH). Skips the download if the file is
already present and non-empty.
"""
import gzip
import os
import shutil
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402


def main():
    url = os.environ.get("DEMO_DB_URL")
    if not url:
        print("DEMO_DB_URL not set; nothing to fetch")
        return
    dest = Path(os.environ.get("FL_COUNTY_DB", str(etl.DB_PATH)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"{dest} already present ({dest.stat().st_size/1048576:.0f} MB); skipping")
        return
    tmp = dest.with_suffix(".download")
    print(f"downloading {url}")
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if url.endswith(".gz"):
        with gzip.open(tmp, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        tmp.unlink()
    else:
        tmp.replace(dest)
    print(f"ready: {dest} ({dest.stat().st_size/1048576:.0f} MB)")


if __name__ == "__main__":
    main()
