import recordings as R

REC = {
    "instrument_no": "1", "doc_type": "MTG", "doc_desc": "MORTGAGE", "category": "mortgage",
    "book": None, "page": None, "recorded_at": "2026-08-21T07:44", "consideration": None,
    "legal_desc": "L 1 B 1 TEST SUB",
    "parties": [("grantor", 1, "Smith, John"), ("grantee", 1, "BIG BANK NA")],
}


def test_write_then_rewrite_replaces_parties(conn):
    R.ensure_schema(conn)
    assert R.write_instruments(conn, "hillsborough", [REC], "D1", "2026-09-16T00:00:00") == 1
    rec2 = dict(REC, parties=[("grantor", 1, "Smith, John")], consideration=61300.0)
    R.write_instruments(conn, "hillsborough", [rec2], "D2", "2026-09-17T00:00:00")
    rows = conn.execute("SELECT consideration, source_file FROM recorded_instruments").fetchall()
    assert rows == [(61300.0, "D2")]
    parties = conn.execute(
        "SELECT role, seq, name, name_norm, name_key FROM instrument_parties ORDER BY role").fetchall()
    assert parties == [("grantor", 1, "Smith, John", "SMITH JOHN", "SMITH JOHN")]


def test_file_tracking(conn):
    R.ensure_schema(conn)
    assert not R.file_loaded(conn, "hernando", "01-05-24.csv")
    R.mark_file_loaded(conn, "hernando", "01-05-24.csv", 10, "2026-09-16T00:00:00")
    assert R.file_loaded(conn, "hernando", "01-05-24.csv")
    assert not R.file_loaded(conn, "hillsborough", "01-05-24.csv")
