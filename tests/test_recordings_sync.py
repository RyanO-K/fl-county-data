from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings"


def _fake_hillsborough():
    files = {n: (FX / "hillsborough" / n).read_bytes()
             for n in ("D2026082101id.29", "P2026082101id.29")}
    calls = {"fetch": 0}

    def fetch(name):
        calls["fetch"] += 1
        return files[name]
    src = dict(R.SOURCES["hillsborough"], list=lambda: list(files) + ["readme.txt"], fetch=fetch)
    return src, calls


def test_sync_county_loads_units_once(conn):
    src, calls = _fake_hillsborough()
    assert R.sync_county(conn, "hillsborough", src) is True
    n = conn.execute("SELECT COUNT(*) FROM recorded_instruments WHERE county='hillsborough'").fetchone()[0]
    assert n > 10
    assert conn.execute("SELECT county, file_name, rows FROM recording_files").fetchall() == [("hillsborough", "20260821", n)]
    assert R.file_loaded(conn, "hillsborough", "20260821")
    assert calls["fetch"] == 2
    R.sync_county(conn, "hillsborough", src)
    assert calls["fetch"] == 2  # second run skipped the loaded unit
    status = conn.execute("SELECT status, rows_fetched FROM sync_log WHERE dataset_type='recordings' ORDER BY id").fetchall()
    assert status[0][0] == "success" and status[0][1] == n
    assert status[1] == ("success", 0)


def test_sync_county_records_failure(conn):
    src, _ = _fake_hillsborough()
    src = dict(src, list=lambda: (_ for _ in ()).throw(RuntimeError("listing down")))
    assert R.sync_county(conn, "hillsborough", src) is False
    row = conn.execute("SELECT status, error FROM sync_log WHERE dataset_type='recordings'").fetchone()
    assert row[0] == "failed" and "listing down" in row[1]


def test_broward_skips_without_credentials(conn, monkeypatch):
    monkeypatch.delenv("FL_BROWARD_FTP_USER", raising=False)
    assert R.sync_county(conn, "broward") is None
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
