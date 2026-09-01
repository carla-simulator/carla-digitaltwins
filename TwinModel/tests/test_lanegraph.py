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
                                 point_on_road, _hermite, _offset_polyline, JUNCTION_CLUSTER_M)
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
    assert all(l.width == 3.0 for l in spec.lanes if l.type == "driving")


def test_lanes_oneway_overrides():
    tags = {"oneway": "yes", "lanes": "3", "width": "9", "maxspeed": "50", "sidewalk:both": "separate",
            "cycleway:right": "lane", "parking:left": "lane", "parking:left:orientation": "parallel",
            "turn:lanes": "left;through|through|right"}
    spec = lanes_for_way(tags, "secondary")
    assert spec.oneway and spec.n_forward == 3 and spec.n_backward == 0
    right = [l for l in spec.lanes if l.id < 0]
    assert [l.type for l in right] == ["driving", "driving", "driving", "biking", "sidewalk"]
    assert all(abs(l.width - 3.0) < 1e-9 for l in right if l.type == "driving")
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
        assert max(xs[1] - xs[0], ys[1] - ys[0]) < 3 * JUNCTION_CLUSTER_M


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
            # centreline stays close to the node cluster
            for x, y, *_ in cr.reference_line.coords:
                assert hull.distance(Point(x, y)) <= 15.0, (j.id, c.id)
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
    _set_core_width(r, 3.0)
    assert _core_width(r) == pytest.approx(3.0)
    assert [l.type for l in r.lanes_right()] == ["driving", "sidewalk"]  # shoulder removed
    ids = [l.id for l in r.lanes]
    assert ids == sorted(ids, reverse=True) and 0 not in ids
    assert r.reference_line.coords[0][1] == pytest.approx(-1.5)  # carriageway centre stays put


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
    assert n_in_junction <= 6  # cycle crossings mapped inside the intersection box


def test_no_width_steps_between_linked_roads(model):
    from twinmodel.lanegraph import _core_width, WIDTH_STEP_M, TAPER_MAX_M
    roads = {r.id: r for r in model.roads}
    tapers = [r for r in model.roads if r.tags.get("taper")]
    assert tapers and all(r.length <= TAPER_MAX_M + 1e-6 for r in tapers)
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for link in (r.predecessor, r.successor):
            if link and link.element == "road":
                assert abs(_core_width(r) - _core_width(roads[link.id])) <= WIDTH_STEP_M + 1e-6, (r.id, link.id)
    assert model.metadata["lanegraph"]["widths_reconciled"] >= 1
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
    from twinmodel.lanegraph import SHORT_ROAD_M
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
    est = [l.width for r in model.roads if r.junction_id is None
           for l in r.lanes if l.type == "sidewalk" and l.tags.get("width_source") == "footway"]
    assert len(est) >= 25
    assert all(1.5 <= w <= 6.0 for w in est)
    import numpy as np
    p10, p50, p90 = np.percentile(est, [10, 50, 90])
    assert 3.5 <= p10 and 4.5 <= p50 <= 6.0 and p90 == 6.0
    st = model.metadata["lanegraph"]
    assert st["sidewalks_from_footways"] == len(est) <= st["sidewalk_separate_sides"]


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
