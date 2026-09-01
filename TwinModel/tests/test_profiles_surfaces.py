"""Profiles x surfaces/exports: the verge (planting strip) element under ``us_suburban`` and
byte-identical EU_DENSE output (worker P2)."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import numpy as np
import pytest
import trimesh
from lxml import etree
from shapely.geometry import Point
from shapely.ops import unary_union

from twinmodel import profiles
from twinmodel.datum import RoadDatum, harmonize_junction_z
from twinmodel.export.mesh import MATERIALS, PREVIEW_COLORS, export_obj, export_preview_png
from twinmodel.export.xodr import export_xodr, xodr_lane_type
from twinmodel.surfaces import build_surfaces
from tests import synthetic

FT = synthetic.FT
IN = 0.0254

# sha256 of tests/synthetic.four_way_junction() -> build_surfaces -> export_obj under EU_DENSE,
# computed on 2026-09-01 *before* the profile refactor (the .obj carries no date; the .mtl is
# not part of the digest, it gains a ``verge`` material).
FOURWAY_OBJ_SHA256 = "b5f03361e4c876fbccd2c76c48fb7e6227238ccf3ec70ee9a0e07b3a8d1f7e3f"


def _union(model, kind):
    parts = [s.geometry for s in model.surfaces_of(kind)]
    return unary_union(parts) if parts else Point(0, 0).buffer(0)


def _groups(path):
    counts: dict[str, int] = {}
    group = None
    with open(path) as f:
        for line in f:
            if line.startswith("g "):
                group = line.split()[1]
            elif line.startswith("f "):
                counts[group] = counts.get(group, 0) + 1
    return counts


# --------------------------------------------------------------------------- EU_DENSE unchanged

def test_default_profile_is_eu_dense():
    assert profiles.get() is profiles.EU_DENSE


def test_fourway_obj_checksum_unchanged_under_eu_dense(tmp_path):
    with profiles.use(profiles.EU_DENSE):
        model = build_surfaces(synthetic.four_way_junction())
        path = tmp_path / "fourway.obj"
        export_obj(model, path)
    assert model.metadata["surfaces"]["profile"] == "eu_dense"
    assert model.metadata["surfaces"]["verge_area"] == 0.0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FOURWAY_OBJ_SHA256


def test_eu_dense_values_are_the_shipped_ones():
    P = profiles.EU_DENSE
    assert (P.sidewalk.z, P.sidewalk.curb_height, P.crossing.z, P.crossing.width) == (0.15, 0.15, 0.003, 4.0)
    assert (P.streetspace.sidewalk_to_face_max_m, P.streetspace.ground_reach_m) == (12.0, 12.0)
    assert (P.junction.plaza_radius_m, P.junction.plaza_sidewalk_m, P.junction.chamfer_scan_m,
            P.junction.chamfer_allowance_m) == (45.0, 4.5, 60.0, 15.0)
    assert (P.streetspace.canyon_min_fraction, P.streetspace.plaza_canyon_min_fraction,
            P.streetspace.face_tol_m, P.streetspace.face_sample_step_m) == (0.6, 0.5, 1.5, 4.0)
    assert (P.marking.width, P.marking.broken_dash, P.marking.broken_gap, P.marking.z) == (0.12, 2.0, 4.0, 0.002)
    assert (P.elevation.resample_m, P.elevation.smooth_window_m, P.elevation.datum_max_dist_m,
            P.elevation.junction_blend_m, P.elevation.connecting_blend_m, P.elevation.mesh_grid_m) \
        == (2.0, 10.0, 25.0, 20.0, 15.0, 5.0)


# --------------------------------------------------------------------------- us_suburban verge

@pytest.fixture
def suburban():
    with profiles.use("us_suburban") as P:
        model = build_surfaces(synthetic.suburban_residential())
        yield model, P


def test_verge_surfaces_at_curb_top_between_carriageway_and_sidewalk(suburban):
    model, P = suburban
    verge = model.surfaces_of("verge")
    assert verge, [s.kind for s in model.surfaces]
    for s in verge:
        assert s.z_offset == pytest.approx(P.sidewalk.verge_z) == pytest.approx(6 * IN)
    for s in model.surfaces_of("sidewalk"):
        assert s.z_offset == pytest.approx(P.sidewalk.z)
    drivable, vg, sw = _union(model, "drivable"), _union(model, "verge"), _union(model, "sidewalk")
    assert vg.intersection(drivable).area < 1e-6
    assert vg.intersection(sw).area < 1e-6           # sidewalk ∩ verge = 0
    assert sw.intersection(drivable).area < 1e-6
    cw = 11 * FT + 8 * FT                            # driving + parking per side
    for sign in (1, -1):
        assert drivable.contains(Point(10.0, sign * (cw - 0.2)))
        assert vg.contains(Point(10.0, sign * (cw + 3 * FT)))            # middle of the 6 ft strip
        assert not sw.intersects(Point(10.0, sign * (cw + 3 * FT)))
        assert sw.contains(Point(10.0, sign * (cw + 6 * FT + 2.5 * FT)))  # middle of the 5 ft walk
        assert not vg.intersects(Point(10.0, sign * (cw + 6 * FT + 2.5 * FT)))
    # the verge band is (almost) fully represented along the road
    assert vg.area == pytest.approx(2 * 120.0 * 6 * FT, rel=0.02)
    assert model.metadata["surfaces"]["verge_area"] == pytest.approx(vg.area)
    assert model.metadata["surfaces"]["profile"] == "us_suburban"


def test_curbs_between_drivable_and_verge(suburban):
    model, P = suburban
    assert model.curbs
    drivable, vg = _union(model, "drivable"), _union(model, "verge")
    for c in model.curbs:
        assert c.height == pytest.approx(P.sidewalk.curb_height) == pytest.approx(6 * IN)
        assert c.low_side_kind == "drivable" and c.high_side_kind == "verge"
        assert drivable.boundary.buffer(0.005).contains(c.geometry)
        assert vg.boundary.buffer(0.005).contains(c.geometry)
    assert sum(c.geometry.length for c in model.curbs) == pytest.approx(2 * 120.0, rel=0.01)


def test_crossing_uses_profile_width(suburban):
    model, P = suburban
    crossings = model.surfaces_of("crossing")
    assert len(crossings) == 1
    minx, _, maxx, _ = crossings[0].geometry.bounds
    assert maxx - minx == pytest.approx(P.crossing.width) == pytest.approx(10 * FT)
    assert crossings[0].z_offset == pytest.approx(P.crossing.z)


def test_default_markings_follow_profile():
    with profiles.use("us_suburban"):
        # residential: no centre line (ClassDefaults.center_marking=False), white edge lines
        res = build_surfaces(synthetic.suburban_residential(highway="residential"))
        assert res.markings and all(m.color == "white" for m in res.markings)
        assert all(m.width == pytest.approx(4 * IN) for m in res.markings)
        assert not any(abs(m.geometry.centroid.y) < 0.05 for m in res.markings)
        # secondary: yellow solid centre line between the opposing lanes
        sec = build_surfaces(synthetic.suburban_residential(highway="secondary"))
        centre = [m for m in sec.markings if abs(m.geometry.centroid.y) < 0.05]
        assert len(centre) == 1 and centre[0].color == "yellow" and centre[0].kind == "solid"
        assert all(m.color == "white" for m in sec.markings if m is not centre[0])
    with profiles.use("eu_dense"):
        eu = build_surfaces(synthetic.suburban_residential(highway="secondary"))
        centre = [m for m in eu.markings if abs(m.geometry.centroid.y) < 0.05]
        assert len(centre) == 1 and centre[0].color == "white"


def test_xodr_writes_border_lanes_and_yellow_centre():
    assert xodr_lane_type("verge") == "border" and xodr_lane_type("sidewalk") == "sidewalk"
    assert xodr_lane_type("unknown") == "none"
    with profiles.use("us_suburban") as P:
        model = build_surfaces(synthetic.suburban_residential(highway="secondary"))
        from twinmodel.model import Marking
        model.roads[0].center_marking = Marking("solid", "yellow", P.marking.width)
        text = export_xodr(model)
    root = etree.fromstring(text.encode())
    sec = root.find("road/lanes/laneSection")
    types = {l.get("id"): l.get("type") for side in ("left", "right") for l in sec.findall(f"{side}/lane")}
    assert types == {"4": "sidewalk", "3": "border", "2": "parking", "1": "driving",
                     "-1": "driving", "-2": "parking", "-3": "border", "-4": "sidewalk"}
    border = sec.find("right/lane[@id='-3']")
    h = border.find("height")
    assert float(h.get("inner")) == pytest.approx(P.sidewalk.curb_height)
    assert float(h.get("outer")) == pytest.approx(6 * IN)
    sw = sec.find("right/lane[@id='-4']/height")
    assert float(sw.get("inner")) == pytest.approx(P.sidewalk.z)
    assert sec.find("center/lane/roadMark").get("color") == "yellow"
    cw = root.find(".//object[@type='crosswalk']")
    assert float(cw.get("width")) == pytest.approx(P.crossing.width)
    carla = pytest.importorskip("carla")
    cmap = carla.Map("twin", text)
    wps = cmap.generate_waypoints(1.0)
    driving = [w for w in wps if w.lane_type == carla.LaneType.Driving]
    assert {w.lane_id for w in driving} == {-1, 1}
    assert len(driving) >= 2 * 110
    assert not any(w.lane_type == carla.LaneType.Driving and abs(w.lane_id) == 3 for w in wps)


def test_mesh_has_verge_group(tmp_path):
    assert "verge" in MATERIALS and "verge" in PREVIEW_COLORS
    r, g, b = MATERIALS["verge"]
    assert g > r and g > b  # grass green
    with profiles.use("us_suburban") as P:
        model = build_surfaces(synthetic.suburban_residential())
        path = tmp_path / "suburban.obj"
        export_obj(model, path)
        export_preview_png(model, tmp_path / "suburban.png")
    groups = _groups(path)
    assert groups.get("verge", 0) > 0 and groups.get("sidewalk", 0) > 0 and groups.get("curb", 0) > 0
    assert "newmtl verge" in path.with_suffix(".mtl").read_text()
    scene = trimesh.load(path, force="scene", process=False)
    vg = scene.geometry["verge"].vertices
    assert np.allclose(vg[:, 2], P.sidewalk.verge_z, atol=1e-4)
    assert scene.geometry["curb"].vertices[:, 2].max() == pytest.approx(6 * IN, abs=1e-4)
    assert (tmp_path / "suburban.png").stat().st_size > 10_000


def test_broken_markings_use_profile_dash_pattern(tmp_path):
    """Two same-direction lanes -> a broken lane line; dashes are 10 ft / 30 ft under US."""
    from shapely.geometry import LineString
    from twinmodel.model import Lane, Road, TwinModel
    def one_way(P):
        m = TwinModel(name="oneway", origin_lat=synthetic.ORIGIN[0], origin_lon=synthetic.ORIGIN[1],
                      bbox_wgs84=synthetic.BBOX)
        lanes = [Lane(id=-1, type="driving", width=P.lane.width_for("secondary")),
                 Lane(id=-2, type="driving", width=P.lane.width_for("secondary"))]
        m.roads.append(Road(id="r", reference_line=LineString([(-100, 0, 0), (100, 0, 0)]),
                            lanes=lanes, highway="secondary"))
        return build_surfaces(m)
    counts = {}
    for name in ("us_suburban", "eu_dense"):
        with profiles.use(name) as P:
            model = one_way(P)
            broken = [mk for mk in model.markings if mk.kind == "broken"]
            assert len(broken) == 1 and broken[0].color == P.marking.lane_color
            path = tmp_path / f"{name}.obj"
            export_obj(model, path)
            n_white = _groups(path)["marking_white"]
            # each dash is one flat quad = 2 triangles
            counts[name] = n_white
            expected_dashes = int(np.ceil(200.0 / (P.marking.broken_dash + P.marking.broken_gap)))
            solid_quads = 2 * 2  # two solid edge lines (outer lane edge + reference-line edge)
            assert n_white == pytest.approx(2 * expected_dashes + solid_quads, abs=2)
    assert counts["us_suburban"] < counts["eu_dense"]


def test_fourway_with_verges_keeps_invariants():
    with profiles.use("us_suburban"):
        model = build_surfaces(synthetic.four_way_junction(
            lane_w=11 * FT, sidewalk_w=5 * FT, parking_w=8 * FT, verge_w=6 * FT))
    drivable, vg, sw = _union(model, "drivable"), _union(model, "verge"), _union(model, "sidewalk")
    assert drivable.geom_type == "Polygon"
    assert vg.area > 0 and sw.area > 0
    assert vg.intersection(sw).area < 1e-6 and vg.intersection(drivable).area < 1e-6
    kinds = {c.high_side_kind for c in model.curbs}
    assert "verge" in kinds and kinds <= {"verge", "sidewalk", "island"}
    # the corner apron (sidewalk wrap) makes the ring around the junction raised (verge/sidewalk)
    j = model.junctions[0]
    ring = drivable.buffer(1.0).exterior.intersection(j.polygon.buffer(3.0))
    raised = unary_union([vg, sw]).buffer(0.01)
    assert ring.intersection(raised).length / ring.length > 0.5
    for s in model.surfaces_of("ground"):
        assert s.geometry.intersection(unary_union([drivable, vg, sw])).area < 1e-6
    b = unary_union([bb.footprint for bb in model.buildings])
    assert vg.intersection(b).area < 1e-6 and sw.intersection(b).area < 1e-6


# --------------------------------------------------------------------------- datum / cli glue

def test_datum_defaults_follow_profile():
    from shapely.geometry import LineString
    from twinmodel.model import Lane, Road
    road = Road(id="r", reference_line=LineString([(-50, 0, 1.0), (50, 0, 1.0)]),
                lanes=[Lane(id=-1, type="driving", width=3.0)])
    assert RoadDatum([road]).max_dist == 25.0
    custom = profiles.EU_DENSE.with_(
        name="custom", elevation=replace(profiles.EU_DENSE.elevation, datum_max_dist_m=10.0,
                                         junction_blend_m=7.0, connecting_blend_m=3.0))
    with profiles.use(custom):
        assert RoadDatum([road]).max_dist == 10.0
        m = synthetic.four_way_junction()
        st = harmonize_junction_z(m)
        assert st["blend_m"] == 7.0


def test_cli_elevation_uses_profile_resample_and_window():
    from twinmodel import cli
    from twinmodel.model import Elevation
    xs = np.arange(-200.0, 201.0, 10.0)
    zz = 0.02 * xs[None, :] + 0.0 * xs[:, None]
    dem = Elevation(zz, xs[0], xs[0], 10.0, 10.0, source="synthetic")
    custom = profiles.EU_DENSE.with_(
        name="custom", elevation=replace(profiles.EU_DENSE.elevation, resample_m=5.0, smooth_window_m=30.0))
    with profiles.use(custom):
        m = synthetic.straight_road()
        m.elevation = dem
        stats = cli.apply_elevation(m)
    assert stats["smoothing"] == {"resample_m": 5.0, "window_m": 30.0, "filter": "savgol1"}
    c = np.asarray(m.roads[0].reference_line.coords)
    assert np.allclose(c[:, 2], 0.02 * c[:, 0], atol=1e-6)
