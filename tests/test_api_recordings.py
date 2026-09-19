import json
import sqlite3
import time

import pytest

import dor_values as D
import recordings as R


@pytest.fixture
def client(conn):
    import app as A
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    conn.execute("INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, last_synced_at) "
                 "VALUES ('hillsborough','parcels','A-1','A1','t'), ('hillsborough','parcels','B-2','B2','t')")
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(
        {"CO_NO": 39, "PARCEL_ID": "A-1", "OWN_NAME": "DOE JANE", "OWN_ADDR1": "1 MAIN", "OWN_CITY": "TAMPA",
         "OWN_STATE": "FL", "OWN_ZIPCD": 33601}, "t"))
    R.write_instruments(conn, "hillsborough", [
        {"instrument_no": "10", "doc_type": "MTG", "doc_desc": "MORTGAGE", "category": "mortgage", "book": None,
         "page": None, "recorded_at": "2026-08-21T07:44", "consideration": 61300.0, "legal_desc": None,
         "parties": [("grantor", 1, "DOE JANE"), ("grantee", 1, "BIG BANK")]},
        {"instrument_no": "11", "doc_type": "LN", "doc_desc": "LIEN", "category": "lien", "book": None,
         "page": None, "recorded_at": "2026-08-22", "consideration": None, "legal_desc": None,
         "parties": [("grantor", 1, "DOE JANE"), ("grantee", 1, "HOA")]},
    ], "D1", "t")
    R.link_instruments(conn, "hillsborough")
    conn.commit()
    A.app.config["TESTING"] = True
    return A.app.test_client()


def _ids(resp):
    return sorted(r["feature_key"] for r in json.loads(resp.data)["rows"])


def test_owner_endpoint(client):
    fid = json.loads(client.get("/api/features?county=hillsborough").data)["rows"][0]["id"]
    r = client.get(f"/api/feature/{fid}/owner")
    assert r.status_code == 200
    assert json.loads(r.data)["owner_name"] == "DOE JANE"


def test_instruments_endpoint_newest_first(client):
    fid = json.loads(client.get("/api/features?county=hillsborough").data)["rows"][0]["id"]
    data = json.loads(client.get(f"/api/feature/{fid}/instruments").data)["instruments"]
    assert [d["instrument_no"] for d in data] == ["11", "10"]
    assert data[1]["consideration"] == 61300.0
    assert {"role": "grantee", "name": "BIG BANK"} in data[1]["parties"]


def test_mortgage_and_lien_filters(client):
    assert _ids(client.get("/api/features?mortgage_since=2026-08-01")) == ["A-1"]
    assert _ids(client.get("/api/features?mortgage_since=2026-09-01")) == []
    assert _ids(client.get("/api/features?mortgage_min=50000")) == ["A-1"]
    assert _ids(client.get("/api/features?mortgage_max=50000")) == []
    assert _ids(client.get("/api/features?has_lien=1")) == ["A-1"]
    assert _ids(client.get("/api/features")) == ["A-1", "B-2"]


def test_has_mortgage_filter(client, conn):
    # B-2 gets a mortgage with no amount (Hernando-style feed): it counts as
    # "has a mortgage on file" but not as "mortgage amount known".
    R.write_instruments(conn, "hillsborough", [
        {"instrument_no": "12", "doc_type": "MTG", "doc_desc": "MORTGAGE", "category": "mortgage", "book": None,
         "page": None, "recorded_at": "2026-07-01", "consideration": None, "legal_desc": None,
         "parties": [("grantor", 1, "ROE RICHARD"), ("grantee", 1, "BIG BANK")]},
    ], "D2", "t")
    conn.execute("INSERT INTO instrument_parcels (county, instrument_no, parcel_id, parcel_key, method) "
                 "VALUES ('hillsborough','12','B-2','B2','test')")
    conn.commit()
    assert _ids(client.get("/api/features?has_mortgage=1")) == ["A-1", "B-2"]
    assert _ids(client.get("/api/features?has_mortgage=amount")) == ["A-1"]
    # Combines with the other mortgage filters (same instrument must match all).
    assert _ids(client.get("/api/features?has_mortgage=1&mortgage_since=2026-08-01")) == ["A-1"]
    assert _ids(client.get("/api/features?has_mortgage=amount&mortgage_max=50000")) == []
    # Unknown values are ignored rather than erroring.
    assert _ids(client.get("/api/features?has_mortgage=bogus")) == ["A-1", "B-2"]
    # The map feed and the facet endpoint share build_filters.
    geo = json.loads(client.get("/api/features/geometry?has_mortgage=amount").data)
    assert [r["feature_key"] for r in geo["rows"]] == ["A-1"]


def test_facets_disk_cache_roundtrip(client, conn):
    import app as A
    A._facets_cache.clear()
    data = A.cached_facets(conn, {})
    assert data["zoning_codes"] == [] and "land_use_codes" in data
    assert A._facets_disk_path().exists()
    # A fresh process (empty memory cache) picks the entry up from disk as
    # long as the database file has not changed since it was saved.
    A._facets_cache.clear()
    assert A._load_disk_facets()[A._facets_key({})] == data


def test_facets_concurrent_requests_compute_once(client, conn, monkeypatch):
    import threading
    import app as A
    A._facets_cache.clear()
    calls = []
    real = A.compute_facets

    def slow(c, args):
        calls.append(1)
        time.sleep(0.2)
        return real(c, args)
    monkeypatch.setattr(A, "compute_facets", slow)
    results = []

    def worker():
        c = sqlite3.connect(f"file:{A.DB_PATH.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            results.append(A.cached_facets(c, {"dataset_type": "parcels"}))
        finally:
            c.close()
    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(calls) == 1 and len(results) == 3 and results[0] == results[1] == results[2]


def test_status_has_recordings_cell(client):
    data = json.loads(client.get("/api/status/counties").data)
    assert "recordings" in data["dataset_types"]
    row = next(r for r in data["counties"] if r["county"] == "hillsborough")
    assert row["datasets"]["recordings"]["row_count"] == 2
