"""Parking-lot aisles (``highway=service`` + ``service=parking_aisle``) — synthetic OSM, no network.

The fixture is one residential street with a surface parking lot hanging off it:

    ================================== Elm Street (y = 0) ==============================
                                        |  aisle A (two-way, entrance at the street)
        +-------------------------------|------------------------------+   lot polygon
        |            [store]     aisle B |                             |   (amenity=parking)
        |     aisle D (one-way) ---------+--------------------------   |
        |            [store]     aisle C |                             |
        +-------------------------------|------------------------------+

Two buildings flank aisle A inside the lot: an aisle must not pick up the street-canyon cross
section (sidewalks, parking bands) from them.
"""
from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from twinmodel import profiles, surfaces
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph
from twinmodel.surfaces import build_surfaces, carriageway_polygon, road_markings

FT = 0.3048
ORIGIN = (37.4000, -122.0000)
BBOX = (37.3986, -122.0021, 37.4008, -122.9979 + 0.9958)  # filled in below from the frame

_frame = LocalFrame(*ORIGIN)


def _wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = _frame.to_wgs84(x, y)
    return float(lat), float(lon)


# bbox large enough to hold the whole fixture (±200 m east, -180..+80 m north)
_s, _w = _wgs(-200.0, -180.0)
_n, _e = _wgs(200.0, 80.0)
BBOX = (_s, _w, _n, _e)


def _osm():
    """Synthetic Overpass payload: street, lot, aisles, two buildings."""
    elements = []
    nid = [1]
    node_of: dict[tuple[float, float], int] = {}

    def node(x: float, y: float, tags: dict | None = None) -> int:
        key = (round(x, 3), round(y, 3))
        if key in node_of and not tags:
            return node_of[key]
        lat, lon = _wgs(x, y)
        i = nid[0]
        nid[0] += 1
        el = {"type": "node", "id": i, "lat": lat, "lon": lon}
        if tags:
            el["tags"] = tags
        elements.append(el)
        node_of[key] = i
        return i

    def way(wid: int, pts, tags: dict):
        elements.append({"type": "way", "id": wid,
                         "nodes": [node(x, y) for x, y in pts], "tags": tags})

    # the street runs through the aisle entrance node at x = 0
    way(100, [(-150, 0), (-60, 0), (0, 0), (60, 0), (150, 0)],
        {"highway": "residential", "name": "Elm Street"})
    # aisles: A north-south from the street, B and C across it, D one-way on the west side
    way(200, [(0, 0), (0, -25), (0, -45), (0, -85), (0, -105)],
        {"highway": "service", "service": "parking_aisle"})
    way(201, [(-50, -45), (0, -45), (50, -45)],
        {"highway": "service", "service": "parking_aisle"})
    way(202, [(-50, -85), (0, -85), (50, -85)],
        {"highway": "service", "service": "parking_aisle"})
    way(203, [(-50, -45), (-50, -85)],
        {"highway": "service", "service": "parking_aisle", "oneway": "yes"})
    # the lot itself
    lot = [(-65, -20), (65, -20), (65, -110), (-65, -110), (-65, -20)]
    way(300, lot, {"amenity": "parking", "parking": "surface"})
    # two buildings flanking aisle A (a lot between two big-box stores is not a street canyon)
    way(400, [(-40, -50), (-12, -50), (-12, -95), (-40, -95), (-40, -50)],
        {"building": "retail", "building:levels": "1"})
    way(401, [(12, -50), (40, -50), (40, -95), (12, -95), (12, -50)],
        {"building": "retail", "building:levels": "1"})
    return parse_osm({"elements": elements})


@pytest.fixture(scope="module")
def osm():
    return _osm()


def _build(osm, profile: str):
    with profiles.use(profile):
        m = build_lanegraph(osm, _frame, BBOX, name="lot")
        return build_surfaces(m)


@pytest.fixture(scope="module")
def us(osm):
    return _build(osm, "us_suburban")


@pytest.fixture(scope="module")
def eu(osm):
    return _build(osm, "eu_dense")


def _aisles(model):
    return [r for r in model.roads if r.junction_id is None and r.tags.get("parking_aisle")]


def _streets(model):
    return [r for r in model.roads if r.junction_id is None and not r.tags.get("parking_aisle")]


def _lot(model):
    from shapely import wkt as shapely_wkt
    return unary_union([shapely_wkt.loads(w) for w in model.metadata["parking_lots_wkt"]])


# --------------------------------------------------------------------------- ingestion

def test_eu_dense_leaves_parking_aisles_out(eu):
    """EU_DENSE ships with ParkingAisleRules.include = False (the Eixample regression)."""
    assert profiles.EU_DENSE.parking_aisle.include is False
    assert _aisles(eu) == []
    assert eu.metadata["lanegraph"]["parking_aisle_ways"] == 0
    assert eu.metadata["lanegraph"]["parking_aisle_roads"] == 0
    # the lot is still a parking surface, just without aisles in it
    assert eu.metadata["lanegraph"]["parking_lots"] == 1
    assert any(s.kind == "parking" for s in eu.surfaces)


def test_us_ingests_every_aisle_way(us):
    assert us.metadata["lanegraph"]["parking_aisle_ways"] == 4
    aisles = _aisles(us)
    assert len(aisles) >= 6  # the four ways, split at the aisle-aisle junctions
    assert {r.highway for r in aisles} == {"service"}
    assert sum(r.length for r in aisles) > 200.0


def test_aisle_cross_section_is_driving_lanes_only(us):
    A = profiles.US_SUBURBAN.parking_aisle
    for r in _aisles(us):
        assert {l.type for l in r.lanes} == {"driving"}, r.id
        assert r.center_marking is None
        assert all(l.marking is None for l in r.lanes)
        assert all(l.speed_limit == pytest.approx(A.speed_limit) for l in r.lanes)
        if r.tags.get("oneway_road"):
            assert len(r.lanes) == 1
            assert r.lanes[0].width == pytest.approx(A.one_way_width)
        else:
            assert len(r.lanes) == 2
            assert sum(l.width for l in r.lanes) == pytest.approx(A.two_way_width)
    # the one-way aisle (way 203) is there and is narrow
    oneway = [r for r in _aisles(us) if r.tags.get("oneway_road")]
    assert len(oneway) == 1 and oneway[0].lanes[0].width == pytest.approx(13 * FT)


def test_buildings_beside_an_aisle_do_not_make_it_a_canyon(us):
    """Aisle A runs between two retail buildings 24 m apart: it keeps its profile width and
    gets no sidewalk / parking bands from the faces."""
    for r in _aisles(us):
        assert r.tags.get("cross_section_source") != "buildings", r.id
        assert not any(l.type in ("sidewalk", "verge", "parking") for l in r.lanes), r.id


def test_no_markings_on_aisles(us):
    for r in _aisles(us):
        assert road_markings(r, default_markings=True) == []
    aisle_area = unary_union([carriageway_polygon(r) for r in _aisles(us)])
    for mk in us.markings:
        assert not aisle_area.buffer(-0.3).intersects(mk.geometry)


def test_no_crossings_or_signals_on_aisles(us):
    aisle_ids = {r.id for r in _aisles(us)}
    assert not [s for s in us.signals if s.road_id in aisle_ids]


# --------------------------------------------------------------------------- topology

def test_aisle_reaches_the_street_through_a_junction(us):
    aisle_ids = {r.id for r in _aisles(us)}
    street_ids = {r.id for r in _streets(us)}
    linked = [j for j in us.junctions
              if {c.incoming_road for c in j.connections} & aisle_ids
              and {c.incoming_road for c in j.connections} & street_ids]
    assert linked, "no junction joins an aisle to the street"
    j = linked[0]
    # both directions: street -> aisle and aisle -> street
    pairs = {(c.incoming_road, next(r.tags.get("to_road") for r in us.roads if r.id == c.connecting_road))
             for c in j.connections}
    assert any(a in street_ids and b in aisle_ids for a, b in pairs)
    assert any(a in aisle_ids and b in street_ids for a, b in pairs)


def test_aisle_junctions_are_small_and_do_not_swallow_the_street(us):
    aisle_ids = {r.id for r in _aisles(us)}
    street_ids = {r.id for r in _streets(us)}
    for j in us.junctions:
        arms = {c.incoming_road for c in j.connections}
        if not arms & aisle_ids:
            continue
        assert j.polygon is not None and j.polygon.area < 400.0, (j.id, j.polygon.area)
        if not arms & street_ids:      # aisle-aisle junction inside the lot: tiny
            assert j.polygon.area < 200.0, (j.id, j.polygon.area)
    # the street keeps its length: 300 m of Elm Street minus the one junction box
    street_len = sum(r.length for r in us.roads if r.id in street_ids)
    assert street_len > 280.0, street_len


def test_aisle_junction_count_matches_the_fixture(us):
    """One junction at the street, one per aisle-aisle crossing (4), and no giant cluster."""
    assert 4 <= len(us.junctions) <= 8


# --------------------------------------------------------------------------- surfaces

def test_aisle_carriageway_is_inside_drivable(us):
    drivable = unary_union([s.geometry for s in us.surfaces_of("drivable")])
    for r in _aisles(us):
        cw = carriageway_polygon(r)
        outside = cw.difference(drivable.buffer(0.05)).area
        assert outside < 0.02 * cw.area, (r.id, outside, cw.area)


def test_lot_surface_and_aisles_do_not_overlap(us):
    drivable = unary_union([s.geometry for s in us.surfaces_of("drivable")])
    parking = unary_union([s.geometry for s in us.surfaces_of("parking")])
    assert parking.area > 5000.0
    assert drivable.intersection(parking).area < 1.0        # no double surface / z-fighting
    lot = _lot(us).difference(unary_union([b.footprint for b in us.buildings]))
    covered = unary_union([drivable, parking]).intersection(lot)
    assert covered.area > 0.9 * lot.area                    # the lot is fully surfaced
    # the aisles took their share out of the lot: the lot is not one flat slab any more
    assert drivable.intersection(lot).area > 1500.0


def test_no_sidewalk_over_an_aisle(us):
    aisle_area = unary_union([carriageway_polygon(r) for r in _aisles(us)])
    for kind in ("sidewalk", "verge", "island"):
        raised = unary_union([s.geometry for s in us.surfaces_of(kind)]) if us.surfaces_of(kind) else None
        if raised is None or raised.is_empty:
            continue
        assert raised.intersection(aisle_area).area < 1.0, kind


# --------------------------------------------------------------------------- xodr / CARLA

def test_xodr_lanes_stay_in_drivable_and_aisles_carry_waypoints(us, tmp_path):
    carla = pytest.importorskip("carla")
    from twinmodel.export.xodr import build_id_map, export_xodr
    from twinmodel.validate import validate

    xodr = export_xodr(us)
    rep = validate(us, xodr, out_dir=tmp_path)
    assert rep["topology"]["loaded"]
    assert rep["lane_in_drivable"]["fraction"] >= 0.98, rep["violations"][:5]
    assert rep["junction_containment"]["fraction"] >= 0.98

    ids = build_id_map(us)
    aisle_xodr = {ids.road[r.id] for r in _aisles(us)}
    street_xodr = {ids.road[r.id] for r in _streets(us)}
    cmap = carla.Map("lot", xodr)
    per_road: dict[int, int] = {}
    for w in cmap.generate_waypoints(1.0):
        per_road[w.road_id] = per_road.get(w.road_id, 0) + 1
    assert all(per_road.get(rid, 0) > 0 for rid in aisle_xodr)
    # next() from the aisle entrance reaches the street
    entrance = min(_aisles(us), key=lambda r: r.reference_line.distance(Point(0.0, 0.0)))
    start = None
    for w in cmap.generate_waypoints(1.0):
        if w.road_id == ids.road[entrance.id]:
            start = w
            break
    assert start is not None
    seen, frontier, reached = set(), [start], False
    for _ in range(40):
        nxt = []
        for w in frontier:
            for n in w.next(2.0):
                key = (n.road_id, n.lane_id, round(n.s))
                if key in seen:
                    continue
                seen.add(key)
                if n.road_id in street_xodr:
                    reached = True
                nxt.append(n)
        frontier = nxt
        if reached or not frontier:
            break
    if not reached:   # the entrance lane may point into the lot: walk the other way
        for w in start.previous(2.0):
            frontier = [w]
            for _ in range(40):
                nxt = []
                for v in frontier:
                    for n in v.previous(2.0):
                        if n.road_id in street_xodr:
                            reached = True
                        nxt.append(n)
                frontier = nxt
                if reached or not frontier:
                    break
    assert reached, "no waypoint path from the entrance aisle to Elm Street"
