import make_demo_db as M
import recordings as R


def test_schema_filter_drops_private_tables(conn):
    R.ensure_schema(conn)
    keep = list(M.schema_statements(conn))
    text = " ".join(keep)
    for t in R.PRIVATE_TABLES:
        assert t not in text
    assert "CREATE TABLE features" in text
