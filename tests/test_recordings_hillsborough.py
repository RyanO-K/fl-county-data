from pathlib import Path

import recordings as R

FX = Path(__file__).parent / "fixtures" / "recordings" / "hillsborough"


def _load():
    return R.parse_hillsborough(
        (FX / "D2026082101id.29").read_bytes(), (FX / "P2026082101id.29").read_bytes())


def test_parses_document_fields():
    recs = {r["instrument_no"]: r for r in _load()}
    d = recs["2026327007"]
    assert d["doc_type"] == "D"
    assert d["doc_desc"] == "DEED"
    assert d["category"] == "deed"
    assert d["legal_desc"] == "PT L 34 GIBSONTON ON THE BAY"
    assert d["recorded_at"] == "2026-08-21T07:44"
    assert d["consideration"] == 10.0
    assert d["book"] is None and d["page"] is None


def test_blank_consideration_is_none():
    recs = {r["instrument_no"]: r for r in _load()}
    assert recs["2026326987"]["consideration"] is None
    assert recs["2026326987"]["category"] == "other"


def test_parties_attached_with_roles():
    recs = {r["instrument_no"]: r for r in _load()}
    parties = recs["2026326987"]["parties"]
    assert ("grantor", 1, "MANGIONE RALPH") in parties
    assert ("grantee", 1, "FULLER ROBERT ALLEN JR") in parties
    assert ("grantee", 3, "PROGRESSIVE SELECT INSURANCE COMPANY") in parties


def test_file_sets_pair_d_and_p_by_date():
    names = ["D2026082101id.29", "P2026082101id.29", "M2026082101d.29",
             "D2026082201id.29", "readme.txt"]
    assert R.hillsborough_file_sets(names) == [("20260821", "D2026082101id.29", "P2026082101id.29")]
