"""``DataSources.area_highway``: OSM ``area:highway`` polygons as the road surface."""
from __future__ import annotations

from dataclasses import replace

from shapely.geometry import LineString, shape
from shapely.ops import unary_union

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph
from twinmodel.surfaces import build_surfaces

LAT, LON = 35.9, 139.93
D = 0.0018
BBOX = (LAT - D, LON - D, LAT + D, LON + D)
FRAME = LocalFrame.from_bbox(*BBOX)
ON = profiles.EU_DENSE.with_(name="areas",
                             sources=replace(profiles.EU_DENSE.sources, area_highway=True))
# the kerb of the map: 4 m either side of both centre lines, corners rounded with a 6 m radius
MAJOR = LineString([(-150, 0), (150, 0)])
MINOR = LineString([(0, -150), (0, 150)])


def _kerb(lines):
    return unary_union([ln.buffer(10.0, cap_style="flat") for ln in lines]).buffer(-6.0).buffer(0.0)


def _osm(area_lines):
    def node(nid, x, y, tags=None):
        lon, lat = FRAME.to_wgs84(x, y)
        e = {"type": "node", "id": nid, "lat": lat, "lon": lon}
        if tags:
            e["tags"] = tags
        return e
    els = [node(1, -150, 0), node(2, 0, 0), node(3, 150, 0), node(4, 0, -150), node(5, 0, 150)]
    els += [{"type": "way", "id": 100, "nodes": [1, 2, 3],
             "tags": {"highway": "secondary", "lanes": "2", "name": "Major", "width": "8"}},
            {"type": "way", "id": 200, "nodes": [4, 2, 5],
             "tags": {"highway": "secondary", "lanes": "2", "name": "Minor", "width": "8"}}]
    if area_lines:
        ring = list(_kerb(area_lines).exterior.coords)
        ids = list(range(1000, 1000 + len(ring) - 1))
        els += [node(i, x, y) for i, (x, y) in zip(ids, ring[:-1])]
        els.append({"type": "way", "id": 900, "nodes": ids + [ids[0]],
                    "tags": {"area:highway": "secondary"}})
    return parse_osm({"elements": els})


def _build(profile, area_lines):
    with profiles.use(profile):
        m = build_lanegraph(_osm(area_lines), FRAME, BBOX, name="x")
        return build_surfaces(m)


def _union(m, kind):
    return unary_union([s.geometry for s in m.surfaces if s.kind == kind])


def test_the_area_polygons_are_the_road_surface():
    m = _build(ON, [MAJOR, MINOR])
    assert m.metadata["surfaces"]["drivable_source"] == "area_highway"
    # within the polygons' extent (they stop 6 m short of the road ends, where the lane
    # graph's surface stays)
    window = shape({"type": "Polygon", "coordinates": [[(-140, -140), (140, -140), (140, 140), (-140, 140), (-140, -140)]]})
    drivable = _union(m, "drivable").intersection(window)
    kerb = _kerb([MAJOR, MINOR]).intersection(window)
    iou = drivable.intersection(kerb).area / drivable.union(kerb).area
    assert iou > 0.97, iou
    # the sidewalks meet the new kerb, corners included, without paving over it
    sidewalk = _union(m, "sidewalk")
    assert sidewalk.intersection(_union(m, "drivable")).area < 1.0
    corner = shape({"type": "Point", "coordinates": (6.5, 6.5)})  # on the rounded corner's kerb
    assert sidewalk.distance(corner) < 1.0


def test_without_the_option_the_polygons_are_ignored():
    m = _build(profiles.EU_DENSE, [MAJOR, MINOR])
    assert m.metadata["surfaces"]["drivable_source"] != "area_highway"
    assert "road_areas_wkt" not in m.metadata


def test_a_road_without_a_polygon_keeps_the_lane_graph_surface():
    m = _build(ON, [MAJOR])
    drivable = _union(m, "drivable")
    # the minor road far from the junction is still paved
    for y in (-100.0, 100.0):
        assert drivable.contains(shape({"type": "Point", "coordinates": (0.0, y)}))


def test_a_polygon_narrower_than_the_street_leaves_no_gap():
    """A polygon that maps the major street 2 m short of its north kerb replaces it; the rest
    of the street stays paved, with no strip missing between the two."""
    def narrow(lines):
        return unary_union([ln.buffer(10.0, cap_style="flat") for ln in lines]).buffer(-6.0)

    import sys
    t = sys.modules[__name__]
    real = t._kerb
    try:
        t._kerb = lambda lines: narrow(lines).difference(
            shape({"type": "Polygon", "coordinates": [[(-150, 2), (150, 2), (150, 9), (-150, 9), (-150, 2)]]})
        ).union(narrow([MINOR])).buffer(0)
        m = _build(ON, [MAJOR, MINOR])
    finally:
        t._kerb = real
    drivable = _union(m, "drivable")
    for x in (-80.0, 80.0):
        for y in (-3.0, 0.0, 1.9, 2.5, 3.0):
            assert drivable.contains(shape({"type": "Point", "coordinates": (x, y)})), (x, y)
