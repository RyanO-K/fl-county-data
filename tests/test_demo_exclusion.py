import make_demo_db as M
import recordings as R


def test_schema_filter_drops_private_tables(conn):
    R.ensure_schema(conn)
    keep = list(M.schema_statements(conn))
    text = " ".join(keep)
    for t in R.PRIVATE_TABLES:
        if t not in M.DEMO_RECORDING_TABLES:
            assert t not in text
    for t in M.DEMO_RECORDING_TABLES:
        assert f"CREATE TABLE {t}" in text  # sqlite_master drops IF NOT EXISTS
    assert "instrument_parties" not in text and "parcel_owners" not in text
    assert "CREATE TABLE features" in text
