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

from . import profiles

Side = Literal["left", "right"]

# Distances come from the active profile (``profiles.get().streetspace``) at call time; the
# former module constants are served read-only through ``__getattr__`` for callers that
# still name them (``streetspace.FACE_SAMPLE_STEP_M``).
_LEGACY_NAMES = {
    "BUILDING_PAD_M": ("streetspace", "building_pad_m"),
    "MAX_FACE_DIST_M": ("streetspace", "max_face_dist_m"),
    "FACE_SAMPLE_STEP_M": ("streetspace", "face_sample_step_m"),
    "BLOCKER_MIN_DIST_M": ("streetspace", "blocker_min_dist_m"),
    "FACE_TOL_M": ("streetspace", "face_tol_m"),
}


def __getattr__(name: str):
    try:
        group, attr = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(getattr(profiles.get(), group), attr)


def building_union(model, pad: float | None = None):
    """Union of all building footprints (buffered by ``pad``, default the profile's
    ``streetspace.building_pad_m``), or an empty polygon."""
    if pad is None:
        pad = profiles.get().streetspace.building_pad_m
    geoms = [b.footprint.buffer(pad, join_style="mitre") for b in getattr(model, "buildings", [])]
    return unary_union(geoms) if geoms else Polygon()


def street_void(model, extent: Polygon | None = None, pad: float | None = None) -> Polygon | MultiPolygon:
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


def face_distances(line: LineString, buildings, side: Side, step: float | None = None,
                   max_dist: float | None = None, blockers=None,
                   s_range: tuple[float, float] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """For samples along ``line`` (every ``step`` m) cast a ray along the side normal and return
    (s_values, distances) to the first building face; ``nan`` where nothing is hit within
    ``max_dist``. Left = +90° from the line direction (OpenDRIVE convention).

    ``blockers`` (optional geometry, e.g. the other roads' centrelines): a ray that crosses a
    blocker before the face returns ``nan`` — the space up to that face belongs to the road in
    between (Passeig de Gràcia's laterals sit between the main carriageway and the buildings).
    ``s_range`` restricts the samples to that s-interval of the line. ``step`` / ``max_dist``
    default to the profile's ``streetspace.face_sample_step_m`` / ``max_face_dist_m``; a
    blocker hit closer than ``blocker_min_dist_m`` is the line itself (or its joint)."""
    S = profiles.get().streetspace
    step = S.face_sample_step_m if step is None else step
    max_dist = S.max_face_dist_m if max_dist is None else max_dist
    blocker_min = S.blocker_min_dist_m
    if s_range is None:
        s, pts, tang = _samples(line, step)
    else:
        s, pts, tang = _samples_in(line, step, *s_range)
    out = np.full(len(s), np.nan)
    if buildings is None or buildings.is_empty:
        return s, out
    prep = prepared.prep(buildings)
    prep_block = prepared.prep(blockers) if blockers is not None and not blockers.is_empty else None
    sign = 1.0 if side == "left" else -1.0
    for i, (p, (tx, ty)) in enumerate(zip(pts, tang)):
        nx, ny = -ty * sign, tx * sign
        ray = LineString([(p.x, p.y), (p.x + nx * max_dist, p.y + ny * max_dist)])
        if not prep.intersects(ray):
            continue
        hit = ray.intersection(buildings)
        if hit.is_empty:
            continue
        d = p.distance(hit)
        if prep_block is not None and prep_block.intersects(ray):
            bh = ray.intersection(blockers)
            if not bh.is_empty:
                db = min((p.distance(g) for g in getattr(bh, "geoms", [bh])
                          if p.distance(g) >= blocker_min), default=math.inf)
                if db < d:
                    continue
        out[i] = d
    return s, out


def _samples_in(line: LineString, step: float, s0: float, s1: float):
    """Like ``_samples`` but only between s0 and s1 (clamped to the line)."""
    s0 = max(0.0, min(line.length, s0))
    s1 = max(0.0, min(line.length, s1))
    if s1 <= s0:
        s0, s1 = 0.0, line.length
    n = max(2, int(math.ceil((s1 - s0) / step)) + 1)
    s = np.linspace(s0, s1, n)
    pts = [line.interpolate(v) for v in s]
    eps = min(0.5, line.length / 4)
    tang = []
    for v in s:
        a = line.interpolate(max(0.0, v - eps)); b = line.interpolate(min(line.length, v + eps))
        dx, dy = b.x - a.x, b.y - a.y
        norm = math.hypot(dx, dy) or 1.0
        tang.append((dx / norm, dy / norm))
    return s, pts, tang


def canyon_fraction(dists: np.ndarray) -> float:
    """Fraction of samples that hit a building face — 1.0 means a continuous street canyon."""
    return float(np.isfinite(dists).mean()) if len(dists) else 0.0


def robust_width(dists: np.ndarray, default: float) -> float:
    """Median of the finite distances, or ``default`` when fewer than 3 samples hit."""
    d = dists[np.isfinite(dists)]
    return float(np.median(d)) if len(d) >= 3 else default


def corner_void(centre: Point, buildings, radius: float | None = None) -> Polygon | MultiPolygon:
    """The open space around a junction node: a disc (``radius``, default the profile's
    ``junction.plaza_radius_m``) minus buildings. For an Eixample corner this is the
    chamfered octagon plus the four street arms."""
    if radius is None:
        radius = profiles.get().junction.plaza_radius_m
    disc = centre.buffer(radius, quad_segs=32)
    return disc if buildings is None or buildings.is_empty else disc.difference(buildings)


def canyon_extent(line: LineString, buildings, widths: tuple[float, float], step: float = 1.0,
                  tol: float | None = None, scan: float | None = None, blockers=None,
                  max_dist: float | None = None) -> tuple[float | None, float | None]:
    """s-interval of ``line`` along which *both* building faces are where the street's cross
    section says (within ``widths`` (left, right) + ``tol``): scanning inward from each end,
    the first sample where both rays hit within that distance. Beyond it the face recedes —
    at an Eixample corner the 45° chamfer starts there. -> (s_lo, s_hi), None per end when no
    such sample exists within ``scan`` m of that end (default: the whole line). ``tol``
    defaults to the profile's ``streetspace.face_tol_m`` (a face further out than the
    street's width + tol has ended: the chamfer)."""
    S = profiles.get().streetspace
    tol = S.face_tol_m if tol is None else tol
    max_dist = S.max_face_dist_m if max_dist is None else max_dist
    L = line.length
    scan = L if scan is None else min(L, scan)
    lim = (widths[0] + tol, widths[1] + tol)

    def ok_mask(s0: float, s1: float):
        s, dl = face_distances(line, buildings, "left", step, max_dist, blockers, (s0, s1))
        _, dr = face_distances(line, buildings, "right", step, max_dist, blockers, (s0, s1))
        return s, (np.isfinite(dl) & (dl <= lim[0]) & np.isfinite(dr) & (dr <= lim[1]))

    s, ok = ok_mask(0.0, scan)
    lo = float(s[np.argmax(ok)]) if ok.any() else None
    s, ok = ok_mask(L - scan, L)
    hi = float(s[len(ok) - 1 - np.argmax(ok[::-1])]) if ok.any() else None
    return lo, hi


def arm_corridor(end_xy: tuple[float, float], heading_in: float, half_width: float,
                 length: float, offset: float = 0.0) -> Polygon:
    """Street corridor of a junction arm: the segment from the arm's trimmed end ``length`` m
    along ``heading_in`` (radians, pointing into the junction), buffered by ``half_width``
    (flat caps). ``offset``: lateral shift (+left) of the street centre from ``end_xy``."""
    nx, ny = -math.sin(heading_in), math.cos(heading_in)
    x0, y0 = end_xy[0] + offset * nx, end_xy[1] + offset * ny
    seg = LineString([(x0, y0), (x0 + math.cos(heading_in) * length, y0 + math.sin(heading_in) * length)])
    return seg.buffer(half_width, cap_style="flat")


def junction_plaza(centre: Point, buildings, corridors: list[Polygon], radius: float | None = None
                   ) -> Polygon | MultiPolygon:
    """Open space of a junction: ``corner_void`` clipped to the convex hull of the arms' street
    corridors (each running from its arm's end into the junction). With the arms cut at the
    chamfer line the hull's diagonal edges run along the chamfer faces, so for an Eixample
    corner this is the chamfered octagon including the four corner triangles. Sidewalk bands
    along the faces are *not* subtracted (surfaces does that)."""
    if radius is None:
        radius = profiles.get().junction.plaza_radius_m
    void = corner_void(centre, buildings, radius)
    if not corridors:
        return void
    clip = unary_union(corridors).convex_hull
    out = void.intersection(clip)
    if isinstance(out, MultiPolygon):
        # keep the piece(s) that touch the centre region; a courtyard gap in the disc is not plaza
        near = [g for g in out.geoms if g.distance(centre) <= radius / 3]
        out = unary_union(near) if near else max(out.geoms, key=lambda g: g.area)
    return out
