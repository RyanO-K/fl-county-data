"""The "Agricultural zoning" preset (?preset=ag_zoning): rows whose
(county, zoning_code) is an agricultural district per
scripts/ag_zoning_codes.json. The same code string means different districts
in different counties, so the preset is applied per county, and it composes
(AND) with every other filter."""
import json
import threading
from pathlib import Path

import pytest

import dor_values as D
import recordings as R

ROOT = Path(__file__).resolve().parent.parent
REAL_FILE = ROOT / "scripts" / "ag_zoning_codes.json"

# "A" is agricultural in alpha but not in beta; "AG" is agricultural in both.
CODES = {
    "_notes": "test fixture",
    "counties": {
        "alpha": {"A": "Agriculture", "AG": "Agricultural"},
        "beta": {"AG": "Agricultural"},
    },
    "review": {},
}

EVERYTHING = ["A-1", "A-2", "A-3", "B-1", "B-2", "B-3", "Z-1"]


def _write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def codes_file(tmp_path, monkeypatch):
    import app as A
    path = _write(tmp_path / "ag_zoning_codes.json", CODES)
    monkeypatch.setattr(A, "AG_ZONING_PATH", path)
    return path


@pytest.fixture
def client(conn, codes_file):
    import app as A
    # Importing app starts the facets warm-up thread, which holds its own
    # connection to the test DB; let it finish so it neither fills the cache
    # from a half-built DB nor keeps the file locked for the next test.
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute(
        "INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, zoning_code, "
        "land_use_code, acreage, last_synced_at) VALUES "
        "('alpha','parcels','A-1','A1','A','01',10,'t'), "     # ag in alpha
        "('alpha','parcels','A-2','A2','RS','01',1,'t'), "     # residential
        "('alpha','parcels','A-3','A3','AG','02',50,'t'), "    # ag in alpha
        "('alpha','zoning','Z-1','Z1','A',NULL,200,'t'), "     # ag in alpha, zoning layer
        "('beta','parcels','B-1','B1','A','01',10,'t'), "      # 'A' is NOT ag in beta
        "('beta','parcels','B-2','B2','AG','01',20,'t'), "     # ag in beta
        "('beta','parcels','B-3','B3',NULL,'01',5,'t')")       # no zoning
    conn.commit()
    A._facets_cache.clear()
    A.app.config["TESTING"] = True
    yield A.app.test_client()
    A._facets_cache.clear()


def _get(client, q):
    r = client.get(q)
    assert r.status_code == 200, r.data
    return json.loads(r.data)


def _ids(client, q):
    return sorted(r["feature_key"] for r in _get(client, q)["rows"])


# --- per-county semantics ---------------------------------------------------

def test_same_code_differs_by_county(client):
    assert _ids(client, "/api/features?preset=ag_zoning") == ["A-1", "A-3", "B-2", "Z-1"]
    assert _get(client, "/api/features?preset=ag_zoning")["total"] == 4


def test_preset_with_county(client):
    assert _ids(client, "/api/features?preset=ag_zoning&county=beta") == ["B-2"]
    assert _ids(client, "/api/features?preset=ag_zoning&county=alpha") == ["A-1", "A-3", "Z-1"]
    # A county with no agricultural codes on file matches nothing (not everything).
    assert _ids(client, "/api/features?preset=ag_zoning&county=gamma") == []


def test_preset_clause_is_restricted_to_selected_county(codes_file):
    import app as A
    sql, params = A.build_filters({"preset": "ag_zoning", "county": "beta"})
    assert "alpha" not in params
    assert params.count("beta") == 2  # the county filter and the preset's own pair


def test_no_or_unknown_preset_is_ignored(client):
    assert _ids(client, "/api/features") == EVERYTHING
    assert _ids(client, "/api/features?preset=") == EVERYTHING
    assert _ids(client, "/api/features?preset=nope") == EVERYTHING


# --- composition with the other filters -------------------------------------

def test_preset_and_other_filters(client):
    base = "/api/features?preset=ag_zoning"
    assert _ids(client, base + "&dataset_type=parcels") == ["A-1", "A-3", "B-2"]
    assert _ids(client, base + "&dataset_type=zoning") == ["Z-1"]
    # An explicit zoning selection is ANDed: 'A' only survives where it is ag.
    assert _ids(client, base + "&zoning_code=A") == ["A-1", "Z-1"]
    assert _ids(client, base + "&zoning_code=A&zoning_code=RS") == ["A-1", "Z-1"]
    assert _ids(client, base + "&zoning_code=RS") == []
    assert _ids(client, base + "&land_use_code=01") == ["A-1", "B-2"]
    assert _ids(client, base + "&min_acreage=15&max_acreage=60") == ["A-3", "B-2"]
    assert _ids(client, base + "&q=B-") == ["B-2"]


def test_preset_paging_and_geometry(client):
    d = _get(client, "/api/features?preset=ag_zoning&per_page=1&page=2")
    assert d["total"] == 4 and d["total_pages"] == 4 and len(d["rows"]) == 1
    g = _get(client, "/api/features/geometry?preset=ag_zoning&county=alpha")
    assert g["total"] == 3
    assert sorted(r["feature_key"] for r in g["rows"]) == ["A-1", "A-3", "Z-1"]
    m = _get(client, "/api/features?preset=ag_zoning&geometry=1")
    assert m["total"] == 4


# --- facets and filter counts ------------------------------------------------

def test_facets_follow_preset(client):
    codes = lambda q: [r["zoning_code"] for r in _get(client, q)["zoning_codes"]]
    assert codes("/api/facets?county=beta") == ["A", "AG"]
    assert codes("/api/facets?county=beta&preset=ag_zoning") == ["AG"]
    assert codes("/api/facets?county=alpha&preset=ag_zoning") == ["A", "AG"]
    assert codes("/api/facets?preset=ag_zoning&dataset_type=zoning") == ["A"]


def test_facets_key_includes_preset(codes_file):
    import app as A
    assert A._facets_key({"county": "beta"}) != A._facets_key({"county": "beta", "preset": "ag_zoning"})
    # Unchanged for requests without a preset (disk-cached entries stay valid).
    assert A._facets_key({"county": "beta"}) == json.dumps([["county", "beta"]])


def test_facets_recomputed_when_codes_file_changes(client, codes_file):
    import os
    q = "/api/facets?county=beta&preset=ag_zoning"
    assert [r["zoning_code"] for r in _get(client, q)["zoning_codes"]] == ["AG"]
    _write(codes_file, {"counties": {"beta": {"A": "x", "AG": "y"}}})
    st = codes_file.stat()
    os.utime(codes_file, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert [r["zoning_code"] for r in _get(client, q)["zoning_codes"]] == ["A", "AG"]


def test_filter_counts_with_preset(client):
    d = _get(client, "/api/filter_counts?preset=ag_zoning")
    assert d["counties"] == {"alpha": 3, "beta": 1}
    assert d["datasets"] == {"parcels": 3, "zoning": 1}
    d = _get(client, "/api/filter_counts?preset=ag_zoning&dataset_type=zoning")
    assert d["counties"] == {"alpha": 1, "beta": 0}
    d = _get(client, "/api/filter_counts?preset=ag_zoning&county=beta")
    assert d["datasets"] == {"parcels": 1, "zoning": 0}
    # Intersected with an explicit zoning selection, per county.
    d = _get(client, "/api/filter_counts?preset=ag_zoning&zoning_code=A")
    assert d["counties"] == {"alpha": 2, "beta": 0}
    # Without the preset nothing changes.
    d = _get(client, "/api/filter_counts?zoning_code=A")
    assert d["counties"] == {"alpha": 2, "beta": 1}


def test_presets_endpoint(client):
    d = _get(client, "/api/presets")
    assert d["ag_zoning"]["label"] == "Agricultural zoning"
    assert d["ag_zoning"]["counties"] == {"alpha": 2, "beta": 1}


# --- a missing or broken codes file ------------------------------------------

@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]", '{"counties": []}',
                                     '{"counties": {"alpha": ["A"], "beta": null}}'])
def test_missing_or_garbled_file_matches_nothing(client, codes_file, content):
    if content is None:
        codes_file.unlink()
    else:
        _write(codes_file, content)
    assert _ids(client, "/api/features?preset=ag_zoning") == []
    assert _ids(client, "/api/features?preset=ag_zoning&county=alpha") == []
    assert _get(client, "/api/filter_counts?preset=ag_zoning")["counties"] == {"alpha": 0, "beta": 0}
    assert _get(client, "/api/presets")["ag_zoning"]["counties"] == {}
    # Everything else still works.
    assert _ids(client, "/api/features") == EVERYTHING


def test_malformed_county_entry_is_skipped(client, codes_file):
    _write(codes_file, {"counties": {"alpha": ["A"], "beta": {"AG": "x", "": "blank"}}})
    assert _ids(client, "/api/features?preset=ag_zoning") == ["B-2"]


# --- bound variables -----------------------------------------------------------

def test_many_codes_stay_bound(client, codes_file):
    many = {f"C{i}": "x" for i in range(5000)}
    _write(codes_file, {"counties": {"alpha": {**many, "A": "x"}, "beta": {**many, "AG": "x"}}})
    assert _ids(client, "/api/features?preset=ag_zoning") == ["A-1", "B-2", "Z-1"]


def test_inlined_codes_are_quoted(client, codes_file, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "MAX_PRESET_BOUND", 1)
    _write(codes_file, {"counties": {"alpha": {"A": "x", "O'Brien": "x"}, "beta": {"AG": "x"}}})
    sql, params = A.preset_clause({"preset": "ag_zoning"})
    assert params == [] and "'O''Brien'" in sql
    assert _ids(client, "/api/features?preset=ag_zoning") == ["A-1", "B-2", "Z-1"]


# --- the real file -------------------------------------------------------------

def test_real_file_has_contracted_shape():
    data = json.loads(REAL_FILE.read_text(encoding="utf-8"))
    assert isinstance(data["_notes"], str) and data["_notes"]
    assert isinstance(data["counties"], dict) and data["counties"]
    for county, codes in data["counties"].items():
        assert county == county.lower() and " " not in county, county
        assert isinstance(codes, dict) and codes, county
        for code, desc in codes.items():
            assert isinstance(code, str) and code.strip(), (county, code)
            assert isinstance(desc, str), (county, code)
    assert isinstance(data["review"], dict)
    for county, notes in data["review"].items():
        assert isinstance(notes, dict)
        assert all(isinstance(v, str) and v for v in notes.values())
    # The same code string can be agricultural in one county only.
    assert "A" in data["counties"].get("volusia", {})
    assert "A" not in data["counties"].get("palm_beach", {})
