"""Junction slivers, unlinked arms and lanes that dead-end in the middle of the map.

Every lane in isolation looked right; combined, a parking-lot entrance a few metres from a
street junction (or from the node of a divided arterial) produced

  * a road between two junctions too short to hold a lane link (``validate.junction_slivers``),
  * a junction arm whose driving lanes are the incoming lane of no connection
    (``validate.junction_lane_links``), and
  * a driving lane that stops with no ``next()`` inside the map (``validate.terminal_lanes``).

The two fixtures here are the shapes that produced them, with no network and no imagery:

  fixture A                                   fixture B
  ============ Elm Street ==========          ‖  ‖  divided arterial (two one-way carriageways)
        |          |                          ‖  ‖
        | Oak St   | lot entrance 3 m         =====  cross street at y = 0
        |          | east of the junction     ‖  ‖
                   +---------+                ‖  ‖--- lot entrance 3 m north of the carriageway
                   | lot aisles |                     node, plus a 4 m aisle connector
"""
from __future__ import annotations

import pytest

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import parse_osm
from twinmodel.lanegraph import build_lanegraph
from twinmodel.validate import SLIVER_M, _junction_arm_lanes

ORIGIN = (37.4000, -122.0000)
_frame = LocalFrame(*ORIGIN)


def _wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = _frame.to_wgs84(x, y)
    return float(lat), float(lon)


_s, _w = _wgs(-260.0, -260.0)
_n, _e = _wgs(260.0, 260.0)
BBOX = (_s, _w, _n, _e)


class _Builder:
    def __init__(self) -> None:
        self.elements: list[dict] = []
        self._nid = 1
        self._at: dict[tuple[float, float], int] = {}

    def node(self, x: float, y: float) -> int:
        key = (round(x, 3), round(y, 3))
        if key in self._at:
            return self._at[key]
        lat, lon = _wgs(x, y)
        i = self._nid
        self._nid += 1
        self.elements.append({"type": "node", "id": i, "lat": lat, "lon": lon})
        self._at[key] = i
        return i

    def way(self, wid: int, pts, tags: dict) -> None:
        self.elements.append({"type": "way", "id": wid,
                              "nodes": [self.node(x, y) for x, y in pts], "tags": tags})

    def osm(self):
        return parse_osm({"elements": self.elements})


AISLE = {"highway": "service", "service": "parking_aisle"}


def _fixture_a():
    """A parking lot off Elm Street:

      * its entrance joins Elm 3 m east of the Oak Street crossing,
      * a 4 m aisle connector (shorter than ``ParkingAisleRules.min_length``) is the lot's only
        way on from the entrance — a one-way aisle arrives at that node, so dropping the
        connector leaves the entrance with no legal departure, and
      * two aisle T-nodes 16 m apart inside the lot leave a ~5 m road between their junctions
        once both are trimmed: shorter than ``validate.SLIVER_M``, longer than the US profiles
        used to merge at.
    """
    b = _Builder()
    b.way(100, [(-200, 0), (-60, 0), (0, 0), (3, 0), (60, 0), (200, 0)],
          {"highway": "residential", "name": "Elm Street"})
    b.way(101, [(0, 200), (0, 60), (0, 0), (0, -25)],
          {"highway": "residential", "name": "Oak Street"})
    b.way(200, [(3, 0), (3, -30), (3, -60)], AISLE)                    # entrance, two-way
    b.way(201, [(-45, -140), (-45, -60), (3, -60)],                    # arrives at the entrance
          {**AISLE, "oneway": "yes"})
    b.way(202, [(3, -60), (3, -64)], AISLE)                            # 4 m connector
    b.way(203, [(3, -64), (55, -64), (55, -80), (55, -140), (-45, -140)], AISLE)
    b.way(204, [(55, -64), (95, -64)], AISLE)                          # T one
    b.way(205, [(55, -80), (95, -80)], AISLE)                          # T two, 16 m away
    b.way(300, [(-70, -20), (110, -20), (110, -160), (-70, -160), (-70, -20)],
          {"amenity": "parking", "parking": "surface"})
    return b.osm()


def _fixture_b():
    """A lot entrance on a divided arterial, 16 m from the node where the crossing street meets
    the near carriageway: an aisle-only ("minor") node right beside a dual-carriageway node."""
    b = _Builder()
    # northbound carriageway at x = +6, southbound at x = -6 (12 m median)
    b.way(100, [(6, -200), (6, 0), (6, 16), (6, 200)],
          {"highway": "primary", "name": "Grand Avenue", "oneway": "yes", "lanes": "2"})
    b.way(101, [(-6, 200), (-6, 0), (-6, -200)],
          {"highway": "primary", "name": "Grand Avenue", "oneway": "yes", "lanes": "2"})
    b.way(102, [(-200, 0), (-6, 0), (6, 0), (200, 0)],
          {"highway": "residential", "name": "Cross Street"})
    b.way(200, [(6, 16), (60, 16), (60, -60)], AISLE)                  # entrance
    b.way(201, [(60, -60), (150, -60)], AISLE)
    b.way(300, [(40, -10), (170, -10), (170, -80), (40, -80), (40, -10)],
          {"amenity": "parking", "parking": "surface"})
    return b.osm()


def _build(osm, profile: str):
    with profiles.use(profile):
        return build_lanegraph(osm, _frame, BBOX, name="slivers")


@pytest.fixture(scope="module")
def lot_entrance():
    return _build(_fixture_a(), "us_suburban")


@pytest.fixture(scope="module")
def divided():
    return _build(_fixture_b(), "us_suburban")


# --------------------------------------------------------------------------- the invariants

def _slivers(model):
    return [r for r in model.roads
            if r.junction_id is None and r.length < SLIVER_M
            and r.predecessor is not None and r.predecessor.element == "junction"
            and r.successor is not None and r.successor.element == "junction"]


def _unlinked_arms(model):
    out = []
    for j in model.junctions:
        by_road: dict[str, set[int]] = {}
        for conn in j.connections:
            by_road.setdefault(conn.incoming_road, set()).update(
                ll.from_lane for ll in conn.lane_links)
        for rid, lanes in _junction_arm_lanes(model, j).items():
            missing = sorted(lanes - by_road.get(rid, set()))
            if missing:
                out.append((j.id, rid, missing))
    return out


def _undocumented_dead_ends(model):
    """Road ends with driving lanes arriving and no junction / road link, that the lane graph
    did not mark as a dead end (a cul-de-sac, a one-way funnel, ...)."""
    out = []
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for end, link in (("start", r.predecessor), ("end", r.successor)):
            arriving = [l.id for l in r.lanes
                        if l.type == "driving" and (l.id < 0) == (end == "end")]
            if link is None and arriving and not r.tags.get(f"dead_end_{end}"):
                out.append((r.id, end, arriving))
    return out


@pytest.mark.parametrize("name", ["lot_entrance", "divided"])
def test_no_sliver_between_two_junctions(name, request):
    model = request.getfixturevalue(name)
    assert _slivers(model) == [], [(r.id, round(r.length, 2)) for r in _slivers(model)]


@pytest.mark.parametrize("name", ["lot_entrance", "divided"])
def test_every_arriving_lane_of_every_arm_is_linked(name, request):
    model = request.getfixturevalue(name)
    assert _unlinked_arms(model) == []


@pytest.mark.parametrize("name", ["lot_entrance", "divided"])
def test_no_undocumented_dead_end(name, request):
    model = request.getfixturevalue(name)
    assert _undocumented_dead_ends(model) == []


def test_short_aisle_connector_survives(lot_entrance):
    """The 4 m connector (way 202) is shorter than ParkingAisleRules.min_length but joins two
    nodes other aisles use: dropping it severs the lot's circulation."""
    kept = [r for r in lot_entrance.roads if 202 in r.osm_way_ids]
    absorbed = [j for j in lot_entrance.junctions if 202 in j.osm_way_ids]
    assert kept or absorbed, "the connector was dropped instead of kept or clustered"


def test_lot_entrance_and_street_junction_are_one_junction(lot_entrance):
    """3 m apart, the entrance node and the Oak Street crossing cannot be told apart: one
    junction, not two with a 3 m road between them."""
    assert not _slivers(lot_entrance)
    entrance_j = [j for j in lot_entrance.junctions if len(j.connections) > 0]
    assert entrance_j, "no junction with connections was built"


def test_eu_dense_sliver_merging_is_off():
    """EU_DENSE pins the 2026-09-01 behaviour: sliver_m == 0 switches the merge off entirely."""
    assert profiles.by_name("eu_dense").junction.sliver_m == 0.0
    for name in ("us_urban", "us_suburban"):
        assert profiles.by_name(name).junction.sliver_m >= SLIVER_M, (
            "a profile that merges below validate.SLIVER_M leaves junction_slivers failures")
