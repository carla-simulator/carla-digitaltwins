"""Tests for twinmodel.surfaces on the synthetic models (no network)."""
from __future__ import annotations

import pytest
import shapely
from shapely.geometry import Polygon
from shapely.ops import unary_union

from twinmodel.surfaces import build_surfaces
from tests import synthetic

CASES = list(synthetic.ALL_CASES.items())


@pytest.fixture(params=CASES, ids=[c[0] for c in CASES])
def model(request):
    _, factory = request.param
    return build_surfaces(factory())


def _drivable(model):
    return unary_union([s.geometry for s in model.surfaces_of("drivable")])


def _raised(model):
    parts = [s.geometry for s in model.surfaces if s.kind in ("sidewalk", "island", "median")]
    return unary_union(parts) if parts else Polygon()


def test_drivable_valid_and_covers_reference_lines(model):
    drivable = _drivable(model)
    assert not drivable.is_empty
    assert drivable.is_valid
    for r in model.roads:
        if r.width_left() + r.width_right() <= 0:
            continue
        # 1 m buffer towards each side that carries carriageway lanes (the reference line may be
        # the carriageway edge when all lanes are on one side)
        ref = shapely.force_2d(r.reference_line)
        sides = []
        if r.width_left() > 0:
            sides.append(ref.buffer(min(1.0, r.width_left()), single_sided=True))
        if r.width_right() > 0:
            sides.append(ref.buffer(-min(1.0, r.width_right()), single_sided=True))
        buffered = unary_union(sides)
        missing = buffered.difference(drivable).area
        assert missing < 1e-3, f"road {r.id}: {missing:.3f} m2 of its 1 m buffer is outside drivable"


def test_drivable_is_one_connected_piece(model):
    # the synthetic cases are all connected networks: arms + junction must fuse without gaps
    assert _drivable(model).geom_type == "Polygon"


def test_sidewalk_never_overlaps_drivable(model):
    drivable = _drivable(model)
    for s in model.surfaces:
        if s.kind in ("sidewalk", "island", "median"):
            assert s.geometry.is_valid
            assert s.geometry.intersection(drivable).area < 1e-6, s.id


def test_sidewalks_exist_where_roads_have_sidewalk_lanes(model):
    wants = any(l.type == "sidewalk" for r in model.roads for l in r.lanes)
    has = bool(model.surfaces_of("sidewalk"))
    assert wants == has
    if wants:
        # every sidewalk lane band is at least 80 % represented (buildings/junctions eat some)
        sw = unary_union([s.geometry for s in model.surfaces_of("sidewalk")])
        for r in model.roads:
            if r.junction_id is not None:
                continue
            ref = shapely.force_2d(r.reference_line)
            for l in r.lanes:
                if l.type != "sidewalk":
                    continue
                wl, wr = r.width_left(), r.width_right()
                inner = wl if l.id > 0 else wr
                sign = 1.0 if l.id > 0 else -1.0
                band = (ref.buffer(sign * (inner + l.width), single_sided=True)
                        .difference(ref.buffer(sign * inner, single_sided=True)))
                frac = band.intersection(sw).area / band.area
                assert frac > 0.8, f"road {r.id} lane {l.id}: only {frac:.2f} of the sidewalk band"


def test_sidewalk_never_overlaps_buildings(model):
    if not model.buildings:
        pytest.skip("no buildings in this case")
    b = unary_union([bb.footprint for bb in model.buildings])
    for s in model.surfaces_of("sidewalk"):
        assert s.geometry.intersection(b).area < 1e-6


def test_junction_polygons_contain_connecting_roads(model):
    for j in model.junctions:
        assert j.polygon is not None and j.polygon.is_valid and j.polygon.area > 0
        conn_ids = {c.connecting_road for c in j.connections}
        conn_ids |= {r.id for r in model.roads if r.junction_id == j.id}
        assert conn_ids
        for rid in conn_ids:
            r = model.road(rid)
            assert j.polygon.buffer(0.01).contains(shapely.force_2d(r.reference_line)), rid
        # the incoming roads' ends touch the polygon (no gap)
        for c in j.connections:
            inc = model.road(c.incoming_road)
            assert j.polygon.distance(shapely.force_2d(inc.reference_line)) < 1e-6


def test_sidewalks_wrap_around_junctions(model):
    """A point just outside the junction polygon's corner (between two arms) is sidewalk."""
    if not model.junctions:
        pytest.skip("no junction")
    sw = unary_union([s.geometry for s in model.surfaces_of("sidewalk")])
    for j in model.junctions:
        assert sw.intersects(j.polygon.buffer(0.5))
        # walk around the junction: the ring at 1 m outside the drivable must be mostly sidewalk
        ring = _drivable(model).buffer(1.0).exterior
        probe = ring.intersection(j.polygon.buffer(3.0))
        covered = probe.intersection(sw.buffer(0.01)).length / probe.length
        assert covered > 0.5, f"junction {j.id}: only {covered:.2f} of the surrounding ring is sidewalk"


def test_curbs(model):
    total = sum(c.geometry.length for c in model.curbs)
    assert total > 0
    drivable = _drivable(model)
    raised = _raised(model)
    for c in model.curbs:
        assert c.geometry.geom_type == "LineString"
        assert c.height == pytest.approx(0.15)
        # every curb lies on both boundaries
        assert drivable.boundary.buffer(0.005).contains(c.geometry), c.id
        assert raised.boundary.buffer(0.005).contains(c.geometry), c.id


def test_crossings(model):
    crossings = model.surfaces_of("crossing")
    n_signals = sum(1 for s in model.signals if s.kind == "crosswalk")
    assert len(crossings) == n_signals
    drivable = _drivable(model)
    for c in crossings:
        assert c.geometry.difference(drivable.buffer(0.01)).area < 1e-6
        assert c.geometry.area > 4.0


def test_markings_outside_junctions_and_inside_drivable(model):
    assert model.markings
    drivable = _drivable(model)
    for m in model.markings:
        assert m.geometry is not None and m.geometry.length > 0
        assert drivable.buffer(0.06).contains(m.geometry)
        for j in model.junctions:
            assert not m.geometry.intersects(j.polygon.buffer(-0.01)), "marking inside junction"


def test_idempotent(model):
    n_surfaces, n_curbs, n_markings = len(model.surfaces), len(model.curbs), len(model.markings)
    polys = {j.id: j.polygon for j in model.junctions}
    build_surfaces(model)
    assert (len(model.surfaces), len(model.curbs), len(model.markings)) == (n_surfaces, n_curbs, n_markings)
    for j in model.junctions:
        assert j.polygon.symmetric_difference(polys[j.id]).area < 1e-6


def test_refined_drivable_replaces_polygon():
    model = synthetic.straight_road()
    build_surfaces(model)
    base = _drivable(model)
    refined = base.buffer(0.5)
    build_surfaces(model, refined_drivable=refined)
    d = _drivable(model)
    assert d.area > base.area
    assert all(s.source == "imagery" for s in model.surfaces_of("drivable"))
    assert "refined_iou" in model.metadata["surfaces"]
    for s in model.surfaces_of("sidewalk"):
        assert s.geometry.intersection(d).area < 1e-6


def test_holes_become_islands():
    model = synthetic.straight_road(with_building=False)
    build_surfaces(model)
    ring = Polygon([(-40, -20), (40, -20), (40, 20), (-40, 20)],
                   [[(-10, -3), (10, -3), (10, 3), (-10, 3)]])
    build_surfaces(model, refined_drivable=ring)
    islands = model.surfaces_of("island")
    assert len(islands) == 1
    assert islands[0].z_offset == pytest.approx(0.15)
    assert islands[0].geometry.area == pytest.approx(120.0, rel=0.01)
    island_curbs = [c for c in model.curbs if c.high_side_kind == "island"]
    assert island_curbs and sum(c.geometry.length for c in island_curbs) == pytest.approx(52.0, rel=0.01)


def test_save_load_roundtrip(tmp_path):
    model = build_surfaces(synthetic.four_way_junction())
    from twinmodel.model import TwinModel
    model.save(tmp_path / "x.twin")
    m2 = TwinModel.load(tmp_path / "x.twin")
    assert len(m2.surfaces) == len(model.surfaces)
    assert len(m2.curbs) == len(model.curbs)
    assert len(m2.markings) == len(model.markings)
    assert m2.junction("j1").polygon.area == pytest.approx(model.junction("j1").polygon.area)
