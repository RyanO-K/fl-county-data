"""Land-use codes with no description in the database get one at display time
from scripts/code_descriptions.json (app.describe_code). The lookup is applied
to /api/facets (with a county), feature rows and the feature detail, without
writing to the database or mutating the facets cache."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

import dor_values as D
import recordings as R

ROOT = Path(__file__).resolve().parent.parent
REAL_FILE = ROOT / "scripts" / "code_descriptions.json"

LOOKUP = {
    "_notes": "test fixture",
    "dor_categories": {"01": "Single family", "10": "Vacant commercial"},
    "orange": {
        "parcels": {
            "dor_fallback": True,
            "land_use": {"0110": "SINGLE FAM CLASS I", "0120": "LIST DESC", "SHARED": "from parcels"},
        },
        "future_land_use": {
            "land_use": {"LDR": "Low Density Residential", "SHARED": "from flu"},
        },
    },
}


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def lookup(tmp_path, monkeypatch):
    import app as A
    path = _write(tmp_path / "code_descriptions.json", LOOKUP)
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", path)
    return path


@pytest.fixture
def client(conn, lookup):
    import app as A
    # Importing app starts the facets warm-up thread, which holds its own
    # connection to the test DB; let it finish so it neither fills the cache
    # from a half-built DB nor keeps the file locked for the next test.
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute(
        "INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, land_use_code, land_use_desc, last_synced_at) VALUES "
        "('orange','parcels','O-1','O1','0110',NULL,'t'), "          # in the county list, NULL desc
        "('orange','parcels','O-2','O2','0199','','t'), "            # not listed: DOR category fallback
        "('orange','parcels','O-3','O3','0120','DB DESC','t'), "     # DB description wins over the list
        "('orange','parcels','O-4','O4','XX12','','t'), "            # no match anywhere
        "('orange','future_land_use','F-1','F1','LDR','','t'), "     # FLU list, no DOR fallback
        "('orange','future_land_use','F-2','F2','0199',NULL,'t'), "  # FLU has no dor_fallback
        "('pasco','parcels','P-1','P1','0110','','t')")              # county not in the file
    conn.commit()
    A._facets_cache.clear()
    A.app.config["TESTING"] = True
    yield A.app.test_client()
    A._facets_cache.clear()


def _get(client, q):
    r = client.get(q)
    assert r.status_code == 200, r.data
    return json.loads(r.data)


def _descs(rows):
    return {r["feature_key"]: r["land_use_desc"] for r in rows}


def _facet_descs(data):
    return {e["land_use_code"]: e["land_use_desc"] for e in data["land_use_codes"]}


# --- describe_code -----------------------------------------------------------

def test_describe_code_listed(lookup):
    import app as A
    assert A.describe_code("orange", "parcels", "0110") == "SINGLE FAM CLASS I"
    assert A.describe_code("orange", "future_land_use", "LDR") == "Low Density Residential"


def test_describe_code_dor_fallback(lookup):
    import app as A
    assert A.describe_code("orange", "parcels", "0199") == "Single family (DOR category 01)"
    assert A.describe_code("orange", "parcels", "1000") == "Vacant commercial (DOR category 10)"
    # No fallback when the leading digits are not a known category, the code
    # does not start with two digits, or the dataset has no dor_fallback.
    assert A.describe_code("orange", "parcels", "9912") is None
    assert A.describe_code("orange", "parcels", "XX12") is None
    assert A.describe_code("orange", "parcels", "0X12") is None
    assert A.describe_code("orange", "future_land_use", "0199") is None


def test_describe_code_empty_dataset_tries_all_parcels_first(lookup):
    import app as A
    assert A.describe_code("orange", "", "LDR") == "Low Density Residential"
    assert A.describe_code("orange", "", "0110") == "SINGLE FAM CLASS I"
    assert A.describe_code("orange", "", "SHARED") == "from parcels"
    assert A.describe_code("orange", "future_land_use", "SHARED") == "from flu"
    assert A.describe_code("orange", "", "NOPE") is None


def test_describe_code_unknown_county_or_dataset(lookup):
    import app as A
    assert A.describe_code("pasco", "parcels", "0110") is None
    assert A.describe_code("pasco", "", "0110") is None
    assert A.describe_code("orange", "zoning", "0110") is None
    # Top-level non-county keys are never treated as counties.
    assert A.describe_code("dor_categories", "", "01") is None
    assert A.describe_code("dor_categories", "parcels", "01") is None
    assert A.describe_code("_notes", "", "01") is None


def test_describe_code_blank_code(lookup):
    import app as A
    assert A.describe_code("orange", "parcels", "") is None
    assert A.describe_code("orange", "parcels", None) is None


# --- load_code_descriptions ------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", tmp_path / "nope.json")
    assert A.load_code_descriptions() == {}
    assert A.describe_code("orange", "parcels", "0110") is None
    assert A.describe_code("orange", "parcels", "0199") is None


def test_load_invalid_file_returns_empty(tmp_path, monkeypatch):
    import app as A
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", bad)
    assert A.load_code_descriptions() == {}
    assert A.describe_code("orange", "parcels", "0110") is None


def test_load_reloads_when_file_changes(lookup):
    import app as A
    first = A.load_code_descriptions()
    assert first["orange"]["parcels"]["land_use"]["0110"] == "SINGLE FAM CLASS I"
    assert A.load_code_descriptions() is first  # cached while unchanged
    changed = json.loads(json.dumps(LOOKUP))
    changed["orange"]["parcels"]["land_use"]["0110"] = "CHANGED"
    _write(lookup, changed)
    future = time.time() + 10
    os.utime(lookup, (future, future))  # guarantee a new mtime
    assert A.describe_code("orange", "parcels", "0110") == "CHANGED"


def test_load_follows_path_change(lookup, tmp_path, monkeypatch):
    import app as A
    assert A.describe_code("orange", "parcels", "0110") == "SINGLE FAM CLASS I"
    other = json.loads(json.dumps(LOOKUP))
    other["orange"]["parcels"]["land_use"]["0110"] = "OTHER FILE"
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", _write(tmp_path / "other.json", other))
    assert A.describe_code("orange", "parcels", "0110") == "OTHER FILE"


# --- /api/facets -------------------------------------------------------------

def test_facets_filled_with_county(client):
    got = _facet_descs(_get(client, "/api/facets?county=orange"))
    assert got["0110"] == "SINGLE FAM CLASS I"
    assert got["0199"] == "Single family (DOR category 01)"
    assert got["0120"] == "DB DESC"      # description already in the data wins
    assert got["LDR"] == "Low Density Residential"
    assert not got["XX12"]


def test_facets_filled_with_county_and_dataset(client):
    got = _facet_descs(_get(client, "/api/facets?county=orange&dataset_type=future_land_use"))
    assert got["LDR"] == "Low Density Residential"
    assert not got["0199"]               # FLU has no DOR fallback
    got = _facet_descs(_get(client, "/api/facets?county=orange&dataset_type=parcels"))
    assert got["0110"] == "SINGLE FAM CLASS I"
    assert got["0199"] == "Single family (DOR category 01)"


def test_facets_without_county_not_filled(client):
    data = _get(client, "/api/facets")
    for e in data["land_use_codes"]:
        if e["land_use_code"] != "0120":
            assert not e["land_use_desc"], e


def test_facets_unknown_county_unchanged(client):
    data = _get(client, "/api/facets?county=pasco")
    assert [(e["land_use_code"], e["land_use_desc"] or "") for e in data["land_use_codes"]] == [("0110", "")]


def test_facets_cache_not_mutated(client, tmp_path, monkeypatch):
    import app as A
    got = _facet_descs(_get(client, "/api/facets?county=orange"))
    assert got["0110"] == "SINGLE FAM CLASS I"
    # The cached entry still holds the raw (blank) description.
    cached = A._facets_cache[A._facets_key({"county": "orange"})][1]
    raw = {e["land_use_code"]: e["land_use_desc"] for e in cached["land_use_codes"]}
    assert not raw["0110"] and not raw["0199"] and not raw["LDR"]
    assert raw["0120"] == "DB DESC"
    # A changed lookup shows up on the next call (a cache hit).
    changed = json.loads(json.dumps(LOOKUP))
    changed["orange"]["parcels"]["land_use"]["0110"] = "CHANGED"
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", _write(tmp_path / "changed.json", changed))
    got = _facet_descs(_get(client, "/api/facets?county=orange"))
    assert got["0110"] == "CHANGED"
    assert not A._facets_cache[A._facets_key({"county": "orange"})][1]["land_use_codes"][0]["land_use_desc"]


def test_facets_missing_file_no_fill(client, tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", tmp_path / "missing.json")
    got = _facet_descs(_get(client, "/api/facets?county=orange"))
    assert got["0120"] == "DB DESC"
    assert not got["0110"] and not got["0199"] and not got["LDR"]


# --- feature rows ------------------------------------------------------------

EXPECTED = {
    "O-1": "SINGLE FAM CLASS I",
    "O-2": "Single family (DOR category 01)",
    "O-3": "DB DESC",
    "F-1": "Low Density Residential",
}


def _check_rows(descs):
    for key, want in EXPECTED.items():
        assert descs[key] == want, key
    for key in ("O-4", "F-2", "P-1"):
        assert not descs[key], key


def test_features_rows_filled(client):
    _check_rows(_descs(_get(client, "/api/features")["rows"]))
    # Also with a county filter, and in map mode.
    descs = _descs(_get(client, "/api/features?county=orange")["rows"])
    assert descs["O-1"] == "SINGLE FAM CLASS I" and "P-1" not in descs
    _check_rows(_descs(_get(client, "/api/features?geometry=1")["rows"]))


def test_geometry_rows_filled(client):
    _check_rows(_descs(_get(client, "/api/features/geometry")["rows"]))


def test_feature_detail_filled(client):
    ids = {r["feature_key"]: r["id"] for r in _get(client, "/api/features")["rows"]}
    descs = {k: _get(client, f"/api/feature/{i}")["land_use_desc"] for k, i in ids.items()}
    _check_rows(descs)


def test_rows_missing_file_no_fill(client, tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "CODE_DESCRIPTIONS_PATH", tmp_path / "missing.json")
    descs = _descs(_get(client, "/api/features")["rows"])
    assert descs["O-3"] == "DB DESC"
    for key in ("O-1", "O-2", "O-4", "F-1", "F-2", "P-1"):
        assert not descs[key], key
    ids = {r["feature_key"]: r["id"] for r in _get(client, "/api/features")["rows"]}
    assert not _get(client, f"/api/feature/{ids['O-1']}")["land_use_desc"]


def test_rows_not_written_to_db(client, conn):
    _get(client, "/api/features")
    rows = dict(conn.execute("SELECT feature_key, land_use_desc FROM features").fetchall())
    assert rows["O-1"] is None and rows["O-2"] == "" and rows["F-1"] == ""


# --- the real lookup file ----------------------------------------------------

def test_real_file_sanity():
    import app as A
    assert Path(A.CODE_DESCRIPTIONS_PATH).resolve() == REAL_FILE.resolve()
    data = json.loads(REAL_FILE.read_text(encoding="utf-8"))
    cats = data["dor_categories"]
    assert cats and all(len(k) == 2 and k.isdigit() for k in cats)
    assert all(isinstance(v, str) and v for v in cats.values())
    parcels = data["orange"]["parcels"]
    assert parcels["dor_fallback"] is True
    assert len(parcels["land_use"]) > 50
    assert len(data["orange"]["future_land_use"]["land_use"]) > 5
    for ds in data["orange"].values():
        assert all(isinstance(v, str) and v.strip() for v in ds["land_use"].values())
    loaded = A.load_code_descriptions()
    assert loaded["orange"]["parcels"]["land_use"] == parcels["land_use"]
