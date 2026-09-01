"""Grade separation when the deck has no abutment inside the bbox.

``tests/test_motorway.py`` covers the well-behaved case: a bridge whose two approaches are in
the data, so ``cli.apply_bridge_profiles`` interpolates between two real abutments. Here the
deck is *clipped* — SF SoMa's I-80 is elevated right across the tile, and its ramps leave it on
every side — so there is no abutment anywhere and the DTM under the viaduct is bare earth, i.e.
the street the deck flies over. The straight profile then laid the deck on that street
(``grade_separation`` min z gap -0.09 m).

The fixtures are built directly as OSM ways in local metres, with a DEM that is flat at 0
(a DTM with the structure removed) except where a fixture wants an embankment.
"""
from __future__ import annotations

import numpy as np
import pytest

from twinmodel import profiles
from twinmodel.cli import apply_elevation, deck_road_ids
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import OsmData, OsmNode, OsmWay
from twinmodel.lanegraph import build_lanegraph
from twinmodel.model import Elevation, TwinModel, road_is_bridge, road_osm_layer
from twinmodel.validate import MIN_CLEARANCE_M

ORIGIN = (37.7815, -122.4040)                      # SF SoMa, the fixture this mirrors
BBOX = (37.7790, -122.4080, 37.7840, -122.4000)    # ~550 m x 700 m
STREET = {"highway": "secondary", "lanes": "2", "name": "Cross Street"}
DECK = {"highway": "motorway", "oneway": "yes", "lanes": "3", "name": "Elevated Freeway"}


def _flat_dem(z: float = 0.0) -> Elevation:
    step = 2.0
    xs = np.arange(-600.0, 600.0 + step, step)
    ys = np.arange(-600.0, 600.0 + step, step)
    return Elevation(np.full((len(ys), len(xs)), z), float(xs[0]), float(ys[0]), step, step,
                     source="synthetic")


def _ridge_dem(half: float, top: float) -> Elevation:
    """Flat 0 where the deck spans (|y| <= ``half``, i.e. the crossing street and the trench a
    DTM shows under the structure), climbing to ``top`` over 10 m beyond it — the approach
    embankments, which is all a DTM keeps of a bridge."""
    step = 2.0
    xs = np.arange(-600.0, 600.0 + step, step)
    ys = np.arange(-600.0, 600.0 + step, step)
    _, Y = np.meshgrid(xs, ys)
    z = top * np.clip((np.abs(Y) - half) / 10.0, 0.0, 1.0)
    return Elevation(z, float(xs[0]), float(ys[0]), step, step, source="synthetic")


def _osm(frame: LocalFrame, deck_tags: dict, deck_span: tuple[float, float],
         approaches: bool) -> OsmData:
    """A two-way street along x at y = 0, and a one-way deck along y crossing it at x = 0.

    ``deck_span``: the y range the *deck* way covers. With ``approaches`` the same alignment
    continues beyond it as ordinary layer-0 ways (a real bridge); without them the deck simply
    stops — the bbox cut it, and nothing in the data says how high it is.
    """
    data = OsmData()
    data.bbox_swne = BBOX
    nid = [0]

    def node(x: float, y: float) -> int:
        nid[0] += 1
        lon, lat = frame.to_wgs84(x, y)
        data.nodes[nid[0]] = OsmNode(nid[0], float(lat), float(lon))
        return nid[0]

    data.ways.append(OsmWay(1, [node(-260.0, 0.0), node(-80.0, 0.0), node(0.0, 0.0),
                                node(80.0, 0.0), node(260.0, 0.0)], dict(STREET)))
    lo, hi = deck_span
    n_lo, n_hi = node(0.0, lo), node(0.0, hi)
    data.ways.append(OsmWay(2, [n_lo, node(0.0, lo / 2), node(0.0, 0.0),
                                node(0.0, hi / 2), n_hi], dict(deck_tags)))
    if approaches:
        data.ways.append(OsmWay(3, [node(0.0, lo - 160.0), node(0.0, lo - 60.0), n_lo],
                                dict(DECK)))
        data.ways.append(OsmWay(4, [n_hi, node(0.0, hi + 60.0), node(0.0, hi + 160.0)],
                                dict(DECK)))
    return data


def _build(frame: LocalFrame, data: OsmData, dem: Elevation) -> TwinModel:
    with profiles.use("us_urban"):
        m = build_lanegraph(data, frame, BBOX, name="clipped_deck")
        m.elevation = dem
        m.metadata["elevation"] = apply_elevation(m)
    return m


def _min_crossing_gap(model: TwinModel, radius: float = 4.0) -> float:
    """Smallest z difference between an upper-layer reference-line sample and a lower-layer one
    within ``radius`` in xy — the same quantity ``validate.grade_separation`` measures."""
    from scipy.spatial import cKDTree
    hi, lo = [], []
    for r in model.roads:
        if r.junction_id is not None:
            continue
        c = np.asarray(r.reference_line.segmentize(2.0).coords, dtype=np.float64)
        (hi if road_osm_layer(r) > 0 else lo).append(c)
    assert hi and lo, "fixture must have both layers"
    hi, lo = np.concatenate(hi), np.concatenate(lo)
    d, k = cKDTree(lo[:, :2]).query(hi[:, :2], k=1)
    near = d <= radius
    assert near.any(), "the deck and the street must cross"
    return float(np.min(hi[near][:, 2] - lo[k[near]][:, 2]))


@pytest.fixture(scope="module")
def frame() -> LocalFrame:
    return LocalFrame(*ORIGIN)


# --------------------------------------------------------------- the clipped deck is lifted

def test_clipped_deck_chain_is_lifted_to_the_clearance_datum(frame):
    """No abutment in the data + a bare-earth DTM: the deck must be raised anyway."""
    m = _build(frame, _osm(frame, {**DECK, "bridge": "yes", "layer": "1"}, (-240.0, 240.0),
                           approaches=False), _flat_dem())
    decks = [r for r in m.roads if r.junction_id is None and road_is_bridge(r)]
    assert decks, "no deck road built"
    assert all(r.predecessor is None or r.predecessor.element != "road"
               or road_is_bridge(m.road(r.predecessor.id)) for r in decks), \
        "the fixture must leave the chain without an approach road"
    with profiles.use("us_urban"):
        need = profiles.get().elevation.min_clearance_m
    assert m.metadata["elevation"]["deck_chains_lifted"] == 1
    gap = _min_crossing_gap(m)
    assert gap >= need - 0.05, f"deck cleared the street by only {gap:.2f} m"
    assert gap >= MIN_CLEARANCE_M


def test_a_viaduct_tagged_only_with_layer_is_a_deck_too(frame):
    """``layer=1`` without ``bridge``: still a structure, so still not the DEM under it."""
    m = _build(frame, _osm(frame, {**DECK, "layer": "1"}, (-240.0, 240.0), approaches=False),
               _flat_dem())
    assert deck_road_ids(m), "a layer=1 road must count as a deck"
    assert _min_crossing_gap(m) >= MIN_CLEARANCE_M


def test_the_street_below_a_clipped_deck_stays_on_the_ground(frame):
    m = _build(frame, _osm(frame, {**DECK, "bridge": "yes", "layer": "1"}, (-240.0, 240.0),
                           approaches=False), _flat_dem())
    ground = [r for r in m.roads
              if r.junction_id is None and road_osm_layer(r) == 0 and r.highway == "secondary"]
    assert ground
    z = np.concatenate([np.asarray(r.reference_line.coords)[:, 2] for r in ground])
    assert np.abs(z).max() < 0.3, "the DEM is flat at 0: the street must not follow the deck up"


# ------------------------------------------------------- a real bridge is left where it is

def test_a_deck_with_two_abutments_is_not_lifted(frame):
    """Both ends anchored to an approach: the abutments decide, no clearance lift."""
    m = _build(frame, _osm(frame, {**DECK, "bridge": "yes", "layer": "1"}, (-60.0, 60.0),
                           approaches=True), _ridge_dem(60.0, 7.0))
    assert m.metadata["elevation"]["deck_chains_lifted"] == 0
    decks = [r for r in m.roads if r.junction_id is None and road_is_bridge(r)]
    z = np.concatenate([np.asarray(r.reference_line.coords)[:, 2] for r in decks])
    assert 6.0 <= z.min() <= 8.0, f"deck should sit on the 7 m embankment, got {z.min():.2f} m"


def test_the_abutment_step_is_welded(frame):
    """Deck contact z and approach contact z must agree — the ledge vehicles used to scrape."""
    m = _build(frame, _osm(frame, {**DECK, "bridge": "yes", "layer": "1"}, (-60.0, 60.0),
                           approaches=True), _ridge_dem(60.0, 7.0))
    roads = {r.id: r for r in m.roads}
    decks = deck_road_ids(m)
    steps = []
    for d in m.roads:
        if d.id not in decks:
            continue
        for end, link in (("start", d.predecessor), ("end", d.successor)):
            if link is None or link.element != "road" or link.id in decks:
                continue
            a = roads[link.id]
            zd = np.asarray(d.reference_line.coords)[0 if end == "start" else -1, 2]
            za = np.asarray(a.reference_line.coords)[0 if link.contact == "start" else -1, 2]
            steps.append(abs(float(zd - za)))
    assert steps, "the fixture must have at least one deck/approach joint"
    assert max(steps) <= 0.05, f"abutment step of {max(steps):.2f} m left in the surface"
