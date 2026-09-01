"""Driveways and lots without an ``amenity=parking`` polygon (DESIGN.md "Parking lots and their
aisles", "Surfaces").

Synthetic OSM: a residential street, a lot whose only way in is a ``service=driveway``, an aisle
loop inside it with no lot polygon, a driveway from the loop to a garage, a garage stub off the
street, a one-way garage exit into the loop, a one-way garage entrance out of it, and a small
triangle of residential streets (a real island).

- the driveway is a road that joins the street (minor junction) and feeds the loop;
- the garage stub and the one-way exit are not roads; the garage driveway's free end is a
  ``dead_end_*_reason = "driveway"`` (excluded from ``terminal_lanes`` like a cul-de-sac);
- the loop's interior is a ``parking`` surface at grade with no curb, not a raised island; the
  street triangle's interior is an island;
- every aisle lane is reachable from the street (``validate.unreachable_lanes``), and a lot
  whose driveway only leads *out* is reported as ``exit_only``.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from shapely.geometry import Point

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph
from twinmodel.surfaces import build_surfaces

FT = 0.3048
ORIGIN = (37.4000, -122.0000)
_frame = LocalFrame(*ORIGIN)


def _wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = _frame.to_wgs84(x, y)
    return float(lat), float(lon)


_s, _w = _wgs(-250.0, -150.0)
_n, _e = _wgs(250.0, 100.0)
BBOX = (_s, _w, _n, _e)

D1, D2, D3, D4, D5 = 500, 501, 502, 503, 504      # driveway way ids
A1, A2, A3 = 200, 201, 202                        # aisle loop
T1, T2, T3, TL = 300, 301, 302, 303               # street triangle + its link
LOOP_CENTRE = Point(0.0, -42.0)
TRIANGLE_CENTRE = Point(134.5, -53.0)   # the incentre


def _osm(exit_only: bool = False):
    elements = []
    nid = [1]
    node_of: dict[tuple[float, float], int] = {}

    def node(x: float, y: float) -> int:
        key = (round(x, 3), round(y, 3))
        if key in node_of:
            return node_of[key]
        lat, lon = _wgs(x, y)
        i = nid[0]
        nid[0] += 1
        elements.append({"type": "node", "id": i, "lat": lat, "lon": lon})
        node_of[key] = i
        return i

    def way(wid: int, pts, tags: dict):
        elements.append({"type": "way", "id": wid,
                         "nodes": [node(x, y) for x, y in pts], "tags": tags})

    drv = {"highway": "service", "service": "driveway"}
    aisle = {"highway": "service", "service": "parking_aisle"}
    way(100, [(-200, 0), (-100, 0), (0, 0), (100, 0), (157, 0), (200, 0)],
        {"highway": "residential", "name": "Elm Street"})
    # D1: the lot's only entrance, street -> loop (one-way *out* of the lot when exit_only)
    way(D1, [(0, -30), (0, 0)] if exit_only else [(0, 0), (0, -30)],
        {**drv, "oneway": "yes"} if exit_only else drv)
    # the aisle loop, 30 x 24 m: its interior would be a MAX_ISLAND_AREA-sized island
    way(A1, [(0, -30), (-15, -30), (-15, -54)], aisle)
    way(A2, [(-15, -54), (15, -54)], aisle)
    way(A3, [(15, -54), (15, -30), (0, -30)], aisle)
    # D2: from the loop to a garage (free end): a road, its end a documented dead end
    way(D2, [(15, -54), (15, -80)], drv)
    # D3: garage stub off the street touching nothing else: not a road
    way(D3, [(100, 0), (100, 40)], drv)
    # D4: one-way garage *exit* from a free end into the loop: nothing can enter it
    way(D4, [(-40, -54), (-15, -54)], {**drv, "oneway": "yes"})
    # D5: one-way garage *entrance* out of the loop to a free end: a dead end
    way(D5, [(-15, -30), (-40, -30)], {**drv, "oneway": "yes"})
    # a 45 m triangle of residential streets off Elm Street: the hole between its
    # carriageways (~270 m2) is a real island
    way(TL, [(157, 0), (157, -40)], {"highway": "residential", "name": "Fir Court"})
    way(T1, [(112, -40), (157, -40)], {"highway": "residential", "name": "Fir Court"})
    way(T2, [(157, -40), (134.5, -79)], {"highway": "residential", "name": "Fir Court"})
    way(T3, [(134.5, -79), (112, -40)], {"highway": "residential", "name": "Fir Court"})
    return parse_osm({"elements": elements})


def _build(osm, profile):
    with profiles.use(profile):
        m = build_lanegraph(osm, _frame, BBOX, name="driveways")
        return build_surfaces(m)


@pytest.fixture(scope="module")
def us():
    return _build(_osm(), "us_suburban")


@pytest.fixture(scope="module")
def us_small_clusters():
    """The triangle's corners are 45 m apart: with the suburban 60 m cluster radius they are
    one junction and its interior is paved. Small clusters keep them apart."""
    p = profiles.by_name("us_suburban")
    p = p.with_(junction=replace(p.junction, cluster_m=10.0, dual_carriageway_cluster_m=10.0))
    return _build(_osm(), p)


@pytest.fixture(scope="module")
def us_exit_only():
    return _build(_osm(exit_only=True), "us_suburban")


def _plain(model):
    return [r for r in model.roads if r.junction_id is None]


def _by_way(model, wid):
    return [r for r in _plain(model) if wid in r.osm_way_ids]


# --------------------------------------------------------------------------- ingestion

def test_driveway_is_a_road_that_joins_the_street(us):
    d1 = _by_way(us, D1)
    assert d1, "the entrance driveway was not ingested"
    assert all(r.tags.get("driveway") and r.tags.get("parking_aisle") for r in d1)
    street_ids = {r.id for r in _plain(us) if not r.tags.get("parking_aisle")}
    d1_ids = {r.id for r in d1}
    joined = [j for j in us.junctions
              if {c.incoming_road for c in j.connections} & street_ids
              and {c.incoming_road for c in j.connections} & d1_ids]
    assert joined, "no junction joins the driveway to Elm Street"
    j = joined[0]
    to_road = {r.id: r.tags.get("to_road") for r in us.roads if r.junction_id == j.id}
    pairs = {(c.incoming_road, to_road[c.connecting_road]) for c in j.connections}
    assert any(a in street_ids and b in d1_ids for a, b in pairs), "street -> driveway missing"
    assert any(a in d1_ids and b in street_ids for a, b in pairs), "driveway -> street missing"
    # a minor junction: the street keeps running, the junction did not swallow the block
    assert j.polygon is None or j.polygon.area < 400.0


def test_driveway_cross_section(us):
    P = profiles.by_name("us_suburban").parking_aisle
    two_way = _by_way(us, D1)[0]
    lanes = [l for l in two_way.lanes if l.type == "driving"]
    assert len(lanes) == 2 and all(l.width == pytest.approx(P.two_way_width / 2) for l in lanes)
    assert all(l.type in ("driving",) for l in two_way.lanes)  # no sidewalk / verge / parking
    one_way = _by_way(us, D5)[0]
    lanes = [l for l in one_way.lanes if l.type == "driving"]
    assert len(lanes) == 1 and lanes[0].width == pytest.approx(P.driveway_width)
    assert all(l.speed_limit == pytest.approx(P.speed_limit) for l in lanes)


def test_garage_stub_and_one_way_exit_are_not_roads(us):
    assert not _by_way(us, D3), "a driveway off the street to nothing is not a road"
    assert not _by_way(us, D4), "a one-way driveway from a free end can never be entered"


def test_driveway_free_ends_are_documented_dead_ends(us):
    for wid in (D2, D5):
        roads = _by_way(us, wid)
        assert roads, wid
        tagged = [(end, r) for r in roads for end in ("start", "end") if r.tags.get(f"dead_end_{end}")]
        assert tagged, f"way {wid}: no dead end tagged"
        assert all(r.tags.get(f"dead_end_{end}_reason") == "driveway" for end, r in tagged)
    # the street's open ends are cul-de-sacs (degree-1 OSM nodes), not driveways
    elm = [r for r in _by_way(us, 100) if r.tags.get("dead_end_start") or r.tags.get("dead_end_end")]
    assert elm
    for r in elm:
        for end in ("start", "end"):
            if r.tags.get(f"dead_end_{end}"):
                assert r.tags[f"dead_end_{end}_reason"] == "cul_de_sac"


def test_eu_dense_ignores_driveways():
    m = _build(_osm(), "eu_dense")
    assert not any(r.tags.get("driveway") or r.tags.get("parking_aisle") for r in _plain(m))


# --------------------------------------------------------------------------- surfaces

def test_aisle_loop_without_a_lot_is_parking_at_grade(us):
    parking = [s for s in us.surfaces_of("parking") if s.geometry.contains(LOOP_CENTRE)]
    assert parking, "the aisle loop's interior is not a parking surface"
    assert all(s.z_offset == 0.0 for s in parking)
    assert not any(s.geometry.contains(LOOP_CENTRE) for s in us.surfaces_of("island"))
    assert not any(s.geometry.contains(LOOP_CENTRE) for s in us.surfaces_of("drivable"))
    stats = us.metadata["surfaces"]
    assert stats["inferred_lot_count"] == 1 and stats["parking_lot_count"] == 0
    assert stats["parking_area"] > 0.0
    # flush with the aisles: no curb around the stall field
    lot = parking[0].geometry
    assert not any(c.geometry.intersects(lot.buffer(0.1)) for c in us.curbs)


def test_street_triangle_is_an_island(us_small_clusters):
    m = us_small_clusters
    islands = [s for s in m.surfaces_of("island") if s.geometry.contains(TRIANGLE_CENTRE)]
    assert islands, "the street triangle's interior is not an island"
    assert islands[0].z_offset > 0.0
    assert not any(s.geometry.contains(TRIANGLE_CENTRE) for s in m.surfaces_of("parking"))
    # ... raised behind a curb (the island sits inside the streets' verges: the curb is the
    # carriageway's, whichever raised kind it borders)
    assert any(c.geometry.distance(TRIANGLE_CENTRE) < 15.0 for c in m.curbs)
    # the aisle loop is a lot at grade in this build too
    assert any(s.geometry.contains(LOOP_CENTRE) for s in m.surfaces_of("parking"))


def test_inferred_lots_off_when_the_profile_says_so():
    p = profiles.by_name("us_suburban")
    p = p.with_(parking_aisle=replace(p.parking_aisle, lot_enclosure_fraction=0.0))
    m = _build(_osm(), p)
    assert not m.surfaces_of("parking")
    assert any(s.geometry.contains(LOOP_CENTRE) for s in m.surfaces_of("island"))


# --------------------------------------------------------------------------- reachability

def test_every_aisle_lane_is_reachable_from_the_street(us, tmp_path):
    pytest.importorskip("carla")
    from twinmodel.export.xodr import export_xodr
    from twinmodel.validate import validate

    rep = validate(us, export_xodr(us), out_dir=tmp_path)
    assert rep["topology"]["loaded"]
    assert rep["lane_in_drivable"]["fraction"] == 1.0
    ur = rep["unreachable_lanes"]
    assert ur["count"] == 0 and ur["pass"], ur["groups"]
    # the garage driveways end in documented dead ends, not terminal lanes
    assert rep["terminal_lanes"]["count"] == 0
    assert rep["junction_lane_links"]["unlinked_arms"] == 0
    assert rep["junction_slivers"]["count"] == 0


def test_lot_with_an_exit_only_driveway_is_reported(us_exit_only, tmp_path):
    pytest.importorskip("carla")
    from twinmodel.export.xodr import export_xodr
    from twinmodel.validate import validate

    rep = validate(us_exit_only, export_xodr(us_exit_only), out_dir=tmp_path)
    ur = rep["unreachable_lanes"]
    assert not ur["pass"] and ur["in_bbox_count"] > 0
    reasons = {g["reason"] for g in ur["groups"]}
    assert reasons <= {"exit_only", "isolated"} and reasons
    assert any(a in {A1, A2, A3} for g in ur["groups"] for a in g["osm_way_ids"])
    assert any(v["kind"] == "unreachable_lanes" for v in rep["violations"])
