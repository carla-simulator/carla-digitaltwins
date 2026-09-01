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
from dataclasses import replace

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


def make_osm(frame: LocalFrame, *, gore_x: float = 100.0, mid_lanes: str = "3") -> OsmData:
    """The interchange as OSM data: gores at x = -gore_x (merge) and +gore_x (diverge), the
    mainline between them tagged ``lanes=mid_lanes`` (3 = OSM maps the auxiliary lane, 2 = it
    does not and the taper model has to add it)."""
    data = OsmData()
    data.bbox_swne = BBOX
    nid = [0]

    def node(x: float, y: float) -> int:
        nid[0] += 1
        lon, lat = frame.to_wgs84(x, y)
        data.nodes[nid[0]] = OsmNode(nid[0], float(lat), float(lon))
        return nid[0]

    n_w = node(-400.0, 0.0)
    n1 = node(-gore_x, 0.0)         # merge gore
    n_mid = node(0.0, 0.0)          # 2D crossing with the deck (shared node, different layers)
    n2 = node(gore_x, 0.0)          # diverge gore
    n_e = node(400.0, 0.0)
    mainline = {"highway": "motorway", "oneway": "yes", "name": "Test Freeway", "ref": "T 1"}
    data.ways.append(OsmWay(101, [n_w, n1], {**mainline, "lanes": "2"}))
    data.ways.append(OsmWay(102, [n1, n_mid, n2], {**mainline, "lanes": mid_lanes}))
    data.ways.append(OsmWay(103, [n2, n_e], {**mainline, "lanes": "2"}))

    link = {"highway": "motorway_link", "oneway": "yes", "lanes": "1"}
    on_ramp = [node(-gore_x - 220.0, -70.0), node(-gore_x - 140.0, -60.0), node(-gore_x - 60.0, -25.0), n1]
    data.ways.append(OsmWay(104, on_ramp, dict(link)))
    off_ramp = [n2, node(gore_x + 60.0, -25.0), node(gore_x + 140.0, -60.0), node(gore_x + 220.0, -70.0)]
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
def osm(frame: LocalFrame) -> OsmData:
    return make_osm(frame)


def build_model(osm: OsmData, frame: LocalFrame, profile=None):
    with profiles.use(profile or "us_suburban"):
        m = build_lanegraph(osm, frame, BBOX, name="interchange")
        m.elevation = _dem(frame)
        m.metadata["elevation"] = apply_elevation(m)
        build_surfaces(m)
    return m


@pytest.fixture(scope="module")
def model(osm: OsmData, frame: LocalFrame):
    """The US default: ramp gores as speed-change lanes (``gore_model="taper"``)."""
    return build_model(osm, frame)


def junction_profile():
    P = profiles.by_name("us_suburban")
    return P.with_(junction=replace(P.junction, gore_model="junction"))


@pytest.fixture(scope="module")
def model_junction(osm: OsmData, frame: LocalFrame):
    """The 2026-09-01 model: every gore is an OpenDRIVE junction with connecting roads."""
    return build_model(osm, frame, junction_profile())


@pytest.fixture(scope="module")
def model_aux(frame: LocalFrame):
    """Taper model where OSM does *not* map the auxiliary lane (mainline ``lanes=2`` through
    both gores, 700 m apart): the acceleration and deceleration lanes are added and taper."""
    return build_model(make_osm(frame, gore_x=350.0, mid_lanes="2"), frame)


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


def test_gores_are_gore_junctions_without_a_plaza(model_junction):
    """A ramp end on the mainline makes a compact gore junction, not an intersection: no plaza,
    no chamfer, no sidewalk apron — and the junction is short, not the whole speed-change lane."""
    gores = [j for j in model_junction.junctions if j.tags.get("kind") == "gore"]
    assert len(gores) == 2, [(j.id, j.tags.get("kind")) for j in model_junction.junctions]
    P = profiles.by_name("us_suburban")
    for j in gores:
        assert j.tags.get("plaza_source") == P.junction.gore_cover, (j.id, j.tags)
        assert j.tags.get("plaza_wkt") is None, j.id
        assert j.polygon is not None and not j.polygon.is_empty
        minx, miny, maxx, maxy = j.polygon.bounds
        assert maxx - minx < 120.0, (j.id, maxx - minx)   # a gore, not a 200 m junction
        # every gore movement stays on the freeway classes
        for c in j.connections:
            assert model_junction.road(c.incoming_road).highway in ("motorway", "motorway_link")
    # both ramps are actually connected to the mainline
    for ramp in _plain(model_junction, "motorway_link"):
        links = {l.element for l in (ramp.predecessor, ramp.successor) if l is not None}
        assert "junction" in links, (ramp.id, ramp.predecessor, ramp.successor)


def test_every_gore_lane_is_connected(model_junction):
    """A gore maps lanes across, not by turn class: every arrival lane keeps a successor and
    every departure lane a predecessor, so no lane dead-ends at the nose."""
    for j in [j for j in model_junction.junctions if j.tags.get("kind") == "gore"]:
        arrivals: dict[str, set] = {}
        departures: dict[str, set] = {}
        for c in j.connections:
            inc = model_junction.road(c.incoming_road)
            arrivals.setdefault(inc.id, set()).update(l.from_lane for l in c.lane_links)
            cr = model_junction.road(c.connecting_road)
            out = model_junction.road(cr.successor.id)
            departures.setdefault(out.id, set()).add(int(cr.tags["to_lane"]))
        for rid, used in arrivals.items():
            want = {l.id for l in model_junction.road(rid).lanes if l.type == "driving" and l.id < 0}
            assert want <= used, (j.id, rid, sorted(want - used))
        for rid, used in departures.items():
            want = {l.id for l in model_junction.road(rid).lanes if l.type == "driving" and l.id < 0}
            assert want <= used, (j.id, rid, sorted(want - used))
        # all movements are through movements: a gore has no turns
        assert {c.id for c in j.connections}
        assert all(model_junction.road(c.connecting_road).tags["turn"] == "through" for c in j.connections)


# ------------------------------------------------------------- ramp gores: taper model (7k)

def _gore_junctions(m):
    return [j for j in m.junctions if j.tags.get("kind") == "gore"]


def _ramp(m, kind):
    return next(r for r in m.roads if r.tags.get("gore_kind") == kind)


def test_taper_merge_has_no_junction(model):
    """Under ``gore_model="taper"`` (the US default) the merge gore is not a junction: the
    mainline runs through as a road link and the on-ramp ends at the nose, linked road-to-road
    into the downstream mainline road."""
    ramp = _ramp(model, "merge")
    assert ramp.successor is not None and ramp.successor.element == "road"
    main = model.road(ramp.tags["gore_mainline"])
    assert ramp.successor.id == main.id and ramp.successor.contact == "start"
    assert main.predecessor is not None and main.predecessor.element == "road"  # the mainline arrival
    # only the diverge keeps a junction, and it is the compact nose
    gores = _gore_junctions(model)
    assert len(gores) == 1 and gores[0].tags.get("gore_role") == "diverge_nose", \
        [(j.id, j.tags.get("gore_role")) for j in gores]


def test_taper_diverge_nose_is_compact(model):
    """The diverge keeps a nose junction (a road with two successors must be a junction), but
    it is a few metres of stubs, not the whole speed-change area."""
    P = profiles.by_name("us_suburban")
    (j,) = _gore_junctions(model)
    assert j.polygon is not None and j.polygon.area < 400.0, j.polygon.area
    for c in j.connections:
        cr = model.road(c.connecting_road)
        assert cr.length < 4 * P.junction.gore_nose_m, (cr.id, cr.length)
    # every arrival lane is linked, and the ramp gets the outermost one
    ramp = _ramp(model, "diverge")
    inc = model.road(j.connections[0].incoming_road)
    linked = {ll.from_lane for c in j.connections for ll in c.lane_links}
    assert linked == {l.id for l in inc.lanes if l.type == "driving"}
    to_ramp = [c for c in j.connections
               if model.road(c.connecting_road).successor.id == ramp.id]
    assert to_ramp and all(ll.from_lane == min(l.id for l in inc.lanes if l.type == "driving")
                           for c in to_ramp for ll in c.lane_links)


def test_aux_lanes_added_when_osm_does_not_map_them(model_aux):
    """Mainline ``lanes=2`` through both gores: the taper model adds the acceleration lane
    (full -> 0 over the merge taper) and the deceleration lane (0 -> full before the nose) as
    auxiliary lanes of the mainline roads."""
    from twinmodel.model import aux_width_at
    P = profiles.by_name("us_suburban")
    merge_main = model_aux.road(_ramp(model_aux, "merge").tags["gore_mainline"])
    aux = [l for l in merge_main.lanes if l.tags.get("aux") == "merge"]
    assert len(aux) == 1, [(l.id, l.tags) for l in merge_main.lanes]
    (a,) = aux
    total = P.junction.gore_merge_lane_m + P.junction.gore_merge_taper_m
    assert a.tags["aux_s0"] == 0.0 and abs(a.tags["aux_s1"] - total) < 1.0
    assert abs((a.tags["taper_s1"] - a.tags["taper_s0"]) - P.junction.gore_merge_taper_m) < 1e-6
    assert aux_width_at(a, merge_main, 0.0) == a.width
    assert aux_width_at(a, merge_main, a.tags["aux_s1"]) == 0.0
    mid = (a.tags["taper_s0"] + a.tags["taper_s1"]) / 2.0
    assert abs(aux_width_at(a, merge_main, mid) - a.width / 2.0) < 1e-6

    div_main = model_aux.road(_ramp(model_aux, "diverge").tags["gore_mainline"])
    dec = [l for l in div_main.lanes if l.tags.get("aux") == "diverge"]
    assert len(dec) == 1
    (d,) = dec
    assert abs(d.tags["aux_s1"] - div_main.length) < 1e-6
    assert aux_width_at(d, div_main, d.tags["aux_s0"]) == 0.0
    assert aux_width_at(d, div_main, div_main.length) == d.width
    # the acceleration lane and the deceleration lane may be on the same road; they must not
    # overlap here (700 m between the gores), so both taper fully
    if div_main is merge_main:
        assert a.tags["aux_s1"] <= d.tags["aux_s0"]


def test_taper_wedge_is_in_the_drivable_surface(model_aux):
    """The drivable polygon follows the taper: full width at the nose, base width past the
    taper end, linear in between — with the shoulder always outboard."""
    from shapely.ops import unary_union
    from twinmodel.surfaces import carriageway_extent_at
    merge_main = model_aux.road(_ramp(model_aux, "merge").tags["gore_mainline"])
    (a,) = [l for l in merge_main.lanes if l.tags.get("aux") == "merge"]
    drivable = unary_union([s.geometry for s in model_aux.surfaces_of("drivable")
                            if not s.tags.get("layer")])
    line = merge_main.reference_line
    for s_probe in (1.0, a.tags["taper_s0"] - 5.0, (a.tags["taper_s0"] + a.tags["taper_s1"]) / 2.0,
                    a.tags["aux_s1"] + 5.0, merge_main.length - 5.0):
        wl, wr = carriageway_extent_at(merge_main, s_probe)
        p = line.interpolate(s_probe)
        h = 0.0  # the fixture mainline runs along +x
        inside = Point(p.x, p.y - (wr - 0.15))
        outside = Point(p.x, p.y - (wr + 0.35))
        assert drivable.covers(inside), (s_probe, wr)
        assert not drivable.covers(outside), (s_probe, wr)


def _export_and_validate(m):
    from twinmodel.export.xodr import export_xodr
    from twinmodel import validate as V
    with profiles.use("us_suburban"):
        xodr = export_xodr(m)
        return V.validate(m, xodr, step=1.0), xodr


@pytest.fixture(scope="module")
def aux_report(model_aux):
    return _export_and_validate(model_aux)


def test_taper_xodr_lane_sections(model_aux, aux_report):
    """The exported road carries a <laneSection> at every auxiliary-lane boundary and the lane
    width is a polynomial: CARLA sees the lane count and width change along the mainline."""
    import carla
    _report, xodr = aux_report
    cmap = carla.Map("aux", xodr)
    merge_main = model_aux.road(_ramp(model_aux, "merge").tags["gore_mainline"])
    (a,) = [l for l in merge_main.lanes if l.tags.get("aux") == "merge"]
    from twinmodel.export.xodr import build_id_map
    rid = build_id_map(model_aux).road[merge_main.id]
    wps = [w for w in cmap.generate_waypoints(2.0) if w.road_id == rid
           and w.lane_type == carla.LaneType.Driving]
    # the acceleration lane is the outermost driving lane of the first lane section; the same
    # OpenDRIVE id reappears past the diverge taper (the deceleration lane, renumbered), so
    # restrict to the merge half of the road
    aux_wps = [w for w in wps if w.lane_id == min(w2.lane_id for w2 in wps)
               and w.s < a.tags["aux_s1"] + 20.0]
    assert max(w.s for w in aux_wps) <= a.tags["aux_s1"] + 2.0
    w_at = {round(w.s): w.lane_width for w in aux_wps}
    assert abs(w_at[round(a.tags["taper_s0"] // 2 * 2)] - a.width) < 0.1
    late = [w.lane_width for w in aux_wps if w.s > (a.tags["taper_s0"] + a.tags["taper_s1"]) / 2]
    assert late and max(late) < a.width * 0.75
    # between the two speed-change lanes the road has one driving lane fewer
    (d,) = [l for l in merge_main.lanes if l.tags.get("aux") == "diverge"]
    n_early = len({w.lane_id for w in wps if w.s < a.tags["taper_s0"]})
    n_mid = len({w.lane_id for w in wps if a.tags["aux_s1"] + 5.0 < w.s < d.tags["aux_s0"] - 5.0})
    assert n_early == n_mid + 1, (n_early, n_mid)


def test_taper_ramp_continuity_and_invariants(aux_report):
    """validate: the ramp lanes run on through next() (merge without any junction, diverge
    through nothing but the nose), no terminal lanes, every waypoint on the drivable surface."""
    report, _ = aux_report
    rc = report["ramp_continuity"]
    assert rc["checked"] >= 2 and rc["pass"], rc
    assert report["terminal_lanes"]["count"] == 0, report["terminal_lanes"]
    assert report["lane_in_drivable"]["fraction"] == 1.0, report["lane_in_drivable"]
    assert report["junction_lane_links"]["pass"], report["junction_lane_links"]
    assert report["junction_slivers"]["pass"], report["junction_slivers"]


def test_junction_model_still_available(model_junction):
    """``gore_model="junction"`` keeps the 2026-09-01 behaviour: two gore junctions, ramps
    linked through them (their own tests above), and no auxiliary lanes anywhere."""
    assert len(_gore_junctions(model_junction)) == 2
    assert not [l for r in model_junction.roads for l in r.lanes if l.tags.get("aux")]
    assert not [r for r in model_junction.roads if r.tags.get("gore_model")]


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
    """An underpass tagged ``layer=-1`` without ``tunnel`` is a road of the twin and must
    survive, and so is a public road in a tunnel (``cli.apply_tunnel_profiles`` sinks it);
    only the layer<0 / tunnel *service* aisles of an underground car park and a street through
    a building (``building_passage``) are dropped."""
    from twinmodel.lanegraph import _is_underground, is_tunnel
    assert not _is_underground({"highway": "secondary", "layer": "-1"})
    assert not _is_underground({"highway": "secondary", "tunnel": "yes"})
    assert is_tunnel({"highway": "secondary", "tunnel": "yes"})
    assert is_tunnel({"highway": "secondary", "layer": "-1"})
    assert not is_tunnel({"highway": "secondary", "tunnel": "building_passage"})
    assert _is_underground({"highway": "secondary", "tunnel": "building_passage"})
    assert _is_underground({"highway": "service", "layer": "-1"})
    assert _is_underground({"highway": "service", "tunnel": "yes", "service": "parking_aisle"})
