"""Tests for twinmodel.export.mesh on the synthetic models."""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from twinmodel.export.mesh import export_obj, export_preview_png, triangulate_polygon, MATERIALS
from twinmodel.model import Elevation
from twinmodel.surfaces import build_surfaces
from tests import synthetic

CASES = list(synthetic.ALL_CASES.items())


@pytest.fixture(params=CASES, ids=[c[0] for c in CASES])
def model(request):
    _, factory = request.param
    return build_surfaces(factory())


def _parse_groups(path):
    """group name -> number of faces, straight from the file (trimesh merges by material)."""
    counts: dict[str, int] = {}
    group = None
    with open(path) as f:
        for line in f:
            if line.startswith("g "):
                group = line.split()[1]
            elif line.startswith("f "):
                counts[group] = counts.get(group, 0) + 1
    return counts


def _tri_areas(mesh: trimesh.Trimesh) -> np.ndarray:
    return mesh.area_faces


def test_triangulate_polygon_with_hole():
    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(3, 3), (3, 6), (6, 6), (6, 3)]])
    verts, faces = triangulate_polygon(poly)
    tri = verts[faces]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    area = 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    assert (area > 0).all()
    assert area.sum() == pytest.approx(poly.area, rel=1e-9)
    import shapely
    cent = tri.mean(axis=1)
    assert shapely.contains_xy(poly, cent[:, 0], cent[:, 1]).all()


def test_triangulate_concave():
    poly = Polygon([(0, 0), (10, 0), (10, 10), (5, 2), (0, 10)])
    verts, faces = triangulate_polygon(poly)
    tri = verts[faces]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    area = 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    assert area.sum() == pytest.approx(poly.area, rel=1e-9)


def test_export_obj(model, tmp_path):
    path = tmp_path / f"{model.name}.obj"
    export_obj(model, path)
    assert path.exists() and path.with_suffix(".mtl").exists()
    mtl = path.with_suffix(".mtl").read_text()
    for name in MATERIALS:
        assert f"newmtl {name}" in mtl

    groups = _parse_groups(path)
    for kind in ("drivable", "sidewalk", "curb", "marking_white"):
        assert groups.get(kind, 0) > 0, groups
    if model.surfaces_of("crossing"):
        assert groups.get("crossing", 0) > 0

    mesh = trimesh.load(path, force="mesh", process=False)
    assert isinstance(mesh, trimesh.Trimesh)
    assert len(mesh.faces) == sum(groups.values())
    assert np.isfinite(mesh.vertices).all()
    assert (_tri_areas(mesh) > 1e-9).all()
    # sidewalks sit 0.15 m above the road; curbs span both
    z = mesh.vertices[:, 2]
    assert z.min() == pytest.approx(0.0, abs=1e-6)
    assert z.max() == pytest.approx(0.15, abs=1e-6)

    scene = trimesh.load(path, force="scene", process=False)
    names = set(scene.geometry.keys())
    assert {"drivable", "sidewalk", "curb"} <= names


def test_export_obj_with_elevation(tmp_path):
    model = build_surfaces(synthetic.straight_road())
    # tilted plane: z = 0.02 * x
    xs = np.arange(-100, 101, 10.0)
    ys = np.arange(-50, 51, 10.0)
    zz = 0.02 * xs[None, :] + 0.0 * ys[:, None]
    model.elevation = Elevation(zz, xs[0], ys[0], 10.0, 10.0, source="synthetic")
    path = tmp_path / "elev.obj"
    export_obj(model, path)
    mesh = trimesh.load(path, force="mesh", process=False)
    assert np.isfinite(mesh.vertices).all()
    assert (_tri_areas(mesh) > 1e-9).all()
    v = mesh.vertices
    # z follows the plane (+ offsets up to 0.15)
    resid = v[:, 2] - 0.02 * v[:, 0]
    assert resid.min() >= -1e-6 and resid.max() <= 0.15 + 1e-6
    # subdivided: many more triangles than the flat export
    flat = tmp_path / "flat.obj"
    model.elevation = None
    export_obj(model, flat)
    assert len(mesh.faces) > 4 * len(trimesh.load(flat, force="mesh", process=False).faces)


def test_preview_png(model, tmp_path):
    path = tmp_path / f"{model.name}.png"
    export_preview_png(model, path)
    assert path.exists() and path.stat().st_size > 10_000


def test_preview_png_with_ortho(tmp_path):
    model = build_surfaces(synthetic.four_way_junction())
    ortho = (np.random.default_rng(0).random((64, 64, 3)) * 255).astype(np.uint8)
    path = tmp_path / "ortho.png"
    export_preview_png(model, path, ortho=ortho, extent=(-70, 70, -70, 70))
    assert path.exists() and path.stat().st_size > 10_000
