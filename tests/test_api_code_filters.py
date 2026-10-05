"""Multi-select zoning_code / land_use_code filters: repeated query params are
OR'd within a field and AND'd across fields."""
import json
import threading

import pytest
from werkzeug.datastructures import MultiDict

import dor_values as D
import recordings as R


@pytest.fixture
def client(conn):
    import app as A
    # Importing app starts the facets warm-up thread, which holds its own
    # connection to the test DB; let it finish so it neither fills the cache
    # from a half-built DB nor keeps the file locked for the next test.
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute(
        "INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, zoning_code, land_use_code, last_synced_at) VALUES "
        "('hernando','parcels','H-1','H1','AG','01','t'), "
        "('hernando','parcels','H-2','H2','RS','02','t'), "
        "('hernando','zoning','Z-1','Z1','AG',NULL,'t'), "
        "('pasco','parcels','P-1','P1','RS','01','t'), "
        "('pasco','parcels','P-2','P2','CO','03','t')")
    conn.commit()
    A._facets_cache.clear()
    A.app.config["TESTING"] = True
    yield A.app.test_client()
    A._facets_cache.clear()


def _ids(resp):
    assert resp.status_code == 200
    return sorted(r["feature_key"] for r in json.loads(resp.data)["rows"])


def _get(client, q):
    r = client.get(q)
    assert r.status_code == 200
    return json.loads(r.data)


# --- /api/features ---------------------------------------------------------

def test_single_code(client):
    assert _ids(client.get("/api/features?zoning_code=AG")) == ["H-1", "Z-1"]
    assert _ids(client.get("/api/features?land_use_code=01")) == ["H-1", "P-1"]


def test_two_codes_union(client):
    assert _ids(client.get("/api/features?zoning_code=AG&zoning_code=RS")) == ["H-1", "H-2", "P-1", "Z-1"]
    assert _ids(client.get("/api/features?land_use_code=02&land_use_code=03")) == ["H-2", "P-2"]
    assert _get(client, "/api/features?zoning_code=AG&zoning_code=CO")["total"] == 3


def test_duplicate_code_same_as_single(client):
    assert _ids(client.get("/api/features?zoning_code=AG&zoning_code=AG")) == ["H-1", "Z-1"]


def test_zoning_and_land_use_combine_with_and(client):
    q = "/api/features?zoning_code=AG&zoning_code=RS&land_use_code=01"
    assert _ids(client.get(q)) == ["H-1", "P-1"]
    q = "/api/features?zoning_code=RS&zoning_code=CO&land_use_code=01&land_use_code=03"
    assert _ids(client.get(q)) == ["P-1", "P-2"]
    # Combines with the other filters too.
    assert _ids(client.get(q + "&county=pasco")) == ["P-1", "P-2"]
    assert _ids(client.get(q + "&county=hernando")) == []


def test_empty_value_ignored(client):
    everything = ["H-1", "H-2", "P-1", "P-2", "Z-1"]
    assert _ids(client.get("/api/features?zoning_code=")) == everything
    assert _ids(client.get("/api/features?zoning_code=&land_use_code=")) == everything
    assert _ids(client.get("/api/features?zoning_code=&zoning_code=AG")) == ["H-1", "Z-1"]


def test_unknown_code_matches_nothing(client):
    data = _get(client, "/api/features?zoning_code=NOPE")
    assert data["rows"] == [] and data["total"] == 0
    # An unknown code alongside a real one does not hide the real matches.
    assert _ids(client.get("/api/features?zoning_code=NOPE&zoning_code=CO")) == ["P-2"]


def test_geometry_feed_honours_repeated_params(client):
    data = _get(client, "/api/features/geometry?zoning_code=AG&zoning_code=CO")
    assert sorted(r["feature_key"] for r in data["rows"]) == ["H-1", "P-2", "Z-1"]
    assert data["total"] == 3
    data = _get(client, "/api/features/geometry?zoning_code=AG&zoning_code=RS&land_use_code=02")
    assert [r["feature_key"] for r in data["rows"]] == ["H-2"]
    assert data["total"] == 1


def test_facets_honour_repeated_params(client):
    import app as A
    A._facets_cache.clear()
    data = _get(client, "/api/facets?land_use_code=01&land_use_code=03")
    assert [z["zoning_code"] for z in data["zoning_codes"]] == ["AG", "CO", "RS"]
    # A different selection is a different cache entry, not a stale hit.
    data = _get(client, "/api/facets?land_use_code=03")
    assert [z["zoning_code"] for z in data["zoning_codes"]] == ["CO"]
    data = _get(client, "/api/facets?land_use_code=03&land_use_code=02")
    assert [z["zoning_code"] for z in data["zoning_codes"]] == ["CO", "RS"]


# --- /api/filter_counts ----------------------------------------------------

def test_filter_counts_multiple_zoning_codes(client):
    import app as A
    A._facets_cache.clear()
    get = lambda q: _get(client, "/api/filter_counts?" + q)

    assert get("")["counties"] == {"hernando": 3, "pasco": 2}
    # AG only exists in Hernando: Pasco greys out.
    assert get("zoning_code=AG")["counties"] == {"hernando": 2, "pasco": 0}
    # AG or RS: the per-code counts add up, and Pasco (RS only) comes back.
    r = get("zoning_code=AG&zoning_code=RS")
    assert r["counties"] == {"hernando": 3, "pasco": 1}
    assert r["datasets"] == {"parcels": 3, "zoning": 1}
    # Order of the repeated values does not matter.
    assert get("zoning_code=RS&zoning_code=AG") == r
    # The sum is still capped by the land-use bound (min of the two fields).
    r = get("zoning_code=AG&zoning_code=RS&land_use_code=01")
    assert r["counties"] == {"hernando": 1, "pasco": 1}
    assert r["datasets"] == {"parcels": 2, "zoning": 0}
    # Multiple land-use codes sum the same way.
    r = get("zoning_code=RS&zoning_code=CO&land_use_code=01&land_use_code=03")
    assert r["counties"] == {"hernando": 1, "pasco": 2}
    # Empty values are ignored; unknown codes contribute nothing.
    assert get("zoning_code=")["counties"] == {"hernando": 3, "pasco": 2}
    assert get("zoning_code=NOPE&zoning_code=CO")["counties"] == {"hernando": 0, "pasco": 1}


def test_filter_counts_never_below_true_match(client):
    """The bounds are upper bounds: every county/dataset with real matches
    for a multi-code query must have a positive bound."""
    import app as A
    A._facets_cache.clear()
    for q in ("zoning_code=AG&zoning_code=RS", "zoning_code=RS&land_use_code=01&land_use_code=02",
              "zoning_code=CO&zoning_code=AG&land_use_code=03"):
        bounds = _get(client, "/api/filter_counts?" + q)["counties"]
        for county in ("hernando", "pasco"):
            actual = _get(client, f"/api/features?{q}&county={county}")["total"]
            assert bounds[county] >= actual, (q, county)


# --- arg_list ----------------------------------------------------------------

def test_arg_list_multidict():
    import app as A
    md = MultiDict([("zoning_code", "AG"), ("zoning_code", "RS"), ("county", "pasco")])
    assert A.arg_list(md, "zoning_code") == ["AG", "RS"]
    assert A.arg_list(md, "county") == ["pasco"]
    assert A.arg_list(md, "land_use_code") == []


def test_arg_list_plain_dict_str():
    import app as A
    assert A.arg_list({"zoning_code": "AG"}, "zoning_code") == ["AG"]
    assert A.arg_list({"zoning_code": ""}, "zoning_code") == []
    assert A.arg_list({"zoning_code": None}, "zoning_code") == []
    assert A.arg_list({}, "zoning_code") == []


def test_arg_list_plain_dict_list():
    import app as A
    assert A.arg_list({"zoning_code": ["AG", "RS"]}, "zoning_code") == ["AG", "RS"]
    assert A.arg_list({"zoning_code": ("AG",)}, "zoning_code") == ["AG"]
    assert A.arg_list({"zoning_code": []}, "zoning_code") == []


def test_arg_list_drops_empties_and_dupes_keeping_order():
    import app as A
    md = MultiDict([("z", ""), ("z", "RS"), ("z", "AG"), ("z", "RS"), ("z", "")])
    assert A.arg_list(md, "z") == ["RS", "AG"]
    assert A.arg_list({"z": ["", "RS", None, "AG", "RS"]}, "z") == ["RS", "AG"]


def test_arg_list_limit_counts_distinct_values():
    import app as A
    md = MultiDict([("z", "A"), ("z", "A"), ("z", ""), ("z", "B"), ("z", "C")])
    assert A.arg_list(md, "z", 2) == ["A", "B"]
    assert A.arg_list(md, "z") == ["A", "B", "C"]


def test_too_many_codes_is_a_json_400(client):
    # Past the cap the request is refused with a JSON error naming the
    # parameter and the count, rather than silently dropping codes.
    import app as A
    cap = A.MAX_FILTER_CODES
    for name in ("zoning_code", "land_use_code", "exclude_land_use_code"):
        junk = "&".join(f"{name}=X{i}" for i in range(cap + 50))
        for path in ("/api/features", "/api/features/geometry", "/api/filter_counts"):
            r = client.get(f"{path}?{junk}")
            assert r.status_code == 400, (path, name)
            assert r.is_json
            err = r.get_json()["error"]
            assert name in err and f"{cap + 50:,}" in err


def test_codes_at_the_cap_are_accepted(client):
    import app as A
    junk = "&".join(f"zoning_code=X{i}" for i in range(A.MAX_FILTER_CODES - 1))
    assert _ids(client.get(f"/api/features?zoning_code=CO&{junk}")) == ["P-2"]
    # Duplicates count once.
    assert _ids(client.get(f"/api/features?zoning_code=CO&zoning_code=CO&{junk}")) == ["P-2"]


def test_api_errors_are_json_other_pages_are_not(client):
    r = client.get("/api/features?zoning_code=" + "&zoning_code=".join(str(i) for i in range(600)))
    assert r.status_code == 400 and r.is_json
    # Non-API 404s keep Flask's HTML page; the handler only covers 400/413/414.
    assert not client.get("/nope").is_json


# --- _facets_key -------------------------------------------------------------

def test_facets_key_empty_and_single_values_unchanged():
    import app as A
    old = lambda d: json.dumps(sorted((k, v) for k, v in d.items() if v not in (None, "")))
    assert A._facets_key({}) == old({}) == "[]"
    assert A._facets_key(MultiDict()) == "[]"
    single = {"county": "pasco", "dataset_type": "parcels", "zoning_code": "AG"}
    assert A._facets_key(single) == old(single)
    assert A._facets_key(MultiDict(list(single.items()))) == old(single)
    # Empty values are dropped from the key as before.
    assert A._facets_key({"county": "pasco", "zoning_code": ""}) == old({"county": "pasco"})


def test_facets_key_order_of_repeated_values_irrelevant():
    import app as A
    a = MultiDict([("county", "pasco"), ("zoning_code", "AG"), ("zoning_code", "RS")])
    b = MultiDict([("zoning_code", "RS"), ("county", "pasco"), ("zoning_code", "AG")])
    assert A._facets_key(a) == A._facets_key(b)
    # A duplicated value is the same selection as the value once.
    assert A._facets_key(MultiDict([("zoning_code", "AG"), ("zoning_code", "AG")])) == \
        A._facets_key({"zoning_code": "AG"})


def test_facets_key_different_value_sets_differ():
    import app as A
    k1 = A._facets_key(MultiDict([("zoning_code", "AG")]))
    k2 = A._facets_key(MultiDict([("zoning_code", "AG"), ("zoning_code", "RS")]))
    k3 = A._facets_key(MultiDict([("zoning_code", "AG"), ("zoning_code", "CO")]))
    k4 = A._facets_key(MultiDict([("land_use_code", "AG"), ("land_use_code", "RS")]))
    assert len({k1, k2, k3, k4}) == 4
