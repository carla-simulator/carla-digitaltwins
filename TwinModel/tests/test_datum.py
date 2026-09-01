"""twinmodel.datum (road datum + junction planes) and its use by model/mesh/validate."""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString

from twinmodel.datum import RoadDatum, harmonize_junction_z, roads_have_z
from twinmodel.export.mesh import export_obj
from twinmodel.model import Elevation, Lane, Road, RoadLink, TwinModel
from twinmodel.surfaces import build_surfaces
from tests import synthetic
from tests.synthetic_xodr import straight_road as xodr_straight_road

BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


def _road(rid, coords, lanes, **kw) -> Road:
    return Road(id=rid, reference_line=LineString(coords), lanes=lanes, **kw)


def _lanes(left, right):
    """(left widths, right widths) -> lanes; first entry of each side is a driving lane."""
    lanes = []
    for i, w in enumerate(left, 1):
        lanes.append(Lane(id=i, type="driving" if i == 1 else "sidewalk", width=w))
    for i, w in enumerate(right, 1):
        lanes.append(Lane(id=-i, type="driving" if i == 1 else "sidewalk", width=w))
    return lanes


def _plane_dem(slope=0.02, offset=0.0):
    xs = np.arange(-200.0, 201.0, 10.0)
    ys = np.arange(-200.0, 201.0, 10.0)
    zz = slope * xs[None, :] + 0.0 * ys[:, None] + offset
    return Elevation(zz, xs[0], ys[0], 10.0, 10.0, source="synthetic")


def test_datum_z_along_road_and_far_field_blend():
    # road along x, z = 0.02 x; the DEM is 1 m higher so the blend is visible
    road = _road("r", [(-100, 0, -2.0), (0, 0, 0.0), (100, 0, 2.0)], _lanes([3.0], [3.0]))
    dem = _plane_dem(0.02, offset=1.0)
    d = RoadDatum([road], dem, max_dist=25.0)
    assert not d.empty
    assert d.z(10.0, 0.0) == pytest.approx(0.2, abs=1e-9)
    assert d.z(10.0, 3.0) == pytest.approx(0.2, abs=1e-9)  # flat cross-section
    zs = d.z(np.array([-50.0, 25.0]), np.array([1.0, -1.0]))
    assert zs.shape == (2,) and np.allclose(zs, [-1.0, 0.5])
    # inside max_dist: road z; at 2*max_dist and beyond: the DEM; halfway: the mean
    assert d.z(0.0, 20.0) == pytest.approx(0.0)
    assert d.z(0.0, 50.0) == pytest.approx(1.0)
    assert d.z(0.0, 80.0) == pytest.approx(1.0)
    assert d.z(0.0, 37.5) == pytest.approx(0.5)
    # without a DEM the road z is used everywhere
    assert RoadDatum([road], None).z(0.0, 80.0) == pytest.approx(0.0)


def test_datum_prefers_covering_road_over_nearer_reference_line():
    # A: one-way street, reference line on its left edge, 3 lanes (9 m) + 6 m sidewalk to the
    # right; B: narrow two-way street 12 m south of A's reference line.
    A = _road("A", [(-50, 0, 10.0), (50, 0, 10.0)],
              [Lane(id=1, type="sidewalk", width=2.0)] +
              [Lane(id=-i, type="driving", width=3.0) for i in (1, 2, 3)] +
              [Lane(id=-4, type="sidewalk", width=6.0)])
    B = _road("B", [(-50, -12, 20.0), (50, -12, 20.0)], _lanes([3.25], [3.25]))
    d = RoadDatum([A, B], None)
    # lane 3 of A (7.5 m right of A's reference line) is 4.5 m from B's: nearest-segment
    # would say B; the covering road is A
    assert d.z(0.0, -7.5) == pytest.approx(10.0)
    assert d.z(0.0, -10.5) == pytest.approx(20.0)  # inside B's cross-section, outside A's
    assert d.z(0.0, 2.0) == pytest.approx(10.0)


def test_datum_end_overshoot_goes_to_connecting_road():
    # wide arm A ends at x=0; narrow connecting road C continues; a point 3 m past A's end
    # and 1 m off C's line belongs to C even though A's reach (16 m) is much larger
    A = _road("A", [(-50, 0, 5.0), (0, 0, 5.0)],
              [Lane(id=1, type="sidewalk", width=6.0)] +
              [Lane(id=-i, type="driving", width=3.0) for i in (1, 2, 3)] +
              [Lane(id=-4, type="sidewalk", width=6.0)])
    C = _road("C", [(0, 0, 7.0), (20, 0, 7.0)], [Lane(id=-1, type="driving", width=3.0)],
              junction_id="j")
    d = RoadDatum([A, C], None)
    assert d.z(3.0, -1.0) == pytest.approx(7.0)
    assert d.z(-3.0, -1.0) == pytest.approx(5.0)


def test_roads_have_z_and_model_sample_z_routing():
    m = TwinModel(name="t", origin_lat=0, origin_lon=0, bbox_wgs84=BBOX)
    m.roads = [_road("r", [(-50, 0, 0.0), (50, 0, 0.0)], _lanes([3.0], [3.0]))]
    m.elevation = _plane_dem(0.02, offset=1.0)
    assert not roads_have_z(m.roads)
    assert m.road_datum() is None
    assert m.sample_z(10.0, 0.0) == pytest.approx(1.2)  # DEM fallback
    # give the road a z profile (new LineString object): the cache invalidates itself
    m.roads[0].reference_line = LineString([(-50, 0, -1.0), (50, 0, 1.0)])
    assert roads_have_z(m.roads)
    assert m.road_datum() is not None
    assert m.sample_z(10.0, 0.0) == pytest.approx(0.2)
    assert m.sample_dem_z(10.0, 0.0) == pytest.approx(1.2)
    zs = m.sample_z(np.array([0.0, 25.0]), np.array([0.0, 0.0]))
    assert np.allclose(zs, [0.0, 0.5])
    # explicit rebuild returns the datum; without DEM and without z -> zeros
    assert m.rebuild_datum() is m.road_datum()
    m.elevation = None
    m.roads[0].reference_line = LineString([(-50, 0, 0.0), (50, 0, 0.0)])
    assert m.rebuild_datum() is None
    assert m.sample_z(3.0, 4.0) == 0.0


def test_harmonize_junction_z_puts_contacts_and_connecting_roads_on_one_plane():
    m = synthetic.four_way_junction()
    zc = {"arm_E": 0.0, "arm_N": 0.4, "arm_W": 0.0, "arm_S": 0.0}  # non-coplanar contacts
    for r in m.roads:
        c = np.asarray(r.reference_line.coords)
        if r.junction_id is None:
            r.reference_line = LineString(np.column_stack([c[:, :2], np.full(len(c), zc[r.id])]))
        else:
            r.reference_line = LineString(np.column_stack([c[:, :2], np.zeros(len(c))]))
    far_before = {r.id: np.asarray(r.reference_line.coords)[0, 2] for r in m.roads
                  if r.junction_id is None}
    st = harmonize_junction_z(m, blend_m=20.0)
    assert st["junctions"] == 1 and st["contacts"] == 4 and st["connecting_roads"] == 12
    assert 0.05 < st["contact_adjust_max_m"] <= 0.4
    roads = {r.id: r for r in m.roads}
    # every connecting road starts/ends exactly at the (moved) contact z of the roads it links
    for r in m.roads:
        if r.junction_id is None:
            continue
        c = np.asarray(r.reference_line.coords)
        p = np.asarray(roads[r.predecessor.id].reference_line.coords)[-1]
        s = np.asarray(roads[r.successor.id].reference_line.coords)[-1]
        assert c[0, 2] == pytest.approx(p[2], abs=1e-9)
        assert c[-1, 2] == pytest.approx(s[2], abs=1e-9)
    # all contacts lie on one plane
    P = np.array([np.asarray(roads[k].reference_line.coords)[-1] for k in zc])
    A = np.column_stack([P[:, 0], P[:, 1], np.ones(4)])
    coef, *_ = np.linalg.lstsq(A, P[:, 2], rcond=None)
    assert np.abs(P[:, 2] - A @ coef).max() < 1e-9
    # arms are only touched within blend_m of the contact (arms are 60 m long)
    for rid, z0 in far_before.items():
        assert np.asarray(roads[rid].reference_line.coords)[0, 2] == pytest.approx(z0)
    # datum is now single-valued where connecting roads cross: crossing point of E->W and N->S
    d = RoadDatum([r for r in m.roads], None)
    ew = roads["c_EW"].reference_line
    ns = roads["c_NS"].reference_line
    x = ew.intersection(ns)
    if not x.is_empty and x.geom_type == "Point":
        assert abs(ew.interpolate(ew.project(x)).z - ns.interpolate(ns.project(x)).z) < 1e-6


def test_export_obj_sidewalk_sits_on_datum(tmp_path):
    m = build_surfaces(synthetic.straight_road())
    r = m.roads[0]
    c = np.asarray(r.reference_line.coords)
    r.reference_line = LineString(np.column_stack([c[:, :2], 0.02 * c[:, 0]]))
    assert m.elevation is None
    path = tmp_path / "datum.obj"
    export_obj(m, path)
    scene = trimesh.load(path, force="scene", process=False)
    drv = scene.geometry["drivable"].vertices
    sw = scene.geometry["sidewalk"].vertices
    assert np.allclose(drv[:, 2], 0.02 * drv[:, 0], atol=1e-6)
    assert np.allclose(sw[:, 2], 0.02 * sw[:, 0] + 0.15, atol=1e-6)
    # subdivided along the slope even without a DEM
    assert len(scene.geometry["drivable"].faces) > 20


def test_validate_z_error_measures_xodr_vs_datum_not_dem(tmp_path):
    carla = pytest.importorskip("carla")  # noqa: F841
    from twinmodel.export.xodr import export_xodr
    from twinmodel.validate import validate, summary

    m = xodr_straight_road(with_elevation=True)  # road z = 0.02 x, DEM = 0.02 x
    # corrupt the DEM with a 0.5 m bump: the xodr/mesh agreement must not care
    m.elevation.z += 0.5 * np.sin(np.arange(m.elevation.z.shape[1]) * 1.3)[None, :]
    m.rebuild_datum()
    rep = validate(m, export_xodr(m), out_dir=tmp_path)
    assert rep["z_error"]["pass"] and rep["z_error"]["surface_z"] == "road_datum"
    assert rep["z_error"]["p95"] < 0.02
    assert rep["z_error_dem"]["p95"] > 0.1
    assert "z_error_dem" in summary(rep)
