"""OpenDRIVE export tests (worker C) -- no network; CARLA wheel parses client-side."""
from __future__ import annotations

import math

import numpy as np
import pytest
from lxml import etree
from shapely.geometry import LineString

from twinmodel.export.xodr import (build_id_map, export_xodr, fit_elevation, fit_planview,
                                   read_twin_ids, road_geometry, sample_reference)
from twinmodel.model import Road, TwinModel

from tests.synthetic_xodr import junction_model, straight_road

carla = pytest.importorskip("carla")


# ------------------------------------------------------------------ geometry fitting

def test_two_point_polyline_is_a_line():
    g = fit_planview(np.array([[0, 0], [30, 40]]))
    assert len(g) == 1 and g[0].kind == "line"
    assert g[0].length == pytest.approx(50.0)
    assert g[0].hdg == pytest.approx(math.atan2(40, 30))


def test_parampoly3_hits_vertices_and_is_heading_continuous():
    pts = np.array([[math.cos(a) * 20, math.sin(a) * 20] for a in np.linspace(0, math.pi / 2, 7)])
    pts += np.random.default_rng(0).normal(scale=0.3, size=pts.shape)  # not a perfect arc
    geoms = fit_planview(pts)
    assert len(geoms) == len(pts) - 1
    for g, p_next in zip(geoms, pts[1:]):
        assert g.kind == "paramPoly3"
        x, y = g.point_at(1.0)
        assert math.hypot(x - p_next[0], y - p_next[1]) < 0.02
    # heading at the end of one segment == hdg of the next (G1)
    for g0, g1 in zip(geoms, geoms[1:]):
        _, bU, cU, dU, _, bV, cV, dV = g0.coeffs
        du, dv = bU + 2 * cU + 3 * dU, bV + 2 * cV + 3 * dV
        h_end = g0.hdg + math.atan2(dv, du)
        assert abs((h_end - g1.hdg + math.pi) % (2 * math.pi) - math.pi) < 1e-9
    # cumulative s consistent with lengths
    for g0, g1 in zip(geoms, geoms[1:]):
        assert g1.s == pytest.approx(g0.s + g0.length)


def test_parampoly3_length_matches_chord_integration():
    geoms = fit_planview(np.array([[0, 0], [10, 0.5], [20, 0]]))
    for g in geoms:
        p = np.linspace(0, 1, 2001)
        pts = np.array([g.point_at(pp) for pp in p])
        assert g.length == pytest.approx(np.sum(np.hypot(*np.diff(pts, axis=0).T)), abs=1e-4)


def test_elevation_fit_interpolates_samples():
    s = np.array([0.0, 10.0, 25.0, 40.0])
    z = np.array([0.0, 0.5, 0.2, 1.0])
    recs = fit_elevation(s, z)
    assert len(recs) == 3
    for (s0, a, b, c, d), s1, z1 in zip(recs, s[1:], z[1:]):
        ds = s1 - s0
        assert a + b * ds + c * ds ** 2 + d * ds ** 3 == pytest.approx(z1, abs=1e-9)
    assert fit_elevation(s, np.zeros(4)) == [(0.0, 0.0, 0.0, 0.0, 0.0)]


def test_id_map_keeps_numeric_and_keeps_roads_and_junctions_disjoint():
    m = TwinModel(name="x", origin_lat=0, origin_lon=0, bbox_wgs84=(0, 0, 0, 0))
    m.roads = [Road(id="12", reference_line=LineString([(0, 0), (1, 0)])),
               Road(id="a", reference_line=LineString([(0, 0), (1, 0)])),
               Road(id="0", reference_line=LineString([(0, 0), (1, 0)]))]
    from twinmodel.model import Junction
    m.junctions = [Junction(id="12"), Junction(id="j")]
    ids = build_id_map(m)
    assert ids.road["12"] == 12
    assert set(ids.road.values()).isdisjoint(ids.junction.values())
    assert len(set(ids.road.values()) | set(ids.junction.values())) == 5
    assert min(ids.road.values()) >= 1


# ------------------------------------------------------------------ XML structure

@pytest.fixture(scope="module")
def junction_xodr():
    m = junction_model()
    return m, export_xodr(m)


@pytest.fixture(scope="module")
def straight_xodr(tmp_path_factory):
    m = straight_road()
    path = tmp_path_factory.mktemp("xodr") / "straight.xodr"
    text = export_xodr(m, path)
    assert path.read_text() == text
    return m, text


def test_header_and_georeference(straight_xodr):
    m, text = straight_xodr
    root = etree.fromstring(text.encode())
    h = root.find("header")
    assert h.get("revMajor") == "1" and h.get("revMinor") == "4" and h.get("name") == "straight"
    assert float(h.get("east")) == 50.0 and float(h.get("west")) == -50.0
    assert h.find("geoReference").text == m.geo_reference
    assert "CDATA" in text


def test_lane_structure_markings_heights(straight_xodr):
    _, text = straight_xodr
    root = etree.fromstring(text.encode())
    road = root.find("road")
    assert road.get("junction") == "-1"
    sec = road.find("lanes/laneSection")
    left_ids = [l.get("id") for l in sec.findall("left/lane")]
    right_ids = [l.get("id") for l in sec.findall("right/lane")]
    assert left_ids == ["2", "1"] and right_ids == ["-1", "-2"]
    centre = sec.find("center/lane")
    assert centre.get("id") == "0"
    assert centre.find("roadMark").get("color") == "yellow"
    sw = sec.find("right/lane[@id='-2']")
    assert sw.get("type") == "sidewalk"
    assert sw.find("height").get("inner") == "0.15" and sw.find("height").get("outer") == "0.15"
    drv = sec.find("right/lane[@id='-1']")
    assert drv.find("width").get("a") == "3.25"
    assert drv.find("roadMark").get("type") == "solid" and drv.find("roadMark").get("color") == "white"
    # straight 3-vertex polyline -> two paramPoly3 with zero V coefficients
    geoms = road.findall("planView/geometry")
    assert len(geoms) == 2 and all(g.find("paramPoly3") is not None for g in geoms)
    assert all(float(g.find("paramPoly3").get("cV")) == 0 for g in geoms)
    assert float(road.get("length")) == pytest.approx(100.0, abs=1e-6)
    elev = road.findall("elevationProfile/elevation")
    assert len(elev) == 2 and float(elev[0].get("b")) == pytest.approx(0.02, abs=1e-9)


def test_signals_use_carla_types(junction_xodr, straight_xodr):
    _, text = junction_xodr
    root = etree.fromstring(text.encode())
    sig = {s.get("id"): s for s in root.iter("signal")}
    assert sig["tl_a"].get("type") == "1000001" and sig["tl_a"].get("dynamic") == "yes"
    assert sig["stop_n"].get("type") == "206" and sig["stop_n"].get("dynamic") == "no"
    assert sig["yield_b"].get("type") == "205"
    assert sig["tl_b"].get("orientation") == "-"
    # every traffic light carries an explicit <validity>: without one CARLA synthesises the
    # oncoming side (MapBuilder::GenerateDefaultValiditiesForSignalReferences) and builds no
    # trigger box at all on a one-way-per-side twin approach
    for sid in ("tl_a", "tl_b"):
        assert sig[sid].findall("validity"), f"{sid} has no <validity>"
    ctl = root.findall("controller")
    assert len(ctl) == 1 and ctl[0].get("id") == "ctl_j1"
    assert {c.get("signalId") for c in ctl[0].findall("control")} == {"tl_a", "tl_b"}
    j = root.find("junction")
    assert j.find("controller").get("id") == "ctl_j1"
    _, stext = straight_xodr
    sroot = etree.fromstring(stext.encode())
    sp = sroot.find(".//signal[@id='s_speed']")
    assert sp.get("type") == "274" and sp.get("subtype") == "30" and sp.get("unit") == "km/h"
    cw = sroot.find(".//object[@type='crosswalk']")
    assert cw is not None and len(cw.findall("outline/cornerLocal")) == 5


def test_traffic_light_validity_covers_own_travel_side(junction_xodr):
    """Each light validates the driving lanes of its *own* approach, and CARLA round-trips them.

    The default CARLA would synthesise for a validity-less signal is the opposite side
    (``MapBuilder::GenerateDefaultValiditiesForSignalReferences``: orientation '+' -> lanes
    ``[1, max]``), which on a twin approach road -- sidewalk at +1, driving at -1..-n -- is not
    a ``Driving`` lane at all, so ``UTrafficLightComponent::InitializeSign`` makes zero trigger
    boxes and the light stops nobody.
    """
    m, text = junction_xodr
    root = etree.fromstring(text.encode())
    lane_type = {}   # (road id, lane id) -> type
    for r in root.iter("road"):
        for ln in r.iter("lane"):
            lane_type[(r.get("id"), int(ln.get("id")))] = ln.get("type")

    want_side = {"+": -1, "-": 1}
    seen = {}
    for sig in root.iter("signal"):
        if sig.get("type") != "1000001":
            continue
        vals = sig.findall("validity")
        assert vals, f"{sig.get('id')}: no <validity>"
        rid = sig.getparent().getparent().get("id")
        lanes = []
        for v in vals:
            a, b = int(v.get("fromLane")), int(v.get("toLane"))
            assert not (a == 0 and b == 0), "a (0, 0) validity is dropped by MapBuilder"
            lanes += list(range(min(a, b), max(a, b) + 1))
        for lane in lanes:
            assert lane != 0
            assert lane_type[(rid, lane)] == "driving", f"{sig.get('id')}: lane {lane} not driving"
            assert lane * want_side[sig.get("orientation")] > 0, \
                f"{sig.get('id')}: lane {lane} is on the oncoming side of orientation " \
                f"{sig.get('orientation')}"
        seen[sig.get("id")] = sorted(lanes)
    assert seen == {"tl_a": [-1], "tl_b": [1]}

    # ... and the client parser gives them back verbatim
    cmap = carla.Map(m.name, text)
    got = {str(lm.id): sorted({v for a, b in
                               [tuple(x) for x in lm.get_lane_validities()]
                               for v in range(min(a, b), max(a, b) + 1)})
           for lm in cmap.get_all_landmarks() if lm.type == "1000001"}
    assert got == seen


def test_validity_uses_opendrive_lane_ids_not_model_ids():
    """``section_ids`` renumbers lanes contiguously outward; the validity must follow."""
    from twinmodel.export.xodr import _validity_ranges
    from twinmodel.model import Lane, Signal
    from shapely.geometry import Point
    sections = [(0.0, 100.0, [])]
    # model ids -3, -2 survive but -1 was removed -> OpenDRIVE renumbers them -1, -2
    ids_per_section = [{-3: -2, -2: -1, 1: 1}]
    sig = Signal(id="s", kind="traffic_light", road_id="r", s=50.0, t=0.0, position=Point(0, 0),
                 validities=[(-3, -1)])
    assert _validity_ranges(sig, sections, ids_per_section) == [(-2, -1)]
    # a lane the section does not carry is dropped rather than exported as a dangling id
    sig.validities = [(-9, -9)]
    assert _validity_ranges(sig, sections, ids_per_section) == []
    _ = Lane


def test_junction_and_links(junction_xodr):
    m, text = junction_xodr
    root = etree.fromstring(text.encode())
    ids = build_id_map(m)
    j = root.find("junction")
    assert j.get("id") == str(ids.junction["j1"])
    conns = j.findall("connection")
    assert len(conns) == 3
    assert conns[0].get("incomingRoad") == str(ids.road["a"])
    assert conns[0].get("connectingRoad") == str(ids.road["c1"])
    assert conns[0].find("laneLink").get("from") == "-1"
    roads = {r.get("id"): r for r in root.findall("road")}
    c2 = roads[str(ids.road["c2"])]
    assert c2.get("junction") == str(ids.junction["j1"])
    assert c2.find("link/predecessor").get("elementType") == "road"
    assert c2.find("link/predecessor").get("contactPoint") == "end"
    assert c2.find("link/successor").get("elementId") == str(ids.road["n"])
    lane = c2.find("lanes/laneSection/right/lane[@id='-1']")
    assert lane.find("link/predecessor").get("id") == "-1"  # from laneLink
    assert lane.find("link/successor").get("id") == "-1"    # geometric: n's lane -1
    c3 = roads[str(ids.road["c3"])]
    lane3 = c3.find("lanes/laneSection/right/lane[@id='-1']")
    assert lane3.find("link/predecessor").get("id") == "1"  # laneLink b:+1
    assert lane3.find("link/successor").get("id") == "1"    # geometric: a's lane +1 (contact end)
    a = roads[str(ids.road["a"])]
    assert a.find("link/successor").get("elementType") == "junction"
    assert a.find("lanes/laneSection/right/lane[@id='-1']/link/successor") is None
    assert read_twin_ids(text).road == ids.road
    assert read_twin_ids(text).junction == ids.junction


# ------------------------------------------------------------------ CARLA parses it

def _model_xy(wp):
    loc = wp.transform.location
    return loc.x, -loc.y


def test_carla_loads_straight_and_covers_driving_lanes(straight_xodr):
    m, text = straight_xodr
    cmap = carla.Map("twin", text)
    wps = cmap.generate_waypoints(1.0)
    ids = build_id_map(m)
    driving = [w for w in wps if w.lane_type == carla.LaneType.Driving]
    assert {w.road_id for w in driving} == {ids.road["r1"]}
    assert {w.lane_id for w in driving} == {-1, 1}
    assert len(driving) >= 2 * 95
    # lane -1 centre is 1.625 m right of the reference line; y is flipped by CARLA
    for w in driving:
        x, y = _model_xy(w)
        assert abs(abs(y) - 1.625) < 0.02, (x, y)
        assert abs(w.transform.location.z - 0.02 * x) < 0.02
    lms = cmap.get_all_landmarks()
    assert len(lms) == 1 and lms[0].type == "274" and lms[0].sub_type == "30"
    assert len(cmap.get_crosswalks()) >= 5


def test_carla_loads_junction_model(junction_xodr):
    m, text = junction_xodr
    cmap = carla.Map("twin", text)
    wps = cmap.generate_waypoints(1.0)
    ids = build_id_map(m)
    driving = [w for w in wps if w.lane_type == carla.LaneType.Driving]
    seen = {(ids.road_inv[w.road_id], w.lane_id) for w in driving}
    expected = {(r.id, l.id) for r in m.roads for l in r.lanes if l.type == "driving"}
    assert expected <= seen, expected - seen
    assert {w.junction_id for w in driving if w.is_junction} == {ids.junction["j1"]}
    topo = cmap.get_topology()
    assert len(topo) > 0
    # the incoming lane a:-1 continues into both c1 and c2; c2 continues into n:-1
    a_end = max((w for w in driving if w.road_id == ids.road["a"] and w.lane_id == -1),
                key=lambda w: w.s)
    nxt = {ids.road_inv[w.road_id] for w in a_end.next(3.0)}
    assert nxt == {"c1", "c2"}
    c2_end = max((w for w in driving if w.road_id == ids.road["c2"]), key=lambda w: w.s)
    assert {ids.road_inv[w.road_id] for w in c2_end.next(3.0)} == {"n"}
    c3_end = max((w for w in driving if w.road_id == ids.road["c3"]), key=lambda w: w.s)
    assert {(ids.road_inv[w.road_id], w.lane_id) for w in c3_end.next(3.0)} == {("a", 1)}
    # landmarks: 4 signals, traffic lights are dynamic
    lms = cmap.get_all_landmarks()
    assert len(lms) == 4
    assert {lm.type for lm in lms} == {"1000001", "206", "205"}
    assert all(lm.is_dynamic for lm in lms if lm.type == "1000001")


def test_carla_parampoly3_waypoints_follow_the_curve(junction_xodr):
    """c2 is a quarter circle fitted as 7 paramPoly3s; lane -1 waypoints must sit 1.625 m
    right of the *fitted* reference line, and the fitted line must be within 2 cm of the
    polyline vertices."""
    m, text = junction_xodr
    c2 = m.road("c2")
    geoms, s_v = road_geometry(c2)
    verts = np.asarray(c2.reference_line.coords)[:, :2]
    for g, v in zip(geoms, verts[1:]):
        assert np.hypot(*(np.array(g.point_at(1.0)) - v)) < 0.02
    ref = LineString(sample_reference(c2, 0.1))
    cmap = carla.Map("twin", text)
    ids = build_id_map(m)
    wps = [w for w in cmap.generate_waypoints(0.5) if w.road_id == ids.road["c2"]]
    assert len(wps) > 40
    from shapely.geometry import Point
    for w in wps:
        x, y = _model_xy(w)
        d = ref.distance(Point(x, y))
        assert abs(d - 1.625) < 0.03, (w.s, x, y, d)
    # the curve is a true circle of radius 15 around (-15, 15): the lane is at r = 16.625
    for w in wps:
        x, y = _model_xy(w)
        # endpoint tangents are chords (one-sided), so allow 10 cm off the ideal circle
        assert abs(math.hypot(x + 15, y - 15) - 16.625) < 0.10


def test_export_on_worker_outputs_if_present():
    """Optional: run on worker A/B synthetic models when their modules exist."""
    try:
        from tests import synthetic  # type: ignore
    except ImportError:
        pytest.skip("tests/synthetic.py (worker B) not present")
    builders = [getattr(synthetic, n) for n in dir(synthetic)
                if callable(getattr(synthetic, n)) and not n.startswith("_")]
    ran = 0
    for b in builders:
        try:
            model = b()
        except TypeError:
            continue
        if not isinstance(model, TwinModel) or not model.roads:
            continue
        text = export_xodr(model)
        cmap = carla.Map("twin", text)
        assert len(cmap.generate_waypoints(1.0)) > 0
        ran += 1
    if not ran:
        pytest.skip("no zero-arg TwinModel builders found in tests/synthetic.py")


# ------------------------------------------------------------------ signal country stamp

def test_signal_country_follows_profile():
    """The exported <signal> country attribute is the profile's geo-style hint."""
    from twinmodel import profiles
    m = straight_road()
    with profiles.use(profiles.EU_DENSE):
        assert 'country="ES"' in export_xodr(m)
    with profiles.use(profiles.US_SUBURBAN):
        assert 'country="US"' in export_xodr(m)
