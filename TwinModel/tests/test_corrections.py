"""twinmodel.corrections: OSM patch ops, lane-graph hooks, drivable patch (fixture, no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from twinmodel import corrections as C
from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph

FIX = Path(__file__).parent / "fixtures"
FIXTURE = FIX / "eixample_overpass.json"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


def tiny_raw() -> dict:
    """Two ways sharing node 3: 1-2-3 (residential, lanes=2) and 3-4."""
    return {"elements": [
        {"type": "node", "id": 1, "lat": 41.0, "lon": 2.0},
        {"type": "node", "id": 2, "lat": 41.0, "lon": 2.001},
        {"type": "node", "id": 3, "lat": 41.0, "lon": 2.002, "tags": {"highway": "traffic_signals"}},
        {"type": "node", "id": 4, "lat": 41.001, "lon": 2.002},
        {"type": "way", "id": 10, "nodes": [1, 2, 3], "tags": {"highway": "residential", "lanes": "2"}},
        {"type": "way", "id": 11, "nodes": [3, 4], "tags": {"highway": "residential"}},
    ]}


def by_id(doc, typ):
    return {e["id"]: e for e in doc["elements"] if e["type"] == typ}


def test_validate_catches_bad_ops():
    problems = C.validate([{"op": "way.tags"}, {"op": "nope"}, {"op": "node.add", "node": 5, "lat": 1, "lon": 2},
                           {"op": "drivable.cut", "polygon": [[0, 0]]}])
    assert len(problems) == 4
    assert C.validate([{"op": "way.tags", "way": 10, "set": {"lanes": "3"}}]) == []


def test_apply_osm_tags_nodes_ways():
    ops = [
        {"id": "a", "op": "way.tags", "way": 10, "set": {"lanes": "3", "lanes:psv": "1"}, "unset": ["highway"]},
        {"id": "b", "op": "node.move", "node": 2, "lat": 41.0005, "lon": 2.001},
        {"id": "c", "op": "node.add", "node": -1, "lat": 41.0, "lon": 2.0015},
        {"id": "d", "op": "way.nodes", "way": 10, "nodes": [1, 2, -1, 3]},
        {"id": "e", "op": "way.add", "way": -2, "nodes": [1, 4], "tags": {"highway": "service"}},
        {"id": "f", "op": "way.delete", "way": 11},
        {"id": "g", "op": "way.tags", "way": 999, "set": {"lanes": "1"}},          # unmatched
        {"id": "h", "op": "node.tags", "node": 3, "set": {"crossing": "marked"}, "unset": ["highway"]},
        {"id": "i", "op": "way.split", "way": 10, "node": 2, "new_way": -3},
        {"id": "j", "op": "way.tags", "way": 10, "set": {"x": "1"}, "disabled": True},
    ]
    raw = tiny_raw()
    doc, rep = C.apply_osm(raw, ops)
    assert raw == tiny_raw(), "input must not be modified"
    ways, nodes = by_id(doc, "way"), by_id(doc, "node")
    assert ways[10]["tags"] == {"lanes": "3", "lanes:psv": "1"}
    assert ways[10]["nodes"] == [1, 2]                      # split at 2
    assert ways[-3]["nodes"] == [2, -1, 3] and ways[-3]["tags"] == ways[10]["tags"]
    assert nodes[2]["lat"] == 41.0005 and nodes[-1]["lon"] == 2.0015
    assert ways[-2]["tags"] == {"highway": "service"} and 11 not in ways
    assert nodes[3]["tags"] == {"crossing": "marked"}
    assert [u["id"] for u in rep["unmatched"]] == ["g"]
    assert rep["n_ops"] == 9 and len(rep["applied"]) == 8


def test_node_delete_drops_from_ways():
    doc, _ = C.apply_osm(tiny_raw(), [{"op": "node.delete", "node": 2}])
    assert by_id(doc, "way")[10]["nodes"] == [1, 3] and 2 not in by_id(doc, "node")


def test_split_needs_interior_node():
    _, rep = C.apply_osm(tiny_raw(), [{"id": "s", "op": "way.split", "way": 10, "node": 1, "new_way": -1}])
    assert rep["unmatched"] and rep["unmatched"][0]["id"] == "s"


def test_end_shift_lookup():
    sh = C.end_shifts([{"op": "road.end", "way": 10, "node": 3, "shift_m": -2.5},
                       {"op": "road.end", "way": 11, "node": 3, "shift_m": 1.0, "disabled": True}])
    assert sh == {(10, 3): -2.5}
    assert C.lookup_end_shift(sh, [10, 12], [3, 7]) == -2.5
    assert C.lookup_end_shift(sh, [11], [3]) is None
    assert C.lookup_end_shift({}, [10], [3]) is None


def test_drivable_patch_and_raised_extras():
    frame = LocalFrame(41.0, 2.0)
    tw = frame._to_wgs()
    def ring(b):
        xs, ys = zip(*b.exterior.coords)
        lons, lats = tw.transform(xs, ys)
        return [[float(a), float(b_)] for a, b_ in zip(lons, lats)]
    base = box(0, 0, 100, 20)
    ops = [{"op": "drivable.cut", "polygon": ring(box(40, 15, 60, 30)), "as": "sidewalk"},
           {"op": "drivable.add", "polygon": ring(box(-10, 5, 0, 15))},
           {"op": "drivable.cut", "polygon": ring(box(0, 0, 5, 5)), "as": "ground", "disabled": True}]
    out = C.drivable_patch(ops, frame, base)
    assert abs(out.area - (2000 - 100 + 100)) < 1.0
    assert not out.contains(Point(50, 17)) and out.contains(Point(-5, 10))
    # dict form patches the ground layer only
    outd = C.drivable_patch(ops, frame, {1: box(0, 0, 10, 10), 0: base})
    assert abs(outd[0].area - out.area) < 1.0 and outd[1].area == 100
    extras = C.raised_extras(ops, frame)
    assert len(extras) == 1 and extras[0][1] == "sidewalk" and abs(extras[0][0].area - 300) < 1.0
    assert C.has_drivable_ops(ops) and not C.has_drivable_ops([{"op": "way.tags", "way": 1}])
    assert C.drivable_patch([], frame, base) is base


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    c = C.Corrections(name="t", ops=[{"id": "c1", "op": "way.tags", "way": 10, "set": {"lanes": "3"}}], path=p)
    c.save()
    c2 = C.load(p)
    assert c2.ops == c.ops and c2.name == "t"
    assert C.load_or_empty(tmp_path / "missing.json", "x").ops == []
    assert C.default_path("data", "eixample") == Path("data/corrections/eixample.json")
    assert C.new_id(c.ops) == "c2"
    assert C.min_new_osm_id([{"op": "way.add", "way": -7, "nodes": [-9, 3]}]) == -9


@pytest.fixture(scope="module")
def osm_and_frame():
    raw = json.loads(FIXTURE.read_text())
    return raw, LocalFrame.from_bbox(*BBOX)


def test_lanegraph_end_shift_and_junction_override(osm_and_frame):
    raw, frame = osm_and_frame
    osm = parse_osm(raw)
    with profiles.use(profiles.by_name("eu_dense")):
        base = build_lanegraph(osm, frame, BBOX, name="t")
        # pick a road whose end touches a junction
        r0 = next(r for r in base.roads if r.junction_id is None and r.successor is not None
                  and r.successor.element == "junction" and r.length > 40)
        j0 = base.junction(r0.successor.id)
        way, node = r0.osm_way_ids[0], j0.osm_node_ids[0]
        shifted = build_lanegraph(osm, frame, BBOX, name="t", end_shifts={(way, node): -3.0})
    r1 = next(r for r in shifted.roads if r.osm_way_ids == r0.osm_way_ids and r.junction_id is None
              and r.successor is not None and r.successor.id == r0.successor.id)
    assert r1.tags.get("end_shift_end") == -3.0
    assert 2.0 < r0.length - r1.length < 4.0          # pulled back ~3 m (simplify tolerance)
    p0, p1 = Point(r0.reference_line.coords[-1][:2]), Point(r1.reference_line.coords[-1][:2])
    assert 2.0 < p0.distance(p1) < 4.0
    # junction polygon override keyed by OSM node ids
    tw = frame._to_wgs()
    sq = Polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)])
    xs, ys = zip(*sq.exterior.coords)
    lons, lats = tw.transform(xs, ys)
    rep = C.apply_junctions(shifted, [{"id": "j", "op": "junction.polygon", "nodes": list(j0.osm_node_ids),
                                       "polygon": [[a, b] for a, b in zip(lons, lats)]},
                                      {"id": "k", "op": "junction.polygon", "nodes": [1], "polygon": [[0, 0], [0, 1], [1, 1]]}],
                            frame)
    assert [a["junction"] for a in rep["applied"]] == [j0.id] and rep["unmatched"][0]["id"] == "k"
    jj = shifted.junction(j0.id)
    assert jj.tags["polygon_source"] == "correction" and abs(jj.polygon.area - 100.0) < 0.5
