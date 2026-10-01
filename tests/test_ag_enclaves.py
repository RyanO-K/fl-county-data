"""The "Ag enclaves" filter (?ag_enclave=1): parcels in an agricultural
zoning district that ag_encroachment.py found surrounded by
development. Read from the analysis output (ag_encroachment.db, attached
read-only) or, in the demo database, from its own ag_enclaves table."""
import json
import sqlite3
import threading

import pytest

import dor_values as D
import recordings as R

ALL = ["A-1", "A-2", "A-3", "A-4", "B-1", "Z-1"]


@pytest.fixture
def client(conn, tmp_path, monkeypatch):
    import app as A
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute(
        "INSERT INTO features (id, county, dataset_type, feature_key, feature_key_norm, zoning_code, "
        "land_use_code, acreage, last_synced_at) VALUES "
        "(1,'alpha','parcels','A-1','A1','A','5100',10,'t'), "
        "(2,'alpha','parcels','A-2','A2','A','5100',20,'t'), "
        "(3,'alpha','parcels','A-3','A3','RS','0100',1,'t'), "
        "(4,'alpha','parcels','A-4','A4','A','0100',5,'t'), "
        "(5,'beta','parcels','B-1','B1','AG','6000',40,'t'), "
        "(6,'alpha','zoning','Z-1','Z1','A',NULL,200,'t')")
    conn.commit()
    results = tmp_path / "ag_encroachment.db"
    out = sqlite3.connect(results)
    out.execute("CREATE TABLE results (county TEXT, feature_id INTEGER, surrounded INTEGER, "
                "ag_by_land_use INTEGER, ag_by_zoning INTEGER, PRIMARY KEY (county, feature_id))")
    out.executemany("INSERT INTO results VALUES (?,?,?,?,?)", [
        ("alpha", 1, 1, 1, 1),    # enclave
        ("alpha", 2, 0, 1, 1),    # ag by land use, not surrounded
        ("alpha", 4, 1, 0, 1),    # enclave (ag by zoning only)
        ("beta", 5, 1, 1, 0),     # surrounded, but ag by land use only
    ])
    out.commit(); out.close()
    monkeypatch.setattr(A, "AG_RESULTS_PATH", results)
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


def test_only_zoning_enclaves(client):
    assert _ids(client, "/api/features") == ALL
    assert _ids(client, "/api/features?ag_enclave=1") == ["A-1", "A-4"]
    assert _ids(client, "/api/features?ag_enclave=") == ALL


def test_composes_with_other_filters(client):
    assert _ids(client, "/api/features?ag_enclave=1&county=alpha") == ["A-1", "A-4"]
    assert _ids(client, "/api/features?ag_enclave=1&dataset_type=zoning") == []
    assert _ids(client, "/api/features?ag_enclave=1&min_acreage=10") == ["A-1"]
    assert _ids(client, "/api/features?ag_enclave=1&preset=nope") == ["A-1", "A-4"]
    g = _get(client, "/api/features/geometry?ag_enclave=1")
    assert sorted(r["feature_key"] for r in g["rows"]) == ["A-1", "A-4"]


def test_filter_counts(client):
    d = _get(client, "/api/filter_counts?ag_enclave=1")
    assert d["counties"] == {"alpha": 2, "beta": 0}
    assert d["datasets"] == {"parcels": 2, "zoning": 0}
    d = _get(client, "/api/filter_counts?ag_enclave=1&county=alpha")
    assert d["datasets"] == {"parcels": 2, "zoning": 0}


def test_endpoint(client):
    assert _get(client, "/api/ag_enclaves") == {"available": True, "counties": {"alpha": 2}}


def test_demo_table_wins_over_results_file(client, conn):
    conn.execute("CREATE TABLE ag_enclaves (county TEXT, feature_id INTEGER, PRIMARY KEY (county, feature_id))")
    conn.execute("INSERT INTO ag_enclaves VALUES ('alpha', 2)")
    conn.commit()
    assert _ids(client, "/api/features?ag_enclave=1") == ["A-2"]
    assert _get(client, "/api/ag_enclaves")["counties"] == {"alpha": 1}


def test_no_results_matches_nothing(client, tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "AG_RESULTS_PATH", tmp_path / "missing.db")
    assert _ids(client, "/api/features?ag_enclave=1") == []
    assert _get(client, "/api/ag_enclaves") == {"available": False, "counties": {}}
    assert _get(client, "/api/filter_counts?ag_enclave=1")["counties"] == {"alpha": 0, "beta": 0}
    assert _ids(client, "/api/features") == ALL


def test_demo_build_copies_enclaves(client, conn, tmp_path):
    import make_demo_db as M
    out = sqlite3.connect(tmp_path / "demo.db")
    import app as A
    n = M.copy_ag_enclaves(A.AG_RESULTS_PATH, out, ["alpha", "beta"])
    assert n == 2
    assert out.execute("SELECT county, feature_id FROM ag_enclaves ORDER BY 2").fetchall() == [("alpha", 1), ("alpha", 4)]
    assert M.copy_ag_enclaves(tmp_path / "missing.db", out, ["alpha"]) == 0
