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


# --------------------------------------------------------------------------- building-aware

def _ground(model):
    parts = [s.geometry for s in model.surfaces_of("ground")]
    return unary_union(parts) if parts else Polygon()


def _sidewalk(model):
    parts = [s.geometry for s in model.surfaces_of("sidewalk")]
    return unary_union(parts) if parts else Polygon()


def test_single_node_plaza_is_the_chamfer_octagon():
    """Arms end at the crossing carriageway (convex cover = plain cross); the plaza must be the
    open space between the four chamfered blocks minus a sidewalk band along their faces."""
    from shapely.geometry import Point
    model = build_surfaces(synthetic.eixample_single_node())
    j = model.junction("j1")
    assert j.tags["plaza_source"] == "corner_void"
    d, sw = _drivable(model), _sidewalk(model)
    # street axis at 0, carriageway half 5.5, face at 9.7 (axis) with the chamfer face on
    # x + y = 30; sidewalk 4.5 m along the face -> drivable up to x + y = 23.6
    for sx in (1, -1):
        for sy in (1, -1):
            for r in (7.0, 9.0, 11.0):  # outside the cross, inside the octagon
                p = Point(sx * r, sy * r)
                assert d.contains(p), (sx, sy, r)
                assert j.polygon.contains(p), (sx, sy, r)
            band = Point(sx * 13.5, sy * 13.5)  # 2.3 m inside the chamfer face
            assert sw.contains(band) and not d.intersects(band), (sx, sy)
            corner = Point(sx * 6.0, sy * 20.0)  # arm sidewalk right after the chamfer
            assert sw.contains(corner), (sx, sy)
    # the plaza is one octagon-ish polygon, much bigger than the 15 m cross cover
    assert j.polygon.geom_type == "Polygon" and 800 < j.polygon.area < 1300
    assert sw.intersection(d).area < 1e-6
    b = unary_union([bb.footprint for bb in model.buildings])
    assert sw.intersection(b).area < 1e-6
    # curbs follow the chamfer: the curb line has segments running at 45 degrees
    import numpy as np
    diag = 0.0
    for c in model.curbs:
        xy = np.asarray(c.geometry.coords)[:, :2]
        seg = np.diff(xy, axis=0)
        ang = np.degrees(np.arctan2(np.abs(seg[:, 1]), np.abs(seg[:, 0])))
        diag += np.linalg.norm(seg[(ang > 40) & (ang < 50)], axis=1).sum()
    assert diag > 4 * 10.0, f"only {diag:.1f} m of 45-degree curb"
    # markings never reach into the plaza
    for m in model.markings:
        assert not m.geometry.intersects(j.polygon.buffer(-0.01))


def test_sidewalk_extends_to_building_face():
    """Lane graph says 2 m of sidewalk, the buildings stand 9.7 m from the axis: in a canyon
    the sidewalk runs to the face (and nothing is left for ground fill in between)."""
    from shapely.geometry import Point
    model = build_surfaces(synthetic.eixample_single_node(sidewalk_w=2.0, face_setback=9.7))
    sw, g = _sidewalk(model), _ground(model)
    for p in ((40.0, 9.0), (-40.0, -9.0), (9.0, 40.0), (-9.0, -40.0), (40.0, 6.0)):
        assert sw.contains(Point(*p)), p
    assert g.intersection(Point(0, 0).buffer(50)).area < 1e-6
    assert model.metadata["surfaces"]["sidewalk_sides_to_face"] == 8
    b = unary_union([bb.footprint for bb in model.buildings])
    assert sw.intersection(b).area < 1e-6


def test_sidewalk_reach_is_clamped():
    """Buildings 30 m from the axis: the sidewalk grows at most 12 m past the carriageway,
    the rest (up to 12 m from the sidewalk) is ground."""
    from shapely.geometry import Point
    model = build_surfaces(synthetic.eixample_single_node(sidewalk_w=2.0, face_setback=30.0))
    sw, g = _sidewalk(model), _ground(model)
    assert sw.contains(Point(40.0, 16.0))
    assert not sw.intersects(Point(40.0, 18.5))
    assert g.contains(Point(40.0, 18.5)) and g.contains(Point(40.0, 28.0))


def test_ground_fill():
    from shapely.geometry import Point
    model = build_surfaces(synthetic.four_way_junction())
    g = _ground(model)
    assert not g.is_empty
    covered = unary_union([_drivable(model), _raised(model)])
    assert g.intersection(covered).area < 1e-6
    assert g.difference(covered.buffer(12.0 + 0.01)).area < 1e-6  # never further than 12 m
    b = unary_union([bb.footprint for bb in model.buildings])
    assert g.intersection(b).area < 1e-6
    for s in model.surfaces_of("ground"):
        assert s.z_offset == pytest.approx(0.15)
        assert s.geometry.distance(covered) < 0.06  # every piece touches a surface
    # ground never gets a curb
    for c in model.curbs:
        assert c.high_side_kind in ("sidewalk", "island")
    # a point just beyond the 2 m sidewalk is ground; the block corner building is not
    assert g.contains(Point(30.0, -8.0))


def test_no_plaza_without_buildings():
    from shapely.geometry import Point
    model = synthetic.eixample_single_node()
    model.buildings = []
    build_surfaces(model)
    j = model.junction("j1")
    assert j.tags["plaza_source"] == "convex"
    assert j.polygon.area < 400  # the plain cross cover
    assert not _drivable(model).contains(Point(9.0, 9.0))
    assert not model.surfaces_of("ground") == []


# --------------------------------------------------------------------------- H2: bounded junctions, parking lots

def _hull_of_ends(model):
    from twinmodel.surfaces import _end_cross_section, _junction_roads
    pts = []
    for r, at_end in _junction_roads(model, model.junctions[0])[1]:
        pts.extend(_end_cross_section(r, at_end))
    return shapely.convex_hull(shapely.multipoints(pts))


def test_bounded_cover_does_not_pave_the_block():
    """US profiles: a cluster whose arm ends are 50 m apart gets the union of the arm corridors
    and the connecting carriageways, not the hull of the ends (which spans the block)."""
    from shapely.geometry import Point
    from twinmodel import profiles
    with profiles.use("us_urban"):
        model = build_surfaces(synthetic.elongated_cluster())
    j = model.junctions[0]
    hull = _hull_of_ends(model)
    assert j.tags["plaza_source"] in ("bounded", "lanegraph", "corner_void")
    assert model.metadata["surfaces"]["junction_cover"] == "bounded"
    assert j.polygon.area < 0.6 * hull.area, (j.polygon.area, hull.area)
    # the block corners (inside the hull, far from every path) are not drivable ...
    d = _drivable(model)
    for sx in (1, -1):
        for sy in (1, -1):
            assert not d.intersects(Point(sx * 10.0, sy * 40.0)), (sx, sy)
    # ... but every connecting road and every arm end is
    for r in model.roads:
        assert j.polygon.buffer(0.01).contains(shapely.force_2d(r.reference_line)) or r.junction_id is None
    for c in j.connections:
        assert j.polygon.distance(shapely.force_2d(model.road(c.incoming_road).reference_line)) < 1e-6
    # the ring 1 m outside the junction is mostly raised (sidewalk apron), never a building
    b = unary_union([bb.footprint for bb in model.buildings])
    assert d.intersection(b).area < 1e-6


def test_convex_cover_under_eu_dense_is_the_hull():
    """EU_DENSE keeps DESIGN.md's convex cover: the same cluster is paved hull-wide."""
    from twinmodel import profiles
    with profiles.use("eu_dense"):
        model = build_surfaces(synthetic.elongated_cluster(with_buildings=False))
    j = model.junctions[0]
    assert model.metadata["surfaces"]["junction_cover"] == "convex"
    assert j.polygon.area == pytest.approx(_hull_of_ends(model).area, rel=0.05)


def test_plaza_is_bounded_by_the_arm_envelope_and_capped():
    """A lane-graph plaza that over-reaches (a 45 m disc around the junction) is clipped to the
    envelope built from the arms; under the US profiles it is then capped by
    ``plaza_max_area_factor`` x (widest street)^2 and the junction falls back to its cover."""
    from dataclasses import replace
    from shapely.geometry import Point
    from twinmodel import profiles
    from twinmodel.surfaces import _Arm, arm_info, bound_plaza, junction_envelope, _junction_roads
    from shapely.ops import unary_union as uu

    def with_tag(P, setback):
        m = synthetic.four_way_junction()
        # buildings on every corner, 90 degrees, ``setback`` m from the axes (5.0 = at the
        # sidewalk edge, a plain city corner; 30 = wide open corners)
        m.buildings = []
        for sx in (1, -1):
            for sy in (1, -1):
                m.buildings.append(synthetic.Building(id=f"b{sx}{sy}", footprint=Polygon(
                    [(sx * setback, sy * setback), (sx * 80, sy * setback), (sx * 80, sy * 80), (sx * setback, sy * 80)]), levels=3))
        m.junctions[0].tags["centre"] = [0.0, 0.0]
        m.junctions[0].tags["plaza_wkt"] = Point(0, 0).buffer(45).wkt
        return m

    # uncapped: the plaza is still bounded by construction
    uncapped = profiles.US_URBAN.with_(name="test", junction=replace(profiles.US_URBAN.junction, plaza_max_area_factor=None))
    with profiles.use(uncapped) as P:
        model = build_surfaces(with_tag(P, 5.0))
        j = model.junctions[0]
        assert j.tags["plaza_source"] == "lanegraph" and "plaza_capped" not in j.tags
        b = uu([bb.footprint for bb in model.buildings])
        arms = [arm_info(r, e, b) for r, e in _junction_roads(model, j)[1]]
        env = junction_envelope(j, arms, b)
        assert env is not None and env.envelope.area < Point(0, 0).buffer(45).area
        # the hull spans the arm ends' full street cross-sections (sidewalks included)
        cw_hull = _hull_of_ends(model).area
        assert cw_hull < env.hull.area < 1.5 * cw_hull
        # 90-degree corners with no receding face: closed -> nothing past the hull in the corners
        assert not env.closed.is_empty
        clipped = bound_plaza(shapely.wkt.loads(j.tags["plaza_wkt"]), env)
        assert clipped.area < 0.5 * Point(0, 0).buffer(45).area
        assert clipped.difference(env.envelope).area < 1e-6
        corner = clipped.intersection(env.closed).difference(env.hull.buffer(0.01))
        assert corner.area < 1e-6
        assert j.polygon.area < 0.4 * Point(0, 0).buffer(45).area
        # a (mm-snapped) plaza that already lies inside its envelope comes back as the same object
        from twinmodel.surfaces import _clean
        inside = _clean(env.hull.buffer(-1.0))
        assert bound_plaza(inside, env) is inside
    # wide-open corners (blocks 30 m back: the faces recede past the arm ends -> open wedges):
    # the plaza fills the envelope, which is far above 3 x (10.5 m)^2 -> capped, cover only
    with profiles.use("us_urban") as P:
        model = build_surfaces(with_tag(P, 30.0))
        j = model.junctions[0]
        assert j.tags["plaza_source"] == "bounded" and j.tags["plaza_capped"] > 3.0 * (2 * (3.25 + 2.0)) ** 2
        assert model.metadata["surfaces"]["junctions_plaza_capped"] == 1
        assert j.polygon.area < 3.0 * (2 * (3.25 + 2.0)) ** 2
    with profiles.use(uncapped) as P:  # uncapped: the same open corners are paved up to the envelope
        model = build_surfaces(with_tag(P, 30.0))
        j = model.junctions[0]
        assert j.tags["plaza_source"] == "lanegraph"
        assert 3.0 * (2 * (3.25 + 2.0)) ** 2 < j.polygon.area < Point(0, 0).buffer(45).area
    # EU_DENSE (uncapped, corner_opening="always"): the chamfer octagon behaviour is untouched
    with profiles.use("eu_dense"):
        eu = build_surfaces(synthetic.eixample_single_node())
        assert eu.junctions[0].tags["plaza_source"] == "corner_void"
        assert "plaza_capped" not in eu.junctions[0].tags
        assert 800 < eu.junctions[0].polygon.area < 1300


def test_parking_lots_from_metadata():
    """``metadata["parking_lots_wkt"]`` (lanegraph, OSM amenity=parking) -> ``parking`` surfaces at
    road level that never overlap the carriageway, the sidewalks, a building or the ground fill."""
    from shapely.geometry import Point
    base = build_surfaces(synthetic.straight_road())
    ground_before = _ground(base).area
    model = synthetic.straight_road()
    lot = Polygon([(-30, -3), (30, -3), (30, -25), (-30, -25)])      # overlaps the road + right sidewalk
    lot2 = Polygon([(-15, 4), (5, 4), (5, 20), (-15, 20)])           # overlaps the building on the left
    model.metadata["parking_lots_wkt"] = [lot.wkt, lot2.wkt]
    build_surfaces(model)
    parking = [s for s in model.surfaces if s.kind == "parking"]
    assert parking and all(s.z_offset == 0.0 and s.source == "osm_tags" for s in parking)
    pk = unary_union([s.geometry for s in parking])
    d, sw, g = _drivable(model), _sidewalk(model), _ground(model)
    b = unary_union([bb.footprint for bb in model.buildings])
    assert pk.intersection(d).area < 1e-6
    assert pk.intersection(sw).area < 1e-6
    assert pk.intersection(b).area < 1e-6
    assert pk.intersection(g).area < 1e-6
    assert pk.contains(Point(0, -15)) and not g.intersects(Point(0, -15))
    assert pk.area == pytest.approx(lot.difference(unary_union([d, sw])).area + lot2.difference(unary_union([sw, b])).area, rel=0.02)
    assert model.metadata["surfaces"]["parking_lot_count"] == 2
    assert model.metadata["surfaces"]["parking_area"] == pytest.approx(pk.area)
    assert g.area < ground_before                     # the lot reduces the ground fill
    # a curb between the lot and the raised sidewalk
    pcurbs = [c for c in model.curbs if c.low_side_kind == "parking"]
    assert pcurbs and all(c.high_side_kind == "sidewalk" for c in pcurbs)
    for c in pcurbs:
        assert pk.boundary.buffer(0.005).contains(c.geometry) and sw.boundary.buffer(0.005).contains(c.geometry)
    # idempotent
    n = len(model.surfaces)
    build_surfaces(model)
    assert len(model.surfaces) == n and len([s for s in model.surfaces if s.kind == "parking"]) == len(parking)


def test_no_parking_surfaces_without_lots():
    model = build_surfaces(synthetic.straight_road())
    assert not [s for s in model.surfaces if s.kind == "parking"]
    assert model.metadata["surfaces"]["parking_lot_count"] == 0
