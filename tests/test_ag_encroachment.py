"""scripts/ag_encroachment.py against a synthetic features DB whose parcels
are hand-made squares with known answers. Shapes are drawn in EPSG:3086
metres around a point in central Florida and stored the way the ETL stores
them: WGS84 GeoJSON, zlib-compressed (etl.encode_json) or plain text."""
import hashlib
import json
import sqlite3

import numpy as np
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import box, mapping

import ag_encroachment as ae
import etl

RING_M = 660 * 0.3048
_TO_WGS = Transformer.from_crs("EPSG:3086", "EPSG:4326", always_xy=True)
_TO_PROJ = Transformer.from_crs("EPSG:4326", "EPSG:3086", always_xy=True)
X0, Y0 = _TO_PROJ.transform(-81.5, 28.5)


def to_wgs(geom, dx=0.0, dy=0.0):
    """Local metre geometry (offset by dx, dy km-scale) -> WGS84 GeoJSON dict."""
    moved = shapely.transform(geom, lambda xy: xy + (X0 + dx, Y0 + dy))
    wgs = shapely.transform(moved, lambda xy: np.column_stack(_TO_WGS.transform(xy[:, 0], xy[:, 1])))
    return mapping(wgs)


class FixtureDB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(etl.SCHEMA)
        self.conn.execute("ALTER TABLE features ADD COLUMN dor_use_code TEXT")
        self.n = 0

    def add(self, county, geom, land_use=None, dor=None, zoning=None, dataset="parcels",
            key=None, dx=0.0, dy=0.0, compress=True):
        self.n += 1
        gj = None
        if geom is not None:
            gj = to_wgs(geom, dx, dy)
            gj = etl.encode_json(gj) if compress else json.dumps(gj)
        cur = self.conn.execute(
            "INSERT INTO features (county, dataset_type, feature_key, land_use_code, dor_use_code, "
            "zoning_code, geometry_geojson, last_synced_at) VALUES (?,?,?,?,?,?,?, 'now')",
            (county, dataset, key or f"{county}-{self.n}", land_use, dor, zoning, gj))
        return cur.lastrowid

    def close(self):
        self.conn.commit()
        self.conn.close()


CENTER = box(0, 0, 400, 400)


def ring_tiles():
    """The 8 tiles around CENTER in a 3x3 grid of 400 m squares."""
    return [box(x, y, x + 400, y + 400)
            for x in (-400, 0, 400) for y in (-400, 0, 400) if (x, y) != (0, 0)]


@pytest.fixture
def fixture_db(tmp_path):
    db = FixtureDB(tmp_path / "source.db")
    ids = {}
    # 1. ag ringed by residential (county codes, Orange-style 4 digits)
    ids["surr"] = db.add("surr", CENTER, land_use="5100", key="SURR-AG")
    for t in ring_tiles():
        db.add("surr", t, land_use="0100", compress=False)
    # 2. half residential / half ag (DOR roll codes only)
    ids["half"] = db.add("half", CENTER, dor="066", key="HALF-AG")
    db.add("half", box(-1000, -1000, 200, 1400).difference(CENTER), dor="001")
    db.add("half", box(200, -1000, 1400, 1400).difference(CENTER), dor="061")
    # 3. residential on three sides past a 30 m road gap, water on the fourth
    ids["gaps"] = db.add("gaps", CENTER, land_use="066", key="GAPS-AG")
    db.add("gaps", box(-1000, -1000, -30, 1400), land_use="001")
    db.add("gaps", box(-30, 430, 430, 1400), land_use="001")
    db.add("gaps", box(-30, -1000, 430, -30), land_use="001")
    db.add("gaps", box(430, -1000, 1400, 1400), land_use="095")
    # 4. as 'half', but the residential side is a stack of 6 identical polygons
    ids["stack"] = db.add("stack", CENTER, dor="066", key="STACK-AG")
    for _ in range(6):
        db.add("stack", box(-1000, -1000, 200, 1400).difference(CENTER), dor="001")
    db.add("stack", box(200, -1000, 1400, 1400).difference(CENTER), dor="061")
    # 5. agricultural zoning on the parcel itself (JSON), incl. a comma list
    ids["zjson_a"] = db.add("zjson", CENTER, land_use="0100", zoning="AG-1  ", key="ZJ-A")
    ids["zjson_b"] = db.add("zjson", box(5000, 0, 5400, 400), land_use="0100",
                            zoning="A,A-1,PD-R", key="ZJ-B")
    for t in ring_tiles():
        db.add("zjson", t, land_use="0100", zoning="R-1")
    # 6. zoning from the zoning layer (parcels carry no zoning_code)
    db.add("zlayer", box(-2000, -2000, 2000, 2000), zoning="A-1", dataset="zoning")
    db.add("zlayer", box(2000, -2000, 6000, 2000), zoning="R-1", dataset="zoning")
    ids["zlayer_res"] = db.add("zlayer", CENTER, land_use="001", key="ZL-RES")      # ag by zoning
    ids["zlayer_ag"] = db.add("zlayer", box(-1000, 0, -600, 400), land_use="061", key="ZL-AG")  # both
    ids["zlayer_out"] = db.add("zlayer", box(3000, 0, 3400, 400), land_use="001", key="ZL-OUT")  # neither
    # 7. no DOR-based codes at all (Broward-like) -> county skipped
    for t in ring_tiles():
        db.add("nocodes", t)
    # 8. an isolated ag parcel, an ag parcel with no geometry, an invalid bowtie neighbour far away
    ids["lonely"] = db.add("lonely", CENTER, land_use="060", key="LONELY")
    ids["nogeom"] = db.add("lonely", None, land_use="060", key="NOGEOM")
    bowtie = shapely.Polygon([(50000, 0), (50400, 400), (50400, 0), (50000, 400), (50000, 0)])
    db.add("lonely", bowtie, land_use="001")
    db.close()
    ag_json = tmp_path / "ag_zoning_codes.json"
    ag_json.write_text(json.dumps({"counties": {"zjson": {"AG-1": "Agriculture", "A-1": "Ag"},
                                                "zlayer": {"A-1": "Agricultural"}}}))
    return {"db": db.path, "out": tmp_path / "out.db", "ag_json": ag_json, "ids": ids,
            "tmp": tmp_path}


def run(fx, *extra):
    return ae.main(["--db", str(fx["db"]), "--out", str(fx["out"]),
                    "--ag-zoning-json", str(fx["ag_json"]), *extra])


def results(fx, county=None):
    conn = sqlite3.connect(fx["out"])
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM results" + (" WHERE county = ?" if county else "")
    rows = {r["feature_key"]: dict(r) for r in conn.execute(q, (county,) if county else ())}
    conn.close()
    return rows


def runs(fx):
    conn = sqlite3.connect(fx["out"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY run_id")]
    conn.close()
    return rows


# --- code normalization ---------------------------------------------------------
@pytest.mark.parametrize("county,code,expected", [
    ("orange", "0103", "01"),          # DOR + subtype
    ("orange", "5100", "51"),
    ("miami_dade", "8647", "86"),
    ("alachua", "0100A", "01"),        # letter suffix
    ("baker", "001", "01"),            # zero-padded 3 digits
    ("baker", "066", "66"),
    ("baker", "100", None),            # not a DOR category
    ("citrus", "1", "01"),             # unpadded
    ("citrus", "66", "66"),
    ("lee", "09", "09"),
    ("putnam", "05600", "56"),         # 3-digit DOR + 2-digit subtype
    ("putnam", "00100", "01"),
    ("flagler", "002100", "21"),       # "00" + DOR + subtype
    ("flagler", "000100", "01"),
    ("santa_rosa", "100", "01"),       # unpadded 4-digit
    ("santa_rosa", "0", "00"),
    ("santa_rosa", "9900", "99"),
    ("santa_rosa", "1136", "11"),
    ("leon", "Rural", None),           # future-land-use names, not DOR
    ("leon", "001", None),
    ("duval", " ", None),
    ("nassau", "A.", None),
    ("brevard", None, None),
    ("unlisted_county", "0520", "05"),  # auto: 4 digits = DOR + subtype
    ("unlisted_county", "52", "52"),    # auto: short = the category itself
    ("unlisted_county", "123456", None),
])
def test_normalize_code_per_county_format(county, code, expected):
    assert ae.normalize_code(code, ae.county_code_format(county)) == expected


def test_resolve_category_priority():
    fmt = ae.county_code_format("highlands")
    assert ae.resolve_category("7", "066", fmt) == ("66", "dor_roll")
    assert ae.resolve_category("7", "066", fmt, priority="county") == ("07", "county")
    assert ae.resolve_category("7", None, fmt) == ("07", "county")
    assert ae.resolve_category(None, None, fmt) == (None, None)


def test_every_dor_category_has_a_class():
    for cat in ae.DOR_CATEGORIES:
        assert ae.category_class(cat) != "unknown", cat
    assert {c for c in ae.DOR_CATEGORIES if ae.category_class(c) == "agricultural"} == \
        {f"{n}" for n in range(50, 70)}
    assert ae.category_class("94") == "excluded" and ae.category_class("95") == "excluded"
    assert ae.category_class("97") == "open" and ae.category_class(None) == "unknown"


def test_ag_zoning_matching(tmp_path):
    assert ae.load_ag_zoning(tmp_path / "missing.json") == {}
    p = tmp_path / "z.json"
    p.write_text(json.dumps({"counties": {"x": {"A-1": "", "AG ": ""}}}))
    codes = ae.load_ag_zoning(p)["x"]
    assert ae.is_ag_zoning("a-1", codes) and ae.is_ag_zoning("AG      ", codes)
    assert ae.is_ag_zoning("RS,A-1", codes) and not ae.is_ag_zoning("R-1", codes)
    assert not ae.is_ag_zoning(None, codes) and not ae.is_ag_zoning("A-1", frozenset())


# --- end to end ------------------------------------------------------------------
def test_surrounded_by_residential(fixture_db):
    run(fixture_db, "--county", "surr")
    r = results(fixture_db, "surr")
    assert list(r) == ["SURR-AG"]
    row = r["SURR-AG"]
    assert row["ag_reason"] == "land_use" and row["dor_category"] == "51" and row["code_source"] == "county"
    assert row["surrounded"] == 1
    assert row["developed_share"] == pytest.approx(1.0)
    assert row["share_residential"] == pytest.approx(1.0)
    assert row["coverage"] == pytest.approx(1.0, abs=1e-3)
    assert row["low_coverage"] == 0
    assert row["n_neighbors"] == 8
    assert row["parcel_acres"] == pytest.approx(400 * 400 / 4046.8564224, rel=1e-3)
    assert row["lon"] == pytest.approx(-81.5, abs=0.01) and row["lat"] == pytest.approx(28.5, abs=0.01)


def test_half_residential_is_not_surrounded(fixture_db):
    run(fixture_db, "--county", "half")
    r = results(fixture_db, "half")
    row = r["HALF-AG"]
    assert row["code_source"] == "dor_roll"
    assert row["developed_share"] == pytest.approx(0.5, abs=0.01)
    assert row["share_agricultural"] == pytest.approx(0.5, abs=0.01)
    assert row["surrounded"] == 0
    assert len(r) == 2  # the eastern ag neighbour is itself evaluated


def test_roads_and_water_are_left_out_of_the_denominator(fixture_db):
    run(fixture_db, "--county", "gaps")
    row = results(fixture_db, "gaps")["GAPS-AG"]
    assert row["developed_share"] == pytest.approx(1.0)
    assert row["surrounded"] == 1
    assert 0.15 < row["excluded_frac"] < 0.35       # the water side
    assert row["coverage"] < 0.9                    # road gaps are uncovered
    assert row["classified_frac"] + row["excluded_frac"] == pytest.approx(row["coverage"], abs=1e-6)


def test_stacked_duplicates_do_not_double_count(fixture_db):
    run(fixture_db, "--county", "stack")
    row = results(fixture_db, "stack")["STACK-AG"]
    # six copies of the residential half would give 6/7 = 0.86 if summed
    assert row["developed_share"] == pytest.approx(0.5, abs=0.01)
    assert row["surrounded"] == 0
    assert row["coverage"] == pytest.approx(1.0, abs=1e-3)


def test_zoning_from_json_on_parcel(fixture_db):
    run(fixture_db, "--county", "zjson")
    r = results(fixture_db, "zjson")
    assert set(r) == {"ZJ-A", "ZJ-B"}
    for k in ("ZJ-A", "ZJ-B"):
        assert r[k]["ag_reason"] == "zoning" and r[k]["ag_by_land_use"] == 0
        assert r[k]["zoning_source"] == "parcel"
    assert r["ZJ-A"]["surrounded"] == 1
    assert r["ZJ-B"]["status"] == "no_classified_neighbors" and r["ZJ-B"]["surrounded"] is None


def test_zoning_from_zoning_layer(fixture_db):
    run(fixture_db, "--county", "zlayer")
    r = results(fixture_db, "zlayer")
    assert set(r) == {"ZL-RES", "ZL-AG"}
    assert r["ZL-RES"]["ag_reason"] == "zoning"
    assert r["ZL-RES"]["zoning_source"] == "layer" and r["ZL-RES"]["zoning_code"] == "A-1"
    assert r["ZL-AG"]["ag_reason"] == "land_use+zoning"


def test_missing_zoning_json_disables_zoning_test(fixture_db):
    fixture_db["ag_json"] = fixture_db["tmp"] / "does_not_exist.json"
    run(fixture_db, "--county", "zlayer", "--county", "zjson")
    assert set(results(fixture_db)) == {"ZL-AG"}


def test_county_without_dor_codes_is_skipped(fixture_db):
    summary = run(fixture_db, "--county", "nocodes")
    assert summary == {"nocodes": "skipped"}
    (row,) = runs(fixture_db)
    assert row["status"] == "skipped" and "DOR-based" in row["note"]
    assert results(fixture_db) == {}


def test_isolated_and_null_geometry(fixture_db):
    run(fixture_db, "--county", "lonely")
    r = results(fixture_db, "lonely")
    assert r["LONELY"]["coverage"] == 0 and r["LONELY"]["low_coverage"] == 1
    assert r["LONELY"]["surrounded"] is None
    assert r["NOGEOM"]["status"] == "no_geometry"


def test_all_counties_by_default_and_csv(fixture_db):
    csv_path = fixture_db["tmp"] / "out.csv"
    summary = run(fixture_db, "--csv", str(csv_path))
    assert summary == {"gaps": "done", "half": "done", "lonely": "done", "nocodes": "skipped",
                       "stack": "done", "surr": "done", "zjson": "done", "zlayer": "done"}
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",")[:2] == ["county", "feature_id"]
    assert len(lines) - 1 == len(results(fixture_db))


def test_threshold_flag(fixture_db):
    run(fixture_db, "--county", "half", "--threshold", "0.45")
    assert results(fixture_db, "half")["HALF-AG"]["surrounded"] == 1


def test_resume_skips_done_counties_and_rerun_replaces_only_that_county(fixture_db):
    run(fixture_db, "--county", "surr", "--county", "half")
    first = runs(fixture_db)
    assert [r["status"] for r in first] == ["done", "done"]
    before = results(fixture_db)

    run(fixture_db, "--county", "surr", "--county", "half", "--resume")
    assert len(runs(fixture_db)) == 2          # nothing re-run
    assert results(fixture_db) == before

    run(fixture_db, "--county", "surr")        # no --resume: surr is redone
    after = results(fixture_db)
    assert after["SURR-AG"]["run_id"] != before["SURR-AG"]["run_id"]
    assert after["HALF-AG"] == before["HALF-AG"]   # other county untouched

    run(fixture_db, "--county", "half", "--resume", "--threshold", "0.4")  # new params
    assert results(fixture_db)["HALF-AG"]["surrounded"] == 1
    assert len(runs(fixture_db)) == 4


def test_parallel_workers_match_serial(fixture_db):
    run(fixture_db, "--county", "surr", "--county", "stack", "--county", "nocodes")
    serial = {k: {c: v for c, v in r.items() if c != "run_id"} for k, r in results(fixture_db).items()}
    fixture_db["out"] = fixture_db["tmp"] / "out_parallel.db"
    summary = run(fixture_db, "--county", "surr", "--county", "stack", "--county", "nocodes",
                  "--workers", "2")
    assert summary == {"surr": "done", "stack": "done", "nocodes": "skipped"}
    parallel = {k: {c: v for c, v in r.items() if c != "run_id"} for k, r in results(fixture_db).items()}
    assert parallel == serial


def test_limit(fixture_db):
    run(fixture_db, "--county", "half", "--limit", "1")
    assert len(results(fixture_db, "half")) == 1


def test_source_db_is_read_only(fixture_db):
    digest = hashlib.sha256(fixture_db["db"].read_bytes()).hexdigest()
    conn = ae.open_source(fixture_db["db"])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM features WHERE county = 'surr'")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE x (a)")
    conn.close()
    run(fixture_db)
    assert hashlib.sha256(fixture_db["db"].read_bytes()).hexdigest() == digest


def test_output_must_differ_from_source(fixture_db):
    with pytest.raises(SystemExit):
        ae.main(["--db", str(fixture_db["db"]), "--out", str(fixture_db["db"])])


def test_missing_source_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        ae.open_source(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()
