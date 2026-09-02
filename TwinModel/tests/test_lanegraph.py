"""Lane graph on the cached Eixample fixture (no network)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest
from shapely import wkt
from shapely.geometry import LineString, Point

from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import load_fixture, parse_osm, overpass_query
from twinmodel.lanegraph import (build_lanegraph, lanes_for_way, parse_length, parse_maxspeed,
                                 point_on_road, _hermite, _offset_polyline)
from twinmodel import profiles
from twinmodel.model import TwinModel

FIXTURE = Path(__file__).parent / "fixtures" / "eixample_overpass.json"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


@pytest.fixture(scope="module")
def osm():
    return load_fixture(FIXTURE)


@pytest.fixture(scope="module")
def model(osm):
    return build_lanegraph(osm, LocalFrame.from_bbox(*BBOX), BBOX, name="eixample")


# --------------------------------------------------------------------------- ingest

def test_fixture_counts(osm):
    assert len(osm.nodes) > 5000
    assert len(osm.ways) > 700
    assert len(osm.relations) > 80
    assert sum(1 for w in osm.ways if "highway" in w.tags) > 300
    assert sum(1 for w in osm.ways if "building" in w.tags) > 150
    assert sum(1 for r in osm.relations if r.tags.get("type") == "restriction") == 14
    assert osm.bbox_swne == BBOX


def test_overpass_query_mentions_every_feature_class():
    q = overpass_query(BBOX)
    for token in ('way["highway"]', "traffic_signals", "traffic_sign", '"natural"="tree"',
                  'way["building"]', 'relation["building"]', 'way["area:highway"]',
                  '"type"="restriction"', "out body;", ">;", "out skel qt;"):
        assert token in q


def test_parse_osm_prefers_tagged_duplicates():
    data = {"elements": [
        {"type": "node", "id": 1, "lat": 41.39, "lon": 2.16},
        {"type": "node", "id": 1, "lat": 41.39, "lon": 2.16, "tags": {"highway": "crossing"}},
        {"type": "way", "id": 7, "nodes": [1, 2], "tags": {"highway": "residential"}},
    ]}
    d = parse_osm(data)
    assert d.nodes[1].tags == {"highway": "crossing"}
    assert d.way(7).tags["highway"] == "residential"


# --------------------------------------------------------------------------- tag parsing

def test_parse_helpers():
    assert parse_length("12 m") == 12.0
    assert parse_length("12,5m") == 12.5
    assert abs(parse_length("40 ft") - 12.192) < 1e-3
    assert parse_length(None) is None
    assert abs(parse_maxspeed("50") - 50 / 3.6) < 1e-9
    assert abs(parse_maxspeed("30 mph") - 13.4112) < 1e-3
    assert parse_maxspeed("none") is None


def test_lanes_defaults_two_way_residential():
    spec = lanes_for_way({}, "residential")
    ids = [l.id for l in spec.lanes]
    assert ids == [2, 1, -1, -2]  # sidewalk, driving(back), driving(fwd), sidewalk
    types = {l.id: l.type for l in spec.lanes}
    assert types[1] == "driving" and types[-1] == "driving"
    assert types[2] == "sidewalk" and types[-2] == "sidewalk"
    assert {l.direction for l in spec.lanes if l.id == 1} == {"backward"}
    assert spec.center_marking is not None and spec.center_marking.color == "white"
    assert all(l.width == 3.5 for l in spec.lanes if l.type == "driving")  # 2026-09-02 second ambulance pass


def test_lanes_oneway_overrides():
    tags = {"oneway": "yes", "lanes": "3", "width": "9", "maxspeed": "50", "sidewalk:both": "separate",
            "cycleway:right": "lane", "parking:left": "lane", "parking:left:orientation": "parallel",
            "turn:lanes": "left;through|through|right"}
    spec = lanes_for_way(tags, "secondary")
    assert spec.oneway and spec.n_forward == 3 and spec.n_backward == 0
    right = [l for l in spec.lanes if l.id < 0]
    assert [l.type for l in right] == ["driving", "driving", "driving", "biking", "sidewalk"]
    # width=9 / 3 lanes = 3.0 m, floored to LaneRules.min_width (3.3 m): a tagged width that
    # cannot pass an ambulance is not honoured
    assert all(abs(l.width - profiles.get().lane.min_width) < 1e-9 for l in right if l.type == "driving")
    assert all(abs(l.speed_limit - 50 / 3.6) < 1e-9 for l in right if l.type == "driving")
    assert right[0].tags["turn"] == ["left", "through"] and right[2].tags["turn"] == ["right"]
    assert right[0].marking.kind == "broken" and right[2].marking.kind == "solid"
    left = sorted((l for l in spec.lanes if l.id > 0), key=lambda l: l.id)  # inner -> outer
    assert [l.type for l in left] == ["parking", "sidewalk"]
    assert left[0].width == 2.0


def test_lanes_backward_split_and_service_without_sidewalk():
    spec = lanes_for_way({"lanes": "6", "lanes:backward": "4"}, "tertiary")
    assert spec.n_forward == 2 and spec.n_backward == 4
    assert lanes_for_way({}, "service").lanes and not any(
        l.type == "sidewalk" for l in lanes_for_way({}, "service").lanes)
    assert lanes_for_way({"oneway": "-1"}, "residential").oneway


# --------------------------------------------------------------------------- geometry helpers

def test_offset_and_hermite():
    line = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
    assert _offset_polyline(line, 2.0) == pytest.approx([(0, 2), (10, 2), (20, 2)])
    curve = _hermite((0, 0), 0.0, (20, 10), 0.0, step=1.0)
    assert curve[0] == pytest.approx((0, 0)) and curve[-1] == pytest.approx((20, 10))
    steps = [math.dist(a, b) for a, b in zip(curve, curve[1:])]
    assert max(steps) <= 1.3 and min(steps) > 0.2
    assert LineString(curve).length > math.dist((0, 0), (20, 10))


# --------------------------------------------------------------------------- lane graph

def test_model_counts(model):
    plain = [r for r in model.roads if r.junction_id is None]
    conn = [r for r in model.roads if r.junction_id is not None]
    assert 40 <= len(plain) <= 90
    assert 12 <= len(model.junctions) <= 25
    assert len(conn) >= 100
    assert len(model.signals) > 100
    assert len(model.controllers) >= 8
    assert len(model.buildings) > 200
    assert sum(1 for o in model.objects if o.kind == "tree") == 208
    assert model.metadata["lanegraph"]["restrictions_unresolved"] == 0
    assert model.metadata["lanegraph"]["restricted_pairs"] >= 1


def test_every_road_has_a_driving_lane_and_min_length(model):
    for r in model.roads:
        assert any(l.type == "driving" for l in r.lanes), r.id
        assert r.length >= 1.0, (r.id, r.length)
        assert r.reference_line.has_z
        assert all(len(c) == 3 and c[2] == 0.0 for c in r.reference_line.coords)


def test_lane_ids_and_directions(model):
    for r in model.roads:
        ids = [l.id for l in r.lanes]
        assert 0 not in ids and len(set(ids)) == len(ids), r.id
        assert ids == sorted(ids, reverse=True), r.id  # ordered left -> right
        for l in r.lanes:
            if l.type == "driving":
                assert (l.direction == "forward") == (l.id < 0), (r.id, l.id)
        if r.junction_id is None and r.tags.get("oneway_road"):
            assert not any(l.id > 0 and l.type == "driving" for l in r.lanes)


def test_eixample_boulevard_crossings_are_single_junctions(model):
    """Passeig de Gracia (central + 2 laterals) x cross street must collapse into one junction."""
    multi = {j.id: len(j.osm_node_ids) for j in model.junctions if len(j.osm_node_ids) > 1}
    assert len(multi) >= 3, multi
    assert max(multi.values()) >= 3
    for j in model.junctions:
        hull = wkt.loads(j.tags["hull_wkt"])
        xs, ys = hull.bounds[0::2], hull.bounds[1::2]
        assert max(xs[1] - xs[0], ys[1] - ys[0]) < 3 * profiles.get().junction.cluster_m


def test_junction_connections(model):
    roads = {r.id: r for r in model.roads}
    for j in model.junctions:
        assert len(j.connections) >= 2, j.id
        assert j.polygon is None  # worker B fills it
        hull = wkt.loads(j.tags["hull_wkt"])
        for c in j.connections:
            cr = roads[c.connecting_road]
            inc = roads[c.incoming_road]
            assert cr.junction_id == j.id and inc.junction_id is None
            assert c.contact_point == "start"
            assert cr.predecessor.element == "road" and cr.predecessor.id == inc.id
            assert cr.successor.element == "road" and cr.successor.id in roads
            assert roads[cr.successor.id].junction_id is None
            assert c.lane_links and all(ll.to_lane == -1 for ll in c.lane_links)
            for ll in c.lane_links:
                lane = next(l for l in inc.lanes if l.id == ll.from_lane)
                assert lane.type == "driving"
            # centreline stays inside the junction's open space (arms end at the chamfer
            # line, so up to ~25 m from the node cluster) - or close to the cluster
            plaza = wkt.loads(j.tags["area_wkt"]).buffer(2.0)
            for x, y, *_ in cr.reference_line.coords:
                assert plaza.contains(Point(x, y)) or hull.distance(Point(x, y)) <= 15.0, (j.id, c.id)
            # connecting road touches both incoming end and outgoing start
            p_in = point_on_road(inc, inc.length if cr.predecessor.contact == "end" else 0.0, 0.0)
            assert Point(cr.reference_line.coords[0][:2]).distance(p_in) < inc.width_left() + inc.width_right() + 0.5
            out = roads[cr.successor.id]
            p_out = point_on_road(out, out.length if cr.successor.contact == "end" else 0.0, 0.0)
            assert Point(cr.reference_line.coords[-1][:2]).distance(p_out) < out.width_left() + out.width_right() + 0.5
            # sampled every ~1 m
            steps = [math.dist(a[:2], b[:2]) for a, b in zip(cr.reference_line.coords, cr.reference_line.coords[1:])]
            assert max(steps) <= 1.3


def test_links_are_symmetric(model):
    roads = {r.id: r for r in model.roads}
    junctions = {j.id: j for j in model.junctions}
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for link, contact in ((r.successor, "end"), (r.predecessor, "start")):
            if link is None:
                continue
            if link.element == "junction":
                j = junctions[link.id]
                touching = [c for c in j.connections if c.incoming_road == r.id]
                leaving = [c for c in j.connections
                           if roads[c.connecting_road].successor.id == r.id]
                assert touching or leaving, (r.id, link.id)
                for c in touching:
                    assert roads[c.connecting_road].predecessor.contact in ("start", "end")
            else:
                other = roads[link.id]
                back = other.successor if link.contact == "end" else other.predecessor
                assert back is not None and back.element == "road" and back.id == r.id
                assert back.contact == contact


def test_no_uturn_connections(model):
    roads = {r.id: r for r in model.roads}
    for r in model.roads:
        if r.junction_id is None:
            continue
        assert r.predecessor.id != r.successor.id, r.id
        assert r.tags["turn"] in ("through", "left", "right")


def test_signals_positions_agree_with_road_s_t(model):
    roads = {r.id: r for r in model.roads}
    kinds = {s.kind for s in model.signals}
    assert {"traffic_light", "crosswalk", "speed_limit"} <= kinds
    ctl_ids = {c.id for c in model.controllers}
    for s in model.signals:
        r = roads[s.road_id]
        assert 0.0 <= s.s <= r.length + 1e-6
        p = point_on_road(r, s.s, s.t)
        assert p.distance(s.position) < 1e-6, s.id
        if s.kind == "traffic_light":
            assert s.controller_id in ctl_ids
            assert s.s in (0.0, r.length)
        if s.kind == "speed_limit":
            assert s.value and 2 < s.value < 40
    for c in model.controllers:
        assert c.signal_ids
        assert c.junction_id in {j.id for j in model.junctions}


def test_buildings_and_objects(model):
    heights = [b for b in model.buildings if b.height is not None]
    levels = [b for b in model.buildings if b.levels is not None]
    assert len(levels) > 50
    assert all(b.footprint.is_valid and b.footprint.area > 0.5 for b in model.buildings)
    assert all(b.effective_height() > 0 for b in model.buildings)
    assert any(b.id.startswith("br") for b in model.buildings)  # multipolygon relations
    assert heights or levels


def test_save_load_round_trip(model, tmp_path):
    d = model.save(tmp_path / "eixample.twin")
    m2 = TwinModel.load(d)
    assert len(m2.roads) == len(model.roads)
    assert len(m2.junctions) == len(model.junctions)
    assert len(m2.signals) == len(model.signals)
    assert len(m2.controllers) == len(model.controllers)
    assert len(m2.buildings) == len(model.buildings)
    assert len(m2.objects) == len(model.objects)
    for a, b in zip(model.roads, m2.roads):
        assert a.id == b.id and a.reference_line.equals_exact(b.reference_line, 1e-9)
        assert [(l.id, l.type, l.width, l.direction, l.tags) for l in a.lanes] == \
               [(l.id, l.type, l.width, l.direction, l.tags) for l in b.lanes]
        assert (a.successor and (a.successor.element, a.successor.id, a.successor.contact)) == \
               (b.successor and (b.successor.element, b.successor.id, b.successor.contact))
    for a, b in zip(model.junctions, m2.junctions):
        assert a.id == b.id and a.osm_node_ids == b.osm_node_ids and a.osm_way_ids == b.osm_way_ids
        assert a.tags["hull_wkt"] == b.tags["hull_wkt"]
        assert [(c.id, c.incoming_road, c.connecting_road, [(l.from_lane, l.to_lane) for l in c.lane_links])
                for c in a.connections] == \
               [(c.id, c.incoming_road, c.connecting_road, [(l.from_lane, l.to_lane) for l in c.lane_links])
                for c in b.connections]
    assert m2.metadata["lanegraph"]["junctions"] == len(model.junctions)


# --------------------------------------------------------------------------- fix round (review items)

def test_remove_jogs_and_join_offset():
    from twinmodel.lanegraph import _remove_jogs, _join_offset
    # a 3 m sideways step over a 3.4 m segment on an otherwise straight line
    jog = [(0.0, 0.0), (40.0, 0.0), (42.0, -3.0), (70.0, -3.0), (100.0, -3.0)]
    out, n = _remove_jogs(jog)
    assert n == 1
    assert out[0] == (0.0, 0.0) and out[-1] == (100.0, -3.0)
    hd = [math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) for a, b in zip(out, out[1:])]
    assert max(abs(h) for h in hd) < 12.0  # the step became a gentle shift
    # a real corner is left alone
    corner = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0)]
    assert _remove_jogs(corner) == (corner, 0)
    # two lines offset separately around a 90 degree corner meet at the mitre, no doubling back
    a = [(0.0, 3.0), (10.0, 3.0)]
    b = [(13.0, 0.0), (13.0, 10.0)]
    joined = _join_offset(a, b)
    assert joined == pytest.approx([(0.0, 3.0), (13.0, 3.0), (13.0, 10.0)])


def test_set_core_width_keeps_lane_order_and_recentres():
    from twinmodel.lanegraph import _set_core_width, _core_width
    from twinmodel.model import Road
    spec = lanes_for_way({"oneway": "yes", "width": "6"}, "living_street")
    r = Road(id="x", reference_line=LineString([(0, 0, 0), (50, 0, 0)]), lanes=spec.lanes)
    assert _core_width(r) == pytest.approx(6.0)
    _set_core_width(r, 3.5)  # >= LaneRules.min_width (3.3 since the 2026-09-02 widening)
    assert _core_width(r) == pytest.approx(3.5)
    assert [l.type for l in r.lanes_right()] == ["driving", "sidewalk"]  # shoulder removed
    ids = [l.id for l in r.lanes]
    assert ids == sorted(ids, reverse=True) and 0 not in ids
    assert r.reference_line.coords[0][1] == pytest.approx(-1.25)  # carriageway centre stays put


def test_crossings_stay_on_their_road(model):
    roads = {r.id: r for r in model.roads}
    n_in_junction = 0
    for s in model.signals:
        if s.kind != "crosswalk":
            continue
        r = roads[s.road_id]
        if s.tags.get("in_junction"):
            n_in_junction += 1
            continue
        keep = min(2.5, r.length / 2)
        assert keep - 1e-6 <= s.s <= r.length - keep + 1e-6, (s.id, s.s, r.length)
    # Eixample zebra crossings run between the chamfer corners, i.e. inside the plaza once the
    # arms end at the chamfer line: they stay on their road (s clamped to the end), flagged
    # with the junction id and their node position for the surface builder
    assert 10 <= n_in_junction <= 40
    for s in model.signals:
        if s.kind == "crosswalk" and s.tags.get("in_junction"):
            assert s.tags["in_junction"] in {j.id for j in model.junctions}
            assert len(s.tags["node_xy"]) == 2
    assert model.metadata["lanegraph"]["signal_nodes_unplaced"] == 0


def test_no_width_steps_between_linked_roads(model):
    from twinmodel.lanegraph import _core_width
    WIDTH_STEP_M, TAPER_MAX_M = profiles.get().geometry.width_step_m, profiles.get().geometry.taper_max_m
    roads = {r.id: r for r in model.roads}
    tapers = [r for r in model.roads if r.tags.get("taper")]
    assert tapers and all(r.length <= TAPER_MAX_M + 1e-6 for r in tapers)
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for link in (r.predecessor, r.successor):
            if link and link.element == "road":
                assert abs(_core_width(r) - _core_width(roads[link.id])) <= WIDTH_STEP_M + 1e-6, (r.id, link.id)
    assert model.metadata["lanegraph"]["tapers_inserted"] >= 1


def test_every_junction_has_at_least_two_arms_and_no_through_pairs(model):
    arms = {j.id: set() for j in model.junctions}
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for link in (r.predecessor, r.successor):
            if link and link.element == "junction":
                arms[link.id].add(r.id)
    for j in model.junctions:
        assert len(arms[j.id]) >= 2, j.id
        assert len(j.connections) >= 1, j.id
        hull = wkt.loads(j.tags["hull_wkt"])
        assert hull.geom_type == "Polygon" and hull.area >= 1.0  # widened to the widest arm


def test_car_park_ramps_and_lateral_stubs(model):
    """Ramps into the tunnelled Passeig de Gracia car park are dropped with the aisle; the
    Consell de Cent living street reached through a 6 m lateral stub is attached to its
    junction instead of dangling."""
    st = model.metadata["lanegraph"]
    assert st["ramps_skipped"] == 2
    assert st["roads_reattached"] >= 1
    assert not any(r.highway == "service" and not r.name for r in model.roads if r.junction_id is None)
    consell = [r for r in model.roads if r.junction_id is None and "Consell de Cent" in r.name]
    unlinked = [r.id for r in consell if r.predecessor is None and r.successor is None]
    assert unlinked == []


def test_interior_dead_ends_are_genuine(model):
    """No road may end mid-block without a link (TM would destroy vehicles there)."""
    from shapely.geometry import box
    frame = LocalFrame.from_bbox(*BBOX)
    xa, ya = frame.to_local(BBOX[1], BBOX[0])
    xb, yb = frame.to_local(BBOX[3], BBOX[2])
    border = box(float(xa), float(ya), float(xb), float(yb)).exterior
    dead = []
    for r in model.roads:
        if r.junction_id is not None:
            continue
        c = list(r.reference_line.coords)
        hw = (r.width_left() + r.width_right()) / 2 + 1.5
        for link, p in ((r.successor, c[-1]), (r.predecessor, c[0])):
            if link is None and border.distance(Point(p[:2])) > hw:
                dead.append(r)
    # the 5 m two-way entrance stub to the pedestrian Passatge de Mendez Vigo is absorbed into
    # its junction (DEAD_END_STUB_M): nothing ends mid-block any more
    assert dead == []


def test_reference_lines_are_simplified(model):
    SHORT_ROAD_M = profiles.get().junction.short_road_m
    for r in model.roads:
        if r.junction_id is not None:
            continue
        c = [p[:2] for p in r.reference_line.coords]
        for a, b, d in zip(c, c[1:], c[2:]):
            h0, h1 = math.atan2(b[1] - a[1], b[0] - a[0]), math.atan2(d[1] - b[1], d[0] - b[0])
            turn = abs((h1 - h0 + math.pi) % (2 * math.pi) - math.pi)
            seg = min(math.dist(a, b), math.dist(b, d))
            assert not (turn > math.radians(60) and seg < 3.0), (r.id, b)  # no zig-zags
        if r.length < SHORT_ROAD_M:
            assert r.tags.get("taper"), r.id  # short pieces only as tapers
    assert model.metadata["lanegraph"]["jogs_removed"] >= 1
    assert model.metadata["lanegraph"]["short_roads_merged"] >= 1


def test_sidewalk_widths_from_separate_footways(model):
    from twinmodel.lanegraph import _separate_sides
    assert _separate_sides({"sidewalk": "separate"}) == (True, True)
    assert _separate_sides({"sidewalk:left": "yes", "sidewalk:right": "separate"}) == (False, True)
    assert _separate_sides({"sidewalk:right": "separate", "reversed": True}) == (True, False)
    # footway-derived on canyon roads (2 x (face - footway)) and on the others (2 x (footway
    # - carriageway edge))
    est = [l.width for r in model.roads if r.junction_id is None
           for l in r.lanes if l.type == "sidewalk" and l.tags.get("width_source") == "footway"]
    assert len(est) >= 25
    assert all(1.5 <= w <= 6.0 for w in est)
    import numpy as np
    p10, p50, p90 = np.percentile(est, [10, 50, 90])
    assert 2.5 <= p10 and 4.0 <= p50 <= 6.0 and 5.0 <= p90 <= 6.0
    st = model.metadata["lanegraph"]
    assert 1 <= st["sidewalks_from_footways"] <= st["sidewalk_separate_sides"]


def test_no_road_band_covers_another_carriageway(model):
    """A road's full band (carriageway + sidewalks) must not cover another road's carriageway:
    a raised sidewalk slab across the crossing street's lanes makes CARLA vehicles collide."""
    from twinmodel.lanegraph import _road_band
    plain = [r for r in model.roads if r.junction_id is None]
    full = {r.id: _road_band(r, full=True) for r in plain}
    carriage = {r.id: _road_band(r, full=False) for r in plain}
    def linked(a, b):  # consecutive roads of one street: their flat end caps overlap in a
        return any(l and l.element == "road" and l.id == b.id  # small wedge at every bend
                   for l in (a.predecessor, a.successor))
    worst = []
    for a in plain:
        for b in plain:
            if a is b or linked(a, b) or not full[a.id].intersects(carriage[b.id]):
                continue
            area = full[a.id].intersection(carriage[b.id]).area
            if area >= 1.0:
                worst.append((a.id, b.id, round(area, 1)))
    assert worst == []
    assert model.metadata["lanegraph"]["band_overlap_cuts"] >= 1


# --------------------------------------------------------------------------- building-aware round

def _by_name(model, name):
    return [r for r in model.roads if r.junction_id is None and name in r.name]


def test_canyon_cross_section_from_building_faces(model):
    """Cerda streets are 20 m building-to-building (Arago 30 m): the carriageway comes from
    the faces, not from the 6-7 m width tags, and is centred between them."""
    MIN_LANE_WIDTH, CANYON_LANE_MAX_M = profiles.get().lane.min_width, profiles.get().lane.canyon_max_width
    from twinmodel import streetspace
    st = model.metadata["lanegraph"]
    assert st["canyon_roads"] >= 20
    bld = streetspace.building_union(model)
    for name, w_lo, w_hi, c_lo, c_hi in (("Pau Claris", 18.5, 22.0, 9.5, 11.5),
                                         ("Roger de Ll", 18.5, 21.0, 9.5, 11.5),
                                         ("Carrer de Val", 18.5, 21.0, 9.5, 14.5),
                                         ("Arag", 28.0, 31.0, 17.0, 19.5)):
        canyon = [r for r in _by_name(model, name) if r.tags.get("cross_section_source") == "buildings"]
        assert canyon, name
        for r in canyon:
            assert w_lo <= r.tags["street_width_m"] <= w_hi, (r.id, r.tags["street_width_m"])
            c = r.width_left() + r.width_right()
            assert c_lo <= c <= c_hi, (r.id, c)
            assert all(cf >= 0.6 for cf in r.tags["canyon_fraction"])
            drv = [l for l in r.lanes if l.type == "driving"]
            assert all(MIN_LANE_WIDTH <= l.width <= CANYON_LANE_MAX_M for l in drv)
            assert r.tags["width_source"] == "buildings"
            # the carriageway is centred between the faces: faces measured from the final
            # reference line agree with the tags, and the carriageway centre is mid-street
            if r.tags.get("street_width_guarded"):
                continue  # width taken from the street's median, not from these faces
            line = LineString([(x, y) for x, y, *_ in r.reference_line.coords])
            _, dl = streetspace.face_distances(line, bld, "left")
            _, dr = streetspace.face_distances(line, bld, "right")
            fl, fr = streetspace.robust_width(dl, 0.0), streetspace.robust_width(dr, 0.0)
            centre_t = (r.width_left() - r.width_right()) / 2.0
            assert abs(centre_t - (fl - fr) / 2.0) < 1.5, (r.id, centre_t, fl, fr)
            # sidewalks reach (about) the faces
            sw_l = sum(l.width for l in r.lanes if l.id > 0 and l.type == "sidewalk")
            sw_r = sum(l.width for l in r.lanes if l.id < 0 and l.type == "sidewalk")
            assert r.width_left() + sw_l <= fl + 1.5 and r.width_right() + sw_r <= fr + 1.5, r.id
    # Passeig de Gracia: the laterals sit between the main carriageway and the buildings, so
    # neither is a canyon - both keep their tags
    for r in _by_name(model, "Passeig de Gr"):
        assert r.tags["cross_section_source"] == "tags", r.id
    main = [r for r in _by_name(model, "Passeig de Gr") if "lateral" not in r.name]
    # 6 tagged lanes at the eu_dense 3.5 m urban lane width (was 19.5 with 3.25 m lanes)
    assert main and all(abs(r.width_left() + r.width_right() - 21.0) < 1e-6 for r in main)
    # never a driving lane narrower than MIN_LANE_WIDTH anywhere
    for r in model.roads:
        for l in r.lanes:
            if l.type == "driving":
                assert l.width >= MIN_LANE_WIDTH - 1e-9, (r.id, l.id, l.width)
    assert st["parking_lanes"] >= 1
    for r in model.roads:
        for l in r.lanes:
            if l.type == "parking" and l.tags.get("width_source") == "buildings":
                assert 2.0 <= l.width <= 2.5
                assert r.highway not in ("living_street", "pedestrian")


def test_canyon_arms_end_at_the_chamfer_line(model):
    """A canyon arm is cut where its building faces end (the chamfer start), not at the node
    hull: Valencia x Pau Claris (single node) arms end ~20-28 m from the node."""
    from twinmodel import streetspace
    st = model.metadata["lanegraph"]
    assert st["chamfer_trims"] >= 20
    bld = streetspace.building_union(model)
    j5 = min(model.junctions, key=lambda j: math.dist(j.tags["centre"], (-63, 182)))
    cx, cy = j5.tags["centre"]
    arms = [(r, end) for r in model.roads if r.junction_id is None
            for end, link in (("start", r.predecessor), ("end", r.successor))
            if link and link.element == "junction" and link.id == j5.id]
    assert len(arms) == 4
    for r, end in arms:
        assert r.tags.get("trim_source") == "chamfer", r.id
        p = r.reference_line.coords[-1 if end == "end" else 0]
        d = math.dist(p[:2], (cx, cy))
        assert 18.0 <= d <= 30.0, (r.id, d)
        # both faces are still there at the end (within the street width + tolerance) ...
        line = LineString([(x, y) for x, y, *_ in r.reference_line.coords])
        s_end = line.length if end == "end" else 0.0
        lo, hi = streetspace.canyon_extent(line, bld, (r.tags["face_left_m"], r.tags["face_right_m"]),
                                           scan=10.0)
        assert (hi if end == "end" else lo) is not None
        assert abs((hi if end == "end" else lo) - s_end) <= 2.0, (r.id, lo, hi, s_end)


def test_every_junction_has_a_plaza(model):
    """Every junction (clusters and single nodes) gets the open space between the corner
    buildings; at an Eixample corner that is the chamfered octagon with its corner triangles."""
    from shapely.geometry import Polygon as _P
    st = model.metadata["lanegraph"]
    assert st["plazas"] == len(model.junctions)
    bld_ids = {b.id for b in model.buildings}
    assert bld_ids
    from twinmodel import streetspace
    bld = streetspace.building_union(model, pad=0.0)
    for j in model.junctions:
        assert j.tags["area_source"] == "plaza"
        assert j.tags["area_wkt"] == j.tags["plaza_wkt"]
        plaza = wkt.loads(j.tags["plaza_wkt"])
        assert plaza.is_valid and plaza.area >= 150.0, (j.id, plaza.area)
        assert plaza.intersection(bld).area < 1.0, j.id  # never on a building
        assert plaza.contains(Point(*j.tags["centre"])) or plaza.distance(Point(*j.tags["centre"])) < 3.0
        # every arm end lies on the plaza boundary (within 1 m)
        for r in model.roads:
            if r.junction_id is not None:
                continue
            for end, link in (("start", r.predecessor), ("end", r.successor)):
                if link and link.element == "junction" and link.id == j.id:
                    p = Point(r.reference_line.coords[-1 if end == "end" else 0][:2])
                    assert p.distance(plaza) <= 1.0, (j.id, r.id, end)
    j5 = min(model.junctions, key=lambda j: math.dist(j.tags["centre"], (-63, 182)))
    plaza = wkt.loads(j5.tags["plaza_wkt"])
    assert isinstance(plaza, _P)
    assert 1400 <= plaza.area <= 2200, plaza.area  # 20 m streets + four 14 m chamfer triangles
    hull_area = wkt.loads(j5.tags["hull_wkt"]).area
    assert plaza.area > 5 * hull_area
    # the corner triangles are in: the plaza reaches the four chamfer faces (~24 m out on the
    # diagonals of the two streets)
    cx, cy = j5.tags["centre"]
    ring = plaza.exterior
    assert max(math.dist((x, y), (cx, cy)) for x, y in ring.coords) >= 24.0


def test_laterals_continue_straight_across_the_main_junction(model):
    """Passeig de Gracia x Arago (j4): the laterals get a straight through connection to their
    continuation and do not fan into / out of the main carriageway."""
    roads = {r.id: r for r in model.roads}
    j4 = min(model.junctions, key=lambda j: math.dist(j.tags["centre"], (-97, -35)))
    assert model.metadata["lanegraph"]["parallel_throughs_dropped"] >= 4
    lateral_through = 0
    for c in j4.connections:
        cr = roads[c.connecting_road]
        inc, out = roads[c.incoming_road], roads[cr.successor.id]
        inc_lat, out_lat = "lateral" in inc.name, "lateral" in out.name
        inc_main = inc.name == "Passeig de Gràcia"
        out_main = out.name == "Passeig de Gràcia"
        if cr.tags["turn"] == "through":
            assert not (inc_lat and out_main), (c.id, "lateral feeds the main carriageway")
            assert not (inc_main and out_lat), (c.id, "main carriageway fans into a lateral")
            if inc_lat and out_lat:
                lateral_through += 1
                assert inc.name == out.name  # Besos stays Besos, Llobregat stays Llobregat
                # straight: heading change under 15 degrees and nearly the chord length
                h0 = math.atan2(*(cr.reference_line.coords[1][1::-1]))
                pts = [p[:2] for p in cr.reference_line.coords]
                chord = math.dist(pts[0], pts[-1])
                assert cr.length <= chord * 1.03, (c.id, cr.length, chord)
    assert lateral_through == 2


def test_parking_lots_extracted_to_metadata():
    """Closed ``amenity=parking`` ways and multipolygon relations (surface / untagged) become
    ``metadata["parking_lots_wkt"]``; underground, multi-storey and rooftop lots do not."""
    from shapely import wkt as _wkt
    from twinmodel.frame import LocalFrame
    from twinmodel.ingest.osm import OsmData, OsmMember, OsmNode, OsmRelation, OsmWay
    from twinmodel.lanegraph import _parking_lots
    frame = LocalFrame(41.3925, 2.1660)
    osm = OsmData()
    nid = [0]

    def node(x, y):
        nid[0] += 1
        lon, lat = frame.to_wgs84(x, y)
        osm.nodes[nid[0]] = OsmNode(nid[0], float(lat), float(lon))
        return nid[0]

    def ring(x0, y0, w, h):
        ids = [node(x0, y0), node(x0 + w, y0), node(x0 + w, y0 + h), node(x0, y0 + h)]
        return ids + [ids[0]]

    osm.ways.append(OsmWay(1, ring(0, 0, 20, 10), {"amenity": "parking"}))
    osm.ways.append(OsmWay(2, ring(50, 0, 20, 10), {"amenity": "parking", "parking": "surface"}))
    osm.ways.append(OsmWay(3, ring(100, 0, 20, 10), {"amenity": "parking", "parking": "underground"}))
    osm.ways.append(OsmWay(4, ring(150, 0, 20, 10), {"amenity": "parking", "parking": "multi-storey"}))
    osm.ways.append(OsmWay(5, ring(200, 0, 20, 10), {"amenity": "parking", "parking": "rooftop"}))
    osm.ways.append(OsmWay(6, ring(250, 0, 20, 10)[:-1], {"amenity": "parking"}))   # not closed
    osm.ways.append(OsmWay(7, ring(300, 0, 40, 40), {}))                            # relation outer
    osm.ways.append(OsmWay(8, ring(310, 10, 10, 10), {}))                           # relation inner
    osm.relations.append(OsmRelation(9, [OsmMember("way", 7, "outer"), OsmMember("way", 8, "inner")],
                                     {"type": "multipolygon", "amenity": "parking"}))
    lots = [_wkt.loads(w) for w in _parking_lots(osm, frame)]
    areas = sorted(round(g.area) for g in lots)
    assert areas == [200, 200, 1500], areas
    assert all(g.is_valid for g in lots)
    assert not any(abs(g.centroid.x - 110) < 15 or abs(g.centroid.x - 160) < 15 or abs(g.centroid.x - 210) < 15
                   for g in lots)
