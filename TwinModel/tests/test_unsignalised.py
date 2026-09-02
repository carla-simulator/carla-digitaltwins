"""Unsignalised junctions are governed by something (``JunctionRules.unsignalised_control``).

Without a sign the Traffic Manager treats a crossing as free: ``LocalizationStage`` stops only
for a ``1000001`` junction entry and ``TrafficLightStage`` only for a stop / yield
``RoadInfoSignal``. A twin junction with no ``highway=traffic_signals`` node therefore has to
carry its own regulation -- an all-way stop (MUTCD 2B.07, the US default) or give way on the
minor approaches with a priority road through (Vienna Convention, the EU default).
"""
from __future__ import annotations

import json

import pytest
from lxml import etree

from twinmodel import profiles
from twinmodel.export.xodr import export_xodr
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph, _highway_rank

# a 400 x 400 m box around (0, 0) in a place with no relevant real data
LAT, LON = 41.0, 2.0
D = 0.0018   # ~200 m
BBOX = (LAT - D, LON - D, LAT + D, LON + D)


def _node(nid, dlat, dlon, tags=None):
    e = {"type": "node", "id": nid, "lat": LAT + dlat, "lon": LON + dlon}
    if tags:
        e["tags"] = tags
    return e


def _way(wid, nodes, tags):
    return {"type": "way", "id": wid, "nodes": nodes, "tags": tags}


def _cross(major_tags, minor_tags, centre_tags=None, node_tags=None):
    """A plain 4-arm crossing of an east-west way and a north-south way, no traffic signals.

    ``node_tags`` go on node 6, on the southern arm just short of the junction -- where a real
    ``highway=stop`` / ``highway=give_way`` node sits.
    """
    els = [
        _node(1, 0.0, -D * 0.8), _node(2, 0.0, 0.0, centre_tags), _node(3, 0.0, D * 0.8),
        _node(4, -D * 0.8, 0.0), _node(5, D * 0.8, 0.0), _node(6, -D * 0.08, 0.0, node_tags),
    ]
    els += [_way(100, [1, 2, 3], major_tags), _way(200, [4, 6, 2, 5], minor_tags)]
    return parse_osm({"elements": els})


def _build(osm, profile):
    previous = profiles.get()
    profiles.activate(profile)
    try:
        return build_lanegraph(osm, LocalFrame.from_bbox(*BBOX), BBOX, name="x")
    finally:
        profiles.activate(previous)


def _kinds(model):
    out: dict[str, list] = {}
    for s in model.signals:
        out.setdefault(s.kind, []).append(s)
    return out


PRIMARY = {"highway": "primary", "name": "Major", "lanes": "2"}
RESIDENTIAL = {"highway": "residential", "name": "Minor", "lanes": "2"}


def test_highway_rank_orders_the_classes():
    assert _highway_rank("primary") > _highway_rank("residential") > _highway_rank("service")
    assert _highway_rank("primary") > _highway_rank("primary_link") > _highway_rank("secondary")
    assert _highway_rank("") == 0


def test_us_profile_signs_an_all_way_stop():
    """MUTCD 2B.07, and MUTCD has no priority-road plate at all -- so every approach stops."""
    m = _build(_cross(PRIMARY, RESIDENTIAL), "us_suburban")
    k = _kinds(m)
    assert len(k.get("stop", [])) == 4, {kk: len(v) for kk, v in k.items()}
    assert "priority_road" not in k and "yield" not in k
    assert {s.tags["control"] for s in k["stop"]} == {"all_way_stop"}


def test_eu_profile_gives_way_on_the_minor_road():
    """Give way on the minor approaches, priority road on the major ones. Major = the higher
    OSM highway class, then the larger lane count."""
    m = _build(_cross(PRIMARY, RESIDENTIAL), "eu_dense")
    k = _kinds(m)
    assert len(k.get("yield", [])) == 2 and len(k.get("priority_road", [])) == 2
    major_roads = {s.road_id for s in k["priority_road"]}
    minor_roads = {s.road_id for s in k["yield"]}
    assert not (major_roads & minor_roads)
    for s in k["priority_road"]:
        assert m.road(s.road_id).highway == "primary"
    for s in k["yield"]:
        assert m.road(s.road_id).highway == "residential"


def test_equal_classes_make_everybody_give_way():
    """No approach is more major than the others -> no priority road, everyone yields."""
    m = _build(_cross(RESIDENTIAL, dict(RESIDENTIAL, name="Other")), "eu_dense")
    k = _kinds(m)
    assert len(k.get("yield", [])) == 4
    assert "priority_road" not in k


def test_an_osm_stop_node_wins_over_the_rule():
    """A tagged approach keeps what OSM says; the rule only fills the silence."""
    m = _build(_cross(PRIMARY, RESIDENTIAL, node_tags={"highway": "stop"}), "eu_dense")
    k = _kinds(m)
    stops = k.get("stop", [])
    assert len(stops) == 1 and stops[0].tags.get("source") == "osm"
    # the approach that carries the node is not signed again by the rule
    assert len(k.get("yield", [])) + len(stops) + len(k.get("priority_road", [])) == 4


def test_stop_all_makes_the_whole_junction_an_all_way_stop():
    m = _build(_cross(PRIMARY, RESIDENTIAL, node_tags={"highway": "stop", "stop": "all"}),
               "eu_dense")
    k = _kinds(m)
    assert "priority_road" not in k and "yield" not in k
    assert len(k.get("stop", [])) == 4
    assert {s.tags.get("control", "osm") for s in k["stop"]} == {"all_way_stop", "osm"}


def test_a_signalised_junction_gets_no_regulatory_sign():
    m = _build(_cross(PRIMARY, RESIDENTIAL, centre_tags={"highway": "traffic_signals"}),
               "us_suburban")
    k = _kinds(m)
    assert k.get("traffic_light")
    assert "stop" not in k and "yield" not in k and "priority_road" not in k


def test_a_plain_road_continuation_is_not_an_intersection():
    """Two arms and no crossing movement is a road split, not a junction: nothing is signed."""
    els = [_node(1, 0.0, -D * 0.8), _node(2, 0.0, 0.0), _node(3, 0.0, D * 0.8)]
    els += [_way(100, [1, 2], PRIMARY),
            _way(200, [2, 3], dict(PRIMARY, name="Major continued"))]
    m = _build(parse_osm({"elements": els}), "us_suburban")
    k = _kinds(m)
    assert "stop" not in k and "yield" not in k and "priority_road" not in k


def test_regulatory_signals_export_with_their_carla_types_and_validity():
    """206 stop / 205 give way / 306 priority road (SignalType.h), each with a <validity> over
    its approach's own driving lanes -- without one CARLA synthesises the oncoming side."""
    m = _build(_cross(PRIMARY, RESIDENTIAL), "eu_dense")
    root = etree.fromstring(export_xodr(m).encode())
    want = {"yield": "205", "priority_road": "306"}
    lane_type = {}
    for r in root.iter("road"):
        for ln in r.iter("lane"):
            lane_type[(r.get("id"), int(ln.get("id")))] = ln.get("type")
    by_id = {s.get("id"): s for s in root.iter("signal")}
    seen = 0
    for sig in m.signals:
        if sig.kind not in want:
            continue
        el = by_id[sig.id]
        assert el.get("type") == want[sig.kind]
        assert el.get("dynamic") == "no"
        vs = el.findall("validity")
        assert vs, f"{sig.id} has no <validity>"
        rid = el.getparent().getparent().get("id")
        for v in vs:
            for lane in range(int(v.get("fromLane")), int(v.get("toLane")) + 1):
                assert lane_type[(rid, lane)] == "driving"
                # RHT: orientation '+' governs the negative (right) lanes
                assert (lane < 0) == (el.get("orientation") == "+")
        seen += 1
    assert seen == 4
    # and none of them is in a <controller>: nothing ticks a static sign
    controlled = {c.get("signalId") for ctl in root.findall("controller")
                  for c in ctl.findall("control")}
    assert not (controlled & {s.id for s in m.signals if s.kind in want})


def _placer_functions():
    """``style_for`` / ``pick`` out of ue/place_traffic_signs.py without importing ``unreal``."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "ue" / "place_traffic_signs.py"
    text = src.read_text()
    start = text.index("KMH_PER_MPH")
    end = text.index("def find_actor", start) if "def find_actor" in text[start:] \
        else text.index("def ground_z", start)
    ns: dict = {}
    exec(text[start:end], ns)
    return ns


def test_placer_picks_the_mutcd_stop_plate_for_a_us_signal():
    """A US twin stamps country "US" on every signal (profiles.signal_country), which
    ``--style auto`` maps to MUTCD; the plate is then looked up by OpenDRIVE type."""
    from pathlib import Path
    ns = _placer_functions()
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "ue" / "assets"
         / "sign_catalog_manifest.json").read_text())["assets"]
    us_stop = {"id": "s1", "type": "206", "subtype": "-1", "country": "US"}
    eu_stop = {"id": "s2", "type": "206", "subtype": "-1", "country": "ES"}
    eu_prio = {"id": "s3", "type": "306", "subtype": "-1", "country": "ES"}
    assert ns["style_for"](us_stop, "auto") == "MUTCD"
    assert ns["style_for"](eu_stop, "auto") == "VC"
    got = ns["pick"](manifest, "MUTCD", us_stop["type"], us_stop["subtype"])
    assert got and got[1]["name"] == "stop" and got[1]["style"] == "MUTCD"
    got = ns["pick"](manifest, "VC", eu_stop["type"], eu_stop["subtype"])
    assert got and got[1]["name"] == "stop" and got[1]["style"] == "VC"
    got = ns["pick"](manifest, "VC", eu_prio["type"], eu_prio["subtype"])
    assert got and got[1]["name"] == "priority_road"


def test_us_all_way_stop_picks_the_mutcd_plate(tmp_path):
    """place_traffic_signs.py looks the plate up in the generated catalog manifest by
    (style, OpenDRIVE type, subtype); a US twin stamps country "US", which maps to MUTCD."""
    from pathlib import Path
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "ue" / "assets"
         / "sign_catalog_manifest.json").read_text())
    for style, xtype, name in (("MUTCD", "206", "stop"), ("MUTCD", "205", "yield"),
                               ("VC", "206", "stop"), ("VC", "205", "give_way"),
                               ("VC", "306", "priority_road")):
        hits = [v for v in manifest["assets"].values()
                if v.get("style") == style and str(v.get("xodr_type")) == xtype]
        assert hits, f"no {style} plate for OpenDRIVE type {xtype}"
        assert any(v["name"] == name for v in hits), [v["name"] for v in hits]
    # MUTCD has no priority-road plate, which is exactly why the US profiles sign an all-way
    # stop instead of a priority road
    assert not [v for v in manifest["assets"].values()
                if v.get("style") == "MUTCD" and str(v.get("xodr_type")) == "306"]
    assert profiles.PROFILES["us_suburban"].junction.unsignalised_control == "all_way_stop"
    assert profiles.PROFILES["us_urban"].junction.unsignalised_control == "all_way_stop"
    assert profiles.PROFILES["eu_dense"].junction.unsignalised_control == "minor_yield"
    assert profiles.PROFILES["us_suburban"].signal_country == "US"
