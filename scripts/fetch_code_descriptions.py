"""Build scripts/code_descriptions.json: plain-English meanings for land-use
codes whose source layer carries a code but no description.

The app fills a blank land_use_desc from this file (see app.py
describe_code) when it lists codes and rows, so no ETL run or database write
is needed. Re-run this script when a county revises its code list:

    .venv\\Scripts\\python.exe scripts\\fetch_code_descriptions.py

Sources (all public, no key):
  - Orange parcels: the Orange County Property Appraiser use-code list behind
    webmap.ocpafl.org "Search by Use Code" (4-digit codes: DOR category +
    OCPA subtype).
  - Orange future land use: the coded-value domains of LAND_USE and
    LAND_USE_OLD on the county's Future Land Use layer, plus four newer
    designations the domain lacks (ORANGE_FLU_EXTRA, from the Comprehensive
    Plan).
  - Fallback: the statewide DOR use-code categories (Fla. Admin. Code
    R. 12D-8.008), keyed by the first two digits, for codes a county list
    has retired but parcels still carry.
"""
import json
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent / "code_descriptions.json"
TIMEOUT = 60

OCPA_USE_CODES_URL = "https://webmapapi.ocpafl.org/api/UseCode/GetUseCodeSets"
ORANGE_FLU_LAYER_URL = "https://ocgis4.ocfl.net/arcgis/rest/services/AGOL_Open_Data/MapServer/21"

# Florida DOR use-code categories, Fla. Admin. Code R. 12D-8.008.
DOR_CATEGORIES = {
    "00": "Vacant residential", "01": "Single family", "02": "Mobile homes",
    "03": "Multi-family, 10 units or more", "04": "Condominiums", "05": "Cooperatives",
    "06": "Retirement homes", "07": "Miscellaneous residential", "08": "Multi-family, fewer than 10 units",
    "09": "Residential common elements/areas",
    "10": "Vacant commercial", "11": "Stores, one story", "12": "Mixed use: store and office or residential",
    "13": "Department stores", "14": "Supermarkets", "15": "Regional shopping centers",
    "16": "Community shopping centers", "17": "Office buildings, one story", "18": "Office buildings, multi-story",
    "19": "Professional service buildings", "20": "Airports, terminals, marinas", "21": "Restaurants, cafeterias",
    "22": "Drive-in restaurants", "23": "Financial institutions", "24": "Insurance company offices",
    "25": "Repair service shops", "26": "Service stations", "27": "Auto sales, repair and storage",
    "28": "Parking lots, mobile home parks", "29": "Wholesale outlets, produce houses",
    "30": "Florists, greenhouses", "31": "Drive-in theaters, open stadiums", "32": "Enclosed theaters, auditoriums",
    "33": "Nightclubs, bars", "34": "Bowling alleys, skating rinks, enclosed arenas", "35": "Tourist attractions",
    "36": "Camps", "37": "Race tracks", "38": "Golf courses, driving ranges", "39": "Hotels, motels",
    "40": "Vacant industrial", "41": "Light manufacturing", "42": "Heavy industrial",
    "43": "Lumber yards, sawmills", "44": "Packing plants", "45": "Canneries, bottlers, brewers",
    "46": "Other food processing", "47": "Mineral processing", "48": "Warehousing, distribution terminals",
    "49": "Open storage",
    "50": "Improved agricultural", "51": "Cropland, soil class I", "52": "Cropland, soil class II",
    "53": "Cropland, soil class III", "54": "Timberland, index 90+", "55": "Timberland, index 80-89",
    "56": "Timberland, index 70-79", "57": "Timberland, index 60-69", "58": "Timberland, index 50-59",
    "59": "Timberland, not classified", "60": "Grazing land, class I", "61": "Grazing land, class II",
    "62": "Grazing land, class III", "63": "Grazing land, class IV", "64": "Grazing land, class V",
    "65": "Grazing land, class VI", "66": "Orchards, groves, citrus", "67": "Poultry, bees, fish, rabbits",
    "68": "Dairies, feed lots", "69": "Ornamentals, miscellaneous agricultural",
    "70": "Vacant institutional", "71": "Churches", "72": "Private schools and colleges",
    "73": "Private hospitals", "74": "Homes for the aged", "75": "Orphanages, non-profit or charitable",
    "76": "Mortuaries, cemeteries", "77": "Clubs, lodges, union halls", "78": "Sanitariums, rest homes",
    "79": "Cultural organizations",
    "80": "Undefined (reserved)", "81": "Military", "82": "Forests, parks, recreational areas",
    "83": "Public county schools", "84": "Colleges", "85": "Hospitals", "86": "County-owned",
    "87": "State-owned", "88": "Federal-owned", "89": "Municipal-owned",
    "90": "Leasehold interests (government land, private lessee)", "91": "Utilities",
    "92": "Mining, petroleum and gas lands", "93": "Subsurface rights", "94": "Rights-of-way, streets, canals",
    "95": "Rivers, lakes, submerged lands", "96": "Sewage, waste, borrow pits, wetlands",
    "97": "Outdoor recreational or park land", "98": "Centrally assessed", "99": "Acreage not zoned agricultural",
}


# Orange FLU designations in use on the layer but missing from its domain;
# meanings from the Orange County Comprehensive Plan 2010-2030 (Future Land
# Use Element) and the county Planning and Zoning Quick Reference Guide.
ORANGE_FLU_EXTRA = {
    "IW": "Innovation Way",
    "LP": "Lake Pickett",
    "MHDR": "Medium-High Density Residential",
    "RSLD": "Rural Settlement Low Density",
}


def ocpa_use_codes():
    resp = requests.get(OCPA_USE_CODES_URL, timeout=TIMEOUT, headers={"Referer": "https://webmap.ocpafl.org/"})
    resp.raise_for_status()
    return {r["DorCode"]: (r.get("DescLong") or r.get("DescShort") or "").strip()
            for r in resp.json() if r.get("DorCode")}


def layer_domain(layer_url, *fields):
    """Coded values of the named fields' domains; earlier fields win."""
    resp = requests.get(layer_url, params={"f": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    by_name = {f["name"]: f for f in resp.json().get("fields", [])}
    out = {}
    for name in reversed(fields):
        for cv in ((by_name.get(name) or {}).get("domain") or {}).get("codedValues", []):
            out[str(cv["code"])] = cv["name"].strip()
    return out


def main():
    data = {
        "_notes": "Generated by scripts/fetch_code_descriptions.py; do not edit by hand. "
                  "county -> dataset_type -> {source, dor_fallback, land_use: {code: description}}. "
                  "dor_fallback: codes missing from the list take the DOR category of their first two digits.",
        "dor_categories": DOR_CATEGORIES,
        "orange": {
            "parcels": {
                "source": OCPA_USE_CODES_URL,
                "dor_fallback": True,
                "land_use": ocpa_use_codes(),
            },
            "future_land_use": {
                "source": ORANGE_FLU_LAYER_URL + " (LAND_USE / LAND_USE_OLD domains) + Comprehensive Plan",
                "land_use": {**ORANGE_FLU_EXTRA,
                             **layer_domain(ORANGE_FLU_LAYER_URL, "LAND_USE", "LAND_USE_OLD")},
            },
        },
    }
    OUT_PATH.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for county, datasets in data.items():
        if isinstance(datasets, dict) and county not in ("dor_categories",):
            for dt, v in datasets.items():
                if isinstance(v, dict) and "land_use" in v:
                    print(f"{county}/{dt}: {len(v['land_use'])} codes")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
