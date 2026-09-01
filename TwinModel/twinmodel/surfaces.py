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
from shapely.ops import linemerge, nearest_points, substring, unary_union

from . import profiles, streetspace
from .model import (AUX_EPS, aux_span, aux_width_at, lane_present_at,
                    CurbLine, Junction, Lane, Marking, Road, Signal, Surface, TwinModel,
                    road_is_tunnel, road_osm_layer)

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
MIN_CHAMFER = 2.0          # an arm's face must recede at least this far past its end to open a corner
# regional values live in twinmodel.profiles (P = profiles.get() at call time):
#   P.sidewalk.z / curb_height / verge_z            sidewalk, curb, planting-strip heights
#   P.crossing.width / z                            default zebra width, crossing lift
#   P.junction.plaza_radius_m / chamfer_scan_m / chamfer_allowance_m / plaza_sidewalk_m
#   P.junction.cover / plaza_max_area_factor / corner_opening   cover construction, plaza cap,
#                                                   when a corner may open past the arm-end hull
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


def _aux_lanes(road: Road) -> list[Lane]:
    return [l for l in road.lanes if l.tags.get("aux")]


def carriageway_extent_at(road: Road, s: float) -> tuple[float, float]:
    """:func:`carriageway_extent` at ``s``: an auxiliary lane (``model.aux_span``) only counts
    with its width there, and the lanes outboard of it move in by the rest."""
    aux = _aux_lanes(road)
    if not aux:
        return carriageway_extent(road)
    wl, wr = carriageway_extent(road)
    for l in aux:
        if l.type in CARRIAGEWAY_TYPES:
            missing = l.width - aux_width_at(l, road, s)
            if l.id > 0:
                wl -= missing
            else:
                wr -= missing
    return max(0.0, wl), max(0.0, wr)


def _variable_offset(ref: LineString, s_vals: np.ndarray, t_vals: np.ndarray) -> np.ndarray:
    """Points at signed lateral offsets ``t_vals`` (left positive) at ``s_vals`` along ``ref``."""
    L = ref.length
    out = np.empty((len(s_vals), 2))
    for i, (s, t) in enumerate(zip(s_vals, t_vals)):
        s = min(max(float(s), 0.0), L)
        p = ref.interpolate(s)
        a, b = ref.interpolate(max(0.0, s - 0.5)), ref.interpolate(min(L, s + 0.5))
        dx, dy = b.x - a.x, b.y - a.y
        n = math.hypot(dx, dy) or 1.0
        out[i] = (p.x - t * dy / n, p.y + t * dx / n)
    return out


def aux_wedges(road: Road, step: float = 2.0) -> list[Polygon]:
    """The carriageway strips of the auxiliary lanes: between the carriageway edge *without*
    them and the edge with their width at every s (the shoulder outboard follows the taper).
    Empty for an ordinary road."""
    aux = [l for l in _aux_lanes(road) if l.type in CARRIAGEWAY_TYPES]
    if not aux:
        return []
    ref = _ref2d(road)
    wl, wr = carriageway_extent(road)
    base_l = wl - sum(l.width for l in aux if l.id > 0)
    base_r = wr - sum(l.width for l in aux if l.id < 0)
    out: list[Polygon] = []
    for left in (True, False):
        side = [l for l in aux if (l.id > 0) == left]
        if not side:
            continue
        s0 = min(aux_span(l, road)[0] for l in side)
        s1 = max(aux_span(l, road)[1] for l in side)
        if s1 - s0 <= AUX_EPS:
            continue
        cuts = {s0, s1}
        for l in side:
            cuts.update(aux_span(l, road))
            for key in ("taper_s0", "taper_s1"):
                if l.tags.get(key) is not None:
                    cuts.add(float(l.tags[key]))
        cuts = sorted(c for c in cuts if s0 - AUX_EPS <= c <= s1 + AUX_EPS)
        s_vals: list[float] = []
        for a, b in zip(cuts, cuts[1:]):
            n = max(1, int(math.ceil((b - a) / step)))
            s_vals.extend(np.linspace(a, b, n + 1)[:-1])
        s_vals.append(cuts[-1])
        s_arr = np.asarray(s_vals)
        base = base_l if left else base_r
        w = np.array([sum(aux_width_at(l, road, s) for l in side) for s in s_arr])
        sign = 1.0 if left else -1.0
        inner = _variable_offset(ref, s_arr, sign * base * np.ones_like(s_arr))
        outer = _variable_offset(ref, s_arr, sign * (base + w))
        ring = np.vstack([inner, outer[::-1]])
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            out.append(poly)
    return out


def carriageway_polygon(road: Road) -> Polygon | MultiPolygon:
    """Per-road carriageway polygon, flat caps, mitre joins, asymmetric widths. The strip of
    an auxiliary lane (freeway speed-change lane, lanegraph 7k) is added only where the lane
    exists, as wide as it is there (:func:`aux_wedges`)."""
    ref = _ref2d(road)
    aux = [l for l in _aux_lanes(road) if l.type in CARRIAGEWAY_TYPES]
    if aux:
        wl, wr = carriageway_extent(road)
        wl -= sum(l.width for l in aux if l.id > 0)
        wr -= sum(l.width for l in aux if l.id < 0)
    else:
        wl, wr = carriageway_extent(road)
    parts = []
    if wl > 0:
        parts.append(_side_band(ref, 0.0, wl, left=True))
    if wr > 0:
        parts.append(_side_band(ref, 0.0, wr, left=False))
    if aux:
        parts.extend(aux_wedges(road))
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
    wl, wr = carriageway_extent_at(road, float(_ref2d(road).length) if at_end else 0.0)
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


@dataclass
class _Envelope:
    """Bounds of a junction's open space, built from its arms (see :func:`junction_envelope`)."""
    centre: Point
    wmax: float                 # widest arm street width (incl. sidewalks)
    corridors: BaseGeometry     # union of the widened arm corridors
    hull: BaseGeometry          # convex hull of the arm-end cross-sections within plaza_radius_m
    envelope: BaseGeometry      # corridors | hull.buffer(chamfer_allowance_m)
    closed: BaseGeometry        # corner wedges that may not open past the hull (see junction_envelope)


def junction_envelope(j: Junction, arms: list[_Arm], buildings: BaseGeometry | None) -> Optional[_Envelope]:
    """Envelope a junction plaza may never leave, built by construction from the arms:

    * per arm, a corridor rectangle from its trimmed end along its axis to the junction
      centre's projection and on to the far side of the widest crossing arm (the approach
      capped at ``junction.plaza_radius_m``) and
      outward over its chamfer (``s_chamfer``), as wide as the arm's street (incl. sidewalks,
      or the building face where the arm is in a canyon) plus ``junction.chamfer_allowance_m``
      per side; the envelope is the UNION of these (never their convex hull), together with
    * the convex hull of the arm-end cross-sections (arms within ``plaza_radius_m``) buffered
      by the chamfer allowance.

    A corner wedge (between two adjacent arms by bearing) without any building within
    ``plaza_radius_m`` of the centre has nothing to open a chamfer towards: it is ``closed``
    and :func:`bound_plaza` keeps only the hull part there. Under
    ``junction.corner_opening == "recess"`` a wedge is also closed when both its arms run in
    their canyon right up to their ends (``s_chamfer`` <= ``MIN_CHAMFER``): a 90-degree corner
    never opens, a block-wide "plaza" along a swallowed internal street cannot form. Under
    ``"always"`` (EU_DENSE: Eixample arms are cut at the chamfer line, so the open corner shows
    no receding face) every wedge with a building face gets the full envelope."""
    if not arms:
        return None
    P = profiles.get()
    R = P.junction.plaza_radius_m
    allow = P.junction.chamfer_allowance_m
    ctr = j.tags.get("centre")
    centre = (Point(float(ctr[0]), float(ctr[1])) if ctr
              else Point(*np.mean([a.p for a in arms], axis=0)))
    c = np.array([centre.x, centre.y])
    wmax = max(sum(a.half) for a in arms)
    far = max(max(a.half) for a in arms) + P.junction.trim_margin_m
    corridors: list[BaseGeometry] = []
    ends: list[tuple[float, float]] = [(centre.x, centre.y)]
    for a in arms:
        d = float(np.linalg.norm(a.p - c))
        # the approach runs to the centre's projection on the arm's axis (an arm that enters
        # off-centre must not shoot past the junction), capped at the plaza radius
        inward = min(max(0.0, float(np.dot(c - a.p, -a.u))), R) + far
        outward = min(a.s_chamfer, R) + 1.0
        seg = LineString([a.p - a.u * inward, a.p + a.u * outward])
        w = [max(a.half[i], a.face[i] if np.isfinite(a.face[i]) else 0.0) + allow for i in (0, 1)]
        corridors.append(_band(seg, w[0], w[1]))
        if d <= R:
            n = np.array([-a.u[1], a.u[0]])
            ends.append(tuple(a.p + n * a.half[0]))
            ends.append(tuple(a.p - n * a.half[1]))
    hull = shapely.convex_hull(shapely.multipoints(ends))
    if not isinstance(hull, Polygon) or hull.is_empty:
        hull = centre.buffer(far)
    corr = _clean(unary_union(corridors))
    closed: list[Polygon] = []
    need_recess = P.junction.corner_opening == "recess"
    order = sorted(arms, key=lambda a: a.bearing)
    rr = R + allow + wmax
    disc = centre.buffer(R)
    for k, a in enumerate(order):
        b = order[(k + 1) % len(order)]
        gap = (b.bearing - a.bearing) % (2 * math.pi) if len(order) > 1 else 2 * math.pi
        if gap < math.radians(10):
            continue  # near-parallel arms (a boulevard's laterals) share no corner
        n = max(2, int(math.ceil(gap / math.radians(15))))
        fan = [(centre.x, centre.y)] + [
            (centre.x + rr * math.cos(a.bearing + gap * t / n), centre.y + rr * math.sin(a.bearing + gap * t / n))
            for t in range(n + 1)]
        wedge = Polygon(fan)
        if not wedge.is_valid or wedge.area <= 0:
            continue
        if (buildings is None or buildings.is_empty or not buildings.intersects(wedge.intersection(disc))
                or (need_recess and max(a.s_chamfer, b.s_chamfer) <= MIN_CHAMFER)):
            closed.append(wedge)
    envelope = _clean(unary_union([corr, hull.buffer(allow, join_style="mitre", mitre_limit=MITRE_LIMIT)]))
    return _Envelope(centre, wmax, corr, hull, envelope,
                     _clean(unary_union(closed)) if closed else Polygon())


def bound_plaza(plaza: Polygon | MultiPolygon | None, env: Optional[_Envelope]) -> Polygon | MultiPolygon | None:
    """``plaza`` clipped to the junction envelope and, inside closed corner wedges, to the
    arm-end hull. The input object is returned untouched when nothing is cut, so a plaza that
    already lies inside its envelope (every Eixample corner) rebuilds byte-identically."""
    if env is None or plaza is None or plaza.is_empty:
        return plaza
    out = plaza.intersection(env.envelope)
    if not env.closed.is_empty:
        inside = out.intersection(env.closed)
        out = unary_union([out.difference(env.closed), inside.intersection(env.hull)])
    out = _clean(out)
    if abs(out.area - plaza.area) < 1e-6:
        return plaza
    return out


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


def _arm_inward(road: Road, at_end: bool) -> Optional[np.ndarray]:
    """Unit tangent at the road's junction end pointing INTO the junction."""
    c = np.asarray(_ref2d(road).coords, dtype=np.float64)
    if len(c) < 2:
        return None
    d = (c[-1] - c[-2]) if at_end else (c[0] - c[1])
    n = float(np.linalg.norm(d))
    return d / n if n > 1e-9 else None


def _bridge_parts(poly: MultiPolygon, width: float) -> Polygon | MultiPolygon:
    """Join the parts of a multipolygon with flat corridors of ``width`` between nearest points
    (largest part first)."""
    parts = sorted(_parts(poly), key=lambda g: g.area, reverse=True)
    out: BaseGeometry = parts[0]
    for g in parts[1:]:
        a, b = nearest_points(out, g)
        link = LineString([a, b]).buffer(max(width, 0.5), cap_style="flat") if a.distance(b) > 1e-6 else Polygon()
        out = _clean(unary_union([out, g, link]))
    return out


def junction_cover_polygon(model: TwinModel, j: Junction,
                   carriageways: dict[str, Polygon | MultiPolygon],
                   cover: str = "convex", buildings: BaseGeometry | None = None) -> Optional[Polygon]:
    """Union of the connecting roads' carriageways and a cover of the incoming roads' end
    cross-sections so the polygon fully spans the space between the road ends.

    ``cover="convex"`` (DESIGN.md, ``profiles.EU_DENSE``): convex hull of all end cross-sections
    and connecting-road samples — matches the Eixample octagon, may pave over a corner in front
    of an arm that sits far from the others. ``cover="adjacent"``: union of the hulls of
    consecutive arm pairs (by bearing) plus the polygon through the arm-end centres — hugs the
    arms, but gives lumpy concave edges where parallel arms (carriageway + lateral) enter on the
    same side. ``cover="bounded"`` (US profiles): no hull at all — every arm's carriageway is
    extruded into the junction (through the centre to the far side of the widest crossing arm,
    the approach capped at ``junction.plaza_radius_m``, never over a ``buildings`` footprint)
    and the connecting roads' carriageways carry the rest; holes are filled unless they hold a
    building. This is what a 40–60 m cluster that swallowed internal ways needs: the hull of
    arm ends 80 m apart would pave the whole block."""
    connecting, incoming = _junction_roads(model, j)
    parts: list[BaseGeometry] = []
    centre_pts: list[tuple[float, float]] = []
    for r in connecting:
        cw = carriageways.get(r.id)
        if cw is not None and not cw.is_empty:
            parts.append(cw)
        centre_pts.extend(_ref2d(r).coords)
    sections3 = [(_end_cross_section(r, at_end), r, at_end) for r, at_end in incoming]
    sections = [(xs, r) for xs, r, _ in sections3 if xs]
    for xs, _ in sections:
        centre_pts.extend(xs)
    if cover == "bounded" and sections:
        P = profiles.get()
        ctr = j.tags.get("centre")
        centre = (np.array([float(ctr[0]), float(ctr[1])]) if ctr
                  else np.mean([xs[len(xs) // 2] for xs, _ in sections], axis=0))
        far = max(max(carriageway_extent(r)) for _, r in sections) + P.junction.trim_margin_m
        has_bld = buildings is not None and not buildings.is_empty
        for xs, r, at_end in sections3:
            u = _arm_inward(r, at_end) if xs else None
            if u is None:
                continue
            a, b = np.asarray(xs[0]), np.asarray(xs[-1])
            mid = np.asarray(xs[len(xs) // 2])
            # to the centre's projection on the arm's axis (not past the junction when the arm
            # enters off-centre), capped at the plaza radius, plus the far side's half width
            L = min(max(0.0, float(np.dot(centre - mid, u))), P.junction.plaza_radius_m) + far
            rect = Polygon([a, b, b + u * L, a + u * L]) if float(np.linalg.norm(a - b)) > 1e-6 else Polygon()
            if rect.is_empty or not rect.is_valid:
                continue
            if has_bld:
                rect = _keep_touching(_clean(rect.difference(buildings)), LineString([a, b]))
            if not rect.is_empty:
                parts.append(rect)
        if len(sections) == 1 and not connecting:
            parts.append(Polygon(sections[0][0]).buffer(far) if len(sections[0][0]) >= 3 else
                         Point(*sections[0][0][0]).buffer(far))
    elif cover == "adjacent" and len(sections) >= 3:
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
    if isinstance(poly, MultiPolygon) and cover == "bounded":
        # disconnected pieces: link them with corridors as wide as the widest arm
        poly = _bridge_parts(poly, max(max(carriageway_extent(r)) for _, r in sections) if sections else 1.0)
    if isinstance(poly, MultiPolygon):
        # disconnected pieces: bridge with the hull of everything
        poly = _clean(unary_union([poly, shapely.convex_hull(poly)]))
    if isinstance(poly, MultiPolygon):
        poly = max(poly.geoms, key=lambda p: p.area)
    if poly.is_empty:
        return None
    # fill holes (a junction interior is drivable through and through) unless one holds a building
    holes = [ring for ring in poly.interiors
             if buildings is not None and not buildings.is_empty and Polygon(ring).intersects(buildings)]
    return Polygon(poly.exterior, holes)


def junction_polygon(model: TwinModel, j: Junction,
                     carriageways: dict[str, Polygon | MultiPolygon],
                     cover: str = "convex", plaza: BaseGeometry | None = None,
                     keep_out: BaseGeometry | None = None,
                     buildings: BaseGeometry | None = None) -> Optional[Polygon]:
    """Junction polygon: :func:`junction_cover_polygon`, and — when a building-derived ``plaza``
    (drivable part of the corner void) is given — that cover minus the sidewalk band
    ``keep_out`` along the buildings, united with the plaza and the connecting roads'
    carriageways. Holes are filled unless they hold a building."""
    base = junction_cover_polygon(model, j, carriageways, cover=cover,
                                  buildings=buildings if cover == "bounded" else None)
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
    if road.tags.get("parking_aisle"):
        return []  # a lot aisle carries no centre, lane or edge line (profiles.ParkingAisleRules)
    ref = _ref2d(road)
    bands = lane_bands(road)
    wl, wr = carriageway_extent(road)
    out: list[Marking] = []

    def offset(dist: float) -> list[LineString]:
        if abs(dist) < 1e-9:
            return [ref]
        g = ref.offset_curve(dist, join_style="mitre", mitre_limit=MITRE_LIMIT)
        return _lines(g)

    aux = [b for b in bands if b.lane.tags.get("aux")]

    def aux_outboard(b: _LaneBand) -> list[_LaneBand]:
        return [o for o in aux if o.left == b.left and o.inner >= b.outer - 1e-6]

    def clip_span(line: LineString, s0: float, s1: float, keep_inside: bool) -> list[LineString]:
        """The part(s) of an offset line whose reference s lies inside / outside [s0, s1]."""
        a = line.project(ref.interpolate(min(s0, ref.length)))
        c = line.project(ref.interpolate(min(s1, ref.length)))
        a, c = min(a, c), max(a, c)
        if keep_inside:
            seg = substring(line, a, c)
            return [seg] if isinstance(seg, LineString) and seg.length > 1e-6 else []
        parts = []
        if a > 1e-6:
            parts.append(substring(line, 0.0, a))
        if line.length - c > 1e-6:
            parts.append(substring(line, c, line.length))
        return [q for q in parts if isinstance(q, LineString) and q.length > 1e-6]

    for b in bands:
        if b.lane.type not in CARRIAGEWAY_TYPES:
            continue
        mk = b.lane.marking
        if mk is None and default_markings:
            mk = _default_marking(bands, b)
        if mk is None:
            continue
        if b.lane.tags.get("aux"):
            # the outer edge of a speed-change lane follows its width; lanes outboard of it
            # (its shoulder) carry no line
            s0, s1 = aux_span(b.lane, road)
            n = max(2, int(math.ceil((s1 - s0) / 2.0)) + 1)
            s_arr = np.linspace(s0, s1, n)
            inboard_aux = [o for o in aux if o.left == b.left and o.outer <= b.inner + 1e-6]
            base = b.inner - sum(o.lane.width for o in inboard_aux)
            t = np.array([base + sum(aux_width_at(o.lane, road, s) for o in inboard_aux)
                          + aux_width_at(b.lane, road, s) for s in s_arr])
            pts = _variable_offset(ref, s_arr, (1.0 if b.left else -1.0) * t)
            if len(pts) >= 2:
                out.append(Marking(kind=mk.kind, color=mk.color, width=mk.width,
                                   geometry=LineString(pts)))
            continue
        edge = b.outer
        is_outermost = abs(edge - (wl if b.left else wr)) < 1e-6
        if is_outermost:
            edge = max(0.0, edge - EDGE_MARKING_INSET)
        dist = edge if b.left else -edge
        beside = [o for o in aux_outboard(b) if abs(o.inner - b.outer) < 1e-6]
        if beside:
            # the lane a speed-change lane runs beside: a broken line while the auxiliary
            # lane exists, the carriageway edge line elsewhere
            s0 = min(aux_span(o.lane, road)[0] for o in beside)
            s1 = max(aux_span(o.lane, road)[1] for o in beside)
            M = profiles.get().marking
            edge_mk = Marking("solid", M.edge_color, M.width)
            for line in offset(dist):
                for seg in clip_span(line, s0, s1, True):
                    out.append(Marking(kind=mk.kind, color=mk.color, width=mk.width, geometry=seg))
                for seg in clip_span(line, s0, s1, False):
                    out.append(Marking(kind=edge_mk.kind, color=edge_mk.color, width=edge_mk.width,
                                       geometry=seg))
            continue
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

def ground_layer_of(layers: Iterable[int]) -> int:
    """The OSM layer the ground (and the islands, parking lots, ground fill) sits on: 0 when a
    road is on it, else the lowest layer above ground (a bbox with only decks), else the
    highest one (only tunnels). A tunnel (layer < 0) is *under* the ground, never the ground."""
    ls = sorted(set(layers))
    above = [l for l in ls if l >= 0]
    return above[0] if above else (ls[-1] if ls else 0)


def tunnel_trench(model: TwinModel, tunnel_roads: Iterable[Road],
                  clearance: Optional[float] = None, step: float = 2.0) -> Polygon | MultiPolygon:
    """The open cut of a tunnel: the street-space band of every tunnel road wherever the DEM
    (the ground above) is less than ``clearance`` (default ``elevation.tunnel_height_m``: the
    ceiling would stand above the ground) over the tunnel road — the ramp between the portal
    and the covered part, where the ground surface must not be laid over the road. Empty
    without a DEM or without tunnel roads."""
    if model.elevation is None:
        return Polygon()
    if clearance is None:
        clearance = profiles.get().elevation.tunnel_height_m
    parts: list[BaseGeometry] = []
    for r in tunnel_roads:
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        if c.shape[0] < 2 or c.shape[1] < 3:
            continue
        line = LineString(c[:, :2])
        s_v = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(c[:, :2], axis=0).T))])
        s = np.unique(np.concatenate([np.arange(0.0, s_v[-1], step), [s_v[-1]]]))
        pts = shapely.line_interpolate_point(line, s)
        z_dem = np.asarray(model.elevation.sample(shapely.get_x(pts), shapely.get_y(pts)), dtype=np.float64)
        z_road = np.interp(s, s_v, c[:, 2])
        open_ = (z_dem - z_road) < clearance
        if not open_.any():
            continue
        wl, wr = _street_extent(r)
        # runs of open samples -> substrings -> bands
        i = 0
        while i < len(s):
            if not open_[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(s) and open_[j + 1]:
                j += 1
            a, b = (s[max(i - 1, 0)], s[min(j + 1, len(s) - 1)])
            if b - a > 0.1:
                parts.append(_band(substring(line, a, b), wl + 0.5, wr + 0.5))
            i = j + 1
    return _clean(unary_union(parts)) if parts else Polygon()


def tunnel_enclosure(model: TwinModel, tunnel_roads: Iterable[Road],
                     trench: BaseGeometry | None = None) -> list[Surface]:
    """The box the mesh draws over a tunnel road so it reads as a tunnel: a ``tunnel_ceiling``
    over the road's street space (``elevation.tunnel_height_m`` above the tunnel datum) and a
    ``tunnel_wall`` ring of ``tunnel_wall_m`` around it, one set per negative layer, tagged with
    the layer so the exporter samples the tunnel datum. The ceiling is cut back where the
    ``trench`` is open to the sky (the walls stay: they retain the cut)."""
    E = profiles.get().elevation
    by_layer: dict[int, list[BaseGeometry]] = {}
    for r in tunnel_roads:
        wl, wr = _street_extent(r)
        band = _band(_ref2d(r), wl, wr)
        if not band.is_empty:
            by_layer.setdefault(road_osm_layer(r), []).append(band)
    out: list[Surface] = []
    k = 0
    for lay in sorted(by_layer):
        inner = _clean(unary_union(by_layer[lay]))
        outer = _clean(inner.buffer(E.tunnel_wall_m, join_style="mitre", mitre_limit=MITRE_LIMIT))
        ceiling = outer
        if trench is not None and not trench.is_empty:
            ceiling = _clean(outer.difference(trench), min_area=MIN_SURFACE_AREA)
        for part in _parts(ceiling):
            out.append(Surface(id=f"tunnel_ceiling_{k}", kind="tunnel_ceiling", geometry=part,
                               z_offset=E.tunnel_height_m, source="osm_tags",
                               tags={"layer": lay, "height": E.tunnel_height_m}))
            k += 1
        for part in _parts(_clean(outer.difference(inner), min_area=MIN_SURFACE_AREA)):
            out.append(Surface(id=f"tunnel_wall_{k}", kind="tunnel_wall", geometry=part,
                               z_offset=0.0, source="osm_tags",
                               tags={"layer": lay, "height": E.tunnel_height_m}))
            k += 1
    return out


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

def _lot_enclosure_classifier(model: TwinModel, carriageways: dict[str, BaseGeometry],
                              junction_polys: dict[str, Polygon]):
    """``f(hole) -> fraction`` of a drivable hole's boundary that runs along lot circulation:
    parking aisles and driveways (``tags["parking_aisle"]``), unnamed ``highway=service``
    roads, the connecting roads and junctions whose arms are all of those. The rest of the
    boundary is street. A boundary nothing classifies (an imagery-refined edge) counts as
    street, so an inferred lot is always positively enclosed by its own aisles."""
    roads_by_id = {r.id: r for r in model.roads}

    def lot_road(r: Road) -> bool:
        if r.tags.get("parking_aisle"):
            return True
        return r.highway == "service" and not r.name

    def is_lot(r: Road) -> bool:
        if r.junction_id is None:
            return lot_road(r)
        ends = [roads_by_id.get(r.predecessor.id) if r.predecessor is not None else None,
                roads_by_id.get(r.tags.get("to_road", ""))]
        return all(e is not None and lot_road(e) for e in ends)

    geoms: list[BaseGeometry] = []
    kinds: list[bool] = []
    for rid, cw in carriageways.items():
        r = roads_by_id.get(rid)
        if r is None or cw.is_empty:
            continue
        geoms.append(cw)
        kinds.append(is_lot(r))
    for j in model.junctions:
        poly = junction_polys.get(j.id)
        if poly is None or poly.is_empty:
            continue
        arms = [roads_by_id.get(c.incoming_road) for c in j.connections]
        geoms.append(poly)
        kinds.append(bool(arms) and all(a is not None and lot_road(a) for a in arms))
    if not geoms:
        return lambda hole: 0.0
    tree = shapely.STRtree(geoms)
    tol = 0.3

    def fraction(hole: Polygon) -> float:
        ring = hole.exterior
        if ring.length <= 0:
            return 0.0
        lot_len = 0.0
        street_len = 0.0
        for k in tree.query(ring.buffer(tol)):
            along = ring.intersection(geoms[k].buffer(tol)).length
            if kinds[k]:
                lot_len += along
            else:
                street_len += along
        # segments lie next to several polygons at a junction: cap at the ring length
        street_len = max(street_len, ring.length - lot_len)
        return lot_len / (lot_len + street_len) if lot_len + street_len > 0 else 0.0

    return fraction


def build_surfaces(model: TwinModel,
                   refined_drivable: Polygon | MultiPolygon | dict | None = None,
                   default_markings: bool = True,
                   junction_cover: Optional[str] = None) -> TwinModel:
    """Fill ``model.surfaces``, ``model.curbs``, ``model.markings`` and every
    ``Junction.polygon`` from the lane graph. Mutates and returns ``model``. Idempotent.

    ``refined_drivable`` (from ``refine.py``) replaces the lane-graph drivable polygon (source
    ``imagery``; the lane-graph one is kept as WKT in ``metadata["surfaces"]``). In a model with
    several OSM layers it is a dict ``{layer: polygon}`` (``refine.refine_layers``) and only the
    listed layers are replaced — the others keep their lane-graph surfaces; a bare polygon is
    taken as the ground layer's (``refine.ground_layer``), never fused across layers.
    ``default_markings``: synthesise edge/centre/lane markings where the lane graph has none.
    ``junction_cover``: see :func:`junction_cover_polygon`; default the profile's
    ``junction.cover``. Surface parking lots listed as WKT in
    ``model.metadata["parking_lots_wkt"]`` (lanegraph, OSM ``amenity=parking``) become
    ``parking`` surfaces."""
    model.surfaces = []
    model.curbs = []
    model.markings = []
    stats: dict = {}
    P = profiles.get()
    cover = junction_cover or P.junction.cover
    plaza_cap = P.junction.plaza_max_area_factor
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

    # vertical stacking (OSM ``layer``): a road and the junctions it belongs to sit on one level
    road_layer = {r.id: road_osm_layer(r) for r in model.roads}
    junction_layer: dict[str, int] = {}
    for j in model.junctions:
        ls = [road_layer.get(c.incoming_road, 0) for c in j.connections]
        junction_layer[j.id] = max(set(ls), key=ls.count) if ls else 0
    # tunnels: under the ground (and under the buildings), on their own negative layer
    tunnel_roads = [r for r in model.roads if r.junction_id is None and road_is_tunnel(r)]
    tunnel_ids = {r.id for r in tunnel_roads}

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
    n_capped = 0
    plaza_ratios: dict[str, float] = {}
    for j in model.junctions:
        if j.tags.get("polygon_source") == "surfaces":
            j.polygon = None  # our own previous result is not an input (idempotent rebuilds)
        j.tags.pop("plaza_capped", None)
        # a ramp gore (freeway merge/diverge, lanegraph 7h-bis) is not an intersection: no
        # plaza, no chamfer, no corner apron — just the arms and the connecting carriageways
        gore = j.tags.get("kind") == "gore"
        j_cover = P.junction.gore_cover if gore else cover
        if j.tags.get("gore_role") == "diverge_nose":
            # the nose junction of a taper-model diverge (lanegraph 7k) is a few metres of
            # stubs between three arm ends: the hull of those ends is the whole of it
            j_cover = "convex"
        base = junction_cover_polygon(model, j, carriageways, cover=j_cover,
                                      buildings=buildings if j_cover == "bounded" else None)
        _, incoming = _junction_roads(model, j)
        plaza_drv = None
        source = j_cover
        if not gore and base is not None and not buildings.is_empty and incoming:
            widths = [w for r, _ in incoming for w in _sidewalk_widths(r) if w > 0]
            sw = round(max(widths) if widths else P.junction.plaza_sidewalk_m, 3)
            tag = j.tags.get("plaza_wkt")
            arms = [arm_info(r, at_end, buildings) for r, at_end in incoming]
            env = junction_envelope(j, arms, buildings)
            plaza = keep_out = None
            plaza_from = "corner_void"
            if tag:
                plaza = _clean(shapely_wkt.loads(tag))
                plaza = plaza if not plaza.is_empty else None
                plaza_from = "lanegraph"
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
            # the plaza never leaves the envelope built from the arms (corridors + arm-end hull);
            # a corner without a building face gets no chamfer opening
            plaza = bound_plaza(plaza, env)
            if plaza is not None and plaza.is_empty:
                plaza = None
            if plaza is not None:
                plaza_drv = _clean(plaza.difference(keep_out))
                if env is not None and env.wmax > 0:
                    plaza_ratios[j.id] = float(plaza_drv.area) / env.wmax ** 2
                if (plaza_cap is not None and env is not None
                        and plaza_drv.area > plaza_cap * env.wmax ** 2):
                    log.info("junction %s: plaza %.0f m2 exceeds %.1f x (%.1f m)^2 = %.0f m2 -> %s cover only",
                             j.id, plaza_drv.area, plaza_cap, env.wmax, plaza_cap * env.wmax ** 2, j_cover)
                    j.tags["plaza_capped"] = round(float(plaza_drv.area), 1)
                    n_capped += 1
                    plaza = plaza_drv = None
            if plaza is not None:
                source = plaza_from
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
                poly = junction_polygon(model, j, carriageways, cover=j_cover,
                                        plaza=plaza_drv, keep_out=keep_out, buildings=buildings)
                n_plaza += 1
            else:
                poly = base
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
    layers = sorted({road_layer[rid] for rid in carriageways} | set(junction_layer.values()))
    refined_by_layer: dict[Optional[int], Polygon | MultiPolygon] = {}
    if isinstance(refined_drivable, dict):
        refined_by_layer = {lay: g for lay, g in refined_drivable.items()
                            if g is not None and not g.is_empty}
        refined_drivable = None
        if len(layers) <= 1:
            # single-layer model: the dict holds one entry (keyed None or the layer itself)
            refined_drivable = next(iter(refined_by_layer.values()), None)
            refined_by_layer = {}
    elif refined_drivable is not None and not refined_drivable.is_empty and len(layers) > 1:
        from .refine import ground_layer
        refined_by_layer = {ground_layer(layers): refined_drivable}
        refined_drivable = None
    if refined_by_layer:
        source = "imagery"
        stats["lanegraph_drivable_wkt"] = lanegraph_drivable.wkt
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
    # (city blocks enclosed by a ring of roads) stay plain holes. A hole inside a surface
    # parking lot is the stall field between the aisles, not a raised island: it stays a hole
    # and step 4b fills it with the lot's `parking` surface.
    lots = [shapely_wkt.loads(w) for w in model.metadata.get("parking_lots_wkt", []) or []]
    lots_union = _clean(unary_union(lots)) if lots else Polygon()
    # A hole enclosed by a lot's own circulation (aisles, driveways, unnamed service roads) is
    # its stall field even when OSM drew no ``amenity=parking`` polygon around it
    # (ParkingAisleRules.lot_enclosure_fraction): inferred lots, filled at grade in step 4b.
    lot_fraction_of = _lot_enclosure_classifier(model, carriageways, junction_polys)
    A = P.parking_aisle

    def _fill_holes(geom: BaseGeometry, islands_out: list[Polygon],
                    inferred_out: list[Polygon]) -> Polygon | MultiPolygon:
        filled: list[Polygon] = []
        for part in _parts(geom):
            keep_holes = []
            for ring in part.interiors:
                hole = Polygon(ring)
                if hole.area < MIN_ISLAND_AREA:
                    continue
                keep_holes.append(ring)
                in_lot = (not lots_union.is_empty
                          and lots_union.intersection(hole).area > 0.5 * hole.area)
                if (not in_lot and A.include and A.lot_enclosure_fraction > 0
                        and hole.area <= A.inferred_lot_max_area
                        and lot_fraction_of(hole) >= A.lot_enclosure_fraction):
                    inferred_out.append(hole)
                    continue
                if hole.area <= MAX_ISLAND_AREA and not hole.intersects(buildings) and not in_lot:
                    islands_out.append(hole)
            filled.append(Polygon(part.exterior, keep_holes))
        return _clean(unary_union(filled)) if filled else Polygon()

    islands: list[Polygon] = []
    inferred_lots: list[Polygon] = []
    drivable = _fill_holes(drivable, islands, inferred_lots)
    if inferred_lots:
        log.info("surfaces: %d aisle-enclosed hole(s) (%.0f m2) taken as parking lots at grade",
                 len(inferred_lots), sum(h.area for h in inferred_lots))

    def touching_roads(geom: BaseGeometry, candidates: Iterable[str]) -> list[str]:
        return [rid for rid in candidates if carriageways[rid].intersects(geom)]

    # grade separation: an overpass and the road under it overlap in 2D but are different
    # surfaces at different z. Emit one drivable surface per OSM ``layer`` (tagged with it) so
    # the mesh and the road datum can keep them apart; with a single layer (the usual case)
    # nothing changes and the surfaces carry no layer tag.
    layer_groups: list[tuple[Optional[int], Polygon | MultiPolygon]] = [(None, drivable)]
    layer_source: dict[Optional[int], str] = {None: source}
    if len(layers) > 1:
        layer_groups = []
        for lay in layers:
            geoms = [g for rid, g in carriageways.items() if road_layer[rid] == lay]
            geoms += [g for jid, g in junction_polys.items() if junction_layer.get(jid, 0) == lay]
            if not geoms:
                continue
            g = _clean(unary_union(geoms))
            g = _clean(g.simplify(SIMPLIFY_TOL, preserve_topology=True))
            layer_source[lay] = "osm_tags"
            rg = refined_by_layer.get(lay)
            if rg is not None:
                # this layer was refined against the imagery (refine.refine_layers): the
                # lane-graph polygon of the layer is the reference for the IoU
                rg = _clean(rg.simplify(SIMPLIFY_TOL, preserve_topology=True))
                inter = rg.intersection(g).area
                union = rg.union(g).area
                stats.setdefault("refined_iou_by_layer", {})[str(lay)] = inter / union if union > 0 else 0.0
                stats["refined_iou"] = stats["refined_iou_by_layer"][str(lay)]
                g = rg
                layer_source[lay] = "imagery"
            layer_groups.append((lay, _fill_holes(g, [], [])))
        stats["drivable_layers"] = layers
        stats["drivable_area_by_layer"] = {str(lay): float(g.area) for lay, g in layer_groups}
        if refined_by_layer:
            # the all-layer union (sidewalk cutting, parking, ground, curbs) follows the refined
            # layers; islands are re-read from its holes
            islands = []
            inferred_lots = []
            drivable = _fill_holes(_clean(unary_union([g for _, g in layer_groups])), islands,
                                   inferred_lots)
    multi_layer = len(layer_groups) > 1
    drivable_by_layer = {lay: g for lay, g in layer_groups}

    def part_layer(rids, jids) -> Optional[int]:
        """The OSM layer a raised/crossing surface belongs to (None in a single-layer model)."""
        if not multi_layer:
            return None
        ls = [road_layer[r] for r in rids if r in road_layer]
        ls += [junction_layer[j] for j in jids if j in junction_layer]
        return max(set(ls), key=ls.count) if ls else ground_layer

    ground_layer = ground_layer_of(layers) if multi_layer else None
    n = 0
    for lay, geom in layer_groups:
        for part in _parts(geom):
            rids = touching_roads(part, [rid for rid in carriageways
                                         if lay is None or road_layer[rid] == lay])
            jids = [jid for jid, jp in junction_polys.items()
                    if (lay is None or junction_layer.get(jid, 0) == lay) and jp.intersects(part)]
            tags: dict = {"junction_ids": jids} if len(jids) > 1 else {}
            if lay is not None:
                tags["layer"] = lay
            model.surfaces.append(Surface(
                id=f"drivable_{n}", kind="drivable", geometry=part, z_offset=0.0,
                source=layer_source.get(lay, source),
                road_ids=rids, junction_id=jids[0] if len(jids) == 1 else None, tags=tags))
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
        if not buildings.is_empty and r.id not in tunnel_ids \
                and any(b.lane.type == "sidewalk" for b in bands):
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
    walk_parts: list[tuple[BaseGeometry, Optional[int]]] = []   # (geometry, OSM layer): sidewalk + median
    verge_parts: list[tuple[BaseGeometry, Optional[int]]] = []
    sidewalk_so_far: BaseGeometry = Polygon()
    z_of = {"sidewalk": sidewalk_z, "median": sidewalk_z, "verge": verge_z}
    def _owner_layer(rid: str) -> Optional[int]:
        if rid.startswith("junction:"):
            return part_layer([], [rid.split(":", 1)[1]])
        return part_layer([rid], [])

    for kind in ("sidewalk", "median", "verge"):
        items = raised_parts[kind]
        if not items:
            continue
        # one union per OSM layer: the footway of a bridge deck must not merge with the band of
        # the street below it, and must be cut only by the drivable surface of its own layer
        groups: dict[Optional[int], list[tuple[BaseGeometry, str]]] = {}
        for g, rid in items:
            groups.setdefault(_owner_layer(rid), []).append((g, rid))
        k = 0
        kept: list[BaseGeometry] = []
        for lay, group in sorted(groups.items(), key=lambda kv: (kv[0] is not None, kv[0])):
            low = drivable_by_layer.get(lay, drivable) if multi_layer else drivable
            # the buildings stand on the ground: a tunnel's sidewalk runs under them
            cut_buildings = not buildings.is_empty and not (lay is not None and lay < 0)
            geom = _clean(unary_union([g for g, _ in group]))
            geom = _clean(geom.difference(low))
            if cut_buildings:
                geom = _clean(geom.difference(buildings))
            # close hairline gaps between neighbouring bands, then re-snap to the drivable boundary
            geom = _clean(geom.buffer(0.1, join_style="mitre").buffer(-0.1, join_style="mitre"))
            geom = _clean(geom.difference(low), min_area=MIN_SURFACE_AREA)
            if cut_buildings:
                geom = _clean(geom.difference(buildings), min_area=MIN_SURFACE_AREA)
            if kind == "verge" and not sidewalk_so_far.is_empty:
                # sidewalk wins where the two meet (corner aprons): sidewalk ∩ verge = 0
                geom = _clean(geom.difference(sidewalk_so_far), min_area=MIN_SURFACE_AREA)
            for part in _parts(geom):
                rids = [rid for g, rid in group if not rid.startswith("junction:") and g.intersects(part)]
                jids = [rid.split(":", 1)[1] for g, rid in group
                        if rid.startswith("junction:") and g.intersects(part)]
                tags: dict = {"junction_ids": jids} if len(jids) > 1 else {}
                if lay is not None:
                    tags["layer"] = lay
                model.surfaces.append(Surface(
                    id=f"{kind}_{k}", kind=kind, geometry=part, z_offset=z_of[kind], source="osm_tags",
                    road_ids=rids, junction_id=jids[0] if len(jids) == 1 else None, tags=tags))
                k += 1
                raised_union_parts.append(part)
                (verge_parts if kind == "verge" else walk_parts).append((part, lay))
            kept.append(geom)
        if kind == "sidewalk":
            sidewalk_so_far = _clean(unary_union(kept)) if kept else Polygon()

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
                                      z_offset=sidewalk_z, source=source,
                                      tags={"layer": ground_layer} if multi_layer else {}))
    islands = island_geoms

    # 4. crossings ---------------------------------------------------------------------------
    k = 0
    for sig in model.signals:
        if sig.kind not in ("crosswalk", "crossing"):
            continue
        rect = crossing_polygon(model, sig)
        if rect is None:
            continue
        c_lay = part_layer([sig.road_id], [])
        geom = _clean(rect.intersection(drivable_by_layer.get(c_lay, drivable) if multi_layer
                                        else drivable))
        if geom.is_empty:
            continue
        c_tags = {"signal_id": sig.id}
        if c_lay is not None:
            c_tags["layer"] = c_lay
        model.surfaces.append(Surface(id=f"crossing_{k}", kind="crossing", geometry=geom,
                                      z_offset=P.crossing.z, source="osm_tags",
                                      road_ids=[sig.road_id], tags=c_tags))
        k += 1

    # 4b. parking lots (OSM amenity=parking, handed over by the lane graph as WKT): at road
    #     level, never over the carriageway, a raised surface or a building
    raised_all = _clean(unary_union(raised_union_parts + islands)) if (raised_union_parts or islands) else Polygon()
    parking = Polygon()
    if lots or inferred_lots:
        parking = _clean(unary_union([lots_union] + inferred_lots))
        bbox = _model_bbox(model)
        if bbox is not None:
            parking = _clean(parking.intersection(bbox))
        parking = _clean(parking.difference(unary_union([drivable, raised_all])), min_area=MIN_SURFACE_AREA)
        if not buildings.is_empty:
            parking = _clean(parking.difference(buildings), min_area=MIN_SURFACE_AREA)
        for k, part in enumerate(_parts(parking)):
            model.surfaces.append(Surface(id=f"parking_{k}", kind="parking", geometry=part,
                                          z_offset=0.0, source="osm_tags",
                                          tags={"layer": ground_layer} if multi_layer else {}))

    # 5. ground: the street void near the surfaces that is neither drivable nor raised nor
    #    parking lot nor building (open lots, courtyard mouths, the strip beyond a short
    #    sidewalk); block interiors are enclosed by their buildings and stay empty
    covered = _clean(unary_union([drivable, raised_all]))
    trench = Polygon()
    if tunnel_roads:
        # a tunnel is under the ground: the ground above it is intact (unlike under a deck, the
        # surface street over a tunnel keeps its ground), so the tunnel's own surfaces neither
        # cover nor attract ground fill — except the open cut at the portals, which the ground
        # must not roof over
        surface_drivable = [g for lay, g in layer_groups if lay is None or lay >= 0]
        surface_raised = [g for g, lay in walk_parts + verge_parts if lay is None or lay >= 0]
        covered = _clean(unary_union(surface_drivable + surface_raised + islands))
        trench = tunnel_trench(model, tunnel_roads)
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
        if not parking.is_empty:
            ground = _clean(ground.difference(parking), min_area=MIN_GROUND_AREA)
        if not trench.is_empty:
            ground = _clean(ground.difference(trench), min_area=MIN_GROUND_AREA)
        ground = _keep_touching(ground, covered)
        for k, part in enumerate(_parts(ground)):
            model.surfaces.append(Surface(id=f"ground_{k}", kind="ground", geometry=part,
                                          z_offset=ground_z, source="osm_tags", confidence=0.5,
                                          tags={"layer": ground_layer} if multi_layer else {}))
            ground_area += part.area
    # 5b. tunnel enclosures: walls + ceiling over every tunnel road (their own surface kinds)
    if tunnel_roads:
        model.surfaces.extend(tunnel_enclosure(model, tunnel_roads, trench))
        stats["tunnel_roads"] = len(tunnel_roads)
        stats["tunnel_trench_area"] = float(trench.area)

    # 6. curbs (drivable <-> sidewalk/island/verge only), one pass per raised kind so a curb
    #    line is labelled by the surface it actually borders (an arm's verge curb and the
    #    sidewalk apron at the corner are separate lines)
    k = 0
    islands_l = [(g, ground_layer) for g in islands]
    for high_kind, parts in (("sidewalk", walk_parts), ("island", islands_l), ("verge", verge_parts)):
        if not parts:
            continue
        # one pass per OSM layer: a curb of the bridge deck must not be drawn down to the
        # carriageway under it (a 6 m wall instead of a 15 cm step)
        by_layer: dict[Optional[int], list[BaseGeometry]] = {}
        for g, lay in parts:
            by_layer.setdefault(lay, []).append(g)
        for lay, geoms in by_layer.items():
            raised_kind = _clean(unary_union(geoms))
            lows = (("drivable", drivable_by_layer.get(lay, drivable) if multi_layer else drivable),
                    ("parking", parking if (not multi_layer or lay == ground_layer) else Polygon()))
            for low_kind, low in lows:
                if low.is_empty:
                    continue
                for line in curb_lines(low, raised_kind):
                    model.curbs.append(CurbLine(id=f"curb_{k}", geometry=line, height=curb_height,
                                                low_side_kind=low_kind, high_side_kind=high_kind,
                                                layer=lay))
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
                                                  geometry=line, layer=part_layer([r.id], [])))

    stats.update({
        "profile": P.name,
        "drivable_area": float(drivable.area),
        "sidewalk_area": float(sum(s.geometry.area for s in model.surfaces_of("sidewalk"))),
        "verge_area": float(sum(s.geometry.area for s in model.surfaces_of("verge"))),
        "island_count": len(islands),
        "island_area": float(sum(g.area for g in islands)),
        "curb_length": float(sum(c.geometry.length for c in model.curbs)),
        "marking_count": len(model.markings),
        "junctions_with_polygon": len(junction_polys),
        "junctions_with_plaza": n_plaza,
        "junctions_plaza_capped": n_capped,
        "plaza_ratio_max": float(max(plaza_ratios.values())) if plaza_ratios else 0.0,
        "junction_cover": cover,
        "sidewalk_sides_to_face": n_face_sides,
        "ground_area": float(ground_area),
        "parking_lot_count": len(lots),
        "parking_area": float(parking.area),
        "inferred_lot_count": len(inferred_lots),
        "inferred_lot_area": float(sum(h.area for h in inferred_lots)),
        "drivable_source": source,
    })
    model.metadata.setdefault("surfaces", {}).update(stats)
    log.info("surfaces: drivable %.0f m2, sidewalk %.0f m2, ground %.0f m2, %d islands, curbs %.0f m, "
             "%d markings, %d/%d junction plazas from buildings",
             stats["drivable_area"], stats["sidewalk_area"], stats["ground_area"], stats["island_count"],
             stats["curb_length"], stats["marking_count"], n_plaza, len(junction_polys))
    return model
