"""OpenDRIVE 1.4 export of a :class:`~twinmodel.model.TwinModel` (worker C).

The XML is written directly with ``lxml`` rather than through ``scenariogeneration.xodr``:
that library has no ``<controller>`` element at all, its ``PlanView``/``Road`` objects are
built for *generative* layouts (geometries chained from a start pose and then re-adjusted by
``adjust_roads_and_lanes``), lane-level ``<link>`` ids are computed by its own linker, and
absolute-position piecewise ``paramPoly3`` planviews with our own elevation/height/signal
control are simpler to emit than to coax out of it.  The writer is ~400 lines and
self-contained.

Conventions (all verified against ``LibCarla/source/carla/opendrive/parser``):

* road ids are ``uint`` (``RoadParser.cpp:124``), junction ids ``int`` (``JunctionParser.cpp:46``)
  and ``MapBuilder::GetLaneNext`` decides "next element is a junction" by
  ``!ContainsRoad(next_id)`` (``MapBuilder.cpp:686``) -- so road and junction integer ids
  **must be disjoint**.  :func:`build_id_map` allocates them deterministically from the
  model's string ids.
* lane continuation across a road-to-road link needs a lane-level ``<link>``
  (``MapBuilder.cpp:703``); across a junction it comes from ``<laneLink>``.
* signal ``type``/``subtype`` follow ``road/SignalType.h``: traffic light ``1000001``
  (``SignalType.cpp:124`` ``IsTrafficLight``), stop ``206``, yield ``205``, max speed ``274``
  with the km/h value as subtype (``ObjectParser.cpp:86`` shows CARLA itself synthesising
  ``type=274 subtype=<speed>``).  Crosswalks are *not* signals in CARLA: they are
  ``<object type="crosswalk">`` with a ``<cornerLocal>`` outline (``ObjectParser.cpp:36``).
* ``paramPoly3`` is sampled by CARLA in ``p`` steps of ~0.5 m and the chord lengths are
  accumulated as ``s`` (``Geometry.cpp:188`` ``PreComputeSpline``); we emit
  ``pRange="normalized"`` with ``length`` = numerically integrated arc length.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from lxml import etree

from .. import profiles
from ..model import (AUX_EPS, Controller, Lane, Marking, Road, Signal, TwinModel, aux_span,
                     aux_width_at)

log = logging.getLogger("twinmodel.export.xodr")

# CARLA signal conventions ------------------------------------------------------------------
SIGNAL_TYPES: dict[str, tuple[str, str, str]] = {
    # kind -> (type, subtype, dynamic)
    "traffic_light": ("1000001", "-1", "yes"),
    "stop": ("206", "-1", "no"),
    "yield": ("205", "-1", "no"),
    "speed_limit": ("274", "", "no"),  # subtype filled with the km/h value
}
CROSSWALK_KIND = "crosswalk"
# regional (twinmodel.profiles): crosswalk width P.crossing.width, sidewalk height P.sidewalk.z,
# verge (planting strip) height P.sidewalk.curb_height
LANE_TYPES = {"driving", "sidewalk", "shoulder", "parking", "biking", "median", "none"}
# model lane types with no OpenDRIVE equivalent -> the closest type CARLA parses
# (``RoadParser.cpp``: border -> LaneType::Border, a raised non-drivable strip)
LANE_TYPE_MAP = {"verge": "border"}
RAISED_LANE_TYPES = {"sidewalk", "verge"}  # get a <height> record
_MARK_COLORS = {"white": "white", "yellow": "yellow"}
# a fitted planview geometry may bow at most this far (metres) off the polyline it replaces;
# a vertex that would need more rounding is written as a heading kink (see fit_planview). Pure
# fitting tolerance, not a regional constant.
MAX_PLANVIEW_OFFSET = 0.25


def xodr_lane_type(lane_type: str) -> str:
    """OpenDRIVE ``<lane type>`` for a model lane type."""
    if lane_type in LANE_TYPES:
        return lane_type
    return LANE_TYPE_MAP.get(lane_type, "none")


# ------------------------------------------------------------------------------ id mapping

@dataclass
class IdMap:
    """String model ids -> integer OpenDRIVE ids (roads and junctions disjoint)."""
    road: dict[str, int] = field(default_factory=dict)
    junction: dict[str, int] = field(default_factory=dict)

    @property
    def road_inv(self) -> dict[int, str]:
        return {v: k for k, v in self.road.items()}

    @property
    def junction_inv(self) -> dict[int, str]:
        return {v: k for k, v in self.junction.items()}


def _allocate(keys: list[str], used: set[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    pending = []
    for k in keys:
        if k.isdigit() and int(k) >= 1 and int(k) not in used:
            out[k] = int(k)
            used.add(int(k))
        else:
            pending.append(k)
    nxt = max(used, default=0) + 1
    for k in pending:
        while nxt in used:
            nxt += 1
        out[k] = nxt
        used.add(nxt)
    return out


def build_id_map(model: TwinModel) -> IdMap:
    """Deterministic: numeric ids are kept when unique and >= 1, others get the next free int;
    junctions are allocated after roads from the same pool so the two sets never collide."""
    used: set[int] = set()
    roads = _allocate([r.id for r in model.roads], used)
    juncs = _allocate([j.id for j in model.junctions], used)
    return IdMap(road=roads, junction=juncs)


# ------------------------------------------------------------------------------ geometry fit

@dataclass
class Geom:
    s: float
    x: float
    y: float
    hdg: float
    length: float
    kind: str  # "line" | "paramPoly3"
    coeffs: Optional[tuple[float, ...]] = None  # aU bU cU dU aV bV cV dV

    def point_at(self, p: float) -> tuple[float, float]:
        """Evaluate in world coordinates; p in [0, 1] (normalized) or metres for a line."""
        if self.kind == "line":
            return (self.x + math.cos(self.hdg) * p * self.length,
                    self.y + math.sin(self.hdg) * p * self.length)
        aU, bU, cU, dU, aV, bV, cV, dV = self.coeffs
        u = aU + bU * p + cU * p * p + dU * p ** 3
        v = aV + bV * p + cV * p * p + dV * p ** 3
        c, s = math.cos(self.hdg), math.sin(self.hdg)
        return (self.x + c * u - s * v, self.y + s * u + c * v)


def _dedupe(coords: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    keep = [0]
    for i in range(1, len(coords)):
        if np.linalg.norm(coords[i, :2] - coords[keep[-1], :2]) > eps:
            keep.append(i)
    return coords[keep]


def _cubic_length(coeffs: tuple[float, ...], n: int) -> float:
    aU, bU, cU, dU, aV, bV, cV, dV = coeffs
    p = np.linspace(0.0, 1.0, n + 1)
    u = aU + bU * p + cU * p ** 2 + dU * p ** 3
    v = aV + bV * p + cV * p ** 2 + dV * p ** 3
    return float(np.sum(np.hypot(np.diff(u), np.diff(v))))


def _hermite_segment(p0: np.ndarray, p1: np.ndarray, t0: np.ndarray, t1: np.ndarray
                     ) -> tuple[float, float, tuple[float, ...]]:
    """Cubic Hermite from ``p0`` (unit tangent ``t0``) to ``p1`` (unit tangent ``t1``), tangent
    magnitudes = chord length, expressed in the frame rotated to ``t0``.
    Returns ``(hdg, chord_length, coeffs)`` with ``aU = aV = bV = 0``."""
    chord = p1 - p0
    L = float(np.hypot(*chord))
    hdg = math.atan2(t0[1], t0[0])
    c, sn = math.cos(hdg), math.sin(hdg)
    rot = np.array([[c, sn], [-sn, c]])  # world -> local (u along start tangent)
    p1l = rot @ chord
    m0l = np.array([L, 0.0])
    m1l = rot @ (t1 * L)
    # Hermite -> power basis on p in [0, 1]
    b = m0l
    cc = 3.0 * p1l - 2.0 * m0l - m1l
    d = -2.0 * p1l + m0l + m1l
    return hdg, L, (0.0, b[0], cc[0], d[0], 0.0, b[1], cc[1], d[1])


def _chord_offset(coeffs: tuple[float, ...], n: int = 33) -> float:
    """Largest distance of the fitted cubic from the straight chord it spans (local frame)."""
    aU, bU, cU, dU, aV, bV, cV, dV = coeffs
    p = np.linspace(0.0, 1.0, n)
    u = aU + bU * p + cU * p ** 2 + dU * p ** 3
    v = aV + bV * p + cV * p ** 2 + dV * p ** 3
    ex, ey = u[-1], v[-1]
    L = math.hypot(ex, ey)
    if L < 1e-9:
        return float(np.max(np.hypot(u, v)))
    t = np.clip((u * ex + v * ey) / (L * L), 0.0, 1.0)   # projection on the chord
    return float(np.max(np.hypot(u - t * ex, v - t * ey)))


def fit_planview(coords: np.ndarray, max_offset: Optional[float] = None) -> list[Geom]:
    """Fit an (N, 2+) polyline as a piecewise cubic ``paramPoly3`` planview.

    Tangent *directions* at the vertices are Catmull-Rom (chord of the neighbours), the
    tangent magnitude per segment equals the segment chord length so the parametrisation is
    close to arc length and the curve does not overshoot on uneven spacing.  Each segment is a
    cubic Hermite expressed in the local frame rotated to the start tangent, hence
    ``aU = aV = bV = 0`` and ``hdg`` is continuous between consecutive geometries (G1).  A
    two-point polyline becomes a single ``line``.

    A vertex where rounding would pull the curve more than ``max_offset`` off the polyline —
    a right-angle corner with long legs, e.g. an L-shaped parking aisle — becomes a *corner*
    instead: both adjacent geometries take their own chord direction, they meet with a heading
    kink and a leg between two corners is written as a ``line``.  The polyline is what the
    surface builder buffers, so following it is what keeps the lanes inside ``drivable``.
    """
    max_offset = MAX_PLANVIEW_OFFSET if max_offset is None else max_offset
    pts = _dedupe(np.asarray(coords, dtype=np.float64))[:, :2]
    if len(pts) < 2:
        raise ValueError("reference line needs at least two distinct points")
    if len(pts) == 2:
        d = pts[1] - pts[0]
        L = float(np.hypot(*d))
        return [Geom(0.0, float(pts[0, 0]), float(pts[0, 1]), math.atan2(d[1], d[0]), L, "line")]

    n = len(pts)
    chord_dir = pts[1:] - pts[:-1]
    chord_dir = chord_dir / np.linalg.norm(chord_dir, axis=1, keepdims=True)
    # unit tangent directions per vertex (Catmull-Rom; the ends follow their own chord)
    tang = np.zeros_like(pts)
    tang[0], tang[-1] = chord_dir[0], chord_dir[-1]
    tang[1:-1] = pts[2:] - pts[:-2]
    tang /= np.linalg.norm(tang, axis=1, keepdims=True)
    t_out = tang.copy()          # tangent used at the start of segment i
    t_in = tang.copy()           # tangent used at the end of segment i-1
    corner = np.zeros(n, dtype=bool)
    corner[0] = corner[-1] = True

    # lock the vertices whose rounding bows too far off the polyline (a few passes: locking a
    # vertex changes its neighbouring segments only)
    for _pass in range(4):
        locked = False
        for i in range(n - 1):
            _h, _L, coeffs = _hermite_segment(pts[i], pts[i + 1], t_out[i], t_in[i + 1])
            if _chord_offset(coeffs) <= max_offset:
                continue
            # lock the endpoint that bends the segment most (candidates are interior vertices:
            # both ends of the polyline are corners from the start)
            cands = [j for j in (i, i + 1) if not corner[j]]
            if not cands:
                continue
            def turn(k: int) -> float:
                a, b = chord_dir[k - 1], chord_dir[k]
                return abs(math.atan2(float(a[0] * b[1] - a[1] * b[0]), float(a @ b)))
            j = max(cands, key=turn)
            corner[j] = True
            t_out[j], t_in[j] = chord_dir[j], chord_dir[j - 1]
            locked = True
        if not locked:
            break

    geoms: list[Geom] = []
    s = 0.0
    for i in range(n - 1):
        p0, p1 = pts[i], pts[i + 1]
        hdg, L, coeffs = _hermite_segment(p0, p1, t_out[i], t_in[i + 1])
        if corner[i] and corner[i + 1]:      # both tangents are the chord: a straight leg
            geoms.append(Geom(s, float(p0[0]), float(p0[1]), hdg, L, "line"))
            s += L
            continue
        length = _cubic_length(coeffs, max(64, int(L / 0.05)))
        geoms.append(Geom(s, float(p0[0]), float(p0[1]), hdg, length, "paramPoly3", coeffs))
        s += length
    return geoms


def fit_elevation(s_vals: np.ndarray, z_vals: np.ndarray) -> list[tuple[float, float, float, float, float]]:
    """Piecewise cubic Hermite (Catmull-Rom slopes) elevation records ``(s, a, b, c, d)``."""
    s_vals = np.asarray(s_vals, dtype=np.float64)
    z_vals = np.asarray(z_vals, dtype=np.float64)
    if len(z_vals) < 2 or np.allclose(z_vals, z_vals[0], atol=1e-6):
        return [(0.0, float(z_vals[0]) if len(z_vals) else 0.0, 0.0, 0.0, 0.0)]
    n = len(s_vals)
    m = np.zeros(n)
    m[0] = (z_vals[1] - z_vals[0]) / max(s_vals[1] - s_vals[0], 1e-9)
    m[-1] = (z_vals[-1] - z_vals[-2]) / max(s_vals[-1] - s_vals[-2], 1e-9)
    if n > 2:
        m[1:-1] = (z_vals[2:] - z_vals[:-2]) / np.maximum(s_vals[2:] - s_vals[:-2], 1e-9)
    out = []
    for i in range(n - 1):
        L = float(s_vals[i + 1] - s_vals[i])
        if L <= 1e-9:
            continue
        z0, z1, m0, m1 = float(z_vals[i]), float(z_vals[i + 1]), float(m[i]), float(m[i + 1])
        a = z0
        b = m0
        c = (3.0 * (z1 - z0) - 2.0 * m0 * L - m1 * L) / (L * L)
        d = (2.0 * (z0 - z1) + m0 * L + m1 * L) / (L ** 3)
        out.append((float(s_vals[i]), a, b, c, d))
    return out or [(0.0, float(z_vals[0]), 0.0, 0.0, 0.0)]


def road_geometry(road: Road) -> tuple[list[Geom], np.ndarray]:
    """Planview geometries and the cumulative ``s`` of every (deduplicated) vertex."""
    coords = _dedupe(np.asarray(road.reference_line.coords, dtype=np.float64))
    geoms = fit_planview(coords)
    # one geometry per polyline segment, whatever its kind (a straight leg between two corners
    # is a "line"): the cumulative geometry lengths are the s of the vertices
    s_vertices = np.concatenate([[0.0], np.cumsum([g.length for g in geoms])])
    return geoms, s_vertices


def sample_reference(road: Road, step: float = 1.0) -> np.ndarray:
    """Dense (M, 2) sample of the *fitted* reference line (what CARLA will see)."""
    geoms, _ = road_geometry(road)
    pts = []
    for g in geoms:
        n = max(2, int(math.ceil(g.length / step)) + 1)
        for p in np.linspace(0.0, 1.0, n)[:-1]:
            pts.append(g.point_at(p))
    pts.append(geoms[-1].point_at(1.0))
    return np.asarray(pts)


# ------------------------------------------------------------------------------ lane sections

def road_sections(road: Road) -> list[tuple[float, float, list[Lane]]]:
    """``[(s_start, s_end, lanes present)]`` along the road. An ordinary road is one section;
    an auxiliary lane (``model.aux_span``) that begins or ends inside the road opens a new
    ``<laneSection>`` there, in which the lane is absent."""
    L = float(road.length)
    cuts = {0.0, L}
    for l in road.lanes:
        if l.tags.get("aux"):
            for v in aux_span(l, road):
                if AUX_EPS < v < L - AUX_EPS:
                    cuts.add(float(v))
    bounds = sorted(cuts)
    out: list[tuple[float, float, list[Lane]]] = []
    for s0, s1 in zip(bounds, bounds[1:]):
        present = []
        for l in road.lanes:
            a, b = aux_span(l, road)
            if a <= s0 + AUX_EPS and b >= s1 - AUX_EPS:
                present.append(l)
        out.append((s0, s1, present))
    return out


def section_ids(lanes: list[Lane]) -> dict[int, int]:
    """Model lane id -> OpenDRIVE lane id inside one section (ids are contiguous outward)."""
    ids: dict[int, int] = {}
    for i, l in enumerate(sorted((l for l in lanes if l.id > 0), key=lambda l: l.id)):
        ids[l.id] = i + 1
    for i, l in enumerate(sorted((l for l in lanes if l.id < 0), key=lambda l: -l.id)):
        ids[l.id] = -(i + 1)
    return ids


def _contact_lanes(road: Road, contact: str) -> tuple[list[Lane], dict[int, int]]:
    secs = road_sections(road)
    lanes = secs[0][2] if contact == "start" else secs[-1][2]
    return lanes, section_ids(lanes)


# ------------------------------------------------------------------------------ lane linking

def _lane_centre_offsets(road: Road, contact: str = "start") -> dict[int, tuple[float, Lane]]:
    """OpenDRIVE lane id at ``contact`` -> (signed lateral offset t of the lane centre, lane)."""
    lanes, ids = _contact_lanes(road, contact)
    out: dict[int, tuple[float, Lane]] = {}
    acc = 0.0
    for l in sorted((l for l in lanes if l.id > 0), key=lambda l: l.id):
        out[ids[l.id]] = (acc + l.width / 2.0, l)
        acc += l.width
    acc = 0.0
    for l in sorted((l for l in lanes if l.id < 0), key=lambda l: -l.id):
        out[ids[l.id]] = (-(acc + l.width / 2.0), l)
        acc += l.width
    return out


def _end_pose(road: Road, contact: str) -> tuple[np.ndarray, np.ndarray]:
    """(point, unit normal-left) of the reference line at its start or end."""
    c = np.asarray(road.reference_line.coords, dtype=np.float64)[:, :2]
    c = _dedupe(c)
    if contact == "start":
        p, d = c[0], c[1] - c[0]
    else:
        p, d = c[-1], c[-1] - c[-2]
    d = d / max(np.linalg.norm(d), 1e-12)
    return p, np.array([-d[1], d[0]])


def _lane_end_points(road: Road, contact: str) -> dict[int, tuple[np.ndarray, Lane]]:
    p, n = _end_pose(road, contact)
    return {lid: (p + n * t, lane) for lid, (t, lane) in _lane_centre_offsets(road, contact).items()}


def _nearest_lane(point: np.ndarray, other: Road, contact: str, lane_type: str,
                  tol: float = 1.5, want_right: Optional[bool] = None) -> Optional[int]:
    """Nearest lane centre of ``lane_type`` on ``other`` at ``contact`` (its OpenDRIVE id
    there). ``want_right`` restricts the candidates to one side: the lanes of a road are only
    a lane apart, so without it a driving lane can be linked to its oncoming neighbour — a
    link CARLA cannot traverse."""
    best, best_d = None, tol
    for lid, (q, lane) in _lane_end_points(other, contact).items():
        if lane.type != lane_type:
            continue
        if want_right is not None and (lid < 0) != want_right:
            continue
        d = float(np.linalg.norm(q - point))
        if d < best_d:
            best, best_d = lid, d
    return best


def _ordinal_lane(road: Road, lane: Lane, contact: str, other: Road, other_contact: str
                  ) -> Optional[int]:
    """Fallback for :func:`_nearest_lane`: match the lanes of the same type ordinally, inner
    edge outwards, clamping to the outermost one on ``other``.

    The geometric match compares lane *centres*, so it fails whenever the two linked roads have
    different lane counts (a 3-lane carriageway meeting a 4-lane one is centred on the same OSM
    way, so every centre is offset by half a lane). Without a fallback the narrower road's outer
    lanes simply stop: a dead end in the middle of the map that the traffic manager routes
    vehicles into before deleting them, and a stair-stepped wedge in the mesh."""
    flip = contact == other_contact  # end->end / start->start reverses the travel direction
    want_right = (lane.id < 0) != flip
    mine_lanes, _ = _contact_lanes(road, contact)
    theirs_lanes, their_ids = _contact_lanes(other, other_contact)
    mine = [l.id for l in sorted((l for l in mine_lanes if (l.id < 0) == (lane.id < 0)),
                                 key=lambda l: abs(l.id)) if l.type == lane.type]
    theirs = [their_ids[l.id] for l in sorted((l for l in theirs_lanes if (l.id < 0) == want_right),
                                              key=lambda l: abs(l.id)) if l.type == lane.type]
    if not theirs or lane.id not in mine:
        return None
    return theirs[min(mine.index(lane.id), len(theirs) - 1)]


def _lane_links(model: TwinModel, road: Road) -> dict[int, dict[str, int]]:
    """Per model lane id -> {"predecessor": id, "successor": id} at road-to-road links; the
    values are the linked road's OpenDRIVE lane ids at its contact.

    Priority: explicit junction ``lane_links`` (for connecting roads), then geometric nearest
    lane of the same type on the linked road (within 1.5 m).  Links into a junction get no
    lane-level entry (``<laneLink>`` carries them). A lane absent at a contact (an auxiliary
    lane that tapered out before it) gets no link there.
    """
    links: dict[int, dict[str, int]] = {l.id: {} for l in road.lanes}
    # explicit lane links from junction connections (incoming -> connecting road)
    if road.junction_id is not None:
        try:
            junction = model.junction(road.junction_id)
        except KeyError:
            junction = None
        if junction is not None:
            for conn in junction.connections:
                if conn.connecting_road != road.id:
                    continue
                key = "predecessor" if conn.contact_point == "start" else "successor"
                inc_ids = _incoming_contact_ids(model, junction, conn.incoming_road)
                for ll in conn.lane_links:
                    if ll.to_lane in links and key not in links[ll.to_lane]:
                        links[ll.to_lane][key] = inc_ids.get(ll.from_lane, ll.from_lane)
    for key, link, contact in (("predecessor", road.predecessor, "start"),
                               ("successor", road.successor, "end")):
        if link is None or link.element != "road":
            continue
        try:
            other = model.road(link.id)
        except KeyError:
            log.warning("road %s links to unknown road %s", road.id, link.id)
            continue
        other_contact = link.contact or ("start" if key == "successor" else "end")
        mine, my_ids = _contact_lanes(road, contact)
        ends = _lane_end_points(road, contact)
        for lane in mine:
            if key in links[lane.id]:
                continue
            want_right = ((lane.id < 0) != (contact == other_contact)
                          if lane.type == "driving" else None)
            nearest = _nearest_lane(ends[my_ids[lane.id]][0], other, other_contact, lane.type,
                                    want_right=want_right)
            if nearest is None and lane.type == "driving" and not lane.tags.get("aux"):
                # (an auxiliary lane is fed by its ramp, or ends in the lane beside it: never
                # by the ordinal fallback, which would declare a through lane its origin)
                nearest = _ordinal_lane(road, lane, contact, other, other_contact)
                if nearest is not None:
                    log.debug("road %s lane %d -> %s lane %d by ordinal match (lane counts differ)",
                              road.id, lane.id, other.id, nearest)
            if nearest is not None:
                links[lane.id][key] = nearest
    return links


def _incoming_contact_ids(model: TwinModel, junction, road_id: str) -> dict[int, int]:
    """Model lane id -> OpenDRIVE lane id of ``road_id`` at the end that touches ``junction``
    (an auxiliary lane absent there renumbers the lanes outboard of it)."""
    try:
        road = model.road(road_id)
    except KeyError:
        return {}
    contact = ("end" if road.successor is not None and road.successor.element == "junction"
               and road.successor.id == junction.id else "start")
    _lanes, ids = _contact_lanes(road, contact)
    return ids


def _inboard_continuing(lane: Lane, lanes: list[Lane]) -> Optional[Lane]:
    """The nearest lane inboard of ``lane`` on its side that is present in ``lanes`` (where an
    auxiliary lane ends, its traffic continues in the lane it merges into)."""
    side = [l for l in lanes if (l.id < 0) == (lane.id < 0) and abs(l.id) < abs(lane.id)
            and l.type == "driving"]
    return max(side, key=lambda l: abs(l.id)) if side else None


# ------------------------------------------------------------------------------ XML helpers

def _f(x: float) -> str:
    return f"{float(x):.10g}" if abs(x) > 1e-12 else "0"


def _sub(parent, tag, **attrs):
    return etree.SubElement(parent, tag, {k: (v if isinstance(v, str) else _f(v))
                                          for k, v in attrs.items() if v is not None})


def _road_mark(parent, m: Optional[Marking], default_type: str = "none") -> None:
    if m is None:
        _sub(parent, "roadMark", sOffset=0, type=default_type, weight="standard",
             color="standard", width=0, laneChange="none", height=0)
        return
    _sub(parent, "roadMark", sOffset=0, type=m.kind, weight="standard",
         color=_MARK_COLORS.get(m.color, "standard"), material="standard", width=m.width,
         laneChange="both" if m.kind == "broken" else "none", height=0.0)


def _signal_attrs(sig: Signal) -> dict[str, str]:
    if sig.kind == CROSSWALK_KIND or sig.kind not in SIGNAL_TYPES:
        raise ValueError(sig.kind)
    stype, subtype, dynamic = SIGNAL_TYPES[sig.kind]
    value = None
    unit = None
    if sig.kind == "speed_limit":
        kmh = int(round((sig.value or 0.0) * 3.6))
        subtype = str(kmh)
        value = kmh
        unit = "km/h"
    name = sig.tags.get("name") or {"traffic_light": "Signal_3Light_Post01", "stop": "Sign_Stop",
                                    "yield": "Sign_Yield", "speed_limit": f"Speed_{subtype}"}[sig.kind]
    # Stamp the build profile's primary ISO country instead of the stock "OpenDRIVE"
    # sentinel: the type/subtype vocabulary stays SignalType.h (StVO codes) either way, but
    # the country attribute is the geo-style hint a prop selector can key on (CARLA parses it
    # into Signal::GetCountry() / carla.Landmark.country and currently ignores it).
    country = profiles.get().signal_country
    attrs = dict(id=sig.id, s=sig.s, t=sig.t, name=name, dynamic=dynamic, orientation=sig.orientation,
                 zOffset=0, country=country, type=stype, subtype=subtype, hOffset=0, pitch=0, roll=0)
    if value is not None:
        attrs.update(value=value, unit=unit)
    return attrs


def _validity_ranges(sig: Signal, sections: list, ids_per_section: list[dict[int, int]]
                     ) -> list[tuple[int, int]]:
    """``sig.validities`` (model lane ids) as OpenDRIVE lane-id runs at the signal's ``s``.

    ``section_ids`` renumbers lanes contiguously outward inside each ``<laneSection>``, so a
    road whose model ids are not contiguous (an auxiliary lane that ends mid-road) exports
    different ids than the model carries. Translate through the section that covers ``sig.s``;
    lanes absent there are dropped.
    """
    if not sig.validities:
        return []
    k = 0
    for i, (s0, _s1, _lanes) in enumerate(sections):
        if s0 <= sig.s + AUX_EPS:
            k = i
    ids = ids_per_section[k]
    out: set[int] = set()
    for a, b in sig.validities:
        lo, hi = (a, b) if a <= b else (b, a)
        for lid in range(lo, hi + 1):
            if lid == 0:
                continue
            x = ids.get(lid)
            if x is not None and x != 0:
                out.add(x)
    runs: list[tuple[int, int]] = []
    for i in sorted(out):
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _controllers(model: TwinModel) -> list[Controller]:
    """Model controllers, or synthesised one-per-controller_id from the traffic lights."""
    if model.controllers:
        return list(model.controllers)
    incoming_to_junction: dict[str, str] = {}
    for j in model.junctions:
        for c in j.connections:
            incoming_to_junction.setdefault(c.incoming_road, j.id)
    by_id: dict[str, Controller] = {}
    for sig in model.signals:
        if sig.kind != "traffic_light" or sig.controller_id is None:
            continue
        if sig.controller_id not in by_id:
            try:
                road = model.road(sig.road_id)
            except KeyError:
                road = None
            jid = incoming_to_junction.get(sig.road_id) or (road.junction_id if road else None) or ""
            by_id[sig.controller_id] = Controller(id=sig.controller_id, junction_id=jid)
        by_id[sig.controller_id].signal_ids.append(sig.id)
    return list(by_id.values())


# ------------------------------------------------------------------------------ writer

def _write_road(parent, model: TwinModel, road: Road, ids: IdMap, signals: list[Signal]) -> None:
    geoms, s_vertices = road_geometry(road)
    length = float(sum(g.length for g in geoms))
    junction = ids.junction.get(road.junction_id, -1) if road.junction_id else -1
    r = _sub(parent, "road", name=road.name or road.id, length=length, id=str(ids.road[road.id]),
             junction=str(junction))
    link = _sub(r, "link")
    for tag, rl in (("predecessor", road.predecessor), ("successor", road.successor)):
        if rl is None:
            continue
        if rl.element == "road":
            if rl.id not in ids.road:
                log.warning("road %s: %s -> unknown road %s", road.id, tag, rl.id)
                continue
            _sub(link, tag, elementType="road", elementId=str(ids.road[rl.id]),
                 contactPoint=rl.contact or "start")
        else:
            if rl.id not in ids.junction:
                log.warning("road %s: %s -> unknown junction %s", road.id, tag, rl.id)
                continue
            _sub(link, tag, elementType="junction", elementId=str(ids.junction[rl.id]))
    speeds = [l.speed_limit for l in road.lanes if l.speed_limit]
    t = _sub(r, "type", s=0, type="town")
    if speeds:
        _sub(t, "speed", max=max(speeds) * 3.6, unit="km/h")

    pv = _sub(r, "planView")
    for g in geoms:
        ge = _sub(pv, "geometry", s=g.s, x=g.x, y=g.y, hdg=g.hdg, length=g.length)
        if g.kind == "line":
            _sub(ge, "line")
        else:
            aU, bU, cU, dU, aV, bV, cV, dV = g.coeffs
            _sub(ge, "paramPoly3", aU=aU, bU=bU, cU=cU, dU=dU, aV=aV, bV=bV, cV=cV, dV=dV,
                 pRange="normalized")

    coords = _dedupe(np.asarray(road.reference_line.coords, dtype=np.float64))
    z = coords[:, 2] if coords.shape[1] > 2 else np.zeros(len(coords))
    ep = _sub(r, "elevationProfile")
    for s, a, b, c, d in fit_elevation(s_vertices, z):
        _sub(ep, "elevation", s=s, a=a, b=b, c=c, d=d)
    _sub(r, "lateralProfile")

    lanes = _sub(r, "lanes")
    _sub(lanes, "laneOffset", s=0, a=0, b=0, c=0, d=0)
    links = _lane_links(model, road)
    P = profiles.get()
    heights = {"sidewalk": P.sidewalk.z, "verge": P.sidewalk.curb_height}
    sections = road_sections(road)
    ids_per_section = [section_ids(lanes_) for _s0, _s1, lanes_ in sections]

    def width_records(lane: Lane, s0: float, s1: float) -> list[tuple[float, float, float]]:
        """``(sOffset, a, b)`` of the lane's width polynomial(s) inside section ``[s0, s1]``."""
        if not lane.tags.get("aux"):
            return [(0.0, lane.width, 0.0)]
        t0, t1 = lane.tags.get("taper_s0"), lane.tags.get("taper_s1")
        if t0 is None or t1 is None or float(t1) - float(t0) <= AUX_EPS:
            return [(0.0, lane.width, 0.0)]
        t0, t1 = max(float(t0), s0), min(float(t1), s1)
        if t1 - t0 <= AUX_EPS:
            return [(0.0, aux_width_at(lane, road, s0), 0.0)]
        slope = (aux_width_at(lane, road, t1) - aux_width_at(lane, road, t0)) / (t1 - t0)
        recs = []
        if t0 - s0 > AUX_EPS:
            recs.append((0.0, aux_width_at(lane, road, s0), 0.0))
        recs.append((t0 - s0, aux_width_at(lane, road, t0), slope))
        if s1 - t1 > AUX_EPS:
            recs.append((t1 - s0, aux_width_at(lane, road, t1), 0.0))
        return recs

    for k, (s0, s1, present) in enumerate(sections):
        sec = _sub(lanes, "laneSection", s=s0)
        ids = ids_per_section[k]
        prev_ids = ids_per_section[k - 1] if k > 0 else None
        next_ids = ids_per_section[k + 1] if k + 1 < len(sections) else None

        def write_lane(container, lane: Lane):
            le = _sub(container, "lane", id=str(ids[lane.id]), type=xodr_lane_type(lane.type), level="false")
            lk = _sub(le, "link")
            # predecessor: the previous section (an auxiliary lane starting here branches off
            # the lane inboard of it), or the linked road at the road start
            if prev_ids is not None:
                if lane.id in prev_ids:
                    _sub(lk, "predecessor", id=str(prev_ids[lane.id]))
                else:
                    inb = _inboard_continuing(lane, sections[k - 1][2])
                    if inb is not None:
                        _sub(lk, "predecessor", id=str(prev_ids[inb.id]))
            elif "predecessor" in links[lane.id]:
                _sub(lk, "predecessor", id=str(links[lane.id]["predecessor"]))
            if next_ids is not None:
                if lane.id in next_ids:
                    _sub(lk, "successor", id=str(next_ids[lane.id]))
                else:
                    inb = _inboard_continuing(lane, sections[k + 1][2])
                    if inb is not None:
                        _sub(lk, "successor", id=str(next_ids[inb.id]))
            elif "successor" in links[lane.id]:
                _sub(lk, "successor", id=str(links[lane.id]["successor"]))
            for s_off, a, b in width_records(lane, s0, s1):
                _sub(le, "width", sOffset=s_off, a=a, b=b, c=0, d=0)
            _road_mark(le, lane.marking)
            if lane.type in RAISED_LANE_TYPES:
                h = heights[lane.type]
                _sub(le, "height", sOffset=0, inner=h, outer=h)
            if lane.speed_limit:
                _sub(le, "speed", sOffset=0, max=lane.speed_limit * 3.6, unit="km/h")

        left = sorted((l for l in present if l.id > 0), key=lambda l: l.id)
        if left:
            el = _sub(sec, "left")
            for lane in reversed(left):  # OpenDRIVE: highest id first
                write_lane(el, lane)
        centre = _sub(_sub(sec, "center"), "lane", id="0", type="none", level="false")
        _sub(centre, "link")
        _road_mark(centre, road.center_marking)
        right = sorted((l for l in present if l.id < 0), key=lambda l: -l.id)
        if right:
            er = _sub(sec, "right")
            for lane in right:
                write_lane(er, lane)

    objects = _sub(r, "objects")
    sigs = _sub(r, "signals")
    wl = road.width_left()
    wr = road.width_right()
    for sig in signals:
        if sig.kind == CROSSWALK_KIND:
            width_s = float(sig.tags.get("width", P.crossing.width))
            half_t = (wl + wr) / 2.0
            centre_t = (wl - wr) / 2.0
            ob = _sub(objects, "object", id=sig.id, name="Crosswalk", s=sig.s, t=centre_t,
                      zOffset=0, orientation="none", hdg=math.pi / 2, pitch=0, roll=0,
                      type="crosswalk", width=width_s, length=2 * half_t)
            ol = _sub(ob, "outline")
            # u across the road (hdg = pi/2 rotates u onto t), v along s; closed ring
            for u, v in ((-half_t, -width_s / 2), (half_t, -width_s / 2), (half_t, width_s / 2),
                         (-half_t, width_s / 2), (-half_t, -width_s / 2)):
                _sub(ol, "cornerLocal", u=u, v=v, z=0)
            continue
        se = _sub(sigs, "signal", **_signal_attrs(sig))
        for lo, hi in _validity_ranges(sig, sections, ids_per_section):
            _sub(se, "validity", fromLane=str(lo), toLane=str(hi))
    ud = _sub(r, "userData")
    _sub(ud, "twin", roadId=road.id, junctionId=road.junction_id or "")


def export_xodr(model: TwinModel, path: Optional[Path | str] = None) -> str:
    """Write ``model`` as OpenDRIVE 1.4 and return the XML text (also written to ``path``)."""
    ids = build_id_map(model)
    root = etree.Element("OpenDRIVE")

    xs, ys = [], []
    for r in model.roads:
        b = r.reference_line.bounds
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    hdr = _sub(root, "header", revMajor="1", revMinor="4", name=model.name, version="1",
               date=_dt.datetime.now().replace(microsecond=0).isoformat(),
               north=max(ys, default=0.0), south=min(ys, default=0.0),
               east=max(xs, default=0.0), west=min(xs, default=0.0), vendor="twinmodel")
    geo = _sub(hdr, "geoReference")
    geo.text = etree.CDATA(model.geo_reference)

    signals_by_road: dict[str, list[Signal]] = {}
    for sig in model.signals:
        signals_by_road.setdefault(sig.road_id, []).append(sig)
    for sig_road in signals_by_road:
        if sig_road not in ids.road:
            log.warning("signals on unknown road %s dropped", sig_road)

    for road in model.roads:
        _write_road(root, model, road, ids, signals_by_road.get(road.id, []))

    controllers = _controllers(model)
    for ctl in controllers:
        ce = _sub(root, "controller", id=ctl.id, name=f"ctrl_{ctl.id}", sequence="0")
        for sid in ctl.signal_ids:
            _sub(ce, "control", signalId=sid, type="0")

    for j in model.junctions:
        je = _sub(root, "junction", id=str(ids.junction[j.id]), name=j.name or j.id)
        for i, conn in enumerate(j.connections):
            if conn.incoming_road not in ids.road or conn.connecting_road not in ids.road:
                log.warning("junction %s: connection %s references unknown road", j.id, conn.id)
                continue
            ce = _sub(je, "connection", id=str(i), incomingRoad=str(ids.road[conn.incoming_road]),
                      connectingRoad=str(ids.road[conn.connecting_road]),
                      contactPoint=conn.contact_point)
            inc_ids = _incoming_contact_ids(model, j, conn.incoming_road)
            for ll in conn.lane_links:
                _sub(ce, "laneLink", **{"from": str(inc_ids.get(ll.from_lane, ll.from_lane)),
                                        "to": str(ll.to_lane)})
        for ctl in controllers:
            if ctl.junction_id == j.id:
                _sub(je, "controller", id=ctl.id, type="0", sequence="0")
        _sub(_sub(je, "userData"), "twin", junctionId=j.id)

    text = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode()
    if path is not None:
        Path(path).write_text(text)
        log.info("wrote %s (%d roads, %d junctions, %d signals)", path, len(model.roads),
                 len(model.junctions), len(model.signals))
    return text


def read_twin_ids(xodr_text: str) -> Optional[IdMap]:
    """Recover the model-id mapping embedded as ``<userData><twin .../>`` (None if absent)."""
    try:
        root = etree.fromstring(xodr_text.encode())
    except etree.XMLSyntaxError:
        return None
    ids = IdMap()
    for r in root.iter("road"):
        tw = r.find("userData/twin")
        if tw is None:
            return None
        ids.road[tw.get("roadId")] = int(r.get("id"))
        if tw.get("junctionId"):
            ids.junction.setdefault(tw.get("junctionId"), int(r.get("junction")))
    for j in root.findall("junction"):
        tw = j.find("userData/twin")
        if tw is not None and tw.get("junctionId"):
            ids.junction[tw.get("junctionId")] = int(j.get("id"))
    return ids
