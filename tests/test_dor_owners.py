import dor_values as D


def test_owner_row_from_attrs_normalizes():
    a = {"CO_NO": 58, "PARCEL_ID": "01-22-28-0000-00-001", "OWN_NAME": "Smith, John ET AL",
         "OWN_ADDR1": "1 MAIN ST", "OWN_ADDR2": " ", "OWN_CITY": "ORLANDO", "OWN_STATE": "FL",
         "OWN_ZIPCD": 32801, "OWN_STATE_": " ", "OR_BOOK1": " ", "OR_PAGE1": " ",
         "CLERK_NO1": "20260012345", "OR_BOOK2": "1234", "OR_PAGE2": "56", "CLERK_NO2": " "}
    row = D.owner_row_from_attrs(a, "2026-09-16T00:00:00")
    assert row[:3] == ("orange", "01-22-28-0000-00-001", "012228000000001")
    assert row[3:6] == ("Smith, John ET AL", "SMITH JOHN", "SMITH JOHN")
    assert row[6:11] == ("1 MAIN ST", None, "ORLANDO", "FL", "32801")
    assert row[11] is None
    assert row[12:18] == (None, None, "20260012345", "1234", "56", None)


def test_owner_row_skips_unknown_county():
    assert D.owner_row_from_attrs({"CO_NO": 0, "PARCEL_ID": "x"}, "t") is None


def test_owner_upsert_and_county_range(conn):
    D.ensure_schema(conn)
    D.ensure_owner_schema(conn)
    conn.execute("INSERT INTO parcel_values (county, co_no, parcel_id, parcel_key, source_objectid, last_synced_at) "
                 "VALUES ('orange', 58, 'A', 'A', 6429451, 't'), ('orange', 58, 'B', 'B', 6921026, 't'), "
                 "('polk', 63, 'C', 'C', 1, 't')")
    assert D.county_objectid_range(conn, "orange") == (6429451, 6921026)
    assert D.county_objectid_range(conn, "nowhere") is None
    a = {"CO_NO": 58, "PARCEL_ID": "A", "OWN_NAME": "ACME LLC", "OWN_ADDR1": "PO BOX 1"}
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(a, "t1"))
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(dict(a, OWN_NAME="ACME HOLDINGS LLC"), "t2"))
    rows = conn.execute("SELECT owner_name, last_synced_at FROM parcel_owners").fetchall()
    assert rows == [("ACME HOLDINGS LLC", "t2")]
