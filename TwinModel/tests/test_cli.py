"""``twinmodel build`` / ``twinmodel validate`` on the cached Eixample fixture (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from twinmodel import cli
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import load_fixture
from twinmodel.lanegraph import build_lanegraph
from twinmodel.model import Elevation, TwinModel

FIX = Path(__file__).parent / "fixtures"
FIXTURE = FIX / "eixample_overpass.json"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)

carla = pytest.importorskip("carla")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, int]:
    out = tmp_path_factory.mktemp("build")
    rc = cli.main(["-q", "build", "--fixture", str(FIXTURE), "--name", "eixcli", "--out", str(out),
                   "--no-imagery", "--no-dem", "--no-refine", "--cache", str(out / "cache")])
    return out, rc


def test_build_exit_code(built):
    _, rc = built
    assert rc == 0


def test_build_outputs_exist(built):
    out, _ = built
    for name in ("eixcli.xodr", "eixcli.obj", "eixcli.mtl", "eixcli_preview.png", "report.json",
                 "violations.geojson"):
        assert (out / name).exists(), name
    twin = out / "eixcli.twin"
    assert (twin / "model.json").exists() and (twin / "roads.geojson").exists()
    zooms = sorted(out.glob("eixcli_junction_*.png"))
    assert len(zooms) == 3
    assert (out / "eixcli.xodr").stat().st_size > 100_000
    assert (out / "eixcli.obj").stat().st_size > 100_000
    # no ortho -> no plain-preview duplicate, no mask/dem quicklooks
    assert not (out / "eixcli_preview_plain.png").exists()
    assert not (out / "eixcli_mask.png").exists()
    assert not (out / "eixcli_dem.png").exists()


def test_build_report_passes(built):
    out, _ = built
    rep = json.loads((out / "report.json").read_text())
    assert rep["topology"]["loaded"]
    assert rep["topology"]["waypoints"] > 5000
    assert rep["lane_in_drivable"]["pass"]
    assert rep["lane_in_drivable"]["fraction"] >= cli.LANE_IN_DRIVABLE_MIN
    assert rep["junction_containment"]["pass"]
    assert rep["lane_coverage"]["missing"] == []
    assert rep["z_error"]["elevation"] == "none"
    # build metadata is embedded in the report and in the saved model
    b = rep["build"]
    assert b["status"] == "ok"
    assert b["osm_source"].startswith("fixture:")
    for stage in ("osm", "lanegraph", "dem", "imagery", "surfaces", "refine", "export", "preview",
                  "validate"):
        assert stage in b["timings"]
    assert "refine: rejected" not in b["notes"]
    assert rep["refine"]["status"] == "skipped" and rep["refine"]["reason"] == "--no-refine"
    assert rep["elevation"]["source"] == "none"
    meta = json.loads((out / "eixcli.twin" / "model.json").read_text())["metadata"]
    assert meta["build"]["status"] == "ok"
    assert meta["build"]["timings"]["validate"] > 0


def test_saved_model_reloads(built):
    out, _ = built
    m = TwinModel.load(out / "eixcli.twin")
    assert m.name == "eixcli"
    assert len(m.roads) > 100 and len(m.junctions) > 10
    assert m.surfaces_of("drivable") and m.surfaces_of("sidewalk")
    assert m.metadata["build"]["bbox_swne"] == list(BBOX)


def test_validate_subcommand_proxies(built, capsys):
    out, _ = built
    vout = out / "revalidate"
    rc = cli.main(["validate", str(out / "eixcli.twin"), str(out / "eixcli.xodr"), "--out", str(vout),
                   "--step", "5"])
    assert rc == 0
    rep = json.loads((vout / "report.json").read_text())
    assert rep["step"] == 5.0 and rep["lane_in_drivable"]["pass"]
    assert "PASS" in capsys.readouterr().out


def test_build_requires_bbox_or_fixture(tmp_path):
    # no --bbox and no --fixture -> default Eixample bbox would need the network; the parser
    # must at least accept the form and route to the default bbox
    args = cli._build_parser().parse_args(["build", "--out", str(tmp_path)])
    assert args.bbox is None and args.fixture is None


# --------------------------------------------------------------------------- elevation glue

@pytest.fixture(scope="module")
def lanegraph_model() -> TwinModel:
    osm = load_fixture(FIXTURE)
    return build_lanegraph(osm, LocalFrame.from_bbox(*BBOX), BBOX, name="eixample")


def _synthetic_dem(slope_x: float = 0.01, slope_y: float = 0.02, noise: float = 0.3,
                   half: float = 400.0, res: float = 2.0) -> Elevation:
    n = int(2 * half / res) + 1
    xs = -half + np.arange(n) * res
    gx, gy = np.meshgrid(xs, xs)
    rng = np.random.default_rng(0)
    z = 20.0 + slope_x * gx + slope_y * gy + rng.normal(0, noise, gx.shape)
    return Elevation(z, -half, -half, res, res, source="synthetic")


def test_apply_elevation_continuity_and_smoothing(lanegraph_model):
    import copy
    m = copy.deepcopy(lanegraph_model)
    dem = _synthetic_dem()
    stats = cli.apply_elevation(m, dem)
    assert stats["applied"] and stats["source"] == "synthetic"
    assert stats["roads"] > 0 and stats["connecting_roads"] > 0
    assert stats["connecting_roads_dem_fallback"] == 0
    assert 1.5 < stats["slope_pct"] < 3.0
    roads = {r.id: r for r in m.roads}
    for r in m.roads:
        c = np.asarray(r.reference_line.coords)
        assert c.shape[1] == 3 and np.isfinite(c[:, 2]).all()
        assert 10 < c[:, 2].min() and c[:, 2].max() < 40
    # connecting roads start/end exactly at the z of the roads they link
    for r in m.roads:
        if r.junction_id is None:
            continue
        c = np.asarray(r.reference_line.coords)
        z0 = cli._contact_z(roads[r.predecessor.id], r.predecessor.contact)
        z1 = cli._contact_z(roads[r.successor.id], r.successor.contact)
        assert c[0, 2] == pytest.approx(z0, abs=1e-9)
        assert c[-1, 2] == pytest.approx(z1, abs=1e-9)
    # smoothing suppresses the 0.3 m DEM noise: the profile is closer to the underlying plane
    # than the raw (bilinear) DEM samples at the same vertices, at the ends included
    resid, raw = [], []
    for r in m.roads:
        if r.junction_id is not None:
            continue
        c = np.asarray(r.reference_line.coords)
        plane = 20.0 + 0.01 * c[:, 0] + 0.02 * c[:, 1]
        resid.extend(np.abs(c[:, 2] - plane))
        raw.extend(np.abs(np.asarray(dem.sample(c[:, 0], c[:, 1])) - plane))
    assert np.percentile(resid, 95) < 0.75 * np.percentile(raw, 95)
    assert np.percentile(resid, 95) < 0.3
    assert np.mean(resid) < 0.7 * np.mean(raw)
    # signals sit on their road's profile
    for s in m.signals[:20]:
        r = roads[s.road_id]
        p = r.reference_line.interpolate(min(max(s.s, 0.0), r.length))
        assert s.position.has_z and s.position.z == pytest.approx(p.z, abs=1e-9)


def test_apply_elevation_none_zeroes(lanegraph_model):
    import copy
    m = copy.deepcopy(lanegraph_model)
    stats = cli.apply_elevation(m, None)
    assert stats == {"source": "none", "applied": False}
    for r in m.roads:
        assert (np.asarray(r.reference_line.coords)[:, 2] == 0).all()
    assert all(s.position.has_z and s.position.z == 0.0 for s in m.signals)
