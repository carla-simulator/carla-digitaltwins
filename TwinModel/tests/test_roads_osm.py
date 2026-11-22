"""``twinmodel build --roads-osm``: the bbox comes from the file and keeps all of its roads."""
from __future__ import annotations

from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import osm_xml_bbox, parse_osm
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
