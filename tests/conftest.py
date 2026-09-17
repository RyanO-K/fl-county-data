import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_TMP_DB = Path(os.environ.get("PYTEST_TMPDIR", ROOT / "tests" / ".tmp")) / "test_county.db"
_TMP_DB.parent.mkdir(parents=True, exist_ok=True)
# etl.py reads FL_COUNTY_DB at import time, so it must be set before the
# first project import anywhere in the test session.
os.environ["FL_COUNTY_DB"] = str(_TMP_DB)


@pytest.fixture
def tmp_db_path():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_TMP_DB) + suffix)
        if p.exists():
            p.unlink()
    return _TMP_DB


@pytest.fixture
def conn(tmp_db_path):
    import etl
    c = etl.get_conn()
    yield c
    c.close()
