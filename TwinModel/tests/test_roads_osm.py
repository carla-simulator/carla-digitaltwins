"""``twinmodel build --roads-osm``: the bbox comes from the file and keeps all of its roads."""
from __future__ import annotations

from twinmodel.frame import LocalFrame
from twinmodel import cli
from twinmodel.ingest.osm import has_highways, osm_xml_bbox, parse_osm, read_osm_xml, replace_roads
from twinmodel.lanegraph import build_lanegraph

LAT, LON = 35.9, 139.93


def _roads():
    # a street that bows out to the west edge of the file's extent, and one across it
    pts = [(0.0, 0.0), (0.0005, -0.0012), (0.0010, -0.0015), (0.0015, -0.0012), (0.0020, 0.0)]
    els = [{"type": "node", "id": i + 1, "lat": LAT + a, "lon": LON + b} for i, (a, b) in enumerate(pts)]
    els += [{"type": "node", "id": 10, "lat": LAT + 0.0010, "lon": LON + 0.0010}]
    els += [{"type": "way", "id": 1, "nodes": [1, 2, 3, 4, 5],
             "tags": {"highway": "tertiary", "lanes": "2"}},
            {"type": "way", "id": 2, "nodes": [3, 10], "tags": {"highway": "tertiary", "lanes": "2"}}]
    return {"elements": els}


def test_bbox_encloses_every_node():
    roads = _roads()
    s, w, n, e = osm_xml_bbox(roads)
    for el in roads["elements"]:
        if el["type"] == "node":
            assert s < el["lat"] < n and w < el["lon"] < e


def test_a_road_near_the_edge_is_not_clipped():
    roads = _roads()
    bbox = osm_xml_bbox(roads)
    m = build_lanegraph(parse_osm(roads), LocalFrame.from_bbox(*bbox), bbox, name="x")
    streets = [r for r in m.roads if 1 in r.osm_way_ids and r.junction_id is None]
    # both halves of way 1 either side of the junction with way 2, nothing cut away at the edge
    total = sum(r.reference_line.length for r in streets)
    assert len(streets) == 2
    assert total > 230.0, total


def test_local_ids_are_renumbered_without_collisions():
    """A local file may hold both ``5`` and ``-5`` (JOSM gives new elements negative ids):
    they stay distinct elements, and every reference follows its element."""
    raw = {"elements": [{"type": "node", "id": 7, "lat": LAT, "lon": LON},
                        {"type": "way", "id": 70, "nodes": [7], "tags": {"highway": "primary"}},
                        {"type": "way", "id": 71, "nodes": [7], "tags": {"building": "yes"}}]}
    roads = {"elements": [
        {"type": "node", "id": 5, "lat": LAT, "lon": LON, "tags": {}},
        {"type": "node", "id": -5, "lat": LAT + 0.001, "lon": LON, "tags": {}},
        {"type": "node", "id": 6, "lat": LAT + 0.002, "lon": LON, "tags": {}},
        {"type": "way", "id": 5, "nodes": [5, -5], "tags": {"highway": "residential"}},
        {"type": "way", "id": -5, "nodes": [-5, 6], "tags": {"highway": "residential"}},
        {"type": "relation", "id": 1, "tags": {"type": "restriction", "restriction": "no_u_turn"},
         "members": [{"type": "way", "ref": 5, "role": "from"}, {"type": "node", "ref": -5, "role": "via"},
                     {"type": "way", "ref": -5, "role": "to"}]}]}
    out = replace_roads(raw, roads)["elements"]
    local = [e for e in out if e["id"] < 0]
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in local if e["type"] == "node"}
    ways = {e["id"]: e["nodes"] for e in local if e["type"] == "way"}
    assert len(nodes) == 3 and len(ways) == 2
    # way 5 runs 5 -> -5, way -5 runs -5 -> 6: they share exactly the node that was -5
    (a, b), (c, d) = ways.values()
    assert b == c and len({a, b, d}) == 3
    assert nodes[b] == (LAT + 0.001, LON)
    (rel,) = [e for e in local if e["type"] == "relation"]
    assert [m["ref"] for m in rel["members"]] == [*ways.keys()][:1] + [b] + [*ways.keys()][1:]
    # OSM's own non-road elements are kept with their ids; its roads are gone
    assert {e["id"] for e in out if e["id"] > 0} == {7, 71}


def test_road_nodes_other_elements_use_stay_untagged():
    """An OSM crossing node that a kept kerb line runs through keeps the kerb whole, but no
    longer carries its road tags onto the replaced network."""
    raw = {"elements": [
        {"type": "node", "id": 1, "lat": LAT, "lon": LON},
        {"type": "node", "id": 2, "lat": LAT, "lon": LON + 0.001,
         "tags": {"highway": "crossing", "crossing": "zebra"}},
        {"type": "node", "id": 3, "lat": LAT, "lon": LON + 0.002},
        {"type": "node", "id": 4, "lat": LAT + 0.001, "lon": LON, "tags": {"highway": "traffic_signals"}},
        {"type": "way", "id": 10, "nodes": [1, 2, 3], "tags": {"barrier": "kerb"}},
        {"type": "way", "id": 11, "nodes": [4, 1], "tags": {"highway": "primary"}}]}
    roads = {"elements": [{"type": "node", "id": 1, "lat": LAT, "lon": LON, "tags": {}},
                          {"type": "node", "id": 2, "lat": LAT + 0.001, "lon": LON, "tags": {}},
                          {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"highway": "residential"}}]}
    out = {(e["type"], e["id"]): e for e in replace_roads(raw, roads)["elements"]}
    assert ("node", 2) in out and out[("node", 2)]["tags"] == {"crossing": "zebra"}
    assert ("node", 4) not in out and ("way", 11) not in out   # used by the replaced road only
    osm = parse_osm({"elements": list(out.values())})
    kerb = next(w for w in osm.ways if w.id == 10)
    assert len(osm.way_coords(kerb)) == 3


def test_deleted_elements_of_a_josm_file_are_skipped(tmp_path):
    p = tmp_path / "roads.osm"
    p.write_text(f"""<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='JOSM'>
  <node id='1' lat='{LAT}' lon='{LON}' />
  <node id='2' lat='{LAT + 0.001}' lon='{LON}' />
  <node id='-3' action='delete' lat='{LAT + 0.002}' lon='{LON}' />
  <node id='4' visible='false' lat='{LAT + 0.003}' lon='{LON}' />
  <way id='5'><nd ref='1' /><nd ref='2' /><tag k='highway' v='residential' /></way>
  <way id='6' action='delete'><nd ref='2' /><nd ref='-3' /><tag k='highway' v='residential' /></way>
</osm>""")
    els = read_osm_xml(p)["elements"]
    assert sorted((e["type"], e["id"]) for e in els) == [("node", 1), ("node", 2), ("way", 5)]
    # the bbox is the live network's, not stretched to the deleted node
    assert osm_xml_bbox(read_osm_xml(p))[2] < LAT + 0.002


def test_a_file_without_highways_is_refused(tmp_path):
    """A raw Lanelet2 map: lane borders, no highway=* way. Refused before any download."""
    p = tmp_path / "lanelet2.osm"
    p.write_text(f"""<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6'>
  <node id='1' lat='{LAT}' lon='{LON}' />
  <node id='2' lat='{LAT + 0.001}' lon='{LON}' />
  <way id='3'><nd ref='1' /><nd ref='2' /><tag k='type' v='line_thin' /><tag k='subtype' v='solid' /></way>
</osm>""")
    assert not has_highways(read_osm_xml(p))
    assert cli.main(["build", "--roads-osm", str(p), "--out", str(tmp_path / "out"),
                     "--cache", str(tmp_path / "cache")]) == 2
