"""Tests for twinmodel.export.ue (glb + manifest bake export) on the synthetic models."""
from __future__ import annotations
from shapely.ops import unary_union

import json
import math

import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString, Polygon, box

from twinmodel import profiles
from twinmodel.export import ue
from twinmodel.model import Building, Elevation, Lane, Road, Surface, TwinModel
from twinmodel.surfaces import build_surfaces
from tests import synthetic

CASES = list(synthetic.ALL_CASES.items())


@pytest.fixture(params=CASES, ids=[c[0] for c in CASES])
def model(request):
    _, factory = request.param
    return build_surfaces(factory())


# --------------------------------------------------------------------------- coordinates

def test_axis_conventions_round_trip():
    xyz = np.array([[1.0, 2.0, 3.0], [-4.0, 5.5, -0.25]])
    ue_cm = ue.model_to_ue(xyz)
    assert np.allclose(ue_cm, [[100, -200, 300], [-400, -550, -25]])
    g = ue.model_to_gltf(xyz)
    # Interchange: UE = (gx, gz, gy) * 100  ==  (x, -y, z) * 100
    assert np.allclose(np.column_stack([g[:, 0], g[:, 2], g[:, 1]]) * 100, ue_cm)
    assert np.allclose(ue.gltf_to_model(g), xyz)


def test_write_read_glb(tmp_path):
    pos = np.array([[0, 0, 0], [10, 0, 0], [10, 5, 1.0], [0, 5, 1.0]], dtype=float)
    nrm = np.tile([0, 0, 1.0], (4, 1))
    uv = pos[:, :2].copy()
    faces = np.array([[0, 1, 2], [0, 2, 3]])
    bbox = ue.write_glb(tmp_path / "q.glb", "q", pos, nrm, uv, faces, "road", (0.2, 0.2, 0.2))
    assert bbox == {"min": [0.0, -500.0, 0.0], "max": [1000.0, 0.0, 100.0]}
    d = ue.read_glb(tmp_path / "q.glb")
    assert d["material"] == "road"
    assert d["gltf"]["meshes"][0]["name"] == "q"
    assert np.allclose(ue.gltf_to_model(d["positions"]), pos)
    assert np.allclose(d["faces"], faces)
    assert d["gltf"]["accessors"][0]["min"] == pytest.approx([0.0, 0.0, -5.0])
    # trimesh must be able to load it (a third-party reader as a format check)
    scene = trimesh.load(tmp_path / "q.glb", force="scene")
    (geom,) = scene.geometry.values()
    assert len(geom.faces) == 2


# --------------------------------------------------------------------------- geometry

def test_zebra_stripes_cover_the_crossing_along_its_long_axis():
    # 12 m across the road (walking direction), 4 m along it
    poly = box(0, 0, 12, 4)
    stripes = ue.zebra_stripes(poly, stripe=0.5, gap=0.5)
    assert len(stripes) == 12
    for s in stripes:
        minx, miny, maxx, maxy = s.bounds
        assert maxx - minx == pytest.approx(0.5, abs=1e-6)   # stripe width along the long axis
        assert (miny, maxy) == pytest.approx((0.0, 4.0))     # full short side
    assert sum(s.area for s in stripes) == pytest.approx(0.5 * poly.area, rel=0.01)
    # rotated crossing: stripes still run across the long axis
    rot = Polygon([(0, 0), (8.485, 8.485), (5.657, 11.314), (-2.828, 2.828)])  # 12 x 4 at 45 deg
    rs = ue.zebra_stripes(rot)
    assert 11 <= len(rs) <= 12
    assert sum(s.area for s in rs) == pytest.approx(0.5 * rot.area, rel=0.05)
    assert ue.zebra_stripes(box(0, 0, 1.0, 0.8)) == []


def test_tiles_of_splits_on_the_grid():
    poly = box(-10, -10, 260, 30)
    pieces = dict(ue._tiles_of(poly, 250.0))
    assert set(pieces) == {(-1, -1), (-1, 0), (0, -1), (0, 0), (1, -1), (1, 0)}
    assert sum(p.area for p in pieces.values()) == pytest.approx(poly.area)
    assert dict(ue._tiles_of(poly, 0.0)) == {(0, 0): poly}


def test_building_extrusion_heights():
    m = synthetic.straight_road(with_building=True)
    m = build_surfaces(m)
    b = Building(id="b1", footprint=box(20, 20, 40, 35), levels=4)
    base, roof = ue.building_geometry(m, b, level_height=3.0, default_levels=2)
    assert roof - base == pytest.approx(12.0 + ue.BUILDING_SINK)
    b2 = Building(id="b2", footprint=box(20, 20, 40, 35), height=20.0)
    base2, roof2 = ue.building_geometry(m, b2, 3.0, 2)
    assert roof2 - base2 == pytest.approx(20.0 + ue.BUILDING_SINK)
    mb = ue.MeshBuilder()
    n = ue._add_building(mb, m, b, 3.0, 2)
    pos, nrm, uv, faces = mb.arrays()
    assert n == len(faces) == 8 + 2  # 4 walls x 2 tris + roof
    # walls: normals horizontal and pointing away from the centroid; roof: up
    cen = np.array([30.0, 27.5])
    for f in faces:
        c = pos[f].mean(axis=0)
        nn = nrm[f[0]]
        if abs(nn[2]) > 0.5:
            assert c[2] == pytest.approx(roof)
        else:
            assert np.dot(nn[:2], c[:2] - cen) > 0
    # wall UVs are (along, height) in metres
    assert uv[:, 1].max() == pytest.approx(roof - base)


def test_curb_strip_normals_face_the_low_side():
    m = build_surfaces(synthetic.straight_road())
    curbs = [c for c in m.curbs]
    assert curbs
    mb = ue.MeshBuilder()
    n = ue._add_curb_strip(mb, m, curbs[0], subdivide=False)
    pos, nrm, uv, faces = mb.arrays()
    assert n == len(faces) > 0
    assert np.allclose(nrm[:, 2], 0.0)
    assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0)
    # heights span exactly the curb height
    assert pos[:, 2].max() - pos[:, 2].min() == pytest.approx(curbs[0].height, abs=0.05)
    # low side (drivable) is towards the normal: sample a point on the normal side
    p = pos[0][:2] + 0.5 * nrm[0][:2]
    drivable = [s.geometry for s in m.surfaces if s.kind == "drivable"]
    assert any(g.buffer(0.3).contains(__import__("shapely").geometry.Point(p)) for g in drivable)


# --------------------------------------------------------------------------- spawn points

def test_spawn_points_sit_on_driving_lanes_with_the_lane_heading():
    m = build_surfaces(synthetic.straight_road(length=120.0))
    sp = ue.spawn_points(m, spacing=30.0, margin=10.0)
    assert sp
    road = m.roads[0]
    drivable = [s.geometry for s in m.surfaces if s.kind == "drivable"]
    for p in sp:
        x, y, z, h = p["model"]
        assert any(g.contains(__import__("shapely").geometry.Point(x, y)) for g in drivable)
        assert p["x"] == pytest.approx(x * 100, abs=0.1) and p["y"] == pytest.approx(-y * 100, abs=0.1)
        assert p["yaw"] == pytest.approx(-math.degrees(h), abs=0.01)
        lane = next(l for l in road.lanes if l.id == p["lane"])
        assert lane.type == "driving"
        # heading follows the lane direction: right lanes forward, left lanes backward
        ref = np.asarray(road.reference_line.coords)
        d = ref[-1, :2] - ref[0, :2]
        along = math.cos(h) * d[0] + math.sin(h) * d[1]
        assert (along > 0) == (lane.direction == "forward")
    # the two travel directions are present (two-way road)
    assert {p["lane"] > 0 for p in sp} == {True, False}
    # limit is honoured
    assert len(ue.spawn_points(m, spacing=1.0, limit=20)) == 20


# --------------------------------------------------------------------------- export

def test_export_ue_writes_assets_and_manifest(tmp_path, model):
    xodr = tmp_path / "m.xodr"
    xodr.write_text("<OpenDRIVE/>")
    man = ue.export_ue(model, tmp_path / "ue", name="t", xodr_path=xodr, tile_m=0.0)
    assert (tmp_path / "ue" / "manifest.json").exists()
    assert man == json.loads((tmp_path / "ue" / "manifest.json").read_text())
    assert man["schema"] == ue.MANIFEST_SCHEMA
    assert man["xodr"] == str(xodr.resolve())
    kinds = {a["kind"] for a in man["assets"]}
    assert "drivable" in kinds and "marking_white" in kinds
    if model.curbs:
        assert "curb" in kinds
    for a in man["assets"]:
        f = tmp_path / "ue" / a["file"]
        assert f.exists()
        d = ue.read_glb(f)
        assert len(d["faces"]) == a["triangles"] and len(d["positions"]) == a["vertices"]
        assert a["material"] == ue.KIND_MATERIAL[a["kind"]][0]
        assert a["semantic"] == ue.KIND_MATERIAL[a["kind"]][1]
        assert a["tile"] == [0, 0]
        # bbox in the manifest matches the vertices (UE cm)
        p = ue.gltf_to_model(d["positions"])
        assert np.allclose(ue.model_to_ue(p).min(axis=0), a["bbox_ue"]["min"], atol=0.2)
        # all faces non-degenerate
        tri = p[d["faces"]]
        area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        assert (area > 1e-9).all()
        # metric planar UVs on horizontal geometry (walls -- curbs, buildings, the plate
        # prisms' sides (riser), the boundary wall -- carry (along, height))
        if a["kind"] not in ("curb", "building", "boundary", "riser"):
            horiz = ue.gltf_to_model(d["normals"])[:, 2] > 0.99
            assert horiz.any()
            assert np.allclose(d["uvs"][horiz], np.column_stack([p[horiz, 0], -p[horiz, 1]]), atol=1e-3)
        # winding agrees with the declared normal on EVERY face (front face on the normal
        # side): Unreal culls the back face, so a wall wound the other way is invisible
        # from outside and lit from the wrong side where the material is two-sided
        n = ue.gltf_to_model(d["normals"])
        geo = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        assert ((geo * n[d["faces"][:, 0]]).sum(axis=1) > 0).all(), a["kind"]
    assert man["stats"]["assets"] == len(man["assets"])
    assert man["stats"]["spawn_points"] == len(man["spawn_points"]) > 0
    assert man["stats"]["triangles"] == sum(a["triangles"] for a in man["assets"])


def test_export_ue_tiles_and_layers(tmp_path):
    m = build_surfaces(synthetic.straight_road(length=300.0))
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=100.0, buildings=False)
    drivable = [a for a in man["assets"] if a["kind"] == "drivable"]
    assert len(drivable) >= 3  # a 300 m road crosses >= 3 tiles of 100 m
    assert {tuple(a["tile"]) for a in drivable} == {tuple(a["tile"]) for a in drivable}
    for a in drivable:
        i, j = a["tile"]
        lo, hi = a["bbox_ue"]["min"], a["bbox_ue"]["max"]
        assert lo[0] >= i * 100 * 100 - 1 and hi[0] <= (i + 1) * 100 * 100 + 1
        assert a["asset"] == "t_L0_drivable_%s_%s" % (str(i).replace("-", "m"), str(j).replace("-", "m"))
    assert not any(a["kind"] == "building" for a in man["assets"])
    # a bridge deck on layer 1 gets its own assets
    for r in m.roads:
        r.tags["layer"] = 1
    for s in m.surfaces:
        s.tags["layer"] = 1
    man2 = ue.export_ue(m, tmp_path / "ue2", name="t", tile_m=0.0, buildings=False)
    assert all(a["layer"] == 1 for a in man2["assets"] if a["kind"] == "drivable")
    assert 1 in man2["stats"]["layers"]  # curbs/markings without a layer stay on 0


def test_crossings_get_zebra_markings_and_road_material(tmp_path):
    m = build_surfaces(synthetic.four_way_junction())
    crossings = [s for s in m.surfaces if s.kind == "crossing"]
    if not crossings:
        pytest.skip("synthetic junction has no crossings")
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)
    by_kind = {a["kind"]: a for a in man["assets"]}
    assert by_kind["crossing"]["material"] == "road"
    # stripes were added to the white markings: more triangles than the line markings alone
    m2 = build_surfaces(synthetic.four_way_junction())
    m2.surfaces = [s for s in m2.surfaces if s.kind != "crossing"]
    man2 = ue.export_ue(m2, tmp_path / "ue2", name="t", tile_m=0.0, buildings=False)
    assert by_kind["marking_white"]["triangles"] > next(
        a for a in man2["assets"] if a["kind"] == "marking_white")["triangles"]


def test_buildings_clipped_against_the_drivable_network(tmp_path):
    m = build_surfaces(synthetic.straight_road(length=120.0))
    drivable = [s.geometry for s in m.surfaces if s.kind == "drivable"]
    minx, miny, maxx, maxy = drivable[0].bounds
    # one building square across the road, one canopy, one clear of the road
    m.buildings = [
        Building(id="onroad", footprint=box(50, miny - 2, 70, maxy + 2), levels=2),
        Building(id="canopy", footprint=box(80, miny - 2, 90, maxy + 2), levels=1,
                 tags={"building": "roof"}),
        Building(id="clear", footprint=box(20, maxy + 5, 40, maxy + 15), levels=2),
    ]
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0)
    assert man["stats"]["buildings_skipped"] == 1
    assert man["stats"]["buildings_clipped_by_roads"] == 1
    a = next(a for a in man["assets"] if a["kind"] == "building")
    d = ue.read_glb(tmp_path / "ue" / a["file"])
    p = ue.gltf_to_model(d["positions"])
    from shapely.geometry import MultiPoint
    pts = p[:, :2]
    # no building vertex inside the drivable band (0.25 m clearance)
    import shapely as _sh
    inside = _sh.contains_xy(drivable[0].buffer(0.2), pts[:, 0], pts[:, 1])
    assert not inside.any()
    # the clear building survives untouched
    assert pts[:, 1].max() >= maxy + 14


def test_manifest_buildings_array(tmp_path):
    """The manifest ``buildings`` array carries per-building footprint contours for the
    editor-side procedural building generator: clipped rings + the raw exterior, in UE cm."""
    m = build_surfaces(synthetic.straight_road(length=120.0))
    drivable = next(s.geometry for s in m.surfaces if s.kind == "drivable")
    minx, miny, maxx, maxy = drivable.bounds
    m.buildings = [
        Building(id="clear", footprint=box(20, maxy + 5, 40, maxy + 20), levels=3, osm_id=4242,
                 tags={"building": "residential"}),
        Building(id="canopy", footprint=box(-40, maxy + 5, -30, maxy + 15), levels=1,
                 tags={"building": "roof"}),
        Building(id="onroad", footprint=box(0, miny - 2, 20, maxy + 2), levels=2),
    ]
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0)
    bl = man["buildings"]
    # the canopy (building=roof) emits no geometry, so it gets no entry either;
    # ids: OSM id when the building carries one, else the index in model.buildings
    assert [e["id"] for e in bl] == [4242, 2]

    def shoelace(ring):
        a = np.asarray(ring, dtype=float)
        return 0.5 * float(np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1]))

    e = bl[0]
    # exact UE conversion ue = (x, -y) * 100 on the known box corners; open ring (4 points)
    y0, y1 = maxy + 5, maxy + 20
    corners = {(2000.0, round(-y0 * 100, 1)), (4000.0, round(-y0 * 100, 1)),
               (2000.0, round(-y1 * 100, 1)), (4000.0, round(-y1 * 100, 1))}
    (ring,) = e["rings_ue"]
    assert len(ring) == 4 and {tuple(p) for p in ring} == corners
    # Winding convention: CCW in the UE frame == POSITIVE shoelace area over (x_ue, y_ue).
    # (The y-flip of the conversion reverses shapely's CCW; the exporter re-orients the
    # model-frame ring CW so the converted ring's shoelace sign comes out positive.)
    # Matching the full box area also pins the vertex order to a simple traversal.
    assert shoelace(ring) == pytest.approx(2000.0 * 1500.0)
    assert e["raw_ring_ue"] == ring  # unclipped building: raw ring == clipped ring
    assert e["base_z_cm"] < e["roof_z_cm"]
    lh, _dl = ue._profile_building_rules()
    assert e["height_m"] == pytest.approx(3 * lh)
    assert e["roof_z_cm"] - e["base_z_cm"] == pytest.approx((3 * lh + ue.BUILDING_SINK) * 100,
                                                           abs=0.2)
    assert e["levels"] == 3 and e["category"] == "residential"

    # clipped building: rings_ue follows the road clip (two pieces astride the carriageway),
    # raw_ring_ue keeps the full raw footprint; every ring stays CCW (positive shoelace)
    e2 = bl[1]
    assert len(e2["rings_ue"]) == 2
    for r in e2["rings_ue"]:
        assert len(r) >= 3 and shoelace(r) > 0
    raw = {tuple(p) for p in e2["raw_ring_ue"]}
    assert raw == {(0.0, round(-(miny - 2) * 100, 1)), (2000.0, round(-(miny - 2) * 100, 1)),
                   (0.0, round(-(maxy + 2) * 100, 1)), (2000.0, round(-(maxy + 2) * 100, 1))}
    assert e2["category"] == "" and e2["levels"] == 2 and e2["base_z_cm"] < e2["roof_z_cm"]


def test_profile_building_rules():
    assert profiles.EU_DENSE.building.level_height_m == 3.2
    assert profiles.US_SUBURBAN.building.default_levels == 2
    with profiles.use("us_suburban"):
        assert ue._profile_building_rules() == (3.5, 2)


def test_elevation_puts_vertices_on_the_datum(tmp_path):
    m = build_surfaces(synthetic.straight_road(length=100.0))
    z = np.fromfunction(lambda j, i: 5.0 + 0.02 * i, (60, 60))
    m.elevation = Elevation(z, -150.0, -150.0, 5.0, 5.0)
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)
    a = next(a for a in man["assets"] if a["kind"] == "drivable")
    d = ue.read_glb(tmp_path / "ue" / a["file"])
    p = ue.gltf_to_model(d["positions"])
    expect = m.sample_z(p[:, 0], p[:, 1])
    assert np.allclose(p[:, 2], expect, atol=0.02)
    assert p[:, 2].max() - p[:, 2].min() > 0.3  # 0.4 % grade over the 100 m road


# --------------------------------------------------------------------------- zebra orientation

def _bar_cos_x(stripe) -> float:
    """|cos| of the stripe's long (bar) axis against the x axis."""
    r = stripe.minimum_rotated_rectangle
    c = np.asarray(r.exterior.coords)[:4]
    e0, e1 = c[1] - c[0], c[2] - c[1]
    e = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1
    return abs(e[0]) / np.hypot(*e)


def test_zebra_walking_hint_fixes_near_square_crossings():
    # 4 m crossing (x = along the road) over a 3.5 m carriageway: the min-rect long axis IS
    # the road axis, so the unhinted stripes flip to bars across the road — the rotated-zebra
    # bug seen on the baked Eixample. The walking hint (road left normal, y) fixes them.
    poly = box(0, 0, 4.0, 3.5)
    flipped = ue.zebra_stripes(poly)
    assert flipped and all(_bar_cos_x(s) < 0.05 for s in flipped)   # documents the failure mode
    fixed = ue.zebra_stripes(poly, along=np.array([0.0, 1.0]))
    assert fixed and all(_bar_cos_x(s) > 0.95 for s in fixed)       # bars parallel to the road
    # a clearly elongated crossing is not changed by a consistent hint
    wide = box(0, 0, 12, 4)
    a = ue.zebra_stripes(wide)
    b = ue.zebra_stripes(wide, along=np.array([1.0, 0.0]))
    assert len(a) == len(b) == 12
    assert sum(s.area for s in a) == pytest.approx(sum(s.area for s in b))
    for s in b:
        assert _bar_cos_x(s) < 0.05  # bars along y (across the walking direction)


# a real case from the v8 Eixample build (crossing_1 on road r52, residential, one 3.75 m
# lane): the crossing polygon is a 4 m x ~3.75 m near-square rotated ~44 deg, whose min-rect
# long axis lies ALONG the road — PCA alone laid the bars across it (90 deg off).
EIXAMPLE_ROTATED_CROSSING = Polygon([(-212.88, -173.18), (-215.77, -170.41),
                                     (-213.17, -167.71), (-210.29, -170.48)])
EIXAMPLE_CROSSED_ROAD = LineString([(-213.53, -167.36, 0.0), (-157.95, -220.77, 0.0)])


def _one_crossing_model(poly, ref) -> tuple[TwinModel, Surface]:
    m = TwinModel(name="x", origin_lat=41.39, origin_lon=2.16,
                  bbox_wgs84=(41.3905, 2.1630, 41.3945, 2.1690))
    m.roads.append(Road(id="r52", reference_line=ref, highway="residential",
                        lanes=[Lane(id=-1, type="driving", width=3.75)]))
    s = Surface(id="crossing_0", kind="crossing", geometry=poly, z_offset=0.003,
                road_ids=["r52"])
    m.surfaces.append(s)
    return m, s


def test_rotated_eixample_crossing_bars_run_along_the_road(tmp_path):
    m, s = _one_crossing_model(EIXAMPLE_ROTATED_CROSSING, EIXAMPLE_CROSSED_ROAD)
    d = np.asarray(EIXAMPLE_CROSSED_ROAD.coords)[1, :2] - np.asarray(EIXAMPLE_CROSSED_ROAD.coords)[0, :2]
    h = math.atan2(d[1], d[0])

    def dev_deg(stripe) -> float:
        r = stripe.minimum_rotated_rectangle
        c = np.asarray(r.exterior.coords)[:4]
        e0, e1 = c[1] - c[0], c[2] - c[1]
        e = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1
        dd = abs(math.atan2(e[1], e[0]) - h) % math.pi
        return math.degrees(min(dd, math.pi - dd))

    # the failure mode this pins down: unhinted stripes are ~90 deg off on this polygon
    unhinted = ue.zebra_stripes(EIXAMPLE_ROTATED_CROSSING)
    assert unhinted and min(dev_deg(st) for st in unhinted) > 45.0
    # the export path orients from the crossed road
    walk = ue.crossing_walk_dir(m, s, EIXAMPLE_ROTATED_CROSSING)
    assert walk is not None
    assert abs(walk @ (d / np.hypot(*d))) < 1e-6  # walking dir is the road normal
    hinted = ue.zebra_stripes(EIXAMPLE_ROTATED_CROSSING, along=walk)
    assert hinted and max(dev_deg(st) for st in hinted) <= 20.0
    # and export_ue itself writes those stripes into marking_white
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)
    a = next(a for a in man["assets"] if a["kind"] == "marking_white")
    g = ue.read_glb(tmp_path / "ue" / a["file"])
    # the bars are tessellated on the OVERLAY_GRID (so they can follow the road mesh): more
    # than one quad each, but exactly the stripes' area
    p = ue.gltf_to_model(g["positions"])
    tri = p[g["faces"]]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1).sum()
    assert len(g["faces"]) >= 2 * len(hinted)
    assert area == pytest.approx(sum(st.area for st in hinted), rel=0.01)


def test_crossing_walk_dir_none_without_a_road():
    m, s = _one_crossing_model(EIXAMPLE_ROTATED_CROSSING, EIXAMPLE_CROSSED_ROAD)
    s.road_ids = ["missing"]
    assert ue.crossing_walk_dir(m, s, EIXAMPLE_ROTATED_CROSSING) is None


# --------------------------------------------------------------------------- plate prisms

def test_curb_and_riser_faces_map_onto_the_curb_texture_band(tmp_path):
    """CARLA's curb texture is an atlas: the stone face is its bottom band, the rest is
    filler. Curb strips and plate risers must sample that band (like the stock SM_Curb),
    with one repeat per CURB_TEX_REPEAT_M along the face."""
    m = build_surfaces(synthetic.straight_road())
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)
    for kind in ("curb", "riser"):
        a = next(a for a in man["assets"] if a["kind"] == kind)
        d = ue.read_glb(tmp_path / "ue" / a["file"])
        p = ue.gltf_to_model(d["positions"])
        uv = d["uvs"]
        assert uv[:, 1].min() == pytest.approx(ue.CURB_TEX_V_TOP, abs=1e-4)
        assert uv[:, 1].max() == pytest.approx(ue.CURB_TEX_V_BOTTOM, abs=1e-4)
        # top edge of every face on the top of the band, bottom edge (>= band height below)
        # on its bottom row
        top = p[:, 2] >= p[:, 2].max() - 1e-6
        assert np.allclose(uv[top, 1], ue.CURB_TEX_V_TOP, atol=1e-4)
        # u advances one repeat per CURB_TEX_REPEAT_M of face length: quads are
        # (a_bottom, b_bottom, b_top, a_top), so consecutive bottom vertices span one segment
        q = p.reshape(-1, 4, 3)
        seg = np.linalg.norm(q[:, 1, :2] - q[:, 0, :2], axis=1)
        du = uv.reshape(-1, 4, 2)[:, 1, 0] - uv.reshape(-1, 4, 2)[:, 0, 0]
        assert np.allclose(du * ue.CURB_TEX_REPEAT_M, seg, rtol=1e-4, atol=1e-4)  # float32 UVs
    # a 15 cm curb face spans the whole band; a riser (down to -SKIRT_DROP) only its top
    a = next(a for a in man["assets"] if a["kind"] == "riser")
    d = ue.read_glb(tmp_path / "ue" / a["file"])
    p = ue.gltf_to_model(d["positions"])
    below = p[:, 2] < p[:, 2].max() - ue.CURB_TEX_BAND_M - 1e-6
    assert below.any() and np.allclose(d["uvs"][below, 1], ue.CURB_TEX_V_BOTTOM, atol=1e-4)


def test_raised_plates_carry_skirts_down_under_the_ground_slab(tmp_path):
    P = profiles.get()
    m = build_surfaces(synthetic.straight_road())
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)
    a = next(a for a in man["assets"] if a["kind"] == "curb")
    d = ue.read_glb(tmp_path / "ue" / a["file"])
    n = ue.gltf_to_model(d["normals"])
    # curb strips: all vertical, unit normals
    assert np.allclose(n[:, 2], 0.0) and np.allclose(np.linalg.norm(n, axis=1), 1.0)
    # the plates are closed prisms: their side walls (the riser assets, curb concrete) reach
    # from the raised top down under the ground slab (datum z = 0 here), along the whole perimeter
    a = next(a for a in man["assets"] if a["kind"] == "riser")
    assert a["material"] == "riser" and a["semantic"] == "SideWalk"
    d = ue.read_glb(tmp_path / "ue" / a["file"])
    p = ue.gltf_to_model(d["positions"])
    n = ue.gltf_to_model(d["normals"])
    wall = np.abs(n[:, 2]) < 1e-6
    assert wall.any() and np.allclose(np.linalg.norm(n[wall], axis=1), 1.0)
    assert p[wall][:, 2].max() == pytest.approx(P.sidewalk.z, abs=1e-6)
    assert p[wall][:, 2].min() == pytest.approx(-ue.SKIRT_DROP, abs=1e-6)
    assert ue.SKIRT_DROP > ue.GROUND_PLANE_DROP  # ends below the slab: no gap, no z-fight
    # wall length == perimeter of the (inset) plates: no edge left open
    plates = unary_union([s.geometry for s in m.surfaces if s.kind in ue.SKIRT_KINDS])
    rings = ue.plate_wall_rings(plates)
    tri = p[d["faces"]]
    wall_faces = np.abs(n[d["faces"][:, 0]][:, 2]) < 1e-6
    area = 0.5 * np.linalg.norm(np.cross(tri[wall_faces, 1] - tri[wall_faces, 0],
                                         tri[wall_faces, 2] - tri[wall_faces, 0]), axis=1).sum()
    # vertical rectangles of height top + drop along the whole perimeter (flat datum here)
    assert area == pytest.approx(sum(r.length for r in rings) * (P.sidewalk.z + ue.SKIRT_DROP), rel=0.02)


def test_sidewalk_top_sits_a_curb_height_above_the_road_edge(tmp_path):
    P = profiles.get()
    m = build_surfaces(synthetic.straight_road())
    man = ue.export_ue(m, tmp_path / "ue", name="t", tile_m=0.0, buildings=False)

    def pts(kind):
        out = []
        for a in man["assets"]:
            if a["kind"] == kind:
                out.append(ue.gltf_to_model(ue.read_glb(tmp_path / "ue" / a["file"])["positions"]))
        return np.concatenate(out)

    side, driv = pts("sidewalk"), pts("drivable")
    # only the plate tops (the sidewalk assets also carry the prism walls now)
    tops = []
    for a in man["assets"]:
        if a["kind"] == "sidewalk":
            g = ue.read_glb(tmp_path / "ue" / a["file"])
            nn = ue.gltf_to_model(g["normals"])
            tops.append(ue.gltf_to_model(g["positions"])[nn[:, 2] > 0.99])
    side = np.concatenate(tops)
    # for every sidewalk vertex near the carriageway, the top is exactly the curb height
    # above the nearest road vertex — the riser an ambulance sees at the curb
    d2 = ((side[:, None, :2] - driv[None, :, :2]) ** 2).sum(-1)
    nearest = d2.argmin(axis=1)
    near = d2[np.arange(len(side)), nearest] < 1.0
    assert near.sum() >= 4  # the unsubdivided synthetic band shares only its corner verts
    dz = side[near][:, 2] - driv[nearest[near], 2]
    assert np.allclose(dz, P.sidewalk.z, atol=1e-6)
    # crossings stay at road level (vehicles must not beach): their z offset is millimetres
    cross = [s for s in m.surfaces if s.kind == "crossing"]
    assert all(s.z_offset <= 0.01 for s in cross)
