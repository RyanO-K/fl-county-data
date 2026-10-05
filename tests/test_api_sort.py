"""Server-side column sorting for the Browse (/api/features) and Parcel Values
(/api/values) tables: whitelisted keys, asc/desc, missing values last in both
directions, a deterministic tiebreak, and the old URLs still working."""
import json
import threading

import pytest

import dor_values as D


def _app():
    # Imported lazily, after the conn fixture has reset the test DB, like the
    # other API tests: importing app starts a warm-up thread that opens it.
    import app as A
    return A


def _join_warmup():
    # Importing app starts the facets warm-up thread, which holds its own
    # connection to the test DB; let it finish before the test builds tables.
    for t in threading.enumerate():
        if t.name == "facets-warmup":
            t.join(timeout=30)


@pytest.fixture
def client(conn):
    A = _app()
    _join_warmup()
    D.ensure_schema(conn)
    rows = [
        # key, county, city, acreage, zoning, just_value, sale_date, synced
        ("F-1", "pasco", "Dade City", 5.0, "AG", 300.0, "2020-01", "2026-01-03"),
        ("F-2", "hernando", "", 1.0, "RS", None, None, "2026-01-01"),
        ("F-3", "pasco", None, None, None, 100.0, "2019-05", "2026-01-02"),
        ("F-4", "hernando", "Brooksville", 5.0, "CO", 300.0, "", "2026-01-04"),
        ("F-5", "pasco", "Zephyrhills", 2.5, "", 200.0, "2021-07", "2026-01-05"),
    ]
    conn.executemany(
        "INSERT INTO features (county, dataset_type, feature_key, feature_key_norm, city, acreage, "
        "zoning_code, just_value, sale_date, last_synced_at) VALUES (?, 'parcels', ?, ?, ?, ?, ?, ?, ?, ?)",
        [(c, k, k.replace("-", ""), city, ac, z, jv, sd, t) for k, c, city, ac, z, jv, sd, t in rows])
    vals = [
        # county, parcel_id, just_value, site_address, sale_year, sale_month, year_built
        ("pasco", "P3", 300.0, "3 Oak St", 2020, 1, 1990),
        ("pasco", "P1", None, "", 2019, 12, None),
        ("hernando", "H1", 100.0, None, 2020, 6, 2005),
        ("hernando", "H2", 300.0, "1 Elm St", None, None, 1975),
        ("pasco", "P2", 200.0, "2 Ash St", 2020, 3, 2010),
    ]
    conn.executemany(
        "INSERT INTO parcel_values (county, co_no, parcel_id, parcel_key, just_value, site_address, "
        "sale_year, sale_month, year_built, last_synced_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 't')",
        [(c, p, p, jv, a, y, m, yb) for c, p, jv, a, y, m, yb in vals])
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
    return [r["feature_key"] for r in _get(client, q)["rows"]]


def _pids(client, q):
    return [r["parcel_id"] for r in _get(client, q)["rows"]]


# --- order_by_clause -------------------------------------------------------

def test_order_by_clause_shapes(client):
    A = _app()
    num = (("just_value",), A.NUM)
    text = (("city",), A.TEXT)
    assert A.order_by_clause(num, "ASC", ["id"]) == "just_value ASC NULLS LAST, id ASC"
    assert A.order_by_clause(num, "DESC", ["id"]) == "just_value DESC, id DESC"
    assert A.order_by_clause(text, "ASC", ["id"]) == "(city IS NULL OR city = ''), city ASC, id ASC"
    assert A.order_by_clause(text, "DESC", ["id"]) == "city DESC, id DESC"
    # A tiebreak column already in the key is not repeated.
    assert A.order_by_clause(A.VALUES_SORT_COLUMNS["county"], "ASC", ["county", "parcel_id"]) == \
        "county ASC, parcel_id ASC"


def test_sort_direction(client):
    A = _app()
    assert A.sort_direction("asc") == "ASC"
    assert A.sort_direction("DESC") == "DESC"
    assert A.sort_direction(None) == "ASC"
    assert A.sort_direction("sideways", "DESC") == "DESC"
    assert A.sort_direction("1; DROP TABLE features") == "ASC"


def test_every_sort_key_runs(client):
    A = _app()
    for key in A.FEATURES_SORT_COLUMNS:
        for d in ("asc", "desc"):
            assert len(_get(client, f"/api/features?sort={key}&dir={d}")["rows"]) == 5
    for key in A.VALUES_SORT_COLUMNS:
        for d in ("asc", "desc"):
            assert len(_get(client, f"/api/values?sort={key}&dir={d}")["rows"]) == 5


# --- /api/features ---------------------------------------------------------

def test_features_default_is_id_order(client):
    data = _get(client, "/api/features")
    assert [r["feature_key"] for r in data["rows"]] == ["F-1", "F-2", "F-3", "F-4", "F-5"]
    assert (data["sort"], data["dir"]) == ("id", "asc")


def test_features_unknown_sort_falls_back_to_id(client):
    for q in ("sort=nope", "sort=nope&dir=desc", "sort=geometry_geojson", "sort=id;DROP%20TABLE%20features"):
        data = _get(client, f"/api/features?{q}")
        assert [r["feature_key"] for r in data["rows"]] == ["F-1", "F-2", "F-3", "F-4", "F-5"]
        assert data["sort"] == "id"


def test_features_numeric_nulls_last_both_ways(client):
    # Ties (F-1/F-4 at 300) break on id in the same direction.
    assert _keys(client, "/api/features?sort=just_value&dir=asc") == ["F-3", "F-5", "F-1", "F-4", "F-2"]
    assert _keys(client, "/api/features?sort=just_value&dir=desc") == ["F-4", "F-1", "F-5", "F-3", "F-2"]
    assert _keys(client, "/api/features?sort=acreage&dir=asc") == ["F-2", "F-5", "F-1", "F-4", "F-3"]
    assert _keys(client, "/api/features?sort=acreage&dir=desc") == ["F-4", "F-1", "F-5", "F-2", "F-3"]


def test_features_text_blanks_last_both_ways(client):
    # city: F-2 is '' and F-3 is NULL; both go last.
    assert _keys(client, "/api/features?sort=city&dir=asc")[:3] == ["F-4", "F-1", "F-5"]
    assert set(_keys(client, "/api/features?sort=city&dir=asc")[3:]) == {"F-2", "F-3"}
    assert _keys(client, "/api/features?sort=city&dir=desc")[:3] == ["F-5", "F-1", "F-4"]
    assert set(_keys(client, "/api/features?sort=city&dir=desc")[3:]) == {"F-2", "F-3"}
    assert _keys(client, "/api/features?sort=zoning_code&dir=asc")[:3] == ["F-1", "F-4", "F-2"]
    assert _keys(client, "/api/features?sort=sale_date&dir=asc")[:3] == ["F-3", "F-1", "F-5"]


def test_features_county_sort_is_total(client):
    assert _keys(client, "/api/features?sort=county&dir=asc") == ["F-2", "F-4", "F-1", "F-3", "F-5"]
    assert _keys(client, "/api/features?sort=county&dir=desc") == ["F-5", "F-3", "F-1", "F-4", "F-2"]


def test_features_dir_defaults_to_asc(client):
    assert _keys(client, "/api/features?sort=last_synced_at") == ["F-2", "F-3", "F-1", "F-4", "F-5"]
    assert _get(client, "/api/features?sort=last_synced_at&dir=bogus")["dir"] == "asc"


def test_features_pagination_is_deterministic(client):
    seen = []
    for page in (1, 2, 3):
        seen += _keys(client, f"/api/features?sort=just_value&dir=desc&per_page=2&page={page}")
    assert seen == _keys(client, "/api/features?sort=just_value&dir=desc")
    assert sorted(seen) == ["F-1", "F-2", "F-3", "F-4", "F-5"]


def test_features_sort_combines_with_filters(client):
    assert _keys(client, "/api/features?county=pasco&sort=just_value&dir=desc") == ["F-1", "F-5", "F-3"]
    assert _get(client, "/api/features?county=pasco&sort=just_value")["total"] == 3


def test_features_map_feed_ignores_sort(client):
    data = _get(client, "/api/features?geometry=1&sort=just_value&dir=desc")
    assert [r["feature_key"] for r in data["rows"]] == ["F-1", "F-2", "F-3", "F-4", "F-5"]


# --- /api/values -----------------------------------------------------------

def test_values_default_unchanged(client):
    data = _get(client, "/api/values")
    assert (data["sort"], data["dir"]) == ("just_value", "desc")
    # 300s tie on (county, parcel_id) desc: pasco/P3 before hernando/H2.
    assert [r["parcel_id"] for r in data["rows"]] == ["P3", "H2", "P2", "H1", "P1"]


def test_values_legacy_urls(client):
    assert _pids(client, "/api/values?sort=just_value&dir=asc") == ["H1", "P2", "H2", "P3", "P1"]
    assert _pids(client, "/api/values?sort=parcel_id&dir=asc") == ["H1", "H2", "P1", "P2", "P3"]
    assert _pids(client, "/api/values?sort=land_value") == _pids(client, "/api/values?sort=land_value&dir=desc")
    # Unknown key: just_value, in the requested direction.
    assert _pids(client, "/api/values?sort=bogus&dir=asc") == ["H1", "P2", "H2", "P3", "P1"]


def test_values_sale_year_orders_by_year_then_month(client):
    # Both columns follow the direction; the undated sale goes last.
    assert _pids(client, "/api/values?sort=sale_year&dir=desc") == ["H1", "P2", "P3", "P1", "H2"]
    assert _pids(client, "/api/values?sort=sale_year&dir=asc") == ["P1", "P3", "P2", "H1", "H2"]


def test_values_text_blanks_last(client):
    assert _pids(client, "/api/values?sort=site_address&dir=asc")[:3] == ["H2", "P2", "P3"]
    assert set(_pids(client, "/api/values?sort=site_address&dir=asc")[3:]) == {"P1", "H1"}
    assert _pids(client, "/api/values?sort=site_address&dir=desc")[:3] == ["P3", "P2", "H2"]


def test_values_year_built_nulls_last(client):
    assert _pids(client, "/api/values?sort=year_built&dir=asc") == ["H2", "P3", "H1", "P2", "P1"]
    assert _pids(client, "/api/values?sort=year_built&dir=desc") == ["P2", "H1", "P3", "H2", "P1"]


def test_values_pagination_is_deterministic(client):
    seen = []
    for page in (1, 2, 3):
        seen += _pids(client, f"/api/values?sort=just_value&per_page=2&page={page}")
    assert seen == _pids(client, "/api/values?sort=just_value")
