"""One-off: build the covering facet indexes on the main database.

etl.py defines FACET_INDEXES but deliberately never creates them on the main
DB during a sync (the build holds the write lock for minutes). Run this when no
ETL is running; the web UI keeps reading through WAL while it works.

    .venv\\Scripts\\python.exe scripts\\build_facet_indexes.py

Pass a different path as the first argument to target another database.
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etl  # noqa: E402

db = Path(sys.argv[1]) if len(sys.argv) > 1 else etl.DEFAULT_DB_PATH


def index_names(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='features'")]


conn = sqlite3.connect(str(db), timeout=60)
print(f"database: {db}")
print("existing indexes:", index_names(conn), flush=True)
for sql in etl.FACET_INDEXES:
    name = sql.split(" ON ")[0].split()[-1]
    t = time.time()
    conn.execute(sql)
    conn.commit()
    print(f"{name}: {time.time() - t:.0f}s", flush=True)
t = time.time()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
print(f"checkpoint: {time.time() - t:.0f}s", flush=True)
print("indexes now:", index_names(conn), flush=True)
conn.close()
