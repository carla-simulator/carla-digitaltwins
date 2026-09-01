"""Worker D tests: imagery, elevation, refine — no network (fixtures under tests/fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Polygon, shape, box

from twinmodel.frame import LocalFrame
from twinmodel.model import Elevation, Surface, TwinModel
from twinmodel.ingest import imagery, elevation
from twinmodel.ingest.imagery import OrthoImage
from twinmodel import refine

FIX = Path(__file__).parent / "fixtures"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


@pytest.fixture(scope="module")
def ortho() -> OrthoImage:
    return OrthoImage.from_geotiff(FIX / "eixample_ortho_crop.tif")


@pytest.fixture(scope="module")
def prior():
    return shape(json.loads((FIX / "eixample_prior_crop.geojson").read_text())["geometry"])


@pytest.fixture(scope="module")
def dem() -> Elevation:
    return Elevation.from_npz(FIX / "eixample_dem_crop.npz")


# --------------------------------------------------------------------------- imagery

def test_ortho_fixture_geometry(ortho):
    assert ortho.array.dtype == np.uint8 and ortho.array.shape == (400, 400, 3)
    assert ortho.dx == ortho.dy == 0.25
    xmin, ymin, xmax, ymax = ortho.bounds()
    assert xmax - xmin == pytest.approx(100.0) and ymax - ymin == pytest.approx(100.0)
    # pixel-centre convention + rows increase with y
    r, c = ortho.xy_to_rc(ortho.x0, ortho.y0)
    assert r == pytest.approx(0) and c == pytest.approx(0)
    x, y = ortho.rc_to_xy(399, 399)
    assert x == pytest.approx(ortho.x0 + 399 * 0.25) and y == pytest.approx(ortho.y0 + 399 * 0.25)
    e = ortho.extent()
    assert e == (xmin, xmax, ymin, ymax)


def test_ortho_geotiff_roundtrip(ortho, tmp_path):
    frame = LocalFrame.from_bbox(*BBOX)
    p = ortho.save_geotiff(tmp_path / "o.tif", frame=frame)
    back = OrthoImage.from_geotiff(p)
    assert np.array_equal(back.array, ortho.array)
    assert (back.x0, back.y0, back.dx, back.dy) == (ortho.x0, ortho.y0, ortho.dx, ortho.dy)
    import rasterio
    with rasterio.open(p) as ds:
        assert "tmerc" in ds.crs.to_proj4()
        assert ds.transform.e < 0  # file is north-up


def test_model_grid_covers_bbox():
    frame = LocalFrame.from_bbox(*BBOX)
    x0, y0, w, h, t = imagery.model_grid(frame, BBOX, 0.25, pad_m=2.0)
    assert 2000 < w < 2100 and 1750 < h < 1850
    assert t.a == 0.25 and t.e == -0.25
    assert x0 == pytest.approx(t.c + 0.125)


def test_fetch_ortho_unreachable_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(imagery, "WMS_URL", "http://127.0.0.1:9/wms")
    monkeypatch.setattr(imagery, "REQUEST_TIMEOUT", 2)
    frame = LocalFrame.from_bbox(*BBOX)
    assert imagery.fetch_ortho(frame, BBOX, cache_dir=tmp_path) is None


# --------------------------------------------------------------------------- elevation

def test_elevation_fixture_sample(dem):
    assert dem.z.shape == (70, 70) and dem.dx == dem.dy == 2.0
    assert 15 < dem.z.min() < dem.z.max() < 45  # Eixample: 20-40 m a.s.l.
    # exact at grid nodes
    j, i = 10, 20
    assert dem.sample(dem.x0 + i * dem.dx, dem.y0 + j * dem.dy) == pytest.approx(dem.z[j, i])
    # bilinear midpoint
    mid = dem.sample(dem.x0 + (i + 0.5) * dem.dx, dem.y0 + j * dem.dy)
    assert mid == pytest.approx(0.5 * (dem.z[j, i] + dem.z[j, i + 1]))
    # arrays + clamping
    xs = np.array([dem.x0 - 100, dem.x0, dem.x0 + 1e6])
    zs = dem.sample(xs, np.full(3, dem.y0))
    assert zs.shape == (3,) and np.isfinite(zs).all()
    assert zs[0] == pytest.approx(dem.z[0, 0]) and zs[2] == pytest.approx(dem.z[0, -1])


def test_plane_fit_synthetic():
    xs = np.arange(50) * 2.0
    ys = np.arange(40) * 2.0
    gx, gy = np.meshgrid(xs, ys)
    z = 10 + 0.02 * gy - 0.01 * gx  # 2 % up to the north, 1 % down to the east -> uphill NNW
    st = elevation.plane_fit(Elevation(z, 0, 0, 2, 2))
    assert st["slope_pct"] == pytest.approx(100 * np.hypot(0.02, 0.01), rel=1e-3)
    assert st["uphill_azimuth_deg"] == pytest.approx(360 - np.degrees(np.arctan2(0.01, 0.02)), abs=0.1)
    assert st["uphill_toward"] in ("N", "NW")


def test_plane_fit_fixture_slopes_nw(dem):
    st = elevation.plane_fit(dem)
    assert 0.5 < st["slope_pct"] < 5.0
    assert st["uphill_toward"] in ("NW", "N", "W")


def test_fetch_dem_unreachable_returns_none(monkeypatch, tmp_path):
    for name in ("ICGC_MDT2M_WMS", "ICGC_TERR_WMS", "IGN_WCS"):
        monkeypatch.setattr(elevation, name, "http://127.0.0.1:9/x")
    monkeypatch.setattr(elevation, "GFI_TIMEOUT", 2)
    monkeypatch.delenv("OPENTOPO_API_KEY", raising=False)
    frame = LocalFrame.from_bbox(*BBOX)
    assert elevation.fetch_dem(frame, BBOX, cache_dir=tmp_path,
                               sources=["icgc_mdt2m", "icgc_terr", "opentopo", "ign_wcs"]) is None


def test_fetch_dem_cache_hit(monkeypatch, tmp_path, dem):
    frame = LocalFrame.from_bbox(*BBOX)
    dem.to_npz(elevation._cache_path(tmp_path, BBOX, 2.0))
    el = elevation.fetch_dem(frame, BBOX, cache_dir=tmp_path, sources=[])
    assert el is not None and np.array_equal(el.z, dem.z)


# --------------------------------------------------------------------------- refine

def test_road_mask_fraction(ortho, prior):
    mask = refine.road_mask(ortho, prior)
    assert mask.dtype == bool and mask.shape == ortho.array.shape[:2]
    assert 0.10 <= mask.mean() <= 0.50, mask.mean()
    # most of the prior core should be classified road, most of the far field not
    core = refine.rasterize(prior.buffer(-2.0), ortho)
    far = ~refine.rasterize(prior.buffer(8.0), ortho)
    assert mask[core].mean() > 0.6
    assert mask[far].mean() < 0.25


def test_road_mask_without_prior(ortho):
    mask = refine.road_mask(ortho, None)
    assert 0.05 <= mask.mean() <= 0.6


def test_mask_polygon_roundtrip(ortho, prior):
    mask = refine.road_mask(ortho, prior)
    poly = refine.mask_to_polygon(mask, ortho)
    assert poly.geom_type == "MultiPolygon" and poly.is_valid
    assert all(p.area >= refine.MIN_BLOB_M2 for p in poly.geoms)
    back = refine.rasterize(poly, ortho)
    inter = (back & mask).sum(); union = (back | mask).sum()
    assert inter / union > 0.9


def test_refine_drivable_bounds_and_topology(ortho, prior):
    mask = refine.road_mask(ortho, prior)
    refined, st = refine.refine_drivable(prior, mask, ortho, max_shift=2.5, min_lane_width=2.5)
    assert refined.is_valid and not refined.is_empty
    assert refine._n_parts(refined)[0] == refine._n_parts(prior)[0]
    assert st["max_abs_shift"] <= 2.5 + 1e-6
    # no boundary point further than max_shift from the prior boundary
    assert refined.boundary.hausdorff_distance(prior.boundary) <= 2.5 + ortho.dx
    assert st["iou_after"] >= st["iou_before"] - 0.02
    assert set(("iou_before", "iou_after", "mean_abs_shift", "n_vertices")) <= set(st)


def test_refine_drivable_never_below_min_width(ortho):
    # a 3.0 m wide strip against an all-false mask must not shrink below 2.5 m anywhere
    xmin, ymin, xmax, ymax = ortho.bounds()
    strip = box(xmin + 10, ymin + 40, xmax - 10, ymin + 43)
    mask = np.zeros(ortho.array.shape[:2], dtype=bool)
    mask[refine.rasterize(box(xmin + 10, ymin + 40.8, xmax - 10, ymin + 42.2), ortho)] = True
    refined, st = refine.refine_drivable(strip, mask, ortho, max_shift=2.5, min_lane_width=2.5)
    assert refined.is_valid
    # local width check: sample cross-sections
    for x in np.linspace(xmin + 15, xmax - 15, 10):
        cut = refined.intersection(box(x - 0.05, ymin, x + 0.05, ymax))
        assert cut.area / 0.1 >= 2.5 - 0.15


def test_refine_surfaces_updates_model(ortho, prior):
    m = TwinModel(name="t", origin_lat=0, origin_lon=0, bbox_wgs84=BBOX,
                  surfaces=[Surface(id="d0", kind="drivable", geometry=prior)])
    mask = refine.road_mask(ortho, prior)
    refine.refine_surfaces(m, mask, ortho)
    s = m.surfaces[0]
    assert s.source == "imagery" and s.geometry.is_valid
    assert "d0" in m.metadata["refine"]


def test_road_mask_sam_falls_back_without_checkpoint(ortho, prior, monkeypatch, tmp_path):
    monkeypatch.setenv("TWINMODEL_CACHE", str(tmp_path))
    monkeypatch.delenv("SAM_CHECKPOINT", raising=False)
    mask = refine.road_mask(ortho, prior, method="auto")
    assert 0.10 <= mask.mean() <= 0.50
