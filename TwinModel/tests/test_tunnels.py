"""Tunnels and underpasses (``tunnel=yes`` / ``layer < 0``): the mirror image of a bridge.

A DTM over a tunnel is the ground above it, so the tunnel road never samples it
(``cli.apply_tunnel_profiles``): it runs straight between its portals (the z of the approach
roads) and is sunk wherever a road of a higher layer passes over it until it sits
``ElevationRules.min_clearance_m`` below that road, with ramps at ``tunnel_max_grade``. When
the tunnel is too short for the ramp the portal itself sinks and the approach is welded down
into a trench. The surfaces give the tunnel its own negative-layer drivable / sidewalks / curbs
plus an enclosure (``tunnel_wall`` / ``tunnel_ceiling``), and the ground above it stays intact
except over the open cut at the portals.

Synthetic fixtures (no network): a two-way street along x at y = 0 on layer 0, a tunnel along
y under it on layer -1, and a flat DTM at z = 0. Plus the real thing: SF's Stockton Tunnel
(``tests/fixtures/sf_stockton_*``), ``tunnel=yes layer=-2`` under Bush, Pine, California and
the surface Stockton Street on Nob Hill, both portals inside the bbox.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.spatial import cKDTree
from shapely.geometry import Point
from shapely.ops import unary_union

from twinmodel import profiles
from twinmodel.cli import apply_elevation, tunnel_road_ids
from twinmodel.export.mesh import export_obj
from twinmodel.export.xodr import export_xodr
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import OsmData, OsmNode, OsmWay, load_fixture
from twinmodel.lanegraph import build_lanegraph
from twinmodel.model import Elevation, TwinModel, road_is_tunnel, road_osm_layer
from twinmodel.surfaces import build_surfaces
from twinmodel.validate import MIN_CLEARANCE_M, validate

FIX = Path(__file__).parent / "fixtures"
ORIGIN = (37.7815, -122.4040)
BBOX = (37.7790, -122.4080, 37.7840, -122.4000)    # ~550 m x 700 m
STREET = {"highway": "secondary", "lanes": "2", "name": "Cross Street"}
TUNNEL = {"highway": "tertiary", "lanes": "2", "name": "Under Street", "tunnel": "yes", "layer": "-1"}
APPROACH = {"highway": "tertiary", "lanes": "2", "name": "Under Street"}


def _flat_dem(z: float = 0.0) -> Elevation:
    step = 2.0
    xs = np.arange(-600.0, 600.0 + step, step)
    ys = np.arange(-600.0, 600.0 + step, step)
    return Elevation(np.full((len(ys), len(xs)), z), float(xs[0]), float(ys[0]), step, step,
                     source="synthetic")


def _osm(frame: LocalFrame, half: float, approaches: bool, tunnel_tags: dict = TUNNEL) -> OsmData:
    """Street along x at y = 0; tunnel along y over ``[-half, +half]``, continuing as plain
    layer-0 ways to |y| = 300 when ``approaches`` (else the bbox cut it: free ends)."""
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
    n_lo, n_hi = node(0.0, -half), node(0.0, half)
    data.ways.append(OsmWay(2, [n_lo, node(0.0, -half / 2), node(0.0, 0.0),
                                node(0.0, half / 2), n_hi], dict(tunnel_tags)))
    if approaches:
        data.ways.append(OsmWay(3, [node(0.0, -300.0), node(0.0, -half - 60.0), n_lo],
                                dict(APPROACH)))
        data.ways.append(OsmWay(4, [n_hi, node(0.0, half + 60.0), node(0.0, 300.0)],
                                dict(APPROACH)))
    return data


def _build(frame: LocalFrame, data: OsmData, dem: Elevation | None = None,
           surfaces: bool = False) -> TwinModel:
    with profiles.use("us_urban"):
        m = build_lanegraph(data, frame, BBOX, name="tunnel_test")
        m.elevation = dem if dem is not None else _flat_dem()
        m.metadata["elevation"] = apply_elevation(m)
        if surfaces:
            build_surfaces(m)
    return m


def _tunnel_roads(m: TwinModel):
    return [r for r in m.roads if r.junction_id is None and road_is_tunnel(r)]


def _street_roads(m: TwinModel):
    return [r for r in m.roads if r.junction_id is None and r.highway == "secondary"]


def _min_cover(m: TwinModel, radius: float = 4.0) -> float:
    """Smallest (street z - tunnel z) where a street sample sits over a tunnel sample — the
    quantity ``validate.grade_separation`` measures, tunnel as the lower layer."""
    hi = np.concatenate([np.asarray(r.reference_line.segmentize(2.0).coords) for r in _street_roads(m)])
    lo = np.concatenate([np.asarray(r.reference_line.segmentize(2.0).coords) for r in _tunnel_roads(m)])
    d, k = cKDTree(lo[:, :2]).query(hi[:, :2], k=1)
    near = d <= radius
    assert near.any(), "the street must cross the tunnel"
    return float(np.min(hi[near][:, 2] - lo[k[near]][:, 2]))


def _tunnel_profile(m: TwinModel) -> tuple[np.ndarray, np.ndarray]:
    """(s, z) along the tunnel chain, portal to portal (one road here: no lanes change)."""
    roads = _tunnel_roads(m)
    assert len(roads) == 1, [r.id for r in roads]
    c = np.asarray(roads[0].reference_line.coords)
    s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(c[:, :2], axis=0).T))])
    return s, c[:, 2]


@pytest.fixture(scope="module")
def frame() -> LocalFrame:
    return LocalFrame(*ORIGIN)


@pytest.fixture(scope="module")
def short_tunnel(frame) -> TwinModel:
    """60 m each side of the street: too short to reach 6 m of cover at 8 %, so the portals
    sink and the approaches are welded into a trench."""
    return _build(frame, _osm(frame, 60.0, approaches=True), surfaces=True)


@pytest.fixture(scope="module")
def long_tunnel(frame) -> TwinModel:
    """200 m each side: the dip fits between the portals, which stay at ground level."""
    return _build(frame, _osm(frame, 200.0, approaches=True))


@pytest.fixture(scope="module")
def clipped_tunnel(frame) -> TwinModel:
    """No approach on either side: the bbox cut the tunnel, it stays sunk under the DEM."""
    return _build(frame, _osm(frame, 240.0, approaches=False))


def _rules():
    with profiles.use("us_urban"):
        return profiles.get().elevation


# ------------------------------------------------------------------ a tunnel is a road

def test_a_public_tunnel_road_is_kept_on_its_own_layer(short_tunnel):
    roads = _tunnel_roads(short_tunnel)
    assert roads, "tunnel=yes on a public road must survive lanegraph"
    assert tunnel_road_ids(short_tunnel) == {r.id for r in roads}
    assert {road_osm_layer(r) for r in roads} == {-1}
    assert short_tunnel.metadata["elevation"]["tunnel_roads"] == len(roads)
    # linked end to end to its approaches, no junction at the portals
    for r in roads:
        for link in (r.predecessor, r.successor):
            assert link is not None and link.element == "road", (r.id, link)


def test_a_layer_minus_one_underpass_without_tunnel_tag_is_a_tunnel_too(frame):
    m = _build(frame, _osm(frame, 200.0, approaches=True,
                           tunnel_tags={**APPROACH, "layer": "-1"}))
    assert tunnel_road_ids(m), "layer=-1 without tunnel=* must count as a tunnel"
    assert _min_cover(m) >= _rules().min_clearance_m - 0.05


# ------------------------------------------------------------------ elevation

@pytest.mark.parametrize("which", ["short_tunnel", "long_tunnel", "clipped_tunnel"])
def test_the_tunnel_clears_the_street_above(which, request):
    m = request.getfixturevalue(which)
    need = _rules().min_clearance_m
    cover = _min_cover(m)
    assert cover >= need - 0.05, f"only {cover:.2f} m of cover under the street"
    assert cover >= MIN_CLEARANCE_M


def test_the_street_above_stays_on_the_ground(short_tunnel):
    z = np.concatenate([np.asarray(r.reference_line.coords)[:, 2] for r in _street_roads(short_tunnel)])
    assert np.abs(z).max() < 0.3, "the DEM is flat at 0: the street must not follow the tunnel down"


def test_ramps_respect_the_maximum_grade(short_tunnel, long_tunnel):
    g_max = _rules().tunnel_max_grade
    for m in (short_tunnel, long_tunnel):
        s, z = _tunnel_profile(m)
        grade = np.abs(np.diff(z) / np.maximum(np.diff(s), 1e-6))
        assert grade.max() <= g_max + 0.005, f"ramp at {grade.max() * 100:.1f} %"
        assert len(s) > 5, "the tunnel road must be densified so the ramp has vertices"


def test_a_long_tunnel_keeps_its_portals_at_ground_level(long_tunnel):
    s, z = _tunnel_profile(long_tunnel)
    assert abs(z[0]) < 0.05 and abs(z[-1]) < 0.05, (z[0], z[-1])
    assert z.min() <= -_rules().min_clearance_m + 0.05
    assert long_tunnel.metadata["elevation"]["tunnel_chains_sunk"] == 1
    assert long_tunnel.metadata["elevation"]["portals_welded"] == 0


def test_a_short_tunnel_sinks_its_portals_and_welds_the_approaches(short_tunnel):
    E = _rules()
    el = short_tunnel.metadata["elevation"]
    assert el["tunnel_chains_sunk"] == 1
    assert el["portals_welded"] == 2, el
    assert el["portal_sink_m"] > 0.5
    roads = {r.id: r for r in short_tunnel.roads}
    for t in _tunnel_roads(short_tunnel):
        for end, link in (("start", t.predecessor), ("end", t.successor)):
            a = roads[link.id]
            zt = np.asarray(t.reference_line.coords)[0 if end == "start" else -1, 2]
            za = np.asarray(a.reference_line.coords)[0 if link.contact == "start" else -1, 2]
            assert abs(zt - za) <= 0.05, f"portal step of {zt - za:.2f} m left in the surface"
            assert zt < -0.5, "the portal should have sunk"
            # the trench fades out along the approach: its far end is back on the ground
            c = np.asarray(a.reference_line.coords)
            z_far = c[-1 if link.contact == "start" else 0, 2]
            assert abs(z_far) < 0.05, z_far
            # ... at no more than the maximum grade
            s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(c[:, :2], axis=0).T))])
            grade = np.abs(np.diff(c[:, 2]) / np.maximum(np.diff(s), 1e-6))
            assert grade.max() <= E.tunnel_max_grade + 0.005


def test_a_clipped_tunnel_stays_sunk(clipped_tunnel):
    """No portal in the data: the DEM at the chain ends is the ground above the tunnel, so the
    ends are held ``min_clearance_m`` under it (the mirror of a clipped deck being lifted)."""
    s, z = _tunnel_profile(clipped_tunnel)
    assert z.max() <= -_rules().min_clearance_m + 0.05, z.max()


def test_the_approach_does_not_read_the_dem_past_the_portal(frame):
    """A hill over the tunnel right behind the portal: the approach's smoothing window must
    not reach into it, or the portal contact rises by metres."""
    step = 2.0
    xs = np.arange(-600.0, 600.0 + step, step)
    ys = np.arange(-600.0, 600.0 + step, step)
    _, Y = np.meshgrid(xs, ys)
    z = 12.0 * (np.abs(Y) < 200.0)          # a 12 m mesa over the tunnel, cliffs at |y| = 200
    dem = Elevation(z, float(xs[0]), float(ys[0]), step, step, source="synthetic")
    m = _build(frame, _osm(frame, 200.0, approaches=True), dem=dem)
    s, zt = _tunnel_profile(m)
    assert abs(zt[0]) < 0.5 and abs(zt[-1]) < 0.5, "the portals sit on the approaches, not on the mesa"
    assert m.metadata["elevation"]["tunnel_chains_sunk"] == 0, "12 m of hill is cover enough"


# ------------------------------------------------------------------ surfaces / mesh

def test_surfaces_per_layer_ground_intact_above_and_cut_at_the_portal(short_tunnel):
    m = short_tunnel
    drivable_layers = {s.tags.get("layer") for s in m.surfaces_of("drivable")}
    assert drivable_layers == {-1, 0}, drivable_layers
    ground = m.surfaces_of("ground")
    assert ground and all(s.tags.get("layer") == 0 for s in ground), \
        "the ground sits on layer 0, never on the tunnel's"
    assert all(s.tags.get("layer") == 0 for s in m.surfaces_of("island"))
    ground_u = unary_union([s.geometry for s in ground])
    street = _street_roads(m)[0]
    extent = max(street.width_left(), street.width_right()) + max(
        l.width for l in street.lanes if l.type == "sidewalk")
    over_tunnel = Point(0.0, extent + 1.0)            # beside the street's sidewalk, over the tunnel
    assert ground_u.covers(over_tunnel), "the ground above a tunnel is intact (no hole)"
    in_trench = Point(0.0, -55.0)                     # 5 m inside the south portal, in the open cut
    assert not ground_u.intersects(in_trench.buffer(0.5)), "the portal cut must not be roofed by ground"
    assert m.metadata["surfaces"]["tunnel_trench_area"] > 50.0


def test_tunnel_enclosure_surfaces(short_tunnel):
    m = short_tunnel
    E = _rules()
    walls = m.surfaces_of("tunnel_wall")
    ceilings = m.surfaces_of("tunnel_ceiling")
    assert walls and ceilings
    assert {s.tags.get("layer") for s in walls + ceilings} == {-1}
    assert all(abs(s.z_offset - E.tunnel_height_m) < 1e-9 for s in ceilings)
    ceiling_u = unary_union([s.geometry for s in ceilings])
    assert ceiling_u.covers(Point(0.0, 0.0)), "covered under the street"
    assert not ceiling_u.intersects(Point(0.0, -55.0).buffer(0.5)), "open to the sky in the trench"
    wall_u = unary_union([s.geometry for s in walls])
    assert wall_u.intersects(Point(0.0, -55.0).buffer(8.0)), "retaining walls line the trench"
    # the tunnel's own sidewalk is on its layer and the datum answers for it
    tunnel_walks = [s for s in m.surfaces_of("sidewalk") if s.tags.get("layer") == -1]
    assert tunnel_walks
    p = tunnel_walks[0].geometry.representative_point()
    assert float(m.sample_z(p.x, p.y, layer=-1)) < -0.5
    assert float(m.sample_z(p.x, p.y, layer=0)) > -0.3, "a surface query over the tunnel is the ground"
    assert float(m.sample_z(p.x, p.y)) > -0.3, "... and so is a query without a layer"


def _obj_groups(path: Path) -> dict[str, np.ndarray]:
    verts: list[tuple[float, float, float]] = []
    groups: dict[str, list[int]] = {}
    cur = None
    for line in path.read_text().splitlines():
        if line.startswith("v "):
            verts.append(tuple(float(v) for v in line.split()[1:4]))
        elif line.startswith("g "):
            cur = line.split()[1]
        elif line.startswith("f ") and cur is not None:
            groups.setdefault(cur, []).extend(int(t.split("/")[0]) - 1 for t in line.split()[1:])
    v = np.asarray(verts)
    return {g: v[np.asarray(idx)] for g, idx in groups.items()}


def test_mesh_has_tunnel_groups_on_the_tunnel_datum(short_tunnel, tmp_path):
    E = _rules()
    path = tmp_path / "tunnel.obj"
    with profiles.use("us_urban"):
        export_obj(short_tunnel, path)
    g = _obj_groups(path)
    assert "tunnel_wall" in g and "tunnel_ceiling" in g, sorted(g)
    wall_z = g["tunnel_wall"][:, 2]
    assert wall_z.max() - wall_z.min() >= E.tunnel_height_m, "walls are extruded to the tunnel height"
    ceil_z = g["tunnel_ceiling"][:, 2]
    assert ceil_z.min() < E.tunnel_height_m - 5.0, "the ceiling follows the sunk tunnel road"
    mtl = (tmp_path / "tunnel.mtl").read_text()
    assert "newmtl tunnel_wall" in mtl and "newmtl tunnel_ceiling" in mtl


def test_a_twin_without_a_tunnel_writes_no_tunnel_materials(tmp_path):
    from tests.synthetic import straight_road
    m = straight_road()
    with profiles.use("eu_dense"):
        build_surfaces(m)
        export_obj(m, tmp_path / "plain.obj")
    assert "tunnel" not in (tmp_path / "plain.mtl").read_text()
    assert "tunnel" not in (tmp_path / "plain.obj").read_text()


# ------------------------------------------------------------------ validation

def test_validate_reports_the_under_crossing(short_tunnel, tmp_path):
    with profiles.use("us_urban"):
        xodr = export_xodr(short_tunnel)
        report = validate(short_tunnel, xodr, out_dir=tmp_path)
    assert report["topology"]["loaded"], report["topology"]
    gs = report["grade_separation"]
    assert gs is not None and gs["pass"], gs
    assert gs["min_z_gap_m"] >= MIN_CLEARANCE_M
    assert gs["worst"]["lower_road"] in tunnel_road_ids(short_tunnel)
    assert report["lane_in_drivable"]["fraction"] == 1.0, report["lane_in_drivable"]
    assert report["z_error"]["p95"] <= 0.05, report["z_error"]
    assert report["z_error"]["max"] <= 0.30, report["z_error"]
    assert report["terminal_lanes"]["count"] == 0
    assert not report["lane_coverage"]["missing"]


# ------------------------------------------------------------------ the Stockton Tunnel

@pytest.fixture(scope="module")
def stockton() -> tuple[TwinModel, dict]:
    bbox = (37.7888, -122.4100, 37.7942, -122.4050)
    osm = load_fixture(FIX / "sf_stockton_overpass.json")
    with profiles.use("us_urban"):
        m = build_lanegraph(osm, LocalFrame.from_bbox(*bbox), bbox, name="sf_stockton")
        m.elevation = Elevation.from_npz(FIX / "sf_stockton_dem.npz")
        m.metadata["elevation"] = apply_elevation(m)
        build_surfaces(m)
        xodr = export_xodr(m)
        report = validate(m, xodr)
    return m, report


def test_stockton_tunnel_is_built_under_nob_hill(stockton):
    m, report = stockton
    tunnels = _tunnel_roads(m)
    assert len(tunnels) == 1 and road_osm_layer(tunnels[0]) == -2, [(r.id, r.tags) for r in tunnels]
    assert tunnels[0].tags.get("tunnel") == "yes"
    # loose pins: the bbox holds ~35 intersections of the Chinatown / Nob Hill grid
    assert 300 <= len(m.roads) <= 500, len(m.roads)
    assert 25 <= len(m.junctions) <= 60, len(m.junctions)  # widened: service-node junctions (tm/service-chains) add small T-joins
    # both portals are in the data: linked to Stockton Street on layer 0, no sinking needed
    # under the hill (the ground climbs 15 m over the tunnel), no trench ramp
    t = tunnels[0]
    assert t.predecessor.element == "road" and t.successor.element == "road"
    assert m.metadata["elevation"]["tunnel_roads"] == 1
    assert m.metadata["elevation"]["tunnel_chains_sunk"] == 0
    # the tunnel road sits under the DEM, by the full cover away from the portals
    c = np.asarray(t.reference_line.segmentize(2.0).coords)
    s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(c[:, :2], axis=0).T))])
    z_dem = np.asarray(m.elevation.sample(c[:, 0], c[:, 1]))
    inside = (s > 40.0) & (s < s[-1] - 40.0)
    assert (z_dem[inside] - c[inside, 2]).min() >= _rules().min_clearance_m, \
        (z_dem[inside] - c[inside, 2]).min()
    assert (z_dem - c[:, 2]).min() > -0.5, "never above the ground"


def test_stockton_validation(stockton):
    m, report = stockton
    assert report["topology"]["loaded"]
    gs = report["grade_separation"]
    assert gs is not None and gs["pass"] and gs["crossing_waypoints"] > 100, gs
    assert gs["min_z_gap_m"] >= 5.5  # deep cover under the hill (validator floor is 4.5; exact gap shifts with junction geometry)
    assert report["lane_in_drivable"]["fraction"] == 1.0
    assert report["terminal_lanes"]["count"] == 0
    assert report["junction_slivers"]["count"] == 0
    # the tunnel's own waypoints agree with the tunnel datum (z_error is per layer)
    tid = next(iter(tunnel_road_ids(m)))
    bad = [v for v in report["violations"] if v.get("road_id") == tid]
    assert not bad, bad
    # surfaces: enclosure present on layer -2, the ground on layer 0 only
    assert {s.tags.get("layer") for s in m.surfaces_of("tunnel_ceiling")} == {-2}
    assert m.surfaces_of("tunnel_wall")
    assert {s.tags.get("layer") for s in m.surfaces_of("ground")} == {0}
    assert {-2, 0} <= {s.tags.get("layer") for s in m.surfaces_of("drivable")}
