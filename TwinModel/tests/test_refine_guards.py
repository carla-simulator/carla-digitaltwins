"""refine_drivable part guards: low-coverage parts untouched, collapsed parts reverted,
lane keep-out. Synthetic 3 m strips on the cached Eixample ortho grid (no network)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString, MultiPolygon, box

from twinmodel import refine
from twinmodel.ingest.imagery import OrthoImage
from twinmodel.model import Lane, Road, TwinModel

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def ortho() -> OrthoImage:
    return OrthoImage.from_geotiff(FIX / "eixample_ortho_crop.tif")


def _strip(ortho, width=3.0, length=46.0):
    xmin, ymin, xmax, ymax = ortho.bounds()
    x0 = xmin + 20.0
    y0 = ymin + 40.0
    return box(x0, y0, x0 + length, y0 + width)


def _mask_of(ortho, geom):
    m = np.zeros(ortho.array.shape[:2], dtype=bool)
    m[refine.rasterize(geom, ortho)] = True
    return m


def test_low_coverage_part_is_left_untouched(ortho):
    strip = _strip(ortho)  # 46 x 3 m living street
    minx, miny, maxx, maxy = strip.bounds
    # sparse mask: six 3 x 1 m blobs -> ~13 % of the strip
    blobs = [box(minx + 3 + 7 * k, miny, minx + 4 + 7 * k, maxy) for k in range(6)]
    mask = _mask_of(ortho, MultiPolygon(blobs))
    cov = refine.part_coverage(strip, mask, ortho)
    assert 0.08 < cov < 0.2
    refined, st = refine.refine_drivable(strip, mask, ortho, max_shift=2.5, min_lane_width=2.5)
    assert st["n_parts"] == 1 and st["low_coverage_parts"] == 1
    assert st["part_reverted"] == ["low_coverage"]
    assert st["part_coverage"][0] == pytest.approx(cov, abs=1e-3)  # stats round to 3 dp
    assert refined.symmetric_difference(strip).area < 1e-6
    assert refined.area == pytest.approx(strip.area)


def test_two_parts_only_the_sparse_one_is_skipped(ortho):
    strip = _strip(ortho)
    minx, miny, maxx, maxy = strip.bounds
    other = box(minx, miny + 20, maxx, miny + 32)  # 46 x 12 m, fully under the mask
    mask = _mask_of(ortho, other)
    refined, st = refine.refine_drivable(MultiPolygon([strip, other]), mask, ortho)
    assert st["n_parts"] == 2 and st["low_coverage_parts"] == 1
    assert refined.geom_type == "MultiPolygon" and len(refined.geoms) == 2
    kept = min(refined.geoms, key=lambda p: p.area)
    assert kept.symmetric_difference(strip).area < 1e-6


def _fake_ring(shift):
    def fake(coords, *args, **kwargs):
        v = refine._densify_ring(np.asarray(coords, dtype=float)[:, :2], 0.5)
        return v, np.full(len(v), shift)
    return fake


def test_collapsed_part_reverts_on_area(ortho, monkeypatch):
    strip = _strip(ortho)
    mask = _mask_of(ortho, strip)  # full coverage: the part is refined
    # force a 1.2 m inward shift on every vertex: 3 m -> 0.6 m wide, area 20 %
    monkeypatch.setattr(refine, "_refine_ring", _fake_ring(-1.2))
    refined, st = refine.refine_drivable(strip, mask, ortho, min_lane_width=2.5)
    assert st["reverted_parts"] == 1 and st["part_reverted"] == ["area"]
    assert st["low_coverage_parts"] == 0
    assert refined.symmetric_difference(strip).area < 1e-6
    assert st["max_abs_shift"] == 0.0


def test_narrowed_part_reverts_on_min_width(ortho, monkeypatch):
    strip = _strip(ortho)
    mask = _mask_of(ortho, strip)
    # 0.35 m inward per side: area 77 % (passes the area rule) but 2.3 m < 2.5 m wide
    monkeypatch.setattr(refine, "_refine_ring", _fake_ring(-0.35))
    refined, st = refine.refine_drivable(strip, mask, ortho, min_lane_width=2.5)
    assert st["part_reverted"] == ["width"] and st["reverted_parts"] == 1
    assert refined.symmetric_difference(strip).area < 1e-6
    # the same shift on a 6 m strip is fine (5.3 m wide, 88 % area): accepted
    wide = _strip(ortho, width=6.0)
    refined, st = refine.refine_drivable(wide, _mask_of(ortho, wide), ortho, min_lane_width=2.5)
    assert st["reverted_parts"] == 0 and st["part_reverted"] == [None]
    assert refined.area < 0.9 * wide.area
    assert refine.min_width_ok(refined, 2.5)


def test_keep_out_blocks_shrinking_into_lane_centre(ortho):
    # 12 m wide strip whose mask stops 1.5 m inside each edge: without keep-out the boundary
    # shrinks ~1.5 m on each side (area 75 %, passes the guards); a lane centreline 1.5 m
    # inside the edge forbids it
    strip = _strip(ortho, width=12.0)
    minx, miny, maxx, maxy = strip.bounds
    mask = _mask_of(ortho, box(minx, miny + 1.5, maxx, maxy - 1.5))
    free, st0 = refine.refine_drivable(strip, mask, ortho)
    assert free.area < 0.85 * strip.area
    lane = LineString([(minx, miny + 1.5), (maxx, miny + 1.5)]).buffer(1.0)
    kept, st = refine.refine_drivable(strip, mask, ortho, keep=lane)
    assert st["n_keep_rejected"] > 0
    # the lane core (centreline +- 0.5 m) stays inside the surface; the free refinement cut it
    core = LineString([(minx + 1, miny + 1.5), (maxx - 1, miny + 1.5)]).buffer(0.5)
    assert kept.covers(core)
    assert not free.covers(core)
    assert kept.area > free.area


def test_lane_keep_out_from_model():
    m = TwinModel(name="t", origin_lat=0, origin_lon=0, bbox_wgs84=(0, 0, 1, 1))
    m.roads = [Road(id="r", reference_line=LineString([(0, 0, 0), (50, 0, 0)]),
                    lanes=[Lane(id=1, type="sidewalk", width=2.0),
                           Lane(id=-1, type="driving", width=3.0),
                           Lane(id=-2, type="driving", width=3.0),
                           Lane(id=-3, type="sidewalk", width=2.0)])]
    keep = refine.lane_keep_out(m, margin=1.0)
    # lane centres at y = -1.5 and y = -4.5, sidewalks excluded
    assert keep.covers(LineString([(1, -1.5), (49, -1.5)]))
    assert keep.covers(LineString([(1, -4.5), (49, -4.5)]))
    assert not keep.intersects(LineString([(1, 1.5), (49, 1.5)]))
    assert keep.bounds[1] == pytest.approx(-5.5) and keep.bounds[3] == pytest.approx(-0.5)
