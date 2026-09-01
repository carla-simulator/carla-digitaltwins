"""Tests for twinmodel.export.ue (glb + manifest bake export) on the synthetic models."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon, box

from twinmodel import profiles
from twinmodel.export import ue
from twinmodel.model import Building, Elevation
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
        # metric planar UVs on horizontal geometry
        if a["kind"] not in ("curb", "building"):
            assert np.allclose(d["uvs"], np.column_stack([p[:, 0], -p[:, 1]]), atol=1e-3)
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
