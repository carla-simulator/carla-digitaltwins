"""Freeway classes, ramp gores and grade separation, on a synthetic interchange (no network).

The fixture is a piece of a diamond interchange, built as OSM ways in local metres:

                                  crossing street (secondary, two-way)
                                        |  bridge=yes layer=1 between y=-40 and y=+40
    mainline (motorway, oneway) ========+========================>   x
        w1 lanes=2   N1   w2 lanes=3    |    N2   w3 lanes=2
                      \\                 |    /
                  on-ramp (link)       (0,0)  off-ramp (link)

* ``N1`` is a merge gore (the on-ramp ends there, the mainline gains a lane),
  ``N2`` a diverge gore (the off-ramp starts there, the mainline drops a lane).
* The mainline and the deck share the node at (0, 0) — the way plenty of real OSM data is
  mapped — but their ``layer`` differs, so they must not meet there.
* The DEM is flat at 0 except a ridge along the crossing street that climbs to 6 m outside the
  deck: the deck has to interpolate between those abutments instead of dropping into the
  trench the DTM shows under it.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from twinmodel import profiles
from twinmodel.cli import apply_elevation
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import OsmData, OsmNode, OsmWay
from twinmodel.lanegraph import build_lanegraph
from twinmodel.model import Elevation, road_is_bridge, road_osm_layer
from twinmodel.surfaces import build_surfaces

ORIGIN = (37.3980, -122.0280)          # US-101 x Mathilda, the fixture this mirrors
BBOX = (37.3935, -122.0337, 37.4025, -122.0223)   # ~1 km x 1 km around it
FT = 0.3048

DECK_HALF = 40.0        # the bridge spans y in [-40, +40]
ABUTMENT_Z = 6.0        # the DEM ridge outside the deck
RIDGE_HALF_X = 25.0     # how wide the ridge is across the crossing street


def _dem(frame: LocalFrame) -> Elevation:
    """Flat ground at z = 0, with a ridge along the crossing street (|x| <= RIDGE_HALF_X) that
    climbs from 0 at |y| = DECK_HALF to ABUTMENT_Z over 8 m — a DTM with the deck removed."""
    step = 2.0
    xs = np.arange(-400.0, 400.0 + step, step)
    ys = np.arange(-400.0, 400.0 + step, step)
    X, Y = np.meshgrid(xs, ys)
    ramp = np.clip((np.abs(Y) - DECK_HALF) / 8.0, 0.0, 1.0)
    across = (np.abs(X) <= RIDGE_HALF_X).astype(float)
    return Elevation(ABUTMENT_Z * ramp * across, float(xs[0]), float(ys[0]), step, step,
                     source="synthetic")


@pytest.fixture(scope="module")
def frame() -> LocalFrame:
    return LocalFrame(*ORIGIN)


@pytest.fixture(scope="module")
def osm(frame: LocalFrame) -> OsmData:
    data = OsmData()
    data.bbox_swne = BBOX
    nid = [0]

    def node(x: float, y: float) -> int:
        nid[0] += 1
        lon, lat = frame.to_wgs84(x, y)
        data.nodes[nid[0]] = OsmNode(nid[0], float(lat), float(lon))
        return nid[0]

    n_w = node(-400.0, 0.0)
    n1 = node(-100.0, 0.0)          # merge gore
    n_mid = node(0.0, 0.0)          # 2D crossing with the deck (shared node, different layers)
    n2 = node(100.0, 0.0)           # diverge gore
    n_e = node(400.0, 0.0)
    mainline = {"highway": "motorway", "oneway": "yes", "name": "Test Freeway", "ref": "T 1"}
    data.ways.append(OsmWay(101, [n_w, n1], {**mainline, "lanes": "2"}))
    data.ways.append(OsmWay(102, [n1, n_mid, n2], {**mainline, "lanes": "3"}))
    data.ways.append(OsmWay(103, [n2, n_e], {**mainline, "lanes": "2"}))

    link = {"highway": "motorway_link", "oneway": "yes", "lanes": "1"}
    on_ramp = [node(-320.0, -70.0), node(-240.0, -60.0), node(-160.0, -25.0), n1]
    data.ways.append(OsmWay(104, on_ramp, dict(link)))
    off_ramp = [n2, node(160.0, -25.0), node(240.0, -60.0), node(320.0, -70.0)]
    data.ways.append(OsmWay(105, off_ramp, dict(link)))

    street = {"highway": "secondary", "name": "Cross Street", "lanes": "2"}
    south = [node(0.0, -300.0), node(0.0, -150.0), node(0.0, -DECK_HALF)]
    north = [node(0.0, DECK_HALF), node(0.0, 150.0), node(0.0, 300.0)]
    data.ways.append(OsmWay(106, south, dict(street)))
    data.ways.append(OsmWay(107, [south[-1], n_mid, north[0]],
                            {**street, "bridge": "yes", "layer": "1"}))
    data.ways.append(OsmWay(108, north, dict(street)))
    return data


@pytest.fixture(scope="module")
def model(osm: OsmData, frame: LocalFrame):
    with profiles.use("us_suburban"):
        m = build_lanegraph(osm, frame, BBOX, name="interchange")
        m.elevation = _dem(frame)
        m.metadata["elevation"] = apply_elevation(m)
        build_surfaces(m)
    return m


def _plain(model, highway: str) -> list:
    return [r for r in model.roads if r.junction_id is None and r.highway == highway]


def _driving(road) -> int:
    return sum(1 for l in road.lanes if l.type == "driving")


# --------------------------------------------------------------------------- cross sections

def test_motorway_class_defaults_are_a_freeway_cross_section():
    """No sidewalk / verge / parking on the freeway classes, in every profile; a paved outside
    shoulder and (oneway carriageway) a median-side one instead."""
    from twinmodel.lanegraph import lanes_for_way
    for name in ("eu_dense", "us_urban", "us_suburban"):
        with profiles.use(name):
            for hw in ("motorway", "motorway_link"):
                cls = profiles.get().lane.for_class(hw)
                assert cls.sidewalk is None and cls.verge is None and cls.parking == "none", (name, hw)
                assert cls.shoulder and cls.shoulder_inner, (name, hw)
                spec = lanes_for_way({"highway": hw, "oneway": "yes", "lanes": "3"}, hw)
                kinds = {l.type for l in spec.lanes}
                assert kinds == {"driving", "shoulder"}, (name, hw, kinds)
                # the outside shoulder is outboard of the outermost driving lane, the inner one
                # on the other side of the reference line (the left carriageway edge)
                right = [l.type for l in spec.lanes if l.id < 0]
                assert right[-1] == "shoulder" and right.count("shoulder") == 1, (name, hw, right)
                assert [l.type for l in spec.lanes if l.id > 0] == ["shoulder"], (name, hw)


def test_no_pedestrian_or_parking_lanes_on_the_freeway(model):
    banned = {"sidewalk", "verge", "parking", "biking"}
    for r in model.roads:
        if r.highway not in ("motorway", "motorway_link"):
            continue
        assert not banned & {l.type for l in r.lanes}, (r.id, [l.type for l in r.lanes])
    # ... and the freeway carries shoulders
    assert all(any(l.type == "shoulder" for l in r.lanes) for r in _plain(model, "motorway"))
    assert all(any(l.type == "shoulder" for l in r.lanes) for r in _plain(model, "motorway_link"))


def test_no_sidewalk_or_crossing_surface_touches_the_freeway(model):
    """The freeway gets no sidewalk band and no zebra. The deck's own sidewalks do cross the
    mainline corridor in 2D — they are 6 m above it, on layer 1."""
    assert [r for r in model.roads if r.highway in ("motorway", "motorway_link")]  # sanity
    corridor = LineString([(-400.0, 0.0), (400.0, 0.0)]).buffer(12.0)
    for s in model.surfaces:
        if s.kind not in ("sidewalk", "verge", "crossing", "parking"):
            continue
        if s.tags.get("layer"):
            continue  # the bridge deck's own footway, over the freeway
        assert not s.geometry.intersects(corridor), (s.id, s.kind, s.tags)


# --------------------------------------------------------------------------- ramp gores

def test_lane_count_changes_at_the_gores(model):
    """2 lanes in, 3 between the gores, 2 out: the merge adds a lane and the diverge drops it."""
    main = sorted(_plain(model, "motorway"), key=lambda r: r.reference_line.centroid.x)
    assert len(main) == 3, [r.id for r in main]
    assert [_driving(r) for r in main] == [2, 3, 2], [(r.id, _driving(r)) for r in main]
    assert all(_driving(r) == 1 for r in _plain(model, "motorway_link"))


def test_gores_are_gore_junctions_without_a_plaza(model):
    """A ramp end on the mainline makes a compact gore junction, not an intersection: no plaza,
    no chamfer, no sidewalk apron — and the junction is short, not the whole speed-change lane."""
    gores = [j for j in model.junctions if j.tags.get("kind") == "gore"]
    assert len(gores) == 2, [(j.id, j.tags.get("kind")) for j in model.junctions]
    P = profiles.by_name("us_suburban")
    for j in gores:
        assert j.tags.get("plaza_source") == P.junction.gore_cover, (j.id, j.tags)
        assert j.tags.get("plaza_wkt") is None, j.id
        assert j.polygon is not None and not j.polygon.is_empty
        minx, miny, maxx, maxy = j.polygon.bounds
        assert maxx - minx < 120.0, (j.id, maxx - minx)   # a gore, not a 200 m junction
        # every gore movement stays on the freeway classes
        for c in j.connections:
            assert model.road(c.incoming_road).highway in ("motorway", "motorway_link")
    # both ramps are actually connected to the mainline
    for ramp in _plain(model, "motorway_link"):
        links = {l.element for l in (ramp.predecessor, ramp.successor) if l is not None}
        assert "junction" in links, (ramp.id, ramp.predecessor, ramp.successor)


def test_every_gore_lane_is_connected(model):
    """A gore maps lanes across, not by turn class: every arrival lane keeps a successor and
    every departure lane a predecessor, so no lane dead-ends at the nose."""
    for j in [j for j in model.junctions if j.tags.get("kind") == "gore"]:
        arrivals: dict[str, set] = {}
        departures: dict[str, set] = {}
        for c in j.connections:
            inc = model.road(c.incoming_road)
            arrivals.setdefault(inc.id, set()).update(l.from_lane for l in c.lane_links)
            cr = model.road(c.connecting_road)
            out = model.road(cr.successor.id)
            departures.setdefault(out.id, set()).add(int(cr.tags["to_lane"]))
        for rid, used in arrivals.items():
            want = {l.id for l in model.road(rid).lanes if l.type == "driving" and l.id < 0}
            assert want <= used, (j.id, rid, sorted(want - used))
        for rid, used in departures.items():
            want = {l.id for l in model.road(rid).lanes if l.type == "driving" and l.id < 0}
            assert want <= used, (j.id, rid, sorted(want - used))
        # all movements are through movements: a gore has no turns
        assert {c.id for c in j.connections}
        assert all(model.road(c.connecting_road).tags["turn"] == "through" for c in j.connections)


def test_no_traffic_lights_on_the_freeway(model):
    for s in model.signals:
        r = model.road(s.road_id)
        assert r.highway not in ("motorway", "motorway_link"), (s.id, s.kind, r.id)


# --------------------------------------------------------------------------- grade separation

def test_the_crossing_makes_no_junction(model):
    """The deck and the mainline share an OSM node but not a layer: no junction may join them,
    and no junction may have arms of two different layers."""
    for j in model.junctions:
        arms = {model.road(c.incoming_road).highway for c in j.connections}
        assert not ({"motorway", "motorway_link"} & arms and "secondary" in arms), (j.id, arms)
        layers = {road_osm_layer(model.road(c.incoming_road)) for c in j.connections}
        assert len(layers) <= 1, (j.id, layers)
    # the mainline still runs through x = 0 as one road (the deck did not split it)
    middle = [r for r in _plain(model, "motorway")
              if r.reference_line.bounds[0] < 0.0 < r.reference_line.bounds[2]]
    assert len(middle) == 1, [(r.id, r.reference_line.bounds) for r in _plain(model, "motorway")]
    assert _driving(middle[0]) == 3


def test_the_deck_carries_its_own_z_profile(model):
    """The deck interpolates between its abutments (~ABUTMENT_Z) instead of sampling the DTM
    under it (0), and the mainline underneath stays at grade."""
    decks = [r for r in model.roads if road_is_bridge(r)]
    assert decks, "no bridge road built"
    for r in decks:
        z = np.asarray(r.reference_line.coords)[:, 2]
        assert z.min() > ABUTMENT_Z - 1.5, (r.id, z.min(), z.max())
    main = [r for r in _plain(model, "motorway")]
    for r in main:
        z = np.asarray(r.reference_line.coords)[:, 2]
        assert abs(z).max() < 0.5, (r.id, z.min(), z.max())


def test_z_gap_at_the_grade_separated_crossing(model):
    """>= 4.5 m of air between the deck and the mainline where they cross (validate.py's
    MIN_CLEARANCE_M; Caltrans/AASHTO ask 16 ft 6 in over a freeway)."""
    from twinmodel.validate import MIN_CLEARANCE_M
    decks = [r for r in model.roads if road_is_bridge(r)]
    mains = _plain(model, "motorway")
    gaps = []
    for deck in decks:
        d2 = LineString([(x, y) for x, y, *_ in deck.reference_line.coords])
        for main in mains:
            m2 = LineString([(x, y) for x, y, *_ in main.reference_line.coords])
            if not d2.intersects(m2):
                continue
            p = d2.intersection(m2)
            p = p if p.geom_type == "Point" else p.geoms[0]
            zd = deck.reference_line.interpolate(d2.project(p)).z
            zm = main.reference_line.interpolate(m2.project(p)).z
            gaps.append(zd - zm)
    assert gaps, "the deck and the mainline never cross in 2D"
    assert min(gaps) >= MIN_CLEARANCE_M, gaps


def test_drivable_surfaces_are_separated_by_layer(model):
    """The deck and the road under it overlap in 2D: two drivable surfaces, tagged with their
    OSM layer, and the mesh z of each follows its own layer."""
    layers = {s.tags.get("layer") for s in model.surfaces_of("drivable")}
    assert layers == {0, 1}, layers
    deck = unary_union([s.geometry for s in model.surfaces_of("drivable")
                        if s.tags.get("layer") == 1])
    below = unary_union([s.geometry for s in model.surfaces_of("drivable")
                         if s.tags.get("layer") == 0])
    assert deck.intersects(below), "the overpass should overlap the road below in 2D"
    at = deck.intersection(below).representative_point()
    z_deck = float(model.sample_z(at.x, at.y, layer=1))
    z_below = float(model.sample_z(at.x, at.y, layer=0))
    assert z_deck - z_below >= 4.5, (at, z_deck, z_below)


def test_layer_minus_one_public_road_is_kept(frame):
    """An underpass tagged ``layer=-1`` without ``tunnel`` is a surface road and must survive;
    only tunnels and the layer<0 *service* aisles of an underground car park are dropped."""
    from twinmodel.lanegraph import _is_underground
    assert not _is_underground({"highway": "secondary", "layer": "-1"})
    assert _is_underground({"highway": "secondary", "tunnel": "yes"})
    assert _is_underground({"highway": "service", "layer": "-1"})
