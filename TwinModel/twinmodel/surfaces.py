"""Lane graph (+ optional refined mask) -> Surface polygons, junction polygons, curbs, markings.

Implements DESIGN.md §Surfaces. ``build_surfaces`` is idempotent: it clears the surfaces, curbs,
free-standing markings and (recomputed) junction polygons of the model before rebuilding them.

Geometry conventions: model space, metres. A road's reference line has lanes with positive ids on
its *left* (looking along the line) and negative ids on its right. The carriageway on one side is
the band between the reference line and the outer edge of the outermost carriageway-type lane
(driving/parking/biking/shoulder); sidewalk / verge lanes are bands at their own cumulative offset.

Every regional dimension (sidewalk height, curb height, crossing width, canyon thresholds, reach
clamps, marking colours ...) comes from the active :mod:`twinmodel.profiles` profile, read at
call time (``profiles.get()``); only numerical tolerances stay in this module.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import shapely
from shapely import wkt as shapely_wkt
from shapely.geometry import (GeometryCollection, LineString, MultiLineString, MultiPolygon,
                              Point, Polygon, box)
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, substring, unary_union

from . import profiles, streetspace
from .model import CurbLine, Junction, Lane, Marking, Road, Signal, Surface, TwinModel

log = logging.getLogger("twinmodel.surfaces")

CARRIAGEWAY_TYPES: tuple[str, ...] = ("driving", "parking", "biking", "shoulder")
RAISED_TYPES: tuple[str, ...] = ("sidewalk", "median", "verge")  # lane types raised above the datum
# numerical tolerances (not regional) --------------------------------------------------------
SIMPLIFY_TOL = 0.05
GRID = 0.001            # precision grid for all overlays (mm) -> robust shared boundaries
MITRE_LIMIT = 2.0
MIN_SURFACE_AREA = 0.5  # m^2, drop slivers below this
MIN_ISLAND_AREA = 2.0   # m^2, smaller drivable holes are filled instead of becoming islands
MAX_ISLAND_AREA = 400.0  # m^2, larger holes are city blocks (kept as holes, no island surface)
EDGE_MARKING_INSET = 0.10  # outermost edge lines are drawn this far inside the carriageway
ARM_PROBE_STEP = 1.0       # sampling step along a junction arm for canyon / chamfer detection
MIN_GROUND_AREA = 2.0
PLAZA_OPENING = 3.0        # drivable plaza features thinner than 2x this become sidewalk
# regional values live in twinmodel.profiles (P = profiles.get() at call time):
#   P.sidewalk.z / curb_height / verge_z            sidewalk, curb, planting-strip heights
#   P.crossing.width / z                            default zebra width, crossing lift
#   P.junction.plaza_radius_m / chamfer_scan_m / chamfer_allowance_m / plaza_sidewalk_m
#   P.streetspace.canyon_min_fraction / plaza_canyon_min_fraction / face_tol_m /
#                 face_sample_step_m / sidewalk_to_face_max_m / ground_reach_m
#   P.marking.*                                     default marking colours / width


# --------------------------------------------------------------------------- small helpers

def _ref2d(road: Road) -> LineString:
    return shapely.force_2d(road.reference_line)


def _polygonal(geom: BaseGeometry | None) -> Polygon | MultiPolygon:
    """Keep only the polygonal parts of a geometry (drop lines/points), return Polygon/Multi."""
    if geom is None or geom.is_empty:
        return Polygon()
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        parts = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return unary_union(parts) if parts else Polygon()
    return Polygon()


def _clean(geom: BaseGeometry | None, min_area: float = 0.0) -> Polygon | MultiPolygon:
    """make_valid + precision grid + drop tiny parts."""
    if geom is None or geom.is_empty:
        return Polygon()
    g = shapely.make_valid(geom)
    g = shapely.set_precision(g, GRID)
    g = _polygonal(g)
    if min_area > 0:
        parts = [p for p in _parts(g) if p.area >= min_area]
        g = unary_union(parts) if parts else Polygon()
        g = _polygonal(g)
    return g


def _parts(geom: BaseGeometry) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_parts(g))
        return out
    return []


def _lines(geom: BaseGeometry) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out: list[LineString] = []
        for g in geom.geoms:
            out.extend(_lines(g))
        return out
    return []


def _side_band(ref: LineString, inner: float, outer: float, left: bool) -> Polygon | MultiPolygon:
    """Band between offsets ``inner`` and ``outer`` (both >= 0) on one side of ``ref``."""
    if outer - inner <= 1e-6:
        return Polygon()
    sign = 1.0 if left else -1.0
    kw = dict(single_sided=True, join_style="mitre", mitre_limit=MITRE_LIMIT)
    outer_poly = ref.buffer(sign * outer, **kw)
    if inner <= 1e-6:
        return _clean(outer_poly)
    inner_poly = ref.buffer(sign * inner, **kw)
    return _clean(outer_poly.difference(inner_poly))


@dataclass
class _LaneBand:
    lane: Lane
    inner: float
    outer: float
    left: bool


def lane_bands(road: Road) -> list[_LaneBand]:
    """Cumulative offsets of every lane, ordered outward, per side."""
    bands: list[_LaneBand] = []
    for left, lanes in ((True, road.lanes_left()), (False, road.lanes_right())):
        cum = 0.0
        for lane in lanes:
            bands.append(_LaneBand(lane, cum, cum + lane.width, left))
            cum += lane.width
    return bands


def carriageway_extent(road: Road) -> tuple[float, float]:
    """(left, right) outer offset of the carriageway = outer edge of the outermost carriageway-
    type lane on each side (lanes of other types nested inside are included)."""
    wl = wr = 0.0
    for b in lane_bands(road):
        if b.lane.type in CARRIAGEWAY_TYPES:
            if b.left:
                wl = max(wl, b.outer)
            else:
                wr = max(wr, b.outer)
    return wl, wr


def carriageway_polygon(road: Road) -> Polygon | MultiPolygon:
    """Per-road carriageway polygon, flat caps, mitre joins, asymmetric widths."""
    ref = _ref2d(road)
    wl, wr = carriageway_extent(road)
    parts = []
    if wl > 0:
        parts.append(_side_band(ref, 0.0, wl, left=True))
    if wr > 0:
        parts.append(_side_band(ref, 0.0, wr, left=False))
    if not parts:
        return Polygon()
    return _clean(unary_union(parts))


def _end_cross_section(road: Road, at_end: bool) -> list[tuple[float, float]]:
    """End points of the carriageway cross-section at the start/end of the road."""
    c = np.asarray(_ref2d(road).coords)
    if len(c) < 2:
        return []
    p = c[-1] if at_end else c[0]
    d = (c[-1] - c[-2]) if at_end else (c[1] - c[0])
    nrm = np.linalg.norm(d)
    if nrm < 1e-9:
        return [tuple(p)]
    d = d / nrm
    n = np.array([-d[1], d[0]])
    wl, wr = carriageway_extent(road)
    return [tuple(p + n * wl), tuple(p), tuple(p - n * wr)]


def _lane_type_widths(road: Road, lane_type: str) -> tuple[float, float]:
    """Total width of lanes of ``lane_type`` per side (left, right)."""
    wl = sum(l.width for l in road.lanes if l.id > 0 and l.type == lane_type)
    wr = sum(l.width for l in road.lanes if l.id < 0 and l.type == lane_type)
    return wl, wr


def _sidewalk_widths(road: Road) -> tuple[float, float]:
    """Total sidewalk width per side (left, right)."""
    return _lane_type_widths(road, "sidewalk")


def _street_extent(road: Road) -> tuple[float, float]:
    """Total lane-graph street width per side (left, right), every lane type."""
    wl = sum(l.width for l in road.lanes if l.id > 0)
    wr = sum(l.width for l in road.lanes if l.id < 0)
    return wl, wr


def _keep_touching(geom: BaseGeometry, seed: BaseGeometry, tol: float = 0.05) -> Polygon | MultiPolygon:
    """Parts of ``geom`` that touch ``seed`` (within ``tol``)."""
    if geom.is_empty or seed is None or seed.is_empty:
        return Polygon()
    parts = [p for p in _parts(geom) if p.distance(seed) <= tol]
    return _clean(unary_union(parts)) if parts else Polygon()


def _model_bbox(model: TwinModel) -> Optional[Polygon]:
    """The model's WGS84 bbox as a model-space box (None when it cannot be projected)."""
    try:
        from .frame import LocalFrame
        south, west, north, east = model.bbox_wgs84
        frame = LocalFrame(model.origin_lat, model.origin_lon)
        x0, y0 = frame.to_local(west, south)
        x1, y1 = frame.to_local(east, north)
        return box(float(x0), float(y0), float(x1), float(y1))
    except Exception:  # pragma: no cover - pyproj missing / odd bbox
        return None


# --------------------------------------------------------------------------- street space

@dataclass
class _Arm:
    """One road entering/leaving a junction, seen from the junction outward. Per-side tuples
    are (left, right) of ``line_out``."""
    road: Road
    at_end: bool
    line_out: LineString           # oriented away from the junction
    p: np.ndarray                  # junction end (xy)
    u: np.ndarray                  # outward unit tangent at p
    half: tuple[float, float]      # lane-graph street half width per side
    cw: tuple[float, float]        # lane-graph carriageway edge offset per side
    face: tuple[float, float]      # median building-face distance per side (nan: no canyon)
    fraction: tuple[float, float]  # canyon fraction per side
    sidewalk: tuple[float, float]  # effective sidewalk width per side (carriageway edge -> face,
                                   # unclamped: a boulevard corner keeps its wide sidewalk)
    s_chamfer: float               # distance from p where the street canyon begins

    @property
    def bearing(self) -> float:
        return math.atan2(float(self.u[1]), float(self.u[0]))


def _road_faces(road: Road, buildings: BaseGeometry, step: float | None = None
                ) -> dict[bool, tuple[float, float]]:
    """{left: (canyon fraction, median face distance from the reference line)} per side."""
    if step is None:
        step = profiles.get().streetspace.face_sample_step_m
    out: dict[bool, tuple[float, float]] = {}
    ref = _ref2d(road)
    for left, side in ((True, "left"), (False, "right")):
        if buildings is None or buildings.is_empty or ref.length < 1e-6:
            out[left] = (0.0, float("nan"))
            continue
        _, d = streetspace.face_distances(ref, buildings, side, step=step)
        out[left] = (streetspace.canyon_fraction(d), streetspace.robust_width(d, float("nan")))
    return out


def effective_sidewalk(face: float, cw_edge: float, lane_width: float,
                       clamp: float | None | str = "profile") -> float:
    """Sidewalk width from the carriageway edge to the building face, never narrower than the
    lane graph's sidewalk lane and (``clamp``, default the profile's
    ``streetspace.sidewalk_to_face_max_m``; ``None`` = unclamped) never wider than that."""
    P = profiles.get()
    if clamp == "profile":
        clamp = P.streetspace.sidewalk_to_face_max_m
    if not np.isfinite(face):
        return max(lane_width, P.junction.plaza_sidewalk_m)
    w = max(lane_width, face - cw_edge)
    return float(min(clamp, w)) if clamp is not None else float(w)


def arm_info(road: Road, at_end: bool, buildings: BaseGeometry) -> _Arm:
    """Measure one junction arm against the buildings: lane-graph street widths, building-face
    distances per side over its first ``junction.chamfer_scan_m`` m, and the distance from the
    junction end at which both faces settle at their canyon width (the chamfer end)."""
    P = profiles.get()
    probe_length = P.junction.chamfer_scan_m
    ref = _ref2d(road)
    line_out = LineString(list(ref.coords)[::-1]) if at_end else ref
    c = np.asarray(line_out.coords, dtype=np.float64)
    p = c[0]
    d = c[1] - c[0]
    nrm = float(np.linalg.norm(d))
    u = d / nrm if nrm > 1e-9 else np.array([1.0, 0.0])
    L = float(line_out.length)
    probe = substring(line_out, 0.0, min(L, probe_length)) if L > probe_length else line_out
    tl, tr = _street_extent(road)
    cl, cr = carriageway_extent(road)
    sl, sr = _sidewalk_widths(road)
    half = (tr, tl) if at_end else (tl, tr)
    cw = (cr, cl) if at_end else (cl, cr)
    lane_sw = (sr, sl) if at_end else (sl, sr)
    face, frac, dists, s = [], [], [], None
    for side in ("left", "right"):
        if buildings is None or buildings.is_empty:
            s = streetspace._samples(probe, ARM_PROBE_STEP)[0]
            dd = np.full(len(s), np.nan)
        else:
            s, dd = streetspace.face_distances(probe, buildings, side, step=ARM_PROBE_STEP)
        fr = streetspace.canyon_fraction(dd)
        w = (streetspace.robust_width(dd, float("nan"))
             if fr >= P.streetspace.canyon_min_fraction else float("nan"))
        face.append(w)
        frac.append(fr)
        dists.append(dd)
    canyon_sides = [i for i in (0, 1) if np.isfinite(face[i])]
    s_chamfer = 0.0
    if canyon_sides:
        ok = np.ones(len(s), dtype=bool)
        for i in canyon_sides:
            ok &= np.isfinite(dists[i]) & (dists[i] <= face[i] + P.streetspace.face_tol_m)
        s_chamfer = min(L, P.junction.plaza_radius_m)
        for k in range(len(ok)):
            if ok[k:k + 3].all():
                s_chamfer = float(s[k])
                break
    sidewalk = tuple(effective_sidewalk(face[i], cw[i], lane_sw[i], clamp=None) for i in (0, 1))
    return _Arm(road, at_end, line_out, p, u, half, cw, (face[0], face[1]), (frac[0], frac[1]),
                sidewalk, s_chamfer)


def _parallel_groups(arms: list[_Arm], tol_deg: float = 30.0) -> list[list[_Arm]]:
    """Arms leaving the junction in (nearly) the same direction — a boulevard's central
    carriageway and its laterals."""
    groups: list[list[_Arm]] = []
    tol = math.radians(tol_deg)
    for a in arms:
        for g in groups:
            d = abs((a.bearing - g[0].bearing + math.pi) % (2 * math.pi) - math.pi)
            if d <= tol:
                g.append(a)
                break
        else:
            groups.append([a])
    return groups


def _band(line: LineString, w_left: float, w_right: float) -> BaseGeometry:
    kw = dict(single_sided=True, join_style="mitre", mitre_limit=MITRE_LIMIT)
    parts = []
    if w_left > 1e-6:
        parts.append(line.buffer(w_left, **kw))
    if w_right > 1e-6:
        parts.append(line.buffer(-w_right, **kw))
    return _clean(unary_union(parts)) if parts else Polygon()


def junction_plaza(model: TwinModel, j: Junction, arms: list[_Arm], buildings: BaseGeometry,
                   seed: BaseGeometry, connecting_ids: set[str]
                   ) -> Optional[tuple[Polygon | MultiPolygon, BaseGeometry]]:
    """The open space between the corner buildings of a junction (Eixample: the chamfered
    octagon) and the sidewalk keep-out inside it, or None when the arms are not in a canyon.

    Plaza = ``streetspace.corner_void`` (disc minus buildings) clipped to the arms' corridors —
    each arm's junction end extended through the centre and outward to its chamfer end,
    buffered generously on canyon sides (face + ``junction.chamfer_allowance_m``) and by the lane-graph
    width on open sides — minus every arm's street canyon beyond its chamfer end, minus the
    envelope of near-parallel arm groups (boulevard laterals: the plaza stops at their ends),
    minus the streets of any other road nearby; only the parts connected to ``seed`` (the
    convex cover of the arm ends).

    Keep-out = along every building face, a band as wide as the effective sidewalk of the arm
    side facing it (carriageway edge to face, clamped); the plaza minus the keep-out is the
    drivable octagon, plaza ∩ keep-out the chamfer sidewalks."""
    if len(arms) < 3 or buildings is None or buildings.is_empty:
        return None  # a bend / road change has no corner to open up
    P = profiles.get()
    plaza_radius = P.junction.plaza_radius_m
    chamfer_allowance = P.junction.chamfer_allowance_m
    fractions = [f for a in arms for f in a.fraction]
    if float(np.mean(fractions)) < P.streetspace.plaza_canyon_min_fraction:
        return None
    ctr = j.tags.get("centre")
    centre = (Point(float(ctr[0]), float(ctr[1])) if ctr
              else Point(*np.mean([a.p for a in arms], axis=0)))
    void = streetspace.corner_void(centre, buildings, plaza_radius)
    near = buildings.intersection(centre.buffer(plaza_radius + P.streetspace.sidewalk_to_face_max_m + 1.0))
    corridors: list[BaseGeometry] = []
    bands: list[BaseGeometry] = []
    keep: list[BaseGeometry] = []
    for a in arms:
        d_in = centre.distance(Point(a.p)) + 3.0
        seg = LineString([a.p - a.u * d_in, a.p + a.u * (a.s_chamfer + 1.0)])
        for i, sign in ((0, 1.0), (1, -1.0)):
            w = a.face[i] + chamfer_allowance if np.isfinite(a.face[i]) else a.half[i] + 1.0
            corr = seg.buffer(sign * w, single_sided=True)
            corridors.append(corr)
            if not near.is_empty:
                keep.append(near.buffer(a.sidewalk[i], join_style="mitre", mitre_limit=MITRE_LIMIT)
                            .intersection(corr))
        L = a.line_out.length
        if a.s_chamfer < L - 0.5:
            tail = substring(a.line_out, a.s_chamfer, L)
            bands.append(_band(tail, *[a.face[i] + 1.0 if np.isfinite(a.face[i]) else a.half[i] + 1.0
                                       for i in (0, 1)]))
    # corners: between two adjacent arms (by bearing) the sidewalk along the buildings is as
    # wide as the wider of the two facing sidewalks (a boulevard's corner wraps around)
    order = sorted(arms, key=lambda a: a.bearing)
    if len(order) >= 2 and not near.is_empty:
        for k, a in enumerate(order):
            b = order[(k + 1) % len(order)]
            gap = (b.bearing - a.bearing) % (2 * math.pi)
            if not (math.radians(30) <= gap <= math.radians(150)):
                continue
            e = max(a.sidewalk[0], b.sidewalk[1])  # a's left faces b, b's right faces a
            rr = plaza_radius + chamfer_allowance
            fan = [(centre.x, centre.y)] + [
                (centre.x + rr * math.cos(a.bearing + gap * t), centre.y + rr * math.sin(a.bearing + gap * t))
                for t in (0.0, 0.25, 0.5, 0.75, 1.0)]
            wedge = Polygon(fan)
            if wedge.is_valid and wedge.area > 0:
                keep.append(near.buffer(e, join_style="mitre", mitre_limit=MITRE_LIMIT).intersection(wedge))
    for group in _parallel_groups(arms):
        if len(group) < 2:
            continue
        env = shapely.convex_hull(unary_union([_band(a.line_out, a.half[0] + 1.0, a.half[1] + 1.0)
                                               for a in group]))
        bands.append(env)
    arm_ids = {a.road.id for a in arms}
    for r in model.roads:
        if r.id in arm_ids or r.id in connecting_ids or r.junction_id is not None:
            continue
        ref = _ref2d(r)
        if ref.is_empty or ref.distance(centre) > plaza_radius:
            continue
        tl, tr = _street_extent(r)
        bands.append(_band(ref, tl + 1.0, tr + 1.0))
    plaza = _clean(void.intersection(_clean(unary_union(corridors))))
    if bands:
        plaza = _clean(plaza.difference(_clean(unary_union(bands))), min_area=MIN_SURFACE_AREA)
    plaza = _keep_touching(plaza, seed.buffer(0.5))
    if plaza.is_empty:
        return None
    keep_out = _clean(unary_union(keep)) if keep else Polygon()
    return plaza, keep_out


# --------------------------------------------------------------------------- junctions

def _junction_roads(model: TwinModel, j: Junction) -> tuple[list[Road], list[tuple[Road, bool]]]:
    """(connecting roads, [(incoming/outgoing road, touches_at_end)]) of a junction."""
    conn_ids = {c.connecting_road for c in j.connections}
    conn_ids |= {r.id for r in model.roads if r.junction_id == j.id}
    connecting = [r for r in model.roads if r.id in conn_ids]

    ends: dict[str, Optional[bool]] = {}  # road id -> True (end), False (start), None (unknown)
    for r in model.roads:
        if r.id in conn_ids:
            continue
        if r.successor and r.successor.element == "junction" and r.successor.id == j.id:
            ends[r.id] = True
        if r.predecessor and r.predecessor.element == "junction" and r.predecessor.id == j.id:
            ends[r.id] = False
    for c in j.connections:
        ends.setdefault(c.incoming_road, None)
    for r in connecting:
        for link in (r.predecessor, r.successor):
            if link and link.element == "road" and link.id not in conn_ids:
                ends.setdefault(link.id, None if link.contact is None else link.contact == "end")

    # resolve unknown ends: the end nearest to the connecting roads / existing polygon
    anchor: BaseGeometry | None = None
    if connecting:
        anchor = unary_union([_ref2d(r) for r in connecting])
    elif j.polygon is not None:
        anchor = j.polygon
    out: list[tuple[Road, bool]] = []
    for rid, at_end in ends.items():
        try:
            r = model.road(rid)
        except KeyError:
            log.warning("junction %s references unknown road %s", j.id, rid)
            continue
        if at_end is None:
            if anchor is None:
                continue
            c = list(_ref2d(r).coords)
            at_end = anchor.distance(Point(c[-1])) <= anchor.distance(Point(c[0]))
        out.append((r, at_end))
    return connecting, out


def junction_cover_polygon(model: TwinModel, j: Junction,
                   carriageways: dict[str, Polygon | MultiPolygon],
                   cover: str = "convex") -> Optional[Polygon]:
    """Union of the connecting roads' carriageways and a cover of the incoming roads' end
    cross-sections so the polygon fully spans the space between the road ends.

    ``cover="convex"`` (default, DESIGN.md): convex hull of all end cross-sections and
    connecting-road samples — matches the Eixample octagon, may pave over a corner in front of an
    arm that sits far from the others. ``cover="adjacent"``: union of the hulls of consecutive
    arm pairs (by bearing) plus the polygon through the arm-end centres — hugs the arms, but gives
    lumpy concave edges where parallel arms (carriageway + lateral) enter on the same side."""
    connecting, incoming = _junction_roads(model, j)
    parts: list[BaseGeometry] = []
    centre_pts: list[tuple[float, float]] = []
    for r in connecting:
        cw = carriageways.get(r.id)
        if cw is not None and not cw.is_empty:
            parts.append(cw)
        centre_pts.extend(_ref2d(r).coords)
    sections = [(_end_cross_section(r, at_end), r) for r, at_end in incoming]
    sections = [(xs, r) for xs, r in sections if xs]
    for xs, _ in sections:
        centre_pts.extend(xs)
    if cover == "adjacent" and len(sections) >= 3:
        # concave-ish cover: hull of every pair of *adjacent* arms (sorted by bearing around
        # the junction centre) plus the polygon through all arm ends; avoids sweeping the
        # corner space in front of an arm that sits far from the others
        cx = np.mean([p[0] for xs, _ in sections for p in xs])
        cy = np.mean([p[1] for xs, _ in sections for p in xs])
        order = sorted(range(len(sections)), key=lambda i: math.atan2(
            sections[i][0][len(sections[i][0]) // 2][1] - cy,
            sections[i][0][len(sections[i][0]) // 2][0] - cx))
        for k in range(len(order)):
            a, b = sections[order[k]][0], sections[order[(k + 1) % len(order)]][0]
            hull = shapely.convex_hull(shapely.multipoints(a + b))
            if isinstance(hull, Polygon):
                parts.append(hull)
        centres = [xs[len(xs) // 2] for xs, _ in sections]
        centre_poly = shapely.convex_hull(shapely.multipoints(centres))
        if isinstance(centre_poly, Polygon):
            parts.append(centre_poly)
    elif len(centre_pts) >= 3:
        hull = shapely.convex_hull(shapely.multipoints(centre_pts))
        if isinstance(hull, Polygon):
            parts.append(hull)
    if j.polygon is not None and not j.polygon.is_empty:
        parts.append(j.polygon)
    if not parts:
        log.warning("junction %s: no geometry to build a polygon from", j.id)
        return None
    poly = _clean(unary_union(parts))
    if isinstance(poly, MultiPolygon):
        # disconnected pieces: bridge with the hull of everything
        poly = _clean(unary_union([poly, shapely.convex_hull(poly)]))
    if isinstance(poly, MultiPolygon):
        poly = max(poly.geoms, key=lambda p: p.area)
    if poly.is_empty:
        return None
    # fill holes (a junction interior is drivable through and through)
    return Polygon(poly.exterior)


def junction_polygon(model: TwinModel, j: Junction,
                     carriageways: dict[str, Polygon | MultiPolygon],
                     cover: str = "convex", plaza: BaseGeometry | None = None,
                     keep_out: BaseGeometry | None = None,
                     buildings: BaseGeometry | None = None) -> Optional[Polygon]:
    """Junction polygon: :func:`junction_cover_polygon`, and — when a building-derived ``plaza``
    (drivable part of the corner void) is given — that cover minus the sidewalk band
    ``keep_out`` along the buildings, united with the plaza and the connecting roads'
    carriageways. Holes are filled unless they hold a building."""
    base = junction_cover_polygon(model, j, carriageways, cover=cover)
    if plaza is None or plaza.is_empty or base is None:
        return base
    connecting, incoming = _junction_roads(model, j)
    parts: list[BaseGeometry] = [plaza]
    parts.append(base.difference(keep_out) if keep_out is not None and not keep_out.is_empty else base)
    for r in connecting:
        cw = carriageways.get(r.id)
        if cw is not None and not cw.is_empty:
            parts.append(cw)
    for r, _ in incoming:  # the arms' own carriageways stay junction wherever the cover had them
        cw = carriageways.get(r.id)
        if cw is not None and not cw.is_empty:
            parts.append(base.intersection(cw))
    poly = _clean(unary_union(parts))
    if isinstance(poly, MultiPolygon):
        poly = _clean(unary_union([poly, base]))
    if isinstance(poly, MultiPolygon):
        poly = _clean(unary_union([poly, shapely.convex_hull(poly)]))
    if isinstance(poly, MultiPolygon):
        poly = max(poly.geoms, key=lambda p: p.area)
    if poly.is_empty:
        return base
    holes = [ring for ring in poly.interiors
             if buildings is not None and not buildings.is_empty and Polygon(ring).intersects(buildings)]
    return Polygon(poly.exterior, holes)


# --------------------------------------------------------------------------- markings

def _default_marking(bands: list[_LaneBand], b: _LaneBand) -> Optional[Marking]:
    """Default marking on the outer edge of lane ``b`` when the lane graph gave none. Colours
    and width from the profile: ``marking.lane_color`` between same-direction lanes,
    ``marking.center_color`` between opposing lanes, ``marking.edge_color`` at the edge."""
    if b.lane.type != "driving":
        return None
    M = profiles.get().marking
    outer_neighbours = [o for o in bands if o.left == b.left and abs(o.inner - b.outer) < 1e-6]
    if outer_neighbours:
        o = outer_neighbours[0].lane
        if o.type == "driving":
            if o.direction == b.lane.direction:
                return Marking("broken", M.lane_color, M.width)
            return Marking("solid", M.center_color, M.width)
        if o.type in ("parking", "biking", "shoulder"):
            return Marking("solid", M.edge_color, M.width)
        return Marking("solid", M.edge_color, M.width)  # against sidewalk/median/none: edge line
    return Marking("solid", M.edge_color, M.width)  # road edge


def _default_center_marking(road: Road) -> Optional[Marking]:
    """Default marking on the reference line: ``marking.center_color`` between opposing
    driving lanes (suppressed when the profile's class says ``center_marking=False``, e.g. US
    residential streets), ``marking.lane_color`` between same-direction lanes, an edge line
    when the reference line is a carriageway edge."""
    P = profiles.get()
    M = P.marking
    left = [l for l in road.lanes_left() if l.type in CARRIAGEWAY_TYPES]
    right = [l for l in road.lanes_right() if l.type in CARRIAGEWAY_TYPES]
    if not left and not right:
        return None
    if not left or not right:
        return Marking("solid", M.edge_color, M.width)  # reference line is the carriageway edge
    li, ri = left[0], right[0]
    if li.type == "driving" and ri.type == "driving":
        if li.direction == ri.direction:
            return Marking("broken", M.lane_color, M.width)
        if road.highway and not P.lane.for_class(road.highway).center_marking:
            return None
        return Marking("solid", M.center_color, M.width)
    return Marking("solid", M.edge_color, M.width)


def road_markings(road: Road, default_markings: bool = True) -> list[Marking]:
    """Free-standing Marking geometries for one (non-connecting) road."""
    ref = _ref2d(road)
    bands = lane_bands(road)
    wl, wr = carriageway_extent(road)
    out: list[Marking] = []

    def offset(dist: float) -> list[LineString]:
        if abs(dist) < 1e-9:
            return [ref]
        g = ref.offset_curve(dist, join_style="mitre", mitre_limit=MITRE_LIMIT)
        return _lines(g)

    for b in bands:
        if b.lane.type not in CARRIAGEWAY_TYPES:
            continue
        mk = b.lane.marking
        if mk is None and default_markings:
            mk = _default_marking(bands, b)
        if mk is None:
            continue
        edge = b.outer
        is_outermost = abs(edge - (wl if b.left else wr)) < 1e-6
        if is_outermost:
            edge = max(0.0, edge - EDGE_MARKING_INSET)
        dist = edge if b.left else -edge
        for line in offset(dist):
            out.append(Marking(kind=mk.kind, color=mk.color, width=mk.width, geometry=line))
    cm = road.center_marking
    if cm is None and default_markings:
        cm = _default_center_marking(road)
    if cm is not None:
        # when the reference line is a carriageway edge, inset like an edge line
        if wl <= 1e-6 and wr > 0:
            lines = offset(-EDGE_MARKING_INSET)
        elif wr <= 1e-6 and wl > 0:
            lines = offset(EDGE_MARKING_INSET)
        else:
            lines = [ref]
        for line in lines:
            out.append(Marking(kind=cm.kind, color=cm.color, width=cm.width, geometry=line))
    return out


# --------------------------------------------------------------------------- crossings

def crossing_polygon(model: TwinModel, sig: Signal) -> Optional[Polygon]:
    try:
        road = model.road(sig.road_id)
    except KeyError:
        log.warning("crossing %s references unknown road %s", sig.id, sig.road_id)
        return None
    ref = _ref2d(road)
    default_width = profiles.get().crossing.width
    width = float(sig.tags.get("crossing:width", sig.tags.get("width", default_width)) or default_width)
    s = min(max(float(sig.s), 0.0), ref.length)
    p = np.asarray(ref.interpolate(s).coords[0])
    p1 = np.asarray(ref.interpolate(min(ref.length, s + 0.5)).coords[0])
    p0 = np.asarray(ref.interpolate(max(0.0, s - 0.5)).coords[0])
    d = p1 - p0
    if np.linalg.norm(d) < 1e-9:
        return None
    d = d / np.linalg.norm(d)
    n = np.array([-d[1], d[0]])
    wl, wr = carriageway_extent(road)
    if wl + wr <= 0:
        return None
    a, b = p + n * wl, p - n * wr
    h = d * (width / 2.0)
    return Polygon([a - h, b - h, b + h, a + h])


# --------------------------------------------------------------------------- curbs

def curb_lines(drivable: BaseGeometry, raised: BaseGeometry) -> list[LineString]:
    """Shared boundary between the drivable surface and the raised surfaces, merged and split
    into individual LineStrings."""
    if drivable.is_empty or raised.is_empty:
        return []
    shared = drivable.boundary.intersection(raised.boundary)
    lines = [l for l in _lines(shared) if l.length > 1e-3]
    if not lines:
        return []
    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    return [l for l in _lines(merged) if l.length > 1e-3]


# --------------------------------------------------------------------------- main entry

def build_surfaces(model: TwinModel,
                   refined_drivable: Polygon | MultiPolygon | None = None,
                   default_markings: bool = True,
                   junction_cover: str = "convex") -> TwinModel:
    """Fill ``model.surfaces``, ``model.curbs``, ``model.markings`` and every
    ``Junction.polygon`` from the lane graph. Mutates and returns ``model``. Idempotent.

    ``refined_drivable`` (from ``refine.py``) replaces the lane-graph drivable polygon (source
    ``imagery``; the lane-graph one is kept as WKT in ``metadata["surfaces"]``).
    ``default_markings``: synthesise edge/centre/lane markings where the lane graph has none.
    ``junction_cover``: see :func:`junction_polygon`."""
    model.surfaces = []
    model.curbs = []
    model.markings = []
    stats: dict = {}
    P = profiles.get()
    sidewalk_z = P.sidewalk.z
    verge_z = P.sidewalk.verge_z
    ground_z = P.sidewalk.z            # ground fill sits at curb-top level
    curb_height = P.sidewalk.curb_height
    max_sidewalk_reach = P.streetspace.sidewalk_to_face_max_m
    ground_reach = P.streetspace.ground_reach_m
    side_canyon_min = P.streetspace.canyon_min_fraction

    roads_by_id = {r.id: r for r in model.roads}
    connecting_ids = {r.id for r in model.roads if r.junction_id is not None}
    for j in model.junctions:
        connecting_ids |= {c.connecting_road for c in j.connections}

    # 1. carriageways -------------------------------------------------------------------------
    carriageways: dict[str, Polygon | MultiPolygon] = {}
    for r in model.roads:
        cw = carriageway_polygon(r)
        if not cw.is_empty:
            carriageways[r.id] = cw

    buildings = _clean(unary_union([b.footprint for b in model.buildings])) if model.buildings else Polygon()

    # junction polygons ----------------------------------------------------------------------
    # convex cover of the arm ends (DESIGN.md) — or, where the arms run in a street canyon, the
    # drivable part of the corner void between the buildings (Eixample: the chamfer octagon);
    # the band of width ``sw`` along the buildings inside that void becomes sidewalk
    junction_polys: dict[str, Polygon] = {}
    plaza_sidewalks: list[tuple[BaseGeometry, str]] = []
    keep_out_cache: dict[float, BaseGeometry] = {}
    n_plaza = 0
    for j in model.junctions:
        if j.tags.get("polygon_source") == "surfaces":
            j.polygon = None  # our own previous result is not an input (idempotent rebuilds)
        base = junction_cover_polygon(model, j, carriageways, cover=junction_cover)
        _, incoming = _junction_roads(model, j)
        plaza_drv = None
        source = "convex"
        if base is not None and not buildings.is_empty and incoming:
            widths = [w for r, _ in incoming for w in _sidewalk_widths(r) if w > 0]
            sw = round(max(widths) if widths else P.junction.plaza_sidewalk_m, 3)
            tag = j.tags.get("plaza_wkt")
            arms = [arm_info(r, at_end, buildings) for r, at_end in incoming]
            plaza = keep_out = None
            if tag:
                plaza = _clean(shapely_wkt.loads(tag))
                plaza = plaza if not plaza.is_empty else None
                source = "lanegraph"
                # keep-out: the widest effective sidewalk of the arms along every face
                sw = round(max([w for a in arms for w in a.sidewalk] or [sw]), 3)
                if sw not in keep_out_cache:
                    keep_out_cache[sw] = _clean(buildings.buffer(sw, join_style="mitre",
                                                                 mitre_limit=MITRE_LIMIT))
                keep_out = keep_out_cache[sw]
            else:
                res = junction_plaza(model, j, arms, buildings, base, connecting_ids)
                if res is not None:
                    plaza, keep_out = res
                source = "corner_void"
            if plaza is not None:
                plaza_drv = _clean(plaza.difference(keep_out))
                # spikes and slivers (a chamfer shoulder squeezed between the keep-out and an
                # arm) are sidewalk, not plaza; keep only what connects to the cover
                opened = _clean(plaza_drv.buffer(-PLAZA_OPENING, join_style="mitre")
                                .buffer(PLAZA_OPENING, join_style="mitre"))
                opened = _keep_touching(opened, base.buffer(0.5))
                plaza_sw = _clean(unary_union([plaza.intersection(keep_out),
                                               plaza_drv.difference(opened)]))
                plaza_drv = opened
                if not plaza_sw.is_empty:
                    plaza_sidewalks.append((plaza_sw, f"junction:{j.id}"))
                poly = junction_polygon(model, j, carriageways, cover=junction_cover,
                                        plaza=plaza_drv, keep_out=keep_out, buildings=buildings)
                n_plaza += 1
            else:
                poly = base
                source = "convex"
        else:
            poly = base
        j.tags["plaza_source"] = source
        j.tags["polygon_source"] = "surfaces"
        j.polygon = poly
        if poly is not None:
            junction_polys[j.id] = poly
    junction_union = _clean(unary_union(list(junction_polys.values()))) if junction_polys else Polygon()

    # 2. drivable ---------------------------------------------------------------------------
    lanegraph_drivable = _clean(unary_union(list(carriageways.values()) + list(junction_polys.values())))
    lanegraph_drivable = _clean(lanegraph_drivable.simplify(SIMPLIFY_TOL, preserve_topology=True))
    source = "osm_tags"
    if refined_drivable is not None and not refined_drivable.is_empty:
        drivable = _clean(refined_drivable.simplify(SIMPLIFY_TOL, preserve_topology=True))
        source = "imagery"
        inter = drivable.intersection(lanegraph_drivable).area
        union = drivable.union(lanegraph_drivable).area
        stats["refined_iou"] = inter / union if union > 0 else 0.0
        stats["lanegraph_drivable_wkt"] = lanegraph_drivable.wkt
    else:
        drivable = lanegraph_drivable

    # holes: tiny ones are filled; small building-free ones become traffic islands; the rest
    # (city blocks enclosed by a ring of roads) stay plain holes
    islands: list[Polygon] = []
    filled_parts: list[Polygon] = []
    for part in _parts(drivable):
        keep_holes = []
        for ring in part.interiors:
            hole = Polygon(ring)
            if hole.area < MIN_ISLAND_AREA:
                continue
            keep_holes.append(ring)
            if hole.area <= MAX_ISLAND_AREA and not hole.intersects(buildings):
                islands.append(hole)
        filled_parts.append(Polygon(part.exterior, keep_holes))
    drivable = _clean(unary_union(filled_parts)) if filled_parts else Polygon()

    def touching_roads(geom: BaseGeometry, candidates: Iterable[str]) -> list[str]:
        return [rid for rid in candidates if carriageways[rid].intersects(geom)]

    n = 0
    for part in _parts(drivable):
        rids = touching_roads(part, carriageways.keys())
        jids = [jid for jid, jp in junction_polys.items() if jp.intersects(part)]
        model.surfaces.append(Surface(
            id=f"drivable_{n}", kind="drivable", geometry=part, z_offset=0.0, source=source,
            road_ids=rids, junction_id=jids[0] if len(jids) == 1 else None,
            tags={"junction_ids": jids} if len(jids) > 1 else {}))
        n += 1
    # 3. sidewalks / medians / verges --------------------------------------------------------
    # a sidewalk lane is a band of its own width; in a street canyon (cross-section from the
    # buildings, or >= streetspace.canyon_min_fraction of the side faces a building) it runs
    # from the carriageway edge to the building face (never more than
    # streetspace.sidewalk_to_face_max_m). A verge lane (planting strip between the curb and
    # the sidewalk, US profiles) is a band of its own width at curb-top level.
    raised_parts: dict[str, list[tuple[BaseGeometry, str]]] = {"sidewalk": [], "median": [], "verge": []}
    n_face_sides = 0
    for r in model.roads:
        if r.id in connecting_ids:
            continue
        ref = _ref2d(r)
        bands = lane_bands(r)
        faces = None
        if not buildings.is_empty and any(b.lane.type == "sidewalk" for b in bands):
            faces = _road_faces(r, buildings)
        forced = r.tags.get("cross_section_source") == "buildings"
        for b in bands:
            if b.lane.type not in RAISED_TYPES:
                continue
            inner, outer = b.inner, b.outer
            if b.lane.type == "sidewalk" and faces is not None:
                fr, w = faces[b.left]
                if (forced or fr >= side_canyon_min) and np.isfinite(w):
                    reach = min(inner + max_sidewalk_reach, w + 1.0)
                    if reach > outer:
                        outer = reach
                        n_face_sides += 1
            band = _side_band(ref, inner, outer, b.left)
            if not band.is_empty:
                raised_parts[b.lane.type].append((band, r.id))

    # sidewalks around junctions: the band along the corner buildings inside the plaza, or
    # (no buildings around) a wrap of the junction polygon as wide as the arms' raised strip
    # (verge + sidewalk) so the corner apron reaches the sidewalk band
    plaza_ids = {rid for _, rid in plaza_sidewalks}
    raised_parts["sidewalk"].extend(plaza_sidewalks)
    for j in model.junctions:
        poly = junction_polys.get(j.id)
        if poly is None or f"junction:{j.id}" in plaza_ids:
            continue
        _, incoming = _junction_roads(model, j)
        widths = [w for r, _ in incoming for w in _sidewalk_widths(r) if w > 0]
        if not widths:
            continue
        w = max(widths)
        verges = [w for r, _ in incoming for w in _lane_type_widths(r, "verge") if w > 0]
        if verges:
            w = max(w, max(s + v for r, _ in incoming
                           for s, v in zip(_sidewalk_widths(r), _lane_type_widths(r, "verge"))))
        wrap = _clean(poly.buffer(w, join_style="mitre", mitre_limit=MITRE_LIMIT))
        raised_parts["sidewalk"].append((wrap, f"junction:{j.id}"))

    raised_union_parts: list[BaseGeometry] = []
    walk_parts: list[BaseGeometry] = []   # sidewalk + median
    verge_parts: list[BaseGeometry] = []
    sidewalk_so_far: BaseGeometry = Polygon()
    z_of = {"sidewalk": sidewalk_z, "median": sidewalk_z, "verge": verge_z}
    for kind in ("sidewalk", "median", "verge"):
        items = raised_parts[kind]
        if not items:
            continue
        geom = _clean(unary_union([g for g, _ in items]))
        geom = _clean(geom.difference(drivable))
        if not buildings.is_empty:
            geom = _clean(geom.difference(buildings))
        # close hairline gaps between neighbouring bands, then re-snap to the drivable boundary
        geom = _clean(geom.buffer(0.1, join_style="mitre").buffer(-0.1, join_style="mitre"))
        geom = _clean(geom.difference(drivable), min_area=MIN_SURFACE_AREA)
        if not buildings.is_empty:
            geom = _clean(geom.difference(buildings), min_area=MIN_SURFACE_AREA)
        if kind == "verge" and not sidewalk_so_far.is_empty:
            # sidewalk wins where the two meet (corner aprons): sidewalk ∩ verge = 0
            geom = _clean(geom.difference(sidewalk_so_far), min_area=MIN_SURFACE_AREA)
        for k, part in enumerate(_parts(geom)):
            rids = [rid for g, rid in items if not rid.startswith("junction:") and g.intersects(part)]
            jids = [rid.split(":", 1)[1] for g, rid in items if rid.startswith("junction:") and g.intersects(part)]
            model.surfaces.append(Surface(
                id=f"{kind}_{k}", kind=kind, geometry=part, z_offset=z_of[kind], source="osm_tags",
                road_ids=rids, junction_id=jids[0] if len(jids) == 1 else None,
                tags={"junction_ids": jids} if len(jids) > 1 else {}))
            raised_union_parts.append(part)
            (verge_parts if kind == "verge" else walk_parts).append(part)
        if kind == "sidewalk":
            sidewalk_so_far = geom

    # islands: whatever part of a small hole is not already sidewalk
    sidewalk_union = _clean(unary_union(raised_union_parts)) if raised_union_parts else Polygon()
    island_geoms: list[Polygon] = []
    for hole in islands:
        g = _clean(hole.difference(sidewalk_union), min_area=MIN_ISLAND_AREA)
        if not buildings.is_empty:
            g = _clean(g.difference(buildings), min_area=MIN_ISLAND_AREA)
        island_geoms.extend(_parts(g))
    for k, g in enumerate(island_geoms):
        model.surfaces.append(Surface(id=f"island_{k}", kind="island", geometry=g,
                                      z_offset=sidewalk_z, source=source))
    islands = island_geoms

    # 4. crossings ---------------------------------------------------------------------------
    k = 0
    for sig in model.signals:
        if sig.kind not in ("crosswalk", "crossing"):
            continue
        rect = crossing_polygon(model, sig)
        if rect is None:
            continue
        geom = _clean(rect.intersection(drivable))
        if geom.is_empty:
            continue
        model.surfaces.append(Surface(id=f"crossing_{k}", kind="crossing", geometry=geom,
                                      z_offset=P.crossing.z, source="osm_tags",
                                      road_ids=[sig.road_id], tags={"signal_id": sig.id}))
        k += 1

    # 5. ground: the street void near the surfaces that is neither drivable nor raised nor
    #    building (open lots, courtyard mouths, the strip beyond a short sidewalk); block
    #    interiors are enclosed by their buildings and stay empty
    raised_all = _clean(unary_union(raised_union_parts + islands)) if (raised_union_parts or islands) else Polygon()
    covered = _clean(unary_union([drivable, raised_all]))
    ground_area = 0.0
    if not covered.is_empty:
        minx, miny, maxx, maxy = unary_union([covered, buildings]).bounds
        extent = box(minx - ground_reach, miny - ground_reach, maxx + ground_reach, maxy + ground_reach)
        bbox = _model_bbox(model)
        if bbox is not None:
            extent = extent.intersection(bbox)
        void = _clean(extent.difference(buildings)) if not buildings.is_empty else extent
        reach = covered.buffer(ground_reach)
        ground = _clean(void.intersection(reach).difference(covered), min_area=MIN_GROUND_AREA)
        ground = _keep_touching(ground, covered)
        for k, part in enumerate(_parts(ground)):
            model.surfaces.append(Surface(id=f"ground_{k}", kind="ground", geometry=part,
                                          z_offset=ground_z, source="osm_tags", confidence=0.5))
            ground_area += part.area

    # 6. curbs (drivable <-> sidewalk/island/verge only), one pass per raised kind so a curb
    #    line is labelled by the surface it actually borders (an arm's verge curb and the
    #    sidewalk apron at the corner are separate lines)
    k = 0
    for high_kind, parts in (("sidewalk", walk_parts), ("island", islands), ("verge", verge_parts)):
        if not parts:
            continue
        raised_kind = _clean(unary_union(parts))
        for line in curb_lines(drivable, raised_kind):
            model.curbs.append(CurbLine(id=f"curb_{k}", geometry=line, height=curb_height,
                                        low_side_kind="drivable", high_side_kind=high_kind))
            k += 1

    # 7. markings (never inside junctions) ---------------------------------------------------
    clip_out = junction_union.buffer(0.05) if not junction_union.is_empty else None
    keep_in = drivable.buffer(0.05)
    for r in model.roads:
        if r.id in connecting_ids:
            continue
        for mk in road_markings(r, default_markings=default_markings):
            g = mk.geometry
            if clip_out is not None:
                g = g.difference(clip_out)
            g = g.intersection(keep_in)
            for line in _lines(g):
                if line.length > 0.3:
                    model.markings.append(Marking(kind=mk.kind, color=mk.color, width=mk.width,
                                                  geometry=line))

    stats.update({
        "profile": P.name,
        "drivable_area": float(drivable.area),
        "sidewalk_area": float(sum(s.geometry.area for s in model.surfaces_of("sidewalk"))),
        "verge_area": float(sum(s.geometry.area for s in model.surfaces_of("verge"))),
        "island_count": len(islands),
        "curb_length": float(sum(c.geometry.length for c in model.curbs)),
        "marking_count": len(model.markings),
        "junctions_with_polygon": len(junction_polys),
        "junctions_with_plaza": n_plaza,
        "sidewalk_sides_to_face": n_face_sides,
        "ground_area": float(ground_area),
        "drivable_source": source,
    })
    model.metadata.setdefault("surfaces", {}).update(stats)
    log.info("surfaces: drivable %.0f m2, sidewalk %.0f m2, ground %.0f m2, %d islands, curbs %.0f m, "
             "%d markings, %d/%d junction plazas from buildings",
             stats["drivable_area"], stats["sidewalk_area"], stats["ground_area"], stats["island_count"],
             stats["curb_length"], stats["marking_count"], n_plaza, len(junction_polys))
    return model
