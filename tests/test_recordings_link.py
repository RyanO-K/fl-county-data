import dor_values as D
import recordings as R


def _owner(conn, pid, name, clerk1=None):
    conn.execute(D.OWNER_UPSERT, D.owner_row_from_attrs(
        {"CO_NO": 39, "PARCEL_ID": pid, "OWN_NAME": name, "CLERK_NO1": clerk1}, "t"))


def _inst(no, cat, parties, recorded="2026-08-21"):
    return {"instrument_no": no, "doc_type": cat.upper(), "doc_desc": cat.upper(), "category": cat,
            "book": None, "page": None, "recorded_at": recorded, "consideration": None,
            "legal_desc": None, "parties": parties}


def test_links_by_clerk_no_and_owner_name_with_ambiguity_cutoff(conn):
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    _owner(conn, "P1", "DOE JANE", clerk1="2026000001")
    _owner(conn, "P2", "UNIQUE OWNER LLC")
    for i in range(6):
        _owner(conn, f"C{i}", "SMITH JOHN")
    R.write_instruments(conn, "hillsborough", [
        _inst("2026000001", "deed", [("grantor", 1, "SELLER SAM"), ("grantee", 1, "NOBODY HERE")]),
        _inst("2026000002", "mortgage", [("grantor", 1, "Unique Owner, LLC"), ("grantee", 1, "BIG BANK")]),
        _inst("2026000003", "lien", [("grantor", 1, "SMITH JOHN"), ("grantee", 1, "HOA")]),
    ], "D1", "t")
    assert R.link_instruments(conn, "hillsborough") == 2
    links = conn.execute("SELECT instrument_no, parcel_id, method FROM instrument_parcels ORDER BY 1").fetchall()
    assert links == [("2026000001", "P1", "clerk_no"), ("2026000002", "P2", "owner_name")]
    assert R.link_instruments(conn, "hillsborough") == 0  # idempotent


def test_link_stores_parcel_key(conn):
    D.ensure_schema(conn); D.ensure_owner_schema(conn); R.ensure_schema(conn)
    _owner(conn, "12-34-56", "OWNER ONE")
    R.write_instruments(conn, "hillsborough", [_inst("9", "mortgage", [("grantor", 1, "OWNER ONE")])], "D1", "t")
    R.link_instruments(conn, "hillsborough")
    assert conn.execute("SELECT parcel_key FROM instrument_parcels").fetchone()[0] == "123456"
