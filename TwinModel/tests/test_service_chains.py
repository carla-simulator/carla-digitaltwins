"""Chains of ``highway=service`` ways (frontage roads, lot access, driveways) must not fuse into
the street junction they hang off — synthetic OSM, no network.

Sunnyvale's W Olive Ave x S Taaffe St: five lot entrances along Olive, a 335 m parking loop with
both ends on Olive and two access spurs off the loop. Clustered at ``junction.cluster_m`` (60 m)
each node pulled in the next and the "junction" was 7100 m2 with 160 m connecting roads. The
fixture here is that shape::

    alley        Cross drive       (Main Street, tertiary, y = 0)
     |             |  /   30        70        110       150
  ===+=============+=+===+=========+=========+=========+======
     |             |     |                             |          service loop (y = -15)
                         +---------+---------+---------+
                                   |         |                    lot access spurs (y = -80)

North Drive leaves Main Street 8 m from the Cross Street node — inside ``service_cluster_m`` —
and is an extra arm of that junction. Rear Alley (40 m west), the loop ends and the spur nodes
are service nodes: each is its own T-junction.
"""
from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.lanegraph import _cluster_service_nodes, _UnionFind, build_lanegraph
from twinmodel.surfaces import build_surfaces

from .test_junction_slivers import _Builder, _slivers, _undocumented_dead_ends, _unlinked_arms

ORIGIN = (37.4000, -122.0000)
_frame = LocalFrame(*ORIGIN)


def _wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = _frame.to_wgs84(x, y)
    return float(lat), float(lon)


_s, _w = _wgs(-260.0, -260.0)
_n, _e = _wgs(260.0, 260.0)
BBOX = (_s, _w, _n, _e)

SERVICE = {"highway": "service"}

CROSS, DRIVE, ALLEY = (0.0, 0.0), (8.0, 0.0), (-40.0, 0.0)
LOOP_W, LOOP_E = (30.0, 0.0), (150.0, 0.0)
SPUR_A, SPUR_B = (70.0, -15.0), (110.0, -15.0)


def _fixture():
    b = _Builder()
    b.way(100, [(-200, 0), ALLEY, CROSS, DRIVE, LOOP_W, (90, 0), LOOP_E, (200, 0)],
          {"highway": "tertiary", "name": "Main Street"})
    b.way(101, [(0, 200), (0, 60), CROSS, (0, -100)],
          {"highway": "residential", "name": "Cross Street"})
    b.way(102, [ALLEY, (-40, -60)], {**SERVICE, "name": "Rear Alley"})
    b.way(103, [DRIVE, (20, 40), (20, 120)], {**SERVICE, "name": "North Drive"})
    # the lot's access loop: both ends on Main Street, 150 m of unnamed service way
    b.way(200, [LOOP_W, (30, -15), SPUR_A, SPUR_B, (150, -15), LOOP_E], SERVICE)
    b.way(201, [SPUR_A, (70, -80)], SERVICE)                            # lot access spurs
    b.way(202, [SPUR_B, (110, -80)], SERVICE)
    nodes = {name: b.node(*xy) for name, xy in
             (("cross", CROSS), ("drive", DRIVE), ("alley", ALLEY), ("loop_w", LOOP_W),
              ("loop_e", LOOP_E), ("spur_a", SPUR_A), ("spur_b", SPUR_B))}
    return b.osm(), nodes


def _build(profile):
    osm, nodes = _fixture()
    with profiles.use(profile):
        return build_surfaces(build_lanegraph(osm, _frame, BBOX, name="frontage")), nodes


@pytest.fixture(scope="module")
def suburban():
    return _build("us_suburban")


@pytest.fixture(scope="module")
def crawl():
    """The same fixture with the service rule switched off: the 2026-09-01 clustering."""
    p = profiles.by_name("us_suburban")
    return _build(p.with_(junction=replace(p.junction, service_cluster_m=0.0)))


def _junction_of(model, nid: int):
    return next(j for j in model.junctions if nid in j.osm_node_ids)


def test_fixture_reproduces_the_fusion(crawl):
    """Without the rule the alley and every node of the loop are pulled into the Cross Street
    junction, one node at a time."""
    model, n = crawl
    j = _junction_of(model, n["cross"])
    assert {n["alley"], n["loop_w"], n["spur_a"], n["spur_b"], n["loop_e"]} <= set(j.osm_node_ids)


def test_street_junction_holds_street_nodes_only(suburban):
    """Cross Street and the drive 8 m from it are one junction; nothing else joins it."""
    model, n = suburban
    j = _junction_of(model, n["cross"])
    assert set(j.osm_node_ids) == {n["cross"], n["drive"]}
    assert j.polygon is not None and j.polygon.area < 900.0
    assert max(j.polygon.area for j in model.junctions) < 900.0


def test_service_joins_are_their_own_junctions(suburban):
    model, n = suburban
    for key in ("alley", "loop_w", "loop_e", "spur_a", "spur_b"):
        j = _junction_of(model, n[key])
        assert j.osm_node_ids == [n[key]], (key, j.id, j.osm_node_ids)
        assert j.connections, (key, j.id)


def test_no_long_connecting_roads(suburban):
    model, _n = suburban
    longest = max(r.length for r in model.roads if r.junction_id is not None)
    assert longest < 40.0, longest


def test_service_junctions_are_linked_and_clean(suburban):
    model, _n = suburban
    assert _slivers(model) == []
    assert _unlinked_arms(model) == []
    assert _undocumented_dead_ends(model) == []


# --------------------------------------------------------------------------- the rule itself

def _chain(a: int, b: int, xy_a, xy_b, highway: str):
    seg = SimpleNamespace(way=SimpleNamespace(tags={"highway": highway}))
    return SimpleNamespace(nodes=[a, b], xy=[xy_a, xy_b], segments=[seg])


def test_service_nodes_never_bridge_and_never_span_more_than_the_radius():
    """A (street) - s1 - s2 - s3 - B (street), 10 m apart along a service road: s1 joins A and
    s3 joins B (direct links inside the radius), s2 fuses with one neighbour but the service
    nodes of a junction never span more than the radius, and A and B stay apart."""
    xy = {1: (0.0, 0.0), 2: (10.0, 0.0), 3: (20.0, 0.0), 4: (30.0, 0.0), 5: (40.0, 0.0)}
    chains = [_chain(1, 2, xy[1], xy[2], "residential"), _chain(2, 3, xy[2], xy[3], "service"),
              _chain(3, 4, xy[3], xy[4], "service"), _chain(4, 5, xy[4], xy[5], "residential")]
    uf = _UnionFind()
    for nid in xy:
        uf.find(nid)
    with profiles.use("us_suburban"):
        n_suppressed = _cluster_service_nodes(uf, chains, set(xy), {2, 3, 4}, set(), xy,
                                              lambda ch, a, b: 60.0, radius=12.0)
    assert n_suppressed == 0
    assert uf.find(1) == uf.find(2) and uf.find(5) == uf.find(4)
    assert uf.find(1) != uf.find(5)
    groups: dict[int, list[int]] = {}
    for nid in xy:
        groups.setdefault(uf.find(nid), []).append(nid)
    for members in groups.values():
        svc = [n for n in members if n in (2, 3, 4)]
        assert all(math.dist(xy[p], xy[q]) <= 12.0 for p in svc for q in svc)


def test_street_nodes_fuse_through_a_service_node_on_the_street():
    """A - s - B along the street, 20 m end to end: the driveway node between two street nodes
    that cluster does not split the junction in two (a lot entrance inside a median box)."""
    xy = {1: (0.0, 0.0), 2: (8.0, 0.0), 3: (20.0, 0.0), 4: (8.0, -30.0)}
    chains = [_chain(1, 2, xy[1], xy[2], "primary"), _chain(2, 3, xy[2], xy[3], "primary"),
              _chain(2, 4, xy[2], xy[4], "service")]
    uf = _UnionFind()
    for nid in (1, 2, 3):
        uf.find(nid)
    with profiles.use("us_suburban"):
        _cluster_service_nodes(uf, chains, {1, 2, 3}, {2}, set(), xy,
                               lambda ch, a, b: 25.0, radius=12.0)
    assert uf.find(1) == uf.find(3) == uf.find(2)


def test_street_service_link_outside_the_radius_is_counted():
    xy = {1: (0.0, 0.0), 2: (30.0, 0.0)}
    chains = [_chain(1, 2, xy[1], xy[2], "residential")]
    uf = _UnionFind()
    uf.find(1), uf.find(2)
    with profiles.use("us_suburban"):
        n = _cluster_service_nodes(uf, chains, {1, 2}, {2}, set(), xy,
                                   lambda ch, a, b: 60.0, radius=12.0)
    assert n == 1 and uf.find(1) != uf.find(2)


def test_profiles_carry_the_service_radius():
    assert profiles.by_name("us_urban").junction.service_cluster_m == pytest.approx(40 * profiles.FT)
    assert profiles.by_name("us_suburban").junction.service_cluster_m == pytest.approx(40 * profiles.FT)
    assert profiles.by_name("eu_dense").junction.service_cluster_m == 0.0  # pinned 2026-09-01 graph
