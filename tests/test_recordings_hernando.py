from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings" / "hernando" / "12-27-2024.csv"


def test_rows_collapse_to_one_instrument_with_deduped_parties():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    r = recs["2024077386"]
    assert r["doc_desc"] == "JUDGMENT"
    assert r["category"] == "judgment"
    assert r["book"] == "4502" and r["page"] == "426"
    assert r["recorded_at"] == "2024-12-16T10:51:32"
    grantors = [n for role, _, n in r["parties"] if role == "grantor"]
    grantees = [n for role, _, n in r["parties"] if role == "grantee"]
    assert grantors == ["PROGRESSIVE SELECT INS CO", "DAVILA AMANDA SHEAVON"]
    assert "KROLLMCDOWELL SHELLEY" in grantees and len(grantees) == len(set(grantees))


def test_placeholder_legal_is_none():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    assert recs["2024077385"]["legal_desc"] is None


def test_real_legal_is_kept():
    recs = {r["instrument_no"]: r for r in R.parse_hernando(FX.read_bytes())}
    assert recs["2024077431"]["legal_desc"] == "L42 Blk Un SubSPRING RIDGE S T R"
    assert recs["2024077431"]["category"] == "deed"
    assert recs["2024077431"]["consideration"] is None


def test_file_date_both_formats():
    assert R.hernando_file_date("01-05-24.csv") == "2024-01-05"
    assert R.hernando_file_date("12-27-2024.csv") == "2024-12-27"
    assert R.hernando_file_date("readme.txt") is None
