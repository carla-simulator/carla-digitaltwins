"""Street space from building footprints — shared helper for lanegraph (widths) and surfaces
(junction plazas, sidewalks to the building face, ground fill).

In a dense city the buildings delimit the street: everything between opposite building faces is
street space; the sidewalk is a band along the face; the rest is carriageway/plaza. These helpers
only *measure* — the callers decide when to trust the measurement (see ``canyon_fraction``).
All geometry in model space (metres).
"""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
from shapely import prepared
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

Side = Literal["left", "right"]

BUILDING_PAD_M = 0.3      # footprints are drawn slightly inside the real face
MAX_FACE_DIST_M = 40.0    # beyond this we don't call it a canyon
FACE_SAMPLE_STEP_M = 4.0


def building_union(model, pad: float = BUILDING_PAD_M):
    """Union of all building footprints (buffered by ``pad``), or an empty polygon."""
    geoms = [b.footprint.buffer(pad, join_style="mitre") for b in getattr(model, "buildings", [])]
    return unary_union(geoms) if geoms else Polygon()


def street_void(model, extent: Polygon | None = None, pad: float = BUILDING_PAD_M) -> Polygon | MultiPolygon:
    """Everything that is not building inside ``extent`` (default: bbox of roads + buildings, +30 m)."""
    if extent is None:
        geoms = [r.reference_line for r in model.roads] + [b.footprint for b in model.buildings]
        if not geoms:
            return Polygon()
        minx, miny, maxx, maxy = unary_union(geoms).bounds
        extent = box(minx - 30, miny - 30, maxx + 30, maxy + 30)
    return extent.difference(building_union(model, pad))


def _samples(line: LineString, step: float):
    n = max(2, int(math.ceil(line.length / step)) + 1)
    s = np.linspace(0.0, line.length, n)
    pts = [line.interpolate(v) for v in s]
    # tangent by central differences on the same parametrisation
    eps = min(0.5, line.length / 4)
    tang = []
    for v in s:
        a = line.interpolate(max(0.0, v - eps)); b = line.interpolate(min(line.length, v + eps))
        dx, dy = b.x - a.x, b.y - a.y
        norm = math.hypot(dx, dy) or 1.0
        tang.append((dx / norm, dy / norm))
    return s, pts, tang


def face_distances(line: LineString, buildings, side: Side, step: float = FACE_SAMPLE_STEP_M,
                   max_dist: float = MAX_FACE_DIST_M) -> tuple[np.ndarray, np.ndarray]:
    """For samples along ``line`` (every ``step`` m) cast a ray along the side normal and return
    (s_values, distances) to the first building face; ``nan`` where nothing is hit within
    ``max_dist``. Left = +90° from the line direction (OpenDRIVE convention)."""
    if buildings is None or buildings.is_empty:
        s, _, _ = _samples(line, step)
        return s, np.full(len(s), np.nan)
    prep = prepared.prep(buildings)
    s, pts, tang = _samples(line, step)
    sign = 1.0 if side == "left" else -1.0
    out = np.full(len(s), np.nan)
    for i, (p, (tx, ty)) in enumerate(zip(pts, tang)):
        nx, ny = -ty * sign, tx * sign
        ray = LineString([(p.x, p.y), (p.x + nx * max_dist, p.y + ny * max_dist)])
        if not prep.intersects(ray):
            continue
        hit = ray.intersection(buildings)
        if hit.is_empty:
            continue
        out[i] = p.distance(hit)
    return s, out


def canyon_fraction(dists: np.ndarray) -> float:
    """Fraction of samples that hit a building face — 1.0 means a continuous street canyon."""
    return float(np.isfinite(dists).mean()) if len(dists) else 0.0


def robust_width(dists: np.ndarray, default: float) -> float:
    """Median of the finite distances, or ``default`` when fewer than 3 samples hit."""
    d = dists[np.isfinite(dists)]
    return float(np.median(d)) if len(d) >= 3 else default


def corner_void(centre: Point, buildings, radius: float = 45.0) -> Polygon | MultiPolygon:
    """The open space around a junction node: a disc minus buildings. For an Eixample corner this
    is the chamfered octagon plus the four street arms."""
    disc = centre.buffer(radius, quad_segs=32)
    return disc if buildings is None or buildings.is_empty else disc.difference(buildings)
