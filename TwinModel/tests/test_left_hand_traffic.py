"""Left-hand traffic (``StreetProfile.drives_on == "left"``), built by reflection
(``twinmodel.handedness``): the forward lanes are the positive OpenDRIVE ids, on the left of the
reference line, the roads carry ``rule="LHT"``, and everything that names a side follows."""
from __future__ import annotations

import pytest
from lxml import etree

from dataclasses import replace

from twinmodel import handedness, profiles
from twinmodel.export.xodr import export_xodr
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph

LAT, LON = 35.9, 139.93
D = 0.0018   # ~200 m
BBOX = (LAT - D, LON - D, LAT + D, LON + D)

# EU_DENSE driving on the left, with the protected turn across the oncoming traffic
LHT = profiles.EU_DENSE.with_(name="lht", drives_on="left",
                              junction=replace(profiles.EU_DENSE.junction, signal_arrow_turns=("right",)))


def _node(nid, dlat, dlon, tags=None):
    e = {"type": "node", "id": nid, "lat": LAT + dlat, "lon": LON + dlon}
    if tags:
        e["tags"] = tags
    return e


def _way(wid, nodes, tags):
    return {"type": "way", "id": wid, "nodes": nodes, "tags": tags}


def _build(osm, profile):
    with profiles.use(profile):
        return build_lanegraph(osm, LocalFrame.from_bbox(*BBOX), BBOX, name="x")


def _signalised_cross():
    els = [_node(1, 0.0, -D * 0.8), _node(2, 0.0, 0.0, {"highway": "traffic_signals"}),
           _node(3, 0.0, D * 0.8), _node(4, -D * 0.8, 0.0), _node(5, D * 0.8, 0.0)]
    els += [_way(100, [1, 2, 3], {"highway": "primary", "name": "Major", "lanes": "4"}),
            _way(200, [4, 2, 5], {"highway": "secondary", "name": "Minor", "lanes": "2"})]
    return parse_osm({"elements": els})


def test_tags_are_read_as_their_mirror_image():
    t = handedness.mirror_osm_tags({"sidewalk": "left", "sidewalk:left:width": "3",
                                    "turn:lanes": "left|through;right|right",
                                    "restriction": "no_left_turn", "name": "Left Street"})
    assert t == {"sidewalk": "right", "sidewalk:right:width": "3",
                 "turn:lanes": "left|through;left|right",
                 "restriction": "no_right_turn", "name": "Left Street"}


def test_forward_lanes_are_the_positive_ids():
    m = _build(_signalised_cross(), LHT)
    streets = [r for r in m.roads if r.junction_id is None]
    assert streets
    for r in streets:
        for l in r.lanes:
            if l.type == "driving":
                assert (l.direction == "forward") == (l.id > 0), (r.id, l)
    assert m.metadata["drives_on"] == "left"
    assert m.bbox_wgs84 == BBOX


def test_left_hand_build_is_the_mirror_of_the_right_hand_one():
    osm = _signalised_cross()
    lht = _build(osm, LHT)
    mirror = handedness.mirror_osm(osm, LocalFrame.from_bbox(*BBOX).origin_lon)
    rht = _build(mirror, profiles.right_handed(LHT))
    assert len(lht.roads) == len(rht.roads) and len(lht.signals) == len(rht.signals)
    by_id = {r.id: r for r in rht.roads}
    for r in lht.roads:
        o = by_id[r.id]
        for (x, y, *_), (ox, oy, *_) in zip(r.reference_line.coords, o.reference_line.coords):
            assert x == pytest.approx(-ox, abs=1e-6) and y == pytest.approx(oy, abs=1e-6)
        assert sorted(l.id for l in r.lanes) == sorted(-l.id for l in o.lanes)


def test_signals_stand_on_the_left_kerb():
    m = _build(_signalised_cross(), LHT)
    lights = [s for s in m.signals if s.kind == "traffic_light"]
    assert lights
    for s in lights:
        # a "+" signal governs traffic along s, which keeps left: its kerb is at t > 0
        assert (s.t > 0) == (s.orientation == "+"), s
        assert all(a > 0 for a, b in s.validities) == (s.orientation == "+"), s


def test_xodr_roads_say_lht():
    m = _build(_signalised_cross(), LHT)
    root = etree.fromstring(export_xodr(m).encode())
    rules = {r.get("rule") for r in root.iter("road")}
    assert rules == {"LHT"}


def test_turn_lanes_keep_their_kerb():
    """``turn:lanes`` lists the lanes left to right; under left-hand traffic the leftmost lane is
    the kerb lane, which is the outermost (highest) positive id."""
    els = [_node(1, 0.0, -D * 0.8), _node(2, 0.0, 0.0), _node(3, 0.0, D * 0.8),
           _node(4, D * 0.8, 0.0)]
    els += [_way(100, [1, 2], {"highway": "primary", "oneway": "yes", "lanes": "2",
                               "turn:lanes": "left|right"}),
            _way(101, [2, 3], {"highway": "primary", "oneway": "yes", "lanes": "2"}),
            _way(200, [2, 4], {"highway": "secondary", "lanes": "2"})]
    m = _build(parse_osm({"elements": els}), LHT)
    approach = next(r for r in m.roads if 100 in r.osm_way_ids and r.junction_id is None)
    driving = sorted((l for l in approach.lanes if l.type == "driving"), key=lambda l: l.id)
    assert [l.id for l in driving] == [1, 2]
    assert driving[-1].tags.get("turn") == ["left"]
    assert driving[0].tags.get("turn") == ["right"]


def test_right_handed_profile_swaps_the_protected_turn():
    r = profiles.right_handed(LHT)
    assert r.drives_on == "right"
    assert r.junction.signal_arrow_turns == ("left",)
    assert profiles.right_handed(profiles.EU_DENSE) is profiles.EU_DENSE


def test_connecting_roads_name_their_incoming_lane_in_the_real_frame():
    """``from_lane`` / ``to_lane`` on a connecting road are lane ids of its neighbours, so they
    flip sign with the lanes."""
    m = _build(_signalised_cross(), LHT)
    roads = {r.id: r for r in m.roads}
    links = [r for r in m.roads if r.junction_id is not None and "from_lane" in r.tags]
    assert links
    for r in links:
        incoming = roads[r.predecessor.id]
        lane = next(l for l in incoming.lanes if l.id == r.tags["from_lane"])
        # it drives into the junction: forward when the junction is at the incoming road's end
        assert lane.type == "driving"
        assert (lane.direction == "forward") == (r.predecessor.contact == "end")
        out = next(l for l in roads[r.tags["to_road"]].lanes if l.id == r.tags["to_lane"])
        assert out.type == "driving"


def test_copied_osm_tags_are_reflected_back():
    els = [_node(1, 0.0, -D * 0.8), _node(2, 0.0, D * 0.8)]
    els += [_way(100, [1, 2], {"highway": "secondary", "lanes": "2", "sidewalk": "left"})]
    m = _build(parse_osm({"elements": els}), LHT)
    (road,) = [r for r in m.roads if 100 in r.osm_way_ids]
    assert road.tags.get("sidewalk") == "left"


def test_protected_right_turn_is_a_right_arrow():
    """The 右折矢印: the protected turn of a left-hand traffic junction is the right turn across
    the oncoming traffic; the head says so in its tags and in the exported signal name."""
    m = _build(_right_turn_pocket(), LHT)
    arrows = [s for s in m.signals if s.kind == "traffic_light_arrow"]
    assert arrows
    for s in arrows:
        assert s.tags.get("arrow_turn") == "right", s
        # the right-turn pocket is the innermost forward lane, +1 under rule="LHT"
        assert s.validities == [(1, 1)], s
    with profiles.use(LHT):
        root = etree.fromstring(export_xodr(m).encode())
    names = {s.get("id"): s.get("name") for s in root.iter("signal")}
    assert {names[s.id] for s in arrows} == {"Signal_3Light_ArrowRight01"}


def _right_turn_pocket():
    els = [_node(1, 0.0, -D * 0.8), _node(2, 0.0, 0.0, {"highway": "traffic_signals"}),
           _node(3, 0.0, D * 0.8), _node(4, -D * 0.8, 0.0), _node(5, D * 0.8, 0.0)]
    els += [_way(100, [1, 2], {"highway": "primary", "name": "Major", "lanes": "5",
                               "lanes:forward": "3", "lanes:backward": "2",
                               "turn:lanes:forward": "left|through|right"}),
            _way(101, [2, 3], {"highway": "primary", "name": "Major", "lanes": "4"}),
            _way(200, [4, 2, 5], {"highway": "secondary", "name": "Minor", "lanes": "2"})]
    return parse_osm({"elements": els})


def test_per_lane_heads_are_named_after_their_real_lane():
    """``split_lane_signals`` runs inside the mirrored build: its ``_lane_m<n>`` / ``_p<n>`` ids
    and ``controlled_lane`` tags follow the lanes back into the real frame, and every stage
    still names its heads."""
    m = _build(_right_turn_pocket(), LHT)
    split = [s for s in m.signals if "controlled_lane" in s.tags]
    assert any("_lane_" in s.id for s in split)
    for s in split:
        (lane, lane2), = s.validities
        assert s.tags["controlled_lane"] == lane == lane2, s
        if "_lane_" in s.id:
            assert s.id.endswith(f"_lane_{'m' if lane < 0 else 'p'}{abs(lane)}"), s
    ids = {s.id for s in m.signals}
    ctl = {c.id for c in m.controllers}
    assert all(i in ids for c in m.controllers for i in c.signal_ids)
    assert all(s.controller_id in ctl for s in m.signals if s.controller_id)


def test_left_right_pairs_swap():
    assert handedness._tags({"canyon_fraction": [0.1, 0.9]})["canyon_fraction"] == [0.9, 0.1]


def test_traffic_light_audit_reads_the_lht_rule(tmp_path):
    """``tools/xodr_tl_check.py`` takes "+" signals to govern the positive lanes of an LHT road,
    so a correct left-hand traffic build has no side violations."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "xodr_tl_check.py"
    spec = importlib.util.spec_from_file_location("xodr_tl_check", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    m = _build(_right_turn_pocket(), LHT)
    xodr = tmp_path / "lht.xodr"
    with profiles.use(LHT):
        export_xodr(m, xodr)
    report = tool.check(xodr)
    codes = {v["code"] for v in report["violations"]}
    assert not codes & {"V_SIDE", "R_SIDE"}, report["violations"]
    # the same file read as right-hand traffic is all wrong-side
    xodr.write_text(xodr.read_text().replace(' rule="LHT"', ""))
    codes = {v["code"] for v in tool.check(xodr)["violations"]}
    assert "V_SIDE" in codes
