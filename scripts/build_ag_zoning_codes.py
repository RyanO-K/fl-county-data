"""Build scripts/ag_zoning_codes.json: the zoning codes, per county, that are
agricultural districts. The app's "Agricultural zoning" preset
(app.py build_filters, preset=ag_zoning) and scripts/ag_encroachment.py read
it; codes are stored exactly as they appear in features.zoning_code so they
can be used in `zoning_code IN (...)`.

The codes come from the running app's read-only API (never the database
file), so run the app first, then:

    .venv\\Scripts\\python.exe scripts\\build_ag_zoning_codes.py
    .venv\\Scripts\\python.exe scripts\\build_ag_zoning_codes.py --base-url http://host:5000
    .venv\\Scripts\\python.exe scripts\\build_ag_zoning_codes.py --dump   # list every code, write nothing

How a code is judged (first rule that applies wins):
  1. OVERRIDES[county]: explicit include/exclude decisions, made from the
     county's (or city's) zoning ordinance where the data is ambiguous.
  2. A code whose trimmed, prefix-stripped form is a planned-development
     district (PD/PUD/...) is excluded, whatever its description says.
  3. Descriptions (zoning_desc, several per code in some layers): any one
     that names agriculture / farming / ranching / groves / forestry /
     silviculture includes the code; otherwise, if the code has a
     description, it is excluded (the description says what it is).
  4. Codes without a description: the code pattern AG_CODE_RE (A, AG, AGR,
     A-1, A-2, A-5, A-10, AG-1, AR, A-R, AU, ...) includes it.

Datasets that carry no usable zoning code are skipped (see SKIP): e.g.
Pasco's zoning layer stores a polygon id (ZONEID) in zoning_code.
The facets endpoint lists at most 500 (code, description) pairs, so larger
layers are enumerated in acreage bands (every band under 500 pairs); rows
with no acreage cannot be banded, and the script says how many it missed.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent / "ag_zoning_codes.json"
DEFAULT_BASE_URL = os.environ.get("FL_COUNTY_APP_URL", "http://127.0.0.1:5000")
TIMEOUT = 600
FACETS_CAP = 500  # compute_facets() LIMIT
# Acreage band edges for layers whose facets hit FACETS_CAP. A band still at
# the cap is split in half again (to MAX_DEPTH).
BAND_EDGES = [0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10,
              20, 40, 100, 1000, 1e12]
MAX_DEPTH = 12

# (county, dataset_type) pairs whose zoning_code is not a zoning district.
SKIP = {
    ("pasco", "zoning"): "zoning_code holds ZONEID (a polygon id); the district is ZN_TYPE, "
                         "which is not loaded into zoning_code (sources.json field_map)",
}

# Agricultural words in a description (or in a code that is itself a name,
# like Walton's "General Agriculture"). Not "citrus": Hillsborough's Citrus
# Park Village districts are urban.
AG_DESC_RE = re.compile(
    r"agri|agr\b|\bfarm|ranch(?!ette)|grove|silvicult|timber|forestry|\bdairy|pasture|"
    r"livestock|horticult",
    re.I)
# Agricultural code pattern, applied to core_code(): A, AG, AGR, A-1, A-2,
# A-5, A10, AG-1, AG-2.5, AG-20A, AR, A-R, AR-1, AR-5A, AU, AU(L), FARM-1, SILV.
AG_CODE_RE = re.compile(
    r"^(A|AG|AGR|AGRI|AGRICULTURE|AGRICULTURAL|FARM(-?\d+)?|SILV|"
    r"A-?\d+(\.\d+)?[A-Z]?|AG-?\d+(\.\d+)?[A-Z]?|A-?R|A-?R-?\d+(\.\d+)?[A-Z]?|AU|AU\(L\))$")
PD_RE = re.compile(r"^(A?[A-Z]*PUD|PD|PD-[A-Z0-9]+|[A-Z]PD|P-D|[A-Z]+PD)$")

# Per-county clean-ups before AG_CODE_RE: jurisdiction prefixes/suffixes and
# restriction markers that do not change the district.
CORE_CLEANUPS = {
    # Alachua: 4-digit jurisdiction prefix (0100 county, 0101 Alachua city, ...).
    "alachua": [r"^\d{4}(?=[A-Z])"],
    # Orange parcels: city prefix (APK- Apopka, OCO- Ocoee, ORG- county, ...),
    # "RSTD"/"Restricted" (restricted-use variant of the same district),
    # "(ZIP)" (zoning in progress: county district kept after annexation),
    # "(H)" (Winter Park historic overlay).
    "orange": [r"^(APK|BAY|BI|EDG|EVL|LBV|MTL|OAK|OCO|ORG|ORL|WG|WND|WP)-",
               r"^(RSTD|RESTRICTED) ", r"\s*\((ZIP|H)\)$"],
    # Palm Beach: municipal districts carry " (city)".
    "palm_beach": [r"\s*\(CITY\)$"],
}

# Descriptions that are not district names: Martin's ZONING_DETAILS holds
# resolution/covenant notes, so its codes are judged by code only.
DESC_IS_NOTES = {"martin"}

# Explicit per-county decisions, looked up by the exact stored code or its
# trimmed form: code -> (is_agricultural, reason). Every reason is copied to
# the "review" section of the JSON.
OVERRIDES = {
    "alachua": {
        "0100A-RB": (True, "Alachua County A-RB Agricultural Rural Business: an agricultural district "
                           "that also allows rural businesses serving agriculture"),
        "0100AP": (False, "Alachua County AP is Administrative/Professional, not agricultural"),
    },
    "bay": {
        "AR": (False, "Panama City Beach AR (label AR(PCB)); not confirmed as an agricultural district"),
        "A/IND-SP": (False, "Bay County industrial special district (former AG-2/CSVH, PZ06-183)"),
    },
    "brevard": {
        "PA": (True, "Brevard PA Productive Agricultural (citrus groves and cattle ranches)"),
        "ARR": (True, "Brevard ARR Agricultural Rural Residential: agriculture plus large-lot homes"),
        "GU": (False, "Brevard GU General Use: holding district, not an agricultural district"),
        "FARM-1": (True, "municipal FARM-1 district (not in the county code); included on its name"),
    },
    "broward": {
        "A-6": (False, "Broward A-6 Agricultural-Disposal: a solid-waste disposal district"),
        "A-7": (False, "Broward A-7 Agricultural-Restricted Disposal: a disposal district"),
    },
    "clay": {
        "RA": (False, "Clay RA is a residential district (not confirmed as agricultural)"),
    },
    "collier": {
        "E": (False, "Collier E Estates: large-lot residential; the layer files it under 'Agricultural'"),
        "E-ACSC/ST": (False, "Collier E Estates with overlays; residential despite the 'Agricultural' category"),
    },
    "hillsborough": {
        "AI": (True, "Hillsborough AI Agricultural-Industrial: agricultural district allowing agri-industry"),
        "AM": (True, "Hillsborough AM Agricultural-Mining: agricultural district allowing mining"),
        "RSC-6": (False, "Residential single-family; a few polygons carry an agricultural description"),
    },
    "lake": {
        "RA": (True, "Lake RA Ranchette District: 1 unit/5 ac, farm atmosphere, protects prime agricultural areas"),
    },
    "lee": {
        "C-2/AG-2": (True, "split-zoned parcel, part AG-2 Agricultural"),
        "CPD/AG2": (True, "split-zoned parcel, part AG-2 Agricultural"),
    },
    "leon": {
        "R": (True, "Leon R Rural district: agriculture and silviculture, 1 unit/10 ac"),
    },
    "manatee": {
        "A-1/L": (True, "A-1 Suburban Agriculture with the /L suffix"),
        "PD-A": (True, "Manatee PD-A Planned Development Agriculture"),
    },
    "martin": {
        "A-3": (False, "Martin A-3 is the Conservation district"),
    },
    "miami_dade": {
        "9000": (False, "parcel PRIMARY_ZONE code; 32% of its parcels carry an agricultural DOR use but the "
                        "property appraiser's zone table was not found, so not included"),
        "9410": (False, "parcel PRIMARY_ZONE code; all 38 parcels carry an agricultural DOR use, meaning "
                        "unverified, so not included"),
        "8900": (False, "parcel PRIMARY_ZONE code; 12% agricultural DOR use, meaning unverified"),
    },
    "nassau": {
        "OR": (True, "Nassau OR Open Rural: the county's agriculture/silviculture district"),
    },
    "okaloosa": {
        "AA": (True, "Okaloosa AA Agriculture (1 unit/10 ac); AC-.5/AC-1 are Airport Compatibility"),
    },
    "orange": {
        "BAY-A": (False, "City of Bay Lake 'A' district; meaning not confirmed"),
    },
    "osceola": {
        "AC": (True, "Osceola AC Agricultural Development and Conservation"),
        "ARE": (True, "Osceola ARE Agricultural Rural Estate"),
    },
    "palm_beach": {
        "US-1/ICW (city)": (False, "municipal US-1/Intracoastal corridor district; the layer's "
                                   "'AGRICULTURAL' category looks wrong"),
    },
    "sarasota": {
        "OUR": (True, "Sarasota OUR Open Use Rural: agriculture and agriculturally-oriented very "
                      "low-density residential"),
        "OUA": (True, "Sarasota OUA Open Use Agriculture"),
    },
    "st_johns": {
        "OR": (True, "St. Johns OR Open Rural: the county's agricultural/silviculture district"),
    },
    "volusia": {
        # The layer's GenericZoningCode folds Volusia A-1..A-4 (Prime, Rural,
        # Transitional Agriculture) into "A"; the description lists the names.
        "A": (True, "generic code for A-1 Prime / A-2 Rural / A-3, A-4 Transitional Agriculture "
                    "(a few municipal polygons share it)"),
        "COUNTY A": (True, "county A-1..A-4 agriculture (Rural/Transitional)"),
        "VC:A": (True, "A-1..A-4 agriculture (Prime/Rural/Transitional)"),
        "RA": (True, "Volusia RA Rural Agricultural Estate"),
        "RA(1)": (True, "RA Rural Agricultural Estate variant"),
        "RA(1)A": (True, "RA Rural Agricultural Estate variant"),
        "RA(C)": (True, "RA Rural Agricultural Estate (cluster)"),
        "RAA": (True, "RA Rural Agricultural Estate variant"),
        "RAE": (True, "RA Rural Agricultural Estate variant"),
        "RAEA": (True, "RA Rural Agricultural Estate variant"),
        "FR": (True, "Volusia FR Forestry Resource"),
        "FR(4)": (True, "FR Forestry Resource variant"),
        "FR(4)A": (True, "FR Forestry Resource variant"),
        "FRA": (True, "FR Forestry Resource variant"),
        "PC": (False, "mixed generic code (Community / Agricultural / Port Orange Riverwalk)"),
    },
}

# Plain-English names for included codes the layer leaves undescribed; the
# key is core_code() of the stored code.
DESCRIPTIONS = {
    "_default": "Agricultural",
    "alachua": {"A": "Agriculture", "AG": "Agriculture", "AGR": "Agriculture",
                "A-RB": "Agricultural Rural Business"},
    "nassau": {"OR": "Open Rural"},
    "sarasota": {"OUA": "Open Use Agriculture", "OUR": "Open Use Rural"},
    "st_johns": {"OR": "Open Rural"},
    "bay": {"AG": "Agriculture", "AG-1": "General Agriculture", "AG-2": "Agriculture/Timberland",
            "SILV": "Silviculture"},
    "brevard": {"AGR": "Agricultural", "PA": "Productive Agricultural",
                "ARR": "Agricultural Rural Residential", "AU": "Agricultural Residential",
                "AU(L)": "Agricultural Residential (low intensity)", "FARM-1": "Farm"},
    "charlotte": {"AG": "Agriculture"},
    "clay": {"AG": "Agricultural", "AR": "Agricultural/Residential", "AR-1": "Agricultural/Residential",
             "AR-2": "Agricultural/Residential"},
    "duval": {"AGR": "Agriculture"},
    "escambia": {"AGR": "Agriculture"},
    "indian_river": {"A-1": "Agricultural", "A-2": "Agricultural", "A-3": "Agricultural"},
    "lee": {"AG-1": "Agricultural", "AG-2": "Agricultural", "AG-3": "Agricultural",
            "C-2/AG-2": "Split: C-2 / AG-2 Agricultural", "CPD/AG2": "Split: CPD / AG-2 Agricultural"},
    "manatee": {"A": "General Agriculture", "A-1": "Suburban Agriculture",
                "A-1/L": "Suburban Agriculture (/L)", "PD-A": "Planned Development Agriculture"},
    "marion": {"A1": "General Agriculture", "A2": "Improved Agriculture",
               "A3": "Residential Agricultural Estate"},
    "martin": {"A-1": "Small Farms", "A-1A": "Agricultural", "A-2": "Agricultural",
               "AG-20A": "General Agricultural", "AR-5A": "Agricultural Ranchette"},
    "okaloosa": {"AA": "Agriculture"},
    "orange": {"A-1": "Citrus Rural", "A-2": "Farmland Rural", "A-R": "Agricultural-Residential"},
    "st_lucie": {"AG-1": "Agricultural", "AG-2.5": "Agricultural", "AG-5": "Agricultural",
                 "AR-1": "Agricultural Residential"},
    "sumter": {"A10": "Agricultural (10 ac)", "A10C": "Agricultural (10 ac)", "A5": "Agricultural (5 ac)"},
    "volusia": {"AG": "Agricultural", "FR": "Forestry Resource", "FR(4)": "Forestry Resource",
                "FR(4)A": "Forestry Resource", "FRA": "Forestry Resource"},
}


def core_code(county, code):
    """Trimmed, upper-cased code without the jurisdiction decorations some
    layers add (see CORE_CLEANUPS)."""
    c = (code or "").strip().upper()
    for pattern in CORE_CLEANUPS.get(county, []):
        c = re.sub(pattern, "", c).strip()
    return c


def usable_descs(county, code, descs):
    """Descriptions that actually name the district: not notes, not a bare
    number (Orange's ZONETYPE), not just the code repeated (Sarasota)."""
    if county in DESC_IS_NOTES:
        return []
    squash = lambda s: re.sub(r"\W", "", s).upper()
    same = {squash(code), squash(core_code(county, code))}
    return [d for d in descs if not re.fullmatch(r"[\d\W]*", d) and squash(d) not in same]


def get_json(base_url, path, params):
    resp = requests.get(base_url.rstrip("/") + path, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def zoning_pairs(base_url, county, dataset_type):
    """Every distinct (zoning_code, zoning_desc) of one county layer, walking
    acreage bands when the plain facets list is cut at FACETS_CAP."""
    base = {"county": county, "dataset_type": dataset_type}

    def facets(extra):
        rows = get_json(base_url, "/api/facets", {**base, **extra})["zoning_codes"]
        return [(r["zoning_code"], r.get("zoning_desc")) for r in rows]

    first = facets({})
    if len(first) < FACETS_CAP:
        return set(first), 0
    out = set()

    def band(lo, hi, depth):
        rows = facets({"min_acreage": repr(lo), "max_acreage": repr(hi)})
        if len(rows) >= FACETS_CAP and depth < MAX_DEPTH and hi - lo > 1e-6:
            mid = (lo + hi) / 2
            band(lo, mid, depth + 1)
            band(mid, hi, depth + 1)
        else:
            if len(rows) >= FACETS_CAP:
                print(f"  warning: {county}/{dataset_type} acreage {lo}-{hi} still at the cap")
            out.update(rows)

    band(-1e12, BAND_EDGES[0], 0)
    for lo, hi in zip(BAND_EDGES, BAND_EDGES[1:]):
        band(lo, hi, 0)
    total = get_json(base_url, "/api/features", {**base, "per_page": 1})["total"]
    banded = get_json(base_url, "/api/features", {**base, "per_page": 1, "min_acreage": "-1e12"})["total"]
    return out, total - banded


def collect(base_url):
    """{county: {code: [descriptions]}} over every dataset carrying zoning."""
    layers = get_json(base_url, "/api/counties", {})
    codes = {}
    for layer in layers:
        county, dt = layer["county"], layer["dataset_type"]
        if (county, dt) in SKIP:
            print(f"{county}/{dt}: skipped ({SKIP[(county, dt)]})")
            continue
        pairs, missed = zoning_pairs(base_url, county, dt)
        if not pairs:
            continue
        n = len({c for c, _ in pairs})
        print(f"{county}/{dt}: {n} codes" + (f" ({missed} rows without acreage not scanned)" if missed else ""))
        for code, desc in pairs:
            if code is None or not code.strip():
                continue
            descs = codes.setdefault(county, {}).setdefault(code, [])
            desc = (desc or "").strip()
            if desc and desc not in descs:
                descs.append(desc)
    return codes


def classify(county, code, descs):
    """(is_agricultural, reason or None). A reason means the call is worth a
    human look (it is written to the "review" section)."""
    over = OVERRIDES.get(county, {})
    for key in (code, code.strip()):
        if key in over:
            return over[key]
    core = core_code(county, code)
    if "," in core:
        # A parcel that spans several districts (Manatee parcels): it counts
        # when any of its districts is agricultural.
        parts = [p.strip() for p in code.split(",")]
        hits = [p for p in parts if classify(county, p, [])[0]]
        if hits and len(hits) < len(parts):
            return True, f"parcel spans several districts; agricultural part: {', '.join(hits)}"
        return bool(hits), None
    if PD_RE.match(core):
        return False, None
    descs = usable_descs(county, code, descs)
    if descs:
        return any(AG_DESC_RE.search(d) for d in descs), None
    if " " in core or len(core) > 12:
        # The code is itself a district name (Walton, Levy).
        return bool(AG_DESC_RE.search(core)), None
    return bool(AG_CODE_RE.match(core)), None


def ag_description(county, code, descs):
    descs = usable_descs(county, code, descs)
    ag = [d for d in descs if AG_DESC_RE.search(d)]
    if ag or descs:
        return (ag or descs)[0]
    if "," in code:
        return "Parcel spanning several districts, at least one agricultural"
    core = core_code(county, code)
    if " " in core or len(core) > 12:
        return code.strip()
    return DESCRIPTIONS.get(county, {}).get(core) or DESCRIPTIONS["_default"]


def build(base_url):
    codes = collect(base_url)
    counties, review = {}, {}
    for county in sorted(codes):
        for code in sorted(codes[county]):
            descs = codes[county][code]
            ok, reason = classify(county, code, descs)
            if ok:
                counties.setdefault(county, {})[code] = ag_description(county, code, descs)
            if reason:
                review.setdefault(county, {})[code] = ("included: " if ok else "excluded: ") + reason
    return codes, {
        "_notes": NOTES,
        "counties": counties,
        "review": review,
    }


NOTES = ("Generated by scripts/build_ag_zoning_codes.py from the running app's /api/facets; "
         "do not edit by hand (change the rules/overrides in the script and re-run it). "
         "counties: county -> {zoning_code exactly as stored in features.zoning_code: description}; "
         "the same code string can mean different things in different counties, so always match "
         "(county, zoning_code) pairs. review: county -> {code: why a borderline code was included "
         "or excluded}. Included: agricultural districts (agriculture, agricultural estate / "
         "agricultural-residential, ranch, grove, forestry/silviculture, agricultural reserve). "
         "Excluded: rural/estate residential, conservation/preservation, open space, PD/PUD. "
         "Skipped layers: " + "; ".join(f"{c}/{d}: {why}" for (c, d), why in SKIP.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--dump", action="store_true", help="print every code and its verdict; write nothing")
    args = ap.parse_args()
    codes, data = build(args.base_url)
    if args.dump:
        for county in sorted(codes):
            for code in sorted(codes[county]):
                ok = code in data["counties"].get(county, {})
                print(f"{'AG ' if ok else '   '}{county}\t{code!r}\t{' / '.join(codes[county][code])[:100]}")
        return
    OUT_PATH.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    for county, c in data["counties"].items():
        print(f"{county}: {len(c)} agricultural codes")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
