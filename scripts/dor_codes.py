"""Florida DOR land-use code normalization, shared by ag_encroachment.py and
the web app (app.py). Standard library only, so the web app can import it
without pulling in ag_encroachment's numpy/shapely/pyproj.

Every code is reduced to a 2-digit DOR use-code category ("00".."99"; Fla.
Admin. Code R. 12D-8.008, the table is DOR_CATEGORIES in
fetch_code_descriptions.py).
"""
import string

# How each county writes features.land_use_code (inspected through the app's
# /api/facets and /api/features, 2026-09-27). Formats:
#   int      the DOR category as a number, zero-padded to 3 ("001", "066") or
#            not at all ("1", "66"); values >= 100 are not DOR.
#   dor4     4 digits: DOR category + 2-digit county subtype ("0103" = 01).
#            Shorter values are unpadded 4-digit codes ("100" = "0100").
#   dor5     5 digits: 3-digit DOR code + 2-digit subtype ("05600" = 56).
#   dor6     6 digits: "00" + DOR category + subtype ("002100" = 21).
#   not_dor  not a DOR code at all; only the DOR roll (dor_use_code) is used.
# Counties not listed use "auto": up to 3 digits -> int, 4 -> dor4, else
# unknown. A trailing letter suffix ("0100A") is dropped in every format.
COUNTY_CODE_FORMATS = {
    # zero-padded 3-digit DOR codes
    **{c: "int" for c in (
        "baker", "bradford", "calhoun", "columbia", "dixie", "franklin", "gadsden",
        "gilchrist", "glades", "gulf", "hamilton", "hendry", "hillsborough", "holmes",
        "jackson", "lafayette", "liberty", "madison", "martin", "okaloosa", "palm_beach",
        "seminole", "st_lucie", "taylor", "union", "washington")},
    # unpadded 1-2 digit DOR codes (lee: 2-digit DORCODE; its descriptions are noise)
    **{c: "int" for c in (
        "citrus", "desoto", "hardee", "hernando", "highlands", "sumter", "suwannee", "lee")},
    # DOR + subtype, 4 digits
    **{c: "dor4" for c in (
        "alachua", "bay", "brevard", "charlotte", "duval", "indian_river", "levy", "manatee",
        "miami_dade", "nassau", "orange", "pinellas", "polk", "st_johns", "walton",
        "santa_rosa")},  # santa_rosa drops leading zeros: "100" = 0100, "0" = 0000
    "putnam": "dor5",
    "flagler": "dor6",
    "leon": "not_dor",  # land_use_code holds future-land-use names ("Rural", "Institutional")
}
# The statewide roll's DOR_UC is always the category zero-padded to 3 digits.
DOR_ROLL_FORMAT = "int"

# Residential DOR categories: single family, mobile homes, multi-family 10+,
# condominiums, co-ops, retirement homes, misc. residential, multi-family <10,
# residential common elements. 00 (vacant residential) is not among them.
RESIDENTIAL_CATEGORIES = frozenset(f"{n:02d}" for n in range(1, 10))


def normalize_code(code, fmt="auto"):
    """A raw land-use code -> 2-digit DOR category string ("01".."99") or None."""
    if code is None or fmt == "not_dor":
        return None
    s = str(code).strip().upper()
    if s.endswith(".0"):          # numeric field exported as float
        s = s[:-2]
    s = s.rstrip(string.ascii_uppercase).strip()
    if not s.isdigit():
        return None
    if fmt == "auto":
        fmt = "int" if len(s) <= 3 else "dor4" if len(s) == 4 else None
    if fmt == "int":
        n = int(s)
        return f"{n:02d}" if n < 100 else None
    if fmt == "dor4":
        return s.zfill(4)[:2] if len(s) <= 4 else None
    if fmt == "dor5":
        if len(s) > 5:
            return None
        n = int(s.zfill(5)[:3])
        return f"{n:02d}" if n < 100 else None
    if fmt == "dor6":
        return s.zfill(6)[2:4] if len(s) <= 6 else None
    return None


def county_code_format(county):
    return COUNTY_CODE_FORMATS.get(county, "auto")
