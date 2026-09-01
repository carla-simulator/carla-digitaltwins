"""Tests for twinmodel.compare and twinmodel.ingest.osmtiles (no network)."""
from __future__ import annotations

import numpy as np
import pytest

from twinmodel.compare import (
    GridSpec, compose_masks, diff_masks, estimate_shift, read_mtl_colors, render_mesh_top,
    render_surfaces_top,
)
from twinmodel.export.mesh import MATERIALS, export_obj
from twinmodel.ingest.osmtiles import (
    OSM_ROAD_COLORS, lonlat_to_tile, road_mask_from_tiles, tile_bounds_3857, tile_range,
)
from twinmodel.surfaces import build_surfaces
from tests import synthetic

RES = 0.25


@pytest.fixture(scope="module")
def straight_build(tmp_path_factory):
    model = build_surfaces(synthetic.straight_road())
    d = tmp_path_factory.mktemp("straight")
    obj = d / "straight.obj"
    export_obj(model, obj)
    # grid: 140 x 40 m around the 120 m road (y in [-3.25, 3.25] drivable, sidewalks outside)
    grid = GridSpec(x0=-70 + RES / 2, y0=-20 + RES / 2, dx=RES, dy=RES, width=560, height=160)
    return model, obj, grid


def _px(grid: GridSpec, x: float, y: float) -> tuple[int, int]:
    return int(round((y - grid.y0) / grid.dy)), int(round((x - grid.x0) / grid.dx))


def test_render_mesh_top_groups_and_colors(straight_build):
    model, obj, grid = straight_build
    rgb, alpha, masks = render_mesh_top(obj, grid, with_masks=True)
    assert rgb.shape == (grid.height, grid.width, 3) and alpha.shape == (grid.height, grid.width)
    assert {"drivable", "sidewalk"} <= set(masks)
    # inside the lane (off the centre marking) is drivable, 4.5 m off-axis is sidewalk,
    # 19 m off is background (ground fill reaches 12 m beyond the sidewalk)
    r, c = _px(grid, 0.0, 1.5)
    assert masks["drivable"][r, c] and alpha[r, c] == 255
    r0, c0 = _px(grid, 0.0, 0.0)
    assert masks["marking_white"][r0, c0], "centre marking painted on top of drivable"
    r2, c2 = _px(grid, -30.0, -4.5)
    assert masks["sidewalk"][r2, c2] and not masks["drivable"][r2, c2]
    r3, c3 = _px(grid, 0.0, -19.0)
    assert alpha[r3, c3] == 0 and tuple(rgb[r3, c3]) == (0, 0, 0)
    # colours come from the .mtl written by export_obj
    kd = MATERIALS["drivable"]
    assert tuple(rgb[r, c]) == tuple(int(round(v * 255)) for v in kd)
    kd = MATERIALS["sidewalk"]
    assert tuple(rgb[r2, c2]) == tuple(int(round(v * 255)) for v in kd)
    # drivable area ~ 120 m x 6.5 m
    area = masks["drivable"].sum() * RES * RES
    assert 0.9 * 120 * 6.5 <= area <= 1.15 * 120 * 6.5


def test_render_mesh_top_crossing_over_drivable(straight_build):
    model, obj, grid = straight_build
    rgb, alpha, masks = render_mesh_top(obj, grid, with_masks=True)
    if "crossing" not in masks:
        pytest.skip("synthetic straight road produced no crossing surface")
    ys, xs = np.nonzero(masks["crossing"])
    # crossing sits on the carriageway (x ~ -60 + 72 = 12 m) and paints over drivable
    cx = grid.x0 + xs.mean() * grid.dx
    assert abs(cx - 12.0) < 3.0
    kd = tuple(int(round(v * 255)) for v in MATERIALS["crossing"])
    r, c = _px(grid, cx, 1.5)  # off the centre marking
    assert masks["crossing"][r, c] and masks["drivable"][r, c]
    assert tuple(rgb[r, c]) == kd


def test_render_surfaces_matches_mesh(straight_build):
    model, obj, grid = straight_build
    _, _, mm = render_mesh_top(obj, grid, with_masks=True)
    _, _, sm = render_surfaces_top(model, grid, with_masks=True)
    for g in ("drivable", "sidewalk"):
        inter = (mm[g] & sm[g]).sum()
        union = (mm[g] | sm[g]).sum()
        assert inter / union > 0.97, g


def test_grid_spec_from_tuple_and_transform():
    g = GridSpec.of((-10 + RES / 2, -5 + RES / 2, RES, RES, 80, 40))
    assert g.bounds() == pytest.approx((-10, -5, 10, 5))
    t = g.north_up_transform()
    assert (t.c, t.f) == pytest.approx((-10, 5)) and t.e == pytest.approx(-RES)


def test_read_mtl_colors(tmp_path):
    p = tmp_path / "x.mtl"
    p.write_text("newmtl drivable\nKd 0.5 0.25 1.0\nnewmtl weird\nKd 0 0 0\n")
    c = read_mtl_colors(p)
    assert c["drivable"] == (128, 64, 255) and c["weird"] == (0, 0, 0)
    assert c["sidewalk"] == read_mtl_colors(None)["sidewalk"]  # fallback kept


def _tile_image(h: int = 200, w: int = 200) -> np.ndarray:
    img = np.full((h, w, 3), (0xF2, 0xEF, 0xE9), dtype=np.uint8)  # carto land
    img[:, 80:100] = OSM_ROAD_COLORS["residential"]       # 5 m wide vertical road
    img[:, 78:80] = (0xBB, 0xBB, 0xBB); img[:, 100:102] = (0xBB, 0xBB, 0xBB)  # casing
    img[40:60, :] = OSM_ROAD_COLORS["primary"]           # horizontal primary
    img[120:180, 20:60] = (0xD9, 0xD0, 0xC9)             # building
    img[150:152, 120:180] = (255, 255, 255)              # 2 px text halo: must vanish
    img[10:12, 150:152] = (255, 255, 255)
    img[90:92, 85:95] = (0, 0, 0)                        # label ink on the road: must heal
    img[0:30, 160:190] = (0xE0, 0xDF, 0xDF)              # landuse=residential: not road
    img[100:120, 120:160] = (0xF2, 0xEF, 0xE9)           # bare land (5 from living_street)
    return img


def test_road_mask_from_tiles_synthetic():
    img = _tile_image()
    raw = road_mask_from_tiles(img, resolution=RES, open_m=0, close_m=0)
    assert raw[150, 150] and raw[10, 150]            # halos match the colour...
    mask = road_mask_from_tiles(img, resolution=RES)
    assert mask[100, 90] and mask[50, 150]           # road interiors
    assert not mask[150, 30]                         # building
    assert not mask[150, 150] and not mask[10, 150]  # ...but are removed by the opening
    assert mask[90, 90]                              # label ink healed by the closing
    assert not mask[15, 170]                         # landuse residential rejected
    assert not mask[110, 140]                        # bare land rejected
    assert not mask[100, 79]                         # casing is not fill
    # width of the vertical road ~ 20 px
    assert 17 <= mask[100, 60:120].sum() <= 21


def test_diff_stats_synthetic():
    a = np.zeros((100, 100), bool); b = np.zeros((100, 100), bool)
    a[20:60, 10:50] = True   # 1600 px
    b[40:80, 10:50] = True   # 1600 px, overlap 800
    rgb, alpha, st = diff_masks(a, b, RES * RES)
    assert st["iou"] == pytest.approx(800 / 2400)
    assert st["mesh_only_m2"] == pytest.approx(800 * RES * RES)
    assert st["osm_only_m2"] == pytest.approx(800 * RES * RES)
    assert st["agree_m2"] == pytest.approx(800 * RES * RES)
    assert tuple(rgb[50, 20]) == (150, 150, 150) and alpha[50, 20] == 255
    assert tuple(rgb[25, 20]) == (230, 40, 40)
    assert tuple(rgb[75, 20]) == (40, 80, 230)
    assert alpha[5, 5] == 0


def test_estimate_shift_recovers_translation():
    a = np.zeros((200, 200), bool)
    a[60:140, 40:60] = True; a[90:110, 20:180] = True
    b = np.roll(np.roll(a, 8, axis=1), -4, axis=0)  # +2 m in x, -1 m in y (rows increase with y)
    s = estimate_shift(a, b, RES, max_shift_m=5)
    assert s["dx_m"] == pytest.approx(2.0) and s["dy_m"] == pytest.approx(-1.0)
    assert s["peak_corr"] > s["zero_corr"]


def test_tile_maths():
    x, y = lonlat_to_tile(0.0, 0.0, 1)
    assert (x, y) == pytest.approx((1.0, 1.0))
    assert tile_bounds_3857(0, 0, 0) == pytest.approx(
        (-20037508.34, -20037508.34, 20037508.34, 20037508.34), rel=1e-6)
    x0, y0, x1, y1 = tile_range((41.3905, 2.1630, 41.3945, 2.1690), 19)
    assert (x0, y0, x1, y1) == (265293, 195804, 265303, 195814)


def test_compose_masks_draw_order():
    m = {"drivable": np.ones((4, 4), bool), "marking_white": np.zeros((4, 4), bool)}
    m["marking_white"][1, 1] = True
    rgb, alpha = compose_masks(m, {"drivable": (1, 1, 1), "marking_white": (9, 9, 9)}, (4, 4))
    assert tuple(rgb[1, 1]) == (9, 9, 9) and tuple(rgb[0, 0]) == (1, 1, 1) and alpha.all()
