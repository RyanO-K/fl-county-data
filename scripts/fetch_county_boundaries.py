"""Download Florida's 67 county boundaries from the U.S. Census Bureau's
TIGERweb service (official, public, no key) into a small static GeoJSON
the web UI draws as outlines on every map.

Usage:
    python scripts/fetch_county_boundaries.py

Re-run whenever you want to refresh; boundaries change very rarely.
"""
import json
from pathlib import Path

import requests

# TIGERweb "State_County" service; the Counties layer, generalized a little
# so the file stays small (a few hundred KB) but coastlines still look right.
LAYER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1"
OUT = Path(__file__).resolve().parent / "static" / "fl_counties.geojson"

# Census county names -> the snake_case keys used in sources.json / the DB.
SPECIAL = {"Miami-Dade": "miami_dade", "St. Johns": "st_johns", "St. Lucie": "st_lucie",
           "DeSoto": "desoto", "Palm Beach": "palm_beach", "Indian River": "indian_river",
           "Santa Rosa": "santa_rosa"}


def county_key(name):
    return SPECIAL.get(name, name.lower().replace(" ", "_"))


def main():
    resp = requests.post(LAYER + "/query", data={
        "where": "STATE='12'",
        "outFields": "NAME,GEOID,BASENAME",
        "outSR": 4326,
        "f": "geojson",
        "maxAllowableOffset": 0.0005,   # ~50 m generalization
        "geometryPrecision": 5,
    }, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    feats = data.get("features", [])
    if len(feats) != 67:
        raise SystemExit(f"expected 67 Florida counties, got {len(feats)}")
    for f in feats:
        name = f["properties"].get("BASENAME") or f["properties"].get("NAME")
        f["properties"] = {"name": name, "key": county_key(name), "geoid": f["properties"].get("GEOID")}
    feats.sort(key=lambda f: f["properties"]["key"])
    out = {"type": "FeatureCollection",
           "source": "U.S. Census Bureau TIGERweb, State_County/Counties layer, generalized ~50 m",
           "source_url": LAYER,
           "features": feats}
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {len(feats)} counties)")
    print("keys:", ", ".join(f["properties"]["key"] for f in feats))


if __name__ == "__main__":
    main()
