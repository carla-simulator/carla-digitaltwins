"""Divided (dual) carriageway junction model — synthetic OSM, no network.

The fixture is the shape the model exists for: an arterial mapped as two ``oneway=yes`` ways
with the same name and a 14 m median, a side street that crosses the median at one place, and
two more side streets that hit the two carriageways at *offset* nodes 40 m / 75 m up the road.

Without the model the generic 40-60 m node clustering hops along a carriageway and fuses the
crossing with the offset side street into one blob; with it the crossing is one junction (the
median box) and the offset side streets are junctions of their own.
"""
from __future__ import annotations

import math

import pytest

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph
from twinmodel.surfaces import build_surfaces

MEDIAN_GAP = 14.0          # between the two carriageway centrelines
BBOX = (37.3700, -122.0400, 37.3760, -122.0320)   # ~660 x 890 m around the origin
FRAME = LocalFrame.from_bbox(*BBOX)

#            name                     nodes (x, y) in local metres
ARTERIAL_E = [(-260.0, -MEDIAN_GAP / 2), (0.0, -MEDIAN_GAP / 2),
              (40.0, -MEDIAN_GAP / 2), (260.0, -MEDIAN_GAP / 2)]          # travels +x
ARTERIAL_W = [(260.0, MEDIAN_GAP / 2), (75.0, MEDIAN_GAP / 2),
              (0.0, MEDIAN_GAP / 2), (-260.0, MEDIAN_GAP / 2)]            # travels -x
CROSS = [(0.0, -160.0), (0.0, -MEDIAN_GAP / 2), (0.0, MEDIAN_GAP / 2), (0.0, 160.0)]
OFFSET_S = [(40.0, -MEDIAN_GAP / 2), (40.0, -160.0)]     # only meets the eastbound carriageway
OFFSET_N = [(75.0, MEDIAN_GAP / 2), (75.0, 160.0)]       # only meets the westbound carriageway


def _fixture(*, divided: bool = True) -> dict:
    """Overpass-shaped JSON. ``divided=False`` maps the arterial as one two-way way instead,
    so the detector must not fire."""
    nodes: dict[tuple[float, float], int] = {}
    elements: list[dict] = []

    def nid(p: tuple[float, float]) -> int:
        key = (round(p[0], 3), round(p[1], 3))
        if key not in nodes:
            lon, lat = FRAME.to_wgs84(key[0], key[1])
            nodes[key] = 1000 + len(nodes)
            elements.append({"type": "node", "id": nodes[key],
                             "lat": float(lat), "lon": float(lon)})
        return nodes[key]

    def way(wid: int, pts: list[tuple[float, float]], tags: dict) -> None:
        elements.append({"type": "way", "id": wid, "nodes": [nid(p) for p in pts], "tags": tags})

    art = {"highway": "primary", "name": "Test Boulevard", "lanes": "2", "oneway": "yes"}
    if divided:
        way(1, ARTERIAL_E, dict(art))
        way(2, ARTERIAL_W, dict(art))
    else:
        way(1, [(x, 0.0) for x, _ in ARTERIAL_E],
            {"highway": "primary", "name": "Test Boulevard", "lanes": "4"})
    way(3, CROSS if divided else [(0.0, -160.0), (0.0, 0.0), (0.0, 160.0)],
        {"highway": "residential", "name": "Cross Street"})
    way(4, OFFSET_S if divided else [(40.0, 0.0), (40.0, -160.0)],
        {"highway": "residential", "name": "South Street"})
    way(5, OFFSET_N if divided else [(75.0, 0.0), (75.0, 160.0)],
        {"highway": "residential", "name": "North Street"})
    return {"elements": elements}


def _build(profile: str, *, divided: bool = True, dual: bool = True):
    p = profiles.by_name(profile)
    if not dual:
        from dataclasses import replace
        p = p.with_(junction=replace(p.junction, dual_carriageway_max_gap_m=0.0,
                                     median_max_width_m=0.0, sliver_m=0.0))
    with profiles.use(p):
        return build_lanegraph(parse_osm(_fixture(divided=divided)), FRAME, BBOX, name="dual")


def _plain(model):
    return [r for r in model.roads if r.junction_id is None]


def _carriageways(model):
    return [r for r in _plain(model) if r.tags.get("dual_carriageway")]


def _max_connecting(model, junction_id: str | None = None) -> float:
    conn = [r for r in model.roads if r.junction_id is not None
            and (junction_id is None or r.junction_id == junction_id)]
    return max((r.reference_line.length for r in conn), default=0.0)


# --------------------------------------------------------------------------- detection

def test_detects_the_two_carriageways_of_the_arterial():
    m = _build("us_suburban")
    st = m.metadata["lanegraph"]
    assert st["dual_carriageway_pairs"] == 1
    assert st["dual_carriageways"] >= 4          # both carriageways, split at the side streets
    keys = {r.tags["dual_carriageway"] for r in _carriageways(m)}
    assert keys == {"test boulevard"}
    for r in _carriageways(m):
        assert r.tags["median_gap_m"] == pytest.approx(MEDIAN_GAP, abs=0.5)
    # nothing else on the map is a carriageway of a divided arterial
    assert all(not r.tags.get("dual_carriageway") for r in _plain(m)
               if r.name in ("Cross Street", "South Street", "North Street"))


def test_a_two_way_arterial_is_not_a_divided_one():
    m = _build("us_suburban", divided=False)
    assert m.metadata["lanegraph"]["dual_carriageways"] == 0
    assert not _carriageways(m)


def test_eu_dense_never_builds_the_divided_model():
    """EU_DENSE pins the Eixample regression: its switches are off, so the same fixture must
    come out exactly as it does with the model disabled."""
    on = _build("eu_dense")
    off = _build("eu_dense", dual=False)
    assert on.metadata["lanegraph"]["dual_carriageways"] == 0
    assert on.metadata["lanegraph"]["dual_carriageway_pairs"] == 0
    assert len(on.junctions) == len(off.junctions)
    assert [(r.id, len(r.lanes)) for r in on.roads] == [(r.id, len(r.lanes)) for r in off.roads]
    assert not any(l.type == "median" for r in on.roads for l in r.lanes)


# --------------------------------------------------------------------------- the median

def test_the_median_side_carries_a_median_lane_and_no_curb_furniture():
    m = _build("us_suburban")
    P = profiles.by_name("us_suburban")
    for r in _carriageways(m):
        median_side = [l for l in r.lanes if l.id > 0]        # left of travel, drives_on right
        assert [l.type for l in median_side] == ["median"], (r.id, [l.type for l in median_side])
        w = median_side[0].width
        assert 0.25 <= w <= P.junction.median_max_width_m + 1e-9
        # the two medians meet in the middle of the gap, so the strip is contiguous
        carriage = sum(l.width for l in r.lanes
                       if l.type in ("driving", "parking", "biking", "shoulder"))
        assert w == pytest.approx(MEDIAN_GAP / 2 - carriage / 2, abs=0.3)
        # the outer side keeps its sidewalk
        assert any(l.type == "sidewalk" for l in r.lanes if l.id < 0)


def test_the_median_becomes_a_median_surface():
    m = _build("us_suburban")
    build_surfaces(m)
    median = [s for s in m.surfaces if s.kind == "median"]
    assert median and sum(s.geometry.area for s in median) > 500.0
    drivable = [s for s in m.surfaces if s.kind == "drivable"]
    for s in median:                                    # a median is never drivable
        assert all(s.geometry.intersection(d.geometry).area < 1.0 for d in drivable)


# --------------------------------------------------------------------------- clustering

def test_the_crossing_is_one_junction_and_the_offset_side_streets_are_not_in_it():
    m = _build("us_suburban")
    assert len(m.junctions) == 3, [(j.id, j.tags["centre"]) for j in m.junctions]
    centres = sorted((round(j.tags["centre"][0]), round(j.tags["centre"][1])) for j in m.junctions)
    xs = [c[0] for c in centres]
    assert xs == [0, 40, 75], centres
    # the crossing junction holds both carriageway nodes; the offset ones hold a single node
    crossing = next(j for j in m.junctions if abs(j.tags["centre"][0]) < 5)
    assert len(crossing.osm_node_ids) == 2
    assert m.metadata["lanegraph"]["dual_merges_suppressed"] >= 1


def test_without_the_model_the_offset_side_street_is_swallowed():
    off = _build("us_suburban", dual=False)
    assert len(off.junctions) == 2                       # 40 m arm fused into the crossing
    blob = next(j for j in off.junctions if len(j.osm_node_ids) > 1)
    assert len(blob.osm_node_ids) == 3
    assert _max_connecting(off) > _max_connecting(_build("us_suburban"))


def test_connecting_roads_stay_within_the_crossed_street_width():
    m = _build("us_suburban")
    art = _carriageways(m)[0]
    carriage = sum(l.width for l in art.lanes
                   if l.type in ("driving", "parking", "biking", "shoulder"))
    crossed = MEDIAN_GAP + carriage        # both carriageways plus the median
    assert _max_connecting(m) <= 1.5 * crossed, (_max_connecting(m), crossed)
    assert not [r for r in m.roads if r.junction_id is None and r.length < 5.0
                and r.predecessor is not None and r.predecessor.element == "junction"
                and r.successor is not None and r.successor.element == "junction"]


# --------------------------------------------------------------------------- xodr / CARLA

def test_the_divided_arterial_round_trips_through_carla_map():
    carla = pytest.importorskip("carla")
    from twinmodel.export.xodr import export_xodr
    from twinmodel.validate import validate

    with profiles.use("us_suburban"):
        m = _build("us_suburban")
        build_surfaces(m)
        rep = validate(m, export_xodr(m), step=1.0)
    assert rep["topology"]["loaded"]
    assert rep["lane_in_drivable"]["fraction"] >= 0.98
    assert rep["junction_containment"]["fraction"] == pytest.approx(1.0, abs=0.02)
    assert rep["junction_slivers"]["count"] == 0
    assert rep["junction_lane_links"]["unlinked_arms"] == 0
    assert rep["lane_coverage"]["fraction"] == 1.0
    # every driving lane leads somewhere: the only lanes that stop are at the bbox edge
    assert rep["terminal_lanes"]["count"] == 0, rep["terminal_lanes"]["lanes"]
    # the median is a lane in the xodr, not a driving lane
    assert any(l.type == "median" for r in m.roads for l in r.lanes)
    assert math.isfinite(rep["z_error"]["p95"])
