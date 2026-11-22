"""Splits and merges off the freeway (``JunctionRules.split_merge_gores``) and short service
connectors (``LaneRules.service_connectors``): a station rotary whose one-way loop splits into
two branches right behind its entrance."""
from __future__ import annotations

import math
from dataclasses import replace

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph

LAT, LON = 35.9, 139.93
M_LAT = 1.0 / 111_320.0
M_LON = 1.0 / (111_320.0 * math.cos(math.radians(LAT)))
D = 0.0018
BBOX = (LAT - D, LON - D, LAT + D, LON + D)

ON = profiles.EU_DENSE.with_(
    name="rotary",
    junction=replace(profiles.EU_DENSE.junction, split_merge_gores=True),
    lane=replace(profiles.EU_DENSE.lane, service_connectors=True))


def _rotary(entrance: float = 15.0):
    """A two-way street along x; a one-way service loop leaves it at x = -20, splits 15 m in
    into an outer branch and an inner one 4 m from it, and the two rejoin before the exit at
    x = 20."""
    e = entrance
    pts = {1: (-150, 0), 2: (-20, 0), 3: (20, 0), 4: (150, 0),
           5: (-20, e), 6: (20, e),
           7: (-20, e + 20), 8: (20, e + 20),
           9: (-12, e + 4), 10: (12, e + 4)}
    els = [{"type": "node", "id": i, "lat": LAT + y * M_LAT, "lon": LON + x * M_LON}
           for i, (x, y) in pts.items()]
    svc = {"highway": "service", "oneway": "yes"}
    els += [{"type": "way", "id": 100, "nodes": [1, 2, 3, 4],
             "tags": {"highway": "secondary", "lanes": "2", "name": "Main"}},
            {"type": "way", "id": 201, "nodes": [2, 5], "tags": svc},
            {"type": "way", "id": 202, "nodes": [5, 7, 8, 6], "tags": svc},
            {"type": "way", "id": 203, "nodes": [5, 9, 10, 6], "tags": svc},
            {"type": "way", "id": 204, "nodes": [6, 3], "tags": svc}]
    return parse_osm({"elements": els})


def _build(profile, osm=None):
    with profiles.use(profile):
        return build_lanegraph(osm or _rotary(), LocalFrame.from_bbox(*BBOX), BBOX, name="x")


def test_the_split_and_the_merge_are_gores_of_their_own():
    m = _build(ON)
    assert m.metadata["lanegraph"]["split_merge_gores"] == 2
    gores = [j for j in m.junctions if j.tags.get("kind") == "gore"]
    assert len(gores) == 2
    for j in gores:
        assert len(j.osm_node_ids) == 1
        # one lane splits into two (or two merge into one): two lane links, no turns
        assert len(j.connections) == 2, j.connections
    # the two street junctions stay apart from the gores and from each other
    streets = [j for j in m.junctions if j.tags.get("kind") != "gore"]
    assert len(streets) == 2 and all(len(j.osm_node_ids) == 1 for j in streets)
    # both branches survive as roads of their own
    branches = [r for r in m.roads if r.junction_id is None and {202, 203} & set(r.osm_way_ids)]
    assert len(branches) == 2


def test_without_the_options_the_rotary_does_not_reach_the_street():
    m = _build(profiles.EU_DENSE)
    assert m.metadata["lanegraph"].get("split_merge_gores", 0) == 0
    assert not [r for r in m.roads if {201, 204} & set(r.osm_way_ids)]


def test_a_short_connector_is_a_road_only_with_the_option():
    with_option = _build(ON)
    assert [r for r in with_option.roads if 201 in r.osm_way_ids]
    without = _build(profiles.EU_DENSE.with_(
        junction=replace(profiles.EU_DENSE.junction, split_merge_gores=True)))
    assert not [r for r in without.roads if 201 in r.osm_way_ids]


def test_a_gore_too_close_to_the_street_folds_into_the_crossing():
    """Behind a 10 m entrance no road of validate.SLIVER_M (5 m) is left between the split and
    the street: the two become one crossing instead of a gore and a sliver."""
    m = _build(ON, _rotary(entrance=10.0))
    junction_ids = {j.id for j in m.junctions}
    for r in m.roads:
        if r.junction_id is None and r.predecessor and r.successor \
                and r.predecessor.id in junction_ids and r.successor.id in junction_ids:
            assert r.length >= 5.0, (r.id, r.length)
    assert sum(1 for j in m.junctions if j.tags.get("kind") == "gore") < 2


def test_two_short_one_way_stubs_meeting_head_on_stay_dropped():
    """Two short unnamed one-way service ways that end in each other are a dead end in the
    data, not a connector: each meets only the other at its far end."""
    pts = {1: (-150, 0), 2: (-20, 0), 3: (20, 0), 4: (150, 0), 5: (0, 12)}
    els = [{"type": "node", "id": i, "lat": LAT + y * M_LAT, "lon": LON + x * M_LON}
           for i, (x, y) in pts.items()]
    svc = {"highway": "service", "oneway": "yes"}
    els += [{"type": "way", "id": 100, "nodes": [1, 2, 3, 4],
             "tags": {"highway": "secondary", "lanes": "2", "name": "Main"}},
            {"type": "way", "id": 301, "nodes": [2, 5], "tags": svc},
            {"type": "way", "id": 302, "nodes": [3, 5], "tags": svc}]
    m = _build(ON, parse_osm({"elements": els}))
    assert not [r for r in m.roads if {301, 302} & set(r.osm_way_ids)]
