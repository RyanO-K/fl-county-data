"""Land-use presets (?preset=lu_<bucket>): parcels whose DOR use-code category
is in a bucket of scripts/land_use_presets.json, matched like hide_residential
(roll code first, the county's own code as fallback). Unknowns drop."""
import json
import threading

import pytest

import dor_values as D

# key, county, dataset, dor_use_code, land_use_code
ROWS = [
    ("H-SFR", "hernando", "parcels", "001", "1"),        # roll: single family
    ("H-VAC", "hernando", "parcels", "000", "0"),        # roll: vacant residential
    ("H-GROVE", "hernando", "parcels", "066", "1"),      # roll: groves (county code ignored)
    ("H-FB", "hernando", "parcels", None, "55"),         # no roll code: county 55 = timber
    ("H-NONE", "hernando", "parcels", None, None),       # nothing usable: never matches
    ("H-ZONE", "hernando", "zoning", None, "55"),        # not a parcel: never matches
    ("O-ACRE", "orange", "parcels", None, "9900"),       # dor4: 99 acreage
    ("O-COM", "orange", "parcels", None, "1100"),        # dor4: 11 commercial
]

BUCKETS = {
    "buckets": {
        "agricultural": {"label": "Agricultural (all)", "categories": [f"{n}" for n in range(50, 70)]},
        "vacant_land": {"label": "Vacant land", "categories": ["00", "10", "40", "70", "99"]},
        "empty": {"label": "Nothing valid", "categories": ["5", "abc", 7]},
    }
}


@pytest.fixture
def client(conn, tmp_path, monkeypatch):
    import app as A
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    path = tmp_path / "land_use_presets.json"
    path.write_text(json.dumps(BUCKETS), encoding="utf-8")
    monkeypatch.setattr(A, "LAND_USE_PRESETS_PATH", path)
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
    return {r["feature_key"] for r in _get(client, q + "&per_page=500")["rows"]}


def test_agricultural_preset(client):
    assert _keys(client, "/api/features?preset=lu_agricultural") == {"H-GROVE", "H-FB"}


def test_vacant_preset_uses_county_code_fallback(client):
    assert _keys(client, "/api/features?preset=lu_vacant_land") == {"H-VAC", "O-ACRE"}
    assert _keys(client, "/api/features?preset=lu_vacant_land&county=orange") == {"O-ACRE"}


def test_preset_map_geometry(client):
    rows = _get(client, "/api/features/geometry?preset=lu_agricultural")["rows"]
    assert {r["feature_key"] for r in rows} == {"H-GROVE", "H-FB"}


def test_preset_combines_with_hide_residential(client):
    assert _keys(client, "/api/features?preset=lu_vacant_land&hide_residential=1") == {"H-VAC", "O-ACRE"}


def test_bucket_without_valid_categories_is_not_a_preset(client):
    # An unknown preset is ignored (no filter), like any other unknown value.
    assert len(_keys(client, "/api/features?preset=lu_empty")) == len(ROWS)


def test_api_presets_lists_land_use_buckets(client):
    data = _get(client, "/api/presets")
    assert data["ag_zoning"]["kind"] == "zoning"
    assert data["lu_agricultural"]["kind"] == "land_use"
    assert data["lu_agricultural"]["categories"][0] == "50"
    assert data["lu_vacant_land"]["label"] == "Vacant land"
    assert "lu_empty" not in data


def test_shipped_buckets_cover_every_category():
    import app as A
    raw = json.loads(A.LAND_USE_PRESETS_PATH.read_text(encoding="utf-8"))
    cats = {c for b in raw["buckets"].values() for c in b["categories"]}
    assert cats == {f"{n:02d}" for n in range(100)}
