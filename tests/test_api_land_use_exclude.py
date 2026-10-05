"""Filtering land-use codes OUT: ?exclude_land_use_code= (repeatable) and
?hide_residential=1 (DOR categories 01-09, county code as fallback). Both
keep rows with no usable code."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import dor_values as D

ROOT = Path(__file__).resolve().parent.parent

# key, county, dataset, dor_use_code, land_use_code
ROWS = [
    ("H-SFR", "hernando", "parcels", "001", "1"),        # roll: single family
    ("H-VAC", "hernando", "parcels", "000", "0"),        # roll: vacant residential, stays
    ("H-COND", "hernando", "parcels", "004", None),      # roll: condo, no county code
    ("H-FB", "hernando", "parcels", None, "1"),          # no roll code: county "1" = 01
    ("H-NONE", "hernando", "parcels", None, None),       # nothing usable: stays
    ("H-BLANK", "hernando", "parcels", "", "66"),        # blank roll, county 66 (ag)
    ("H-BAD", "hernando", "parcels", "XYZ", "2"),        # unreadable roll code: county 02
    ("H-ROLLWINS", "hernando", "parcels", "066", "1"),   # roll says ag; county code ignored
    ("H-ZONE", "hernando", "zoning", None, "1"),         # not a parcel: never hidden
    ("L-RES", "leon", "parcels", None, "Residential"),   # leon codes are not DOR: stays
    ("L-ROLL", "leon", "parcels", "008", "Residential"), # but the roll still decides
    ("O-SFR", "orange", "parcels", None, "0103"),        # dor4: 0103 = 01
    ("O-VAC", "orange", "parcels", None, "0000"),        # dor4: 00, stays
    ("O-COM", "orange", "parcels", None, "1100"),        # dor4: 11 (commercial)
]
RESIDENTIAL = {"H-SFR", "H-COND", "H-FB", "H-BAD", "L-ROLL", "O-SFR"}
ALL = {r[0] for r in ROWS}


@pytest.fixture
def client(conn):
    import app as A
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    D.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, dor_use_code, "
        "land_use_code, last_synced_at) VALUES (?, ?, ?, ?, ?, ?, 't')",
        [(c, d, k, k, dor, lu) for k, c, d, dor, lu in ROWS])
    conn.commit()
    A._facets_cache.clear()
    A.app.config["TESTING"] = True
    yield A.app.test_client()
    A._facets_cache.clear()


def _get(client, q):
    r = client.get(q)
    assert r.status_code == 200, r.data
    return json.loads(r.data)


def _keys(client, q):
    return set(r["feature_key"] for r in _get(client, q + "&per_page=500")["rows"])


# --- hide_residential ------------------------------------------------------

def test_hide_residential(client):
    assert _keys(client, "/api/features?hide_residential=1") == ALL - RESIDENTIAL
    assert _get(client, "/api/features?hide_residential=1")["total"] == len(ALL - RESIDENTIAL)


def test_hide_residential_off_or_other_values_is_no_filter(client):
    for q in ("", "hide_residential=", "hide_residential=0", "hide_residential=yes"):
        assert _keys(client, f"/api/features?{q}") == ALL


def test_hide_residential_with_county(client):
    assert _keys(client, "/api/features?county=orange&hide_residential=1") == {"O-VAC", "O-COM"}
    assert _keys(client, "/api/features?county=leon&hide_residential=1") == {"L-RES"}


def test_hide_residential_map_geometry(client):
    rows = _get(client, "/api/features/geometry?hide_residential=1")["rows"]
    assert {r["feature_key"] for r in rows} == ALL - RESIDENTIAL


def test_residential_clause_without_cached_codes_uses_roll_only():
    # No connection and nothing cached (the facets path): the county-code
    # fallback has no codes, so only the roll decides and nothing else drops.
    import app as A
    A._facets_cache.pop("code_counts", None)
    sql, params = A.residential_clause(None)
    assert params == [] and "ELSE 0 END" in sql


def test_roll_spellings():
    import app as A
    assert set(A.DOR_ROLL_RESIDENTIAL) == {f"{p}{n}" for n in range(1, 10) for p in ("", "0", "00")}
    assert "000" in A.DOR_ROLL_CODES and "100" not in A.DOR_ROLL_CODES


# --- exclude_land_use_code -------------------------------------------------

def test_exclude_keeps_null_codes(client):
    got = _keys(client, "/api/features?exclude_land_use_code=1")
    assert got == ALL - {"H-SFR", "H-FB", "H-ROLLWINS", "H-ZONE"}
    assert {"H-COND", "H-NONE"} <= got  # NULL land_use_code stays


def test_exclude_repeatable(client):
    got = _keys(client, "/api/features?exclude_land_use_code=1&exclude_land_use_code=Residential"
                        "&exclude_land_use_code=0103")
    assert got == ALL - {"H-SFR", "H-FB", "H-ROLLWINS", "H-ZONE", "L-RES", "L-ROLL", "O-SFR"}


def test_exclude_combines_with_include(client):
    q = "/api/features?land_use_code=1&land_use_code=0103&exclude_land_use_code=1"
    assert _keys(client, q) == {"O-SFR"}
    assert _keys(client, "/api/features?land_use_code=1&exclude_land_use_code=1") == set()


def test_exclude_empty_value_ignored(client):
    assert _keys(client, "/api/features?exclude_land_use_code=") == ALL


def test_exclude_and_hide_residential_together(client):
    got = _keys(client, "/api/features?hide_residential=1&exclude_land_use_code=1100")
    assert got == ALL - RESIDENTIAL - {"O-COM"}


# --- filter counts / facets ------------------------------------------------

def test_filter_counts_exclude_is_exact(client):
    for q in ("exclude_land_use_code=1", "exclude_land_use_code=1&exclude_land_use_code=0103",
              "exclude_land_use_code=Residential&dataset_type=parcels"):
        counts = _get(client, f"/api/filter_counts?{q}")["counties"]
        for county in ("hernando", "leon", "orange"):
            actual = _get(client, f"/api/features?{q}&county={county}")["total"]
            assert counts[county] == actual, (q, county)


def test_filter_counts_include_and_exclude_same_code_is_zero(client):
    counts = _get(client, "/api/filter_counts?land_use_code=1&exclude_land_use_code=1")
    assert set(counts["counties"].values()) == {0}


def test_filter_counts_hide_residential_is_an_upper_bound(client):
    counts = _get(client, "/api/filter_counts?hide_residential=1")["counties"]
    for county in ("hernando", "leon", "orange"):
        assert counts[county] >= _get(client, f"/api/features?hide_residential=1&county={county}")["total"]


def test_facets_cache_key_includes_new_filters():
    import app as A
    base = A._facets_key({"county": "pasco"})
    assert A._facets_key({"county": "pasco", "hide_residential": "1"}) != base
    assert A._facets_key({"county": "pasco", "exclude_land_use_code": ["1", "2"]}) != base


# --- shared code module / errors -------------------------------------------

def test_dor_codes_is_shared_and_light():
    import dor_codes
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import dor_codes; "
         "print(any(m in sys.modules for m in ('numpy', 'shapely', 'pyproj')))"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
    assert dor_codes.normalize_code("0103", dor_codes.county_code_format("orange")) == "01"
    assert dor_codes.RESIDENTIAL_CATEGORIES == {f"{n:02d}" for n in range(1, 10)}


def test_ag_encroachment_reuses_dor_codes():
    ae = pytest.importorskip("ag_encroachment")
    import dor_codes
    assert ae.normalize_code is dor_codes.normalize_code
    assert ae.COUNTY_CODE_FORMATS is dor_codes.COUNTY_CODE_FORMATS


def test_413_and_414_are_json_on_api_only():
    import app as A
    from werkzeug.exceptions import RequestEntityTooLarge, RequestURITooLarge
    for exc in (RequestURITooLarge(), RequestEntityTooLarge()):
        with A.app.test_request_context("/api/features"):
            body, code = A.api_http_error(exc)
            assert code == exc.code and "error" in body.get_json()
        with A.app.test_request_context("/"):
            assert A.api_http_error(exc) is exc
