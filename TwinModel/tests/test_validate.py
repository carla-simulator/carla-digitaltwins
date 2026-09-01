"""twinmodel.validate tests (worker C) -- no network."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from twinmodel.export.xodr import export_xodr
from twinmodel.model import TwinModel
from twinmodel.validate import main, summary, validate, write_report

from tests.synthetic_xodr import junction_model, shifted_junction_model, straight_road

carla = pytest.importorskip("carla")


def test_straight_road_passes(tmp_path):
    m = straight_road()
    rep = validate(m, export_xodr(m), out_dir=tmp_path)
    assert rep["topology"]["loaded"]
    assert rep["topology"]["waypoints"] > 0
    assert rep["lane_in_drivable"]["pass"] and rep["lane_in_drivable"]["fraction"] == 1.0
    assert rep["lane_coverage"]["fraction"] == 1.0 and rep["lane_coverage"]["missing"] == []
    assert rep["z_error"]["pass"] and rep["z_error"]["elevation"] == "synthetic"
    assert rep["z_error"]["p95"] < 0.02
    assert rep["landmarks"]["count"] == 1 and rep["landmarks"]["expected_signals"] == 1
    assert rep["sidewalk_coverage"]["ratio"] == pytest.approx(4.0 / 6.5, abs=0.02)
    assert rep["junction_containment"] is None
    assert rep["topology"]["dead_end_lane_count"] == 2  # open-ended street: both lanes end
    assert rep["topology"]["roads_no_successor"] == ["r1"]
    gj = json.loads((tmp_path / "violations.geojson").read_text())
    assert gj["features"] == []
    write_report(rep, tmp_path / "report.json")
    assert json.loads((tmp_path / "report.json").read_text())["lane_in_drivable"]["pass"]
    assert "PASS" in summary(rep)


def test_junction_model_passes(tmp_path):
    m = junction_model()
    rep = validate(m, export_xodr(m), out_dir=tmp_path)
    assert rep["topology"]["loaded"]
    assert rep["topology"]["topology_pairs"] > 0
    assert rep["topology"]["junctions_in_xodr"] == 1
    assert rep["lane_in_drivable"]["pass"], rep["violations"][:5]
    assert rep["lane_coverage"]["missing"] == []
    assert rep["junction_containment"]["pass"]
    assert rep["junction_containment"]["fraction"] == 1.0
    assert rep["junction_containment"]["reference_line_fraction"] == 1.0
    assert rep["landmarks"]["count"] == 4
    assert rep["z_error"]["elevation"] == "none" and rep["z_error"]["max"] == 0.0
    # open arms: a:+1 (backward, ends at x=-60), b:-1, n:-1 are dead ends; n:+1 arrives at
    # the junction but no connection has n as incoming road; a:-1 and b:+1 continue.
    dead = {(d["road_id"], d["lane_id"]) for d in rep["topology"]["dead_end_lanes"]}
    assert dead == {("a", 1), ("b", -1), ("n", -1), ("n", 1)}


def test_shifted_surfaces_fail_and_write_violations(tmp_path):
    m = shifted_junction_model(6.0)
    rep = validate(m, export_xodr(m), out_dir=tmp_path)
    assert not rep["lane_in_drivable"]["pass"]
    assert rep["lane_in_drivable"]["outside"] > 0
    gj = json.loads((tmp_path / "violations.geojson").read_text())
    assert len(gj["features"]) == rep["violation_count"]
    f = gj["features"][0]
    assert f["geometry"]["type"] == "Point" and f["properties"]["kind"] == "outside_drivable"
    assert f["properties"]["distance"] > 0


def test_missing_surfaces_reports_null():
    m = junction_model()
    m.surfaces = []
    rep = validate(m, export_xodr(m))
    assert rep["lane_in_drivable"] is None
    assert any("drivable" in n for n in rep["notes"])
    assert rep["topology"]["loaded"]
    assert "null" in summary(rep)


def test_broken_xodr_is_reported_not_raised():
    m = straight_road()
    rep = validate(m, "<not xml")
    assert rep["topology"]["loaded"] is False and "error" in rep["topology"]
    assert rep["lane_in_drivable"] is None
    # syntactically fine but empty: carla.Map accepts it, we flag the absence of roads
    rep = validate(m, "<OpenDRIVE><header/></OpenDRIVE>")
    assert rep["topology"]["loaded"] is True and rep["topology"]["waypoints"] == 0
    assert rep["lane_in_drivable"]["pass"] is False
    assert rep["lane_coverage"]["fraction"] == 0.0


def test_cli_roundtrip(tmp_path):
    m = junction_model()
    twin = m.save(tmp_path / "junction3.twin")
    xodr = tmp_path / "junction3.xodr"
    export_xodr(TwinModel.load(twin), xodr)
    rc = main([str(twin), str(xodr)])
    assert rc == 0
    rep = json.loads((twin / "report.json").read_text())
    assert rep["lane_in_drivable"]["pass"] and (twin / "violations.geojson").exists()
    # as a subprocess module too
    out = subprocess.run([sys.executable, "-m", "twinmodel.validate", str(twin), str(xodr),
                          "--out", str(tmp_path / "o")], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0, out.stderr
    assert "lane_in_drivable: 1.0000" in out.stdout
