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
from ..model import Controller, Lane, Marking, Road, Signal, TwinModel

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


def fit_planview(coords: np.ndarray) -> list[Geom]:
    """Fit an (N, 2+) polyline as a G1-continuous piecewise cubic ``paramPoly3`` planview.

    Tangent *directions* at the vertices are Catmull-Rom (chord of the neighbours), the
    tangent magnitude per segment equals the segment chord length so the parametrisation is
    close to arc length and the curve does not overshoot on uneven spacing.  Each segment is a
    cubic Hermite expressed in the local frame rotated to the start tangent, hence
    ``aU = aV = bV = 0`` and ``hdg`` is continuous between consecutive geometries.  A two-point
    polyline becomes a single ``line``.
    """
    pts = _dedupe(np.asarray(coords, dtype=np.float64))[:, :2]
    if len(pts) < 2:
        raise ValueError("reference line needs at least two distinct points")
    if len(pts) == 2:
        d = pts[1] - pts[0]
        L = float(np.hypot(*d))
        return [Geom(0.0, float(pts[0, 0]), float(pts[0, 1]), math.atan2(d[1], d[0]), L, "line")]

    # unit tangent directions per vertex
    tang = np.zeros_like(pts)
    tang[0] = pts[1] - pts[0]
    tang[-1] = pts[-1] - pts[-2]
    tang[1:-1] = pts[2:] - pts[:-2]
    tang /= np.linalg.norm(tang, axis=1, keepdims=True)

    geoms: list[Geom] = []
    s = 0.0
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        chord = p1 - p0
        L = float(np.hypot(*chord))
        hdg = math.atan2(tang[i][1], tang[i][0])
        c, sn = math.cos(hdg), math.sin(hdg)
        rot = np.array([[c, sn], [-sn, c]])  # world -> local (u along start tangent)
        p1l = rot @ chord
        m0l = np.array([L, 0.0])
        m1l = rot @ (tang[i + 1] * L)
        # Hermite -> power basis on p in [0, 1]
        a = np.zeros(2)
        b = m0l
        cc = 3.0 * p1l - 2.0 * m0l - m1l
        d = -2.0 * p1l + m0l + m1l
        coeffs = (a[0], b[0], cc[0], d[0], a[1], b[1], cc[1], d[1])
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
    if geoms[0].kind == "line":
        s_vertices = np.array([0.0, geoms[0].length])
    else:
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


# ------------------------------------------------------------------------------ lane linking

def _lane_centre_offsets(road: Road) -> dict[int, float]:
    """Signed lateral offset (t) of each lane's centre from the reference line."""
    out: dict[int, float] = {}
    acc = 0.0
    for l in road.lanes_left():
        out[l.id] = acc + l.width / 2.0
        acc += l.width
    acc = 0.0
    for l in road.lanes_right():
        out[l.id] = -(acc + l.width / 2.0)
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


def _lane_end_points(road: Road, contact: str) -> dict[int, np.ndarray]:
    p, n = _end_pose(road, contact)
    return {lid: p + n * t for lid, t in _lane_centre_offsets(road).items()}


def _nearest_lane(point: np.ndarray, other: Road, contact: str, lane_type: str,
                  tol: float = 1.5) -> Optional[int]:
    best, best_d = None, tol
    for lid, q in _lane_end_points(other, contact).items():
        ltype = next(l.type for l in other.lanes if l.id == lid)
        if ltype != lane_type:
            continue
        d = float(np.linalg.norm(q - point))
        if d < best_d:
            best, best_d = lid, d
    return best


def _lane_links(model: TwinModel, road: Road) -> dict[int, dict[str, int]]:
    """Per lane id -> {"predecessor": id, "successor": id} at road-to-road links.

    Priority: explicit junction ``lane_links`` (for connecting roads), then geometric nearest
    lane of the same type on the linked road (within 1.5 m).  Links into a junction get no
    lane-level entry (``<laneLink>`` carries them).
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
                for ll in conn.lane_links:
                    if ll.to_lane in links and key not in links[ll.to_lane]:
                        links[ll.to_lane][key] = ll.from_lane
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
        ends = _lane_end_points(road, contact)
        for lane in road.lanes:
            if key in links[lane.id]:
                continue
            nearest = _nearest_lane(ends[lane.id], other, other_contact, lane.type)
            if nearest is not None:
                links[lane.id][key] = nearest
    return links


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
    attrs = dict(id=sig.id, s=sig.s, t=sig.t, name=name, dynamic=dynamic, orientation=sig.orientation,
                 zOffset=0, country="OpenDRIVE", type=stype, subtype=subtype, hOffset=0, pitch=0, roll=0)
    if value is not None:
        attrs.update(value=value, unit=unit)
    return attrs


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
    sec = _sub(lanes, "laneSection", s=0)
    links = _lane_links(model, road)
    P = profiles.get()
    heights = {"sidewalk": P.sidewalk.z, "verge": P.sidewalk.curb_height}

    def write_lane(container, lane: Lane):
        le = _sub(container, "lane", id=str(lane.id), type=xodr_lane_type(lane.type), level="false")
        lk = _sub(le, "link")
        for tag in ("predecessor", "successor"):
            if tag in links[lane.id]:
                _sub(lk, tag, id=str(links[lane.id][tag]))
        _sub(le, "width", sOffset=0, a=lane.width, b=0, c=0, d=0)
        _road_mark(le, lane.marking)
        if lane.type in RAISED_LANE_TYPES:
            h = heights[lane.type]
            _sub(le, "height", sOffset=0, inner=h, outer=h)
        if lane.speed_limit:
            _sub(le, "speed", sOffset=0, max=lane.speed_limit * 3.6, unit="km/h")

    left = road.lanes_left()
    if left:
        el = _sub(sec, "left")
        for lane in reversed(left):  # OpenDRIVE: highest id first
            write_lane(el, lane)
    centre = _sub(_sub(sec, "center"), "lane", id="0", type="none", level="false")
    _sub(centre, "link")
    _road_mark(centre, road.center_marking)
    right = road.lanes_right()
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
        _sub(sigs, "signal", **_signal_attrs(sig))
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
            for ll in conn.lane_links:
                _sub(ce, "laneLink", **{"from": str(ll.from_lane), "to": str(ll.to_lane)})
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
