"""OSM ways -> Roads / Lanes / Junctions / Signals (the lane graph).

Pipeline (all pure functions over :mod:`twinmodel.model` dataclasses):

1. select drivable ``highway=*`` ways, normalise ``oneway=-1``, clip to the bbox
2. node degrees in the drivable graph -> intersection nodes
3. cluster intersection nodes (<= ``profiles.get().junction.cluster_m`` apart *and* joined
   by a way shorter than that) into junctions — the Eixample chamfer octagons collapse into one junction each
4. split ways at intersection nodes, chain compatible pieces through degree-2 nodes -> roads
5. lanes from the active profile's class defaults (``profiles.LaneRules``) + tag overrides; reference line = OSM centreline shifted so
   that it sits between forward and backward carriageway lanes (oneway: left carriageway edge).
   In a street canyon (building faces on both sides, ``streetspace``) the cross section comes
   from the faces instead: sidewalks along the faces, the carriageway centred in between,
   driving lanes widened, leftover width as parking lanes
6. trim roads at the junction area: canyon arms at the chamfer line (where their faces end),
   the others at the cluster hull buffered by half the road width + 2 m; every junction gets
   a plaza polygon (the corner void clipped to the arms' street corridors)
7. connecting roads: cubic Hermite per legal (incoming lane -> outgoing lane) pair, respecting
   ``oneway``, ``turn:lanes`` and ``type=restriction`` relations (no u-turns)
8. signals (traffic lights + one controller per junction, crossings, stop/yield, speed limits)
9. buildings (height / levels parsing) and point objects (trees, traffic signs)

Quick look::

    python -m twinmodel.lanegraph --fixture tests/fixtures/eixample_overpass.json \\
        --out out/eixample_lanes.png
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union, polygonize, substring
from shapely.strtree import STRtree

from . import profiles, streetspace
from .frame import LocalFrame
from .ingest.osm import OsmData, OsmNode, OsmRelation, OsmWay
from .model import (Building, Connection, Controller, Junction, Lane, LaneLink, Marking,
                    PointObject, Road, RoadLink, Signal, TwinModel, lane_present_at)

log = logging.getLogger("twinmodel.lanegraph")

# --------------------------------------------------------------------------- parameters
#
# Every regional / dimensional constant (lane widths per highway class, junction clustering
# distances, marking colours, crossing width, ...) lives in :mod:`twinmodel.profiles`.
# Functions read ``P = profiles.get()`` *at call time* so tests and the CLI can switch the
# active profile; nothing regional is cached at import time. Only pure numerical
# tolerances (precision epsilons, sample counts) remain in this module.

_LEFT_TURNS = {"left", "slight_left", "sharp_left"}
_RIGHT_TURNS = {"right", "slight_right", "sharp_right"}
_THROUGH_TURNS = {"through", "merge_to_left", "merge_to_right"}


# --------------------------------------------------------------------------- small geometry

def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def _heading(p: Iterable[float], q: Iterable[float]) -> float:
    return math.atan2(q[1] - p[1], q[0] - p[0])


def _polyline_length(coords: list[tuple[float, float]]) -> float:
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(coords, coords[1:])))


def _dedupe(coords: list[tuple[float, float]], eps: float = 1e-3) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for c in coords:
        if not out or math.hypot(c[0] - out[-1][0], c[1] - out[-1][1]) > eps:
            out.append((float(c[0]), float(c[1])))
    return out


def _offset_polyline(coords: list[tuple[float, float]], d: float, mitre_limit: float = 2.0
                     ) -> list[tuple[float, float]]:
    """Offset a polyline by ``d`` metres to the *left* (negative = right), mitre joins limited
    to ``mitre_limit * |d|`` so acute vertices do not explode."""
    if abs(d) < 1e-9 or len(coords) < 2:
        return list(coords)
    pts = np.asarray(coords, dtype=float)
    seg = pts[1:] - pts[:-1]
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    seg_len[seg_len == 0] = 1e-9
    normals = np.stack([-seg[:, 1], seg[:, 0]], axis=1) / seg_len[:, None]  # left normals
    out = []
    for i in range(len(pts)):
        if i == 0:
            n = normals[0]
        elif i == len(pts) - 1:
            n = normals[-1]
        else:
            n = normals[i - 1] + normals[i]
            nn = np.hypot(*n)
            if nn < 1e-6:  # 180 degree turn back: fall back to the incoming normal
                n = normals[i - 1]
            else:
                n = n / nn
                cos_half = float(np.dot(n, normals[i]))
                scale = 1.0 / max(cos_half, 1.0 / mitre_limit)
                n = n * scale
        out.append((float(pts[i, 0] + d * n[0]), float(pts[i, 1] + d * n[1])))
    return out


def _heading_along(line: LineString, s: float, ds: float = 0.5) -> float:
    L = line.length
    s0 = max(0.0, min(L, s - ds))
    s1 = max(0.0, min(L, s + ds))
    if s1 - s0 < 1e-9:
        s0, s1 = 0.0, L
    p, q = line.interpolate(s0), line.interpolate(s1)
    return _heading((p.x, p.y), (q.x, q.y))


def point_on_road(road: Road, s: float, t: float) -> Point:
    """Model-space point at (s, t) on a road: t positive = left of the reference line."""
    line = road.reference_line
    s = max(0.0, min(line.length, s))
    p = line.interpolate(s)
    h = _heading_along(line, s)
    return Point(p.x - t * math.sin(h), p.y + t * math.cos(h))


def _hermite(p0, h0, p1, h1, step: Optional[float] = None) -> list[tuple[float, float]]:
    """Cubic Hermite from p0 (heading h0) to p1 (heading h1), resampled every ``step`` m
    (default: the profile's ``geometry.connect_sample_m``)."""
    if step is None:
        step = profiles.get().geometry.connect_sample_m
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    d = float(np.hypot(*(p1 - p0)))
    if d < 1e-6:
        return [tuple(p0), tuple(p1)]
    m0 = np.array([math.cos(h0), math.sin(h0)]) * d
    m1 = np.array([math.cos(h1), math.sin(h1)]) * d
    n = max(16, int(d * 8))
    u = np.linspace(0.0, 1.0, n)[:, None]
    pts = ((2 * u ** 3 - 3 * u ** 2 + 1) * p0 + (u ** 3 - 2 * u ** 2 + u) * m0
           + (-2 * u ** 3 + 3 * u ** 2) * p1 + (u ** 3 - u ** 2) * m1)
    return _resample([tuple(p) for p in pts], step)


def _resample(coords: list[tuple[float, float]], step: float) -> list[tuple[float, float]]:
    line = LineString(coords)
    L = line.length
    if L < step:
        return [coords[0], coords[-1]]
    n = int(math.floor(L / step))
    ss = [i * step for i in range(n + 1)]
    if L - ss[-1] > 0.25 * step:
        ss.append(L)
    else:
        ss[-1] = L
    return [(p.x, p.y) for p in (line.interpolate(s) for s in ss)]


def _line3d(coords: list[tuple[float, float]]) -> LineString:
    return LineString([(x, y, 0.0) for x, y in coords])


def _line2d(line: LineString) -> LineString:
    return LineString([(x, y) for x, y, *_ in line.coords])


def _remove_jogs(coords: list[tuple[float, float]], max_len: Optional[float] = None,
                 min_turn_deg: Optional[float] = None, transition: Optional[float] = None
                 ) -> tuple[list[tuple[float, float]], int]:
    """Replace lateral jogs (a segment shorter than ``max_len`` that turns sharply at both ends
    and comes back to the previous heading — OSM mappers draw a 3 m sideways step this way) by a
    gradual shift spread over ``transition`` m on either side. -> (coords, jogs removed).
    Defaults: the profile's ``geometry.jog_*``."""
    G = profiles.get().geometry
    max_len = G.jog_max_m if max_len is None else max_len
    min_turn_deg = G.jog_min_turn_deg if min_turn_deg is None else min_turn_deg
    transition = G.jog_transition_m if transition is None else transition
    pts = list(coords)
    n_removed = 0
    changed = True
    while changed and len(pts) >= 4:
        changed = False
        for i in range(1, len(pts) - 2):
            a, b, c, d = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            if math.dist(b, c) > max_len:
                continue
            h0, h1, h2 = _heading(a, b), _heading(b, c), _heading(c, d)
            if (abs(_wrap(h1 - h0)) < math.radians(min_turn_deg)
                    or abs(_wrap(h2 - h1)) < math.radians(min_turn_deg)
                    or abs(_wrap(h2 - h0)) > math.radians(30.0)):
                continue
            ta = min(transition, 0.5 * math.dist(a, b))
            td = min(transition, 0.5 * math.dist(c, d))
            pa = (b[0] - math.cos(h0) * ta, b[1] - math.sin(h0) * ta)
            pd = (c[0] + math.cos(h2) * td, c[1] + math.sin(h2) * td)
            pts[i:i + 2] = [pa, pd]
            n_removed += 1
            changed = True
            break
    return _dedupe(pts), n_removed


def _fillet_corners(coords: list[tuple[float, float]], max_radius: Optional[float] = None,
                    min_turn_deg: Optional[float] = None, step: Optional[float] = None
                    ) -> tuple[list[tuple[float, float]], int]:
    """Round every interior corner of a polyline with a tangent circular arc, sampled every
    ``step`` m. OSM maps a road as a few straight legs between nodes; a lateral that swerves
    around a chamfered corner is drawn as two 20-degree corners 10 m apart, and a road built on
    that polyline kinks there (surface, curbs, markings and the vehicles' paths all follow the
    corner: the saw-tooth laterals of Passeig de Gracia). The arc radius is ``max_radius``
    capped so that the two tangent lengths of a corner never take more than half of either
    adjacent leg (consecutive fillets cannot overlap and the end points never move). Corners
    turning less than ``min_turn_deg`` are left alone. -> (coords, corners rounded).
    Defaults: the profile's ``geometry.fillet_*`` and ``connect_sample_m``."""
    G = profiles.get().geometry
    max_radius = G.fillet_radius_m if max_radius is None else max_radius
    min_turn_deg = G.fillet_min_turn_deg if min_turn_deg is None else min_turn_deg
    step = G.connect_sample_m if step is None else step
    pts = _dedupe(list(coords))
    if len(pts) < 3 or max_radius <= 0.0:
        return pts, 0
    n = len(pts)
    legs = [math.dist(pts[i], pts[i + 1]) for i in range(n - 1)]
    out: list[tuple[float, float]] = [pts[0]]
    n_round = 0
    for i in range(1, n - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        h0, h1 = _heading(a, b), _heading(b, c)
        turn = _wrap(h1 - h0)
        if abs(turn) < math.radians(min_turn_deg) or abs(turn) > math.radians(150.0):
            out.append(b)
            continue
        # tangent length t = R tan(turn/2), capped at half of either leg
        half = abs(math.tan(turn / 2.0))
        t = min(max_radius * half, 0.5 * legs[i - 1], 0.5 * legs[i])
        if t < 0.05:
            out.append(b)
            continue
        R = t / half
        p_in = (b[0] - math.cos(h0) * t, b[1] - math.sin(h0) * t)
        # centre: p_in offset to the inside of the turn
        side = 1.0 if turn > 0 else -1.0
        cx, cy = p_in[0] - math.sin(h0) * R * side, p_in[1] + math.cos(h0) * R * side
        arc_len = R * abs(turn)
        k = max(2, int(math.ceil(arc_len / step)))
        for j in range(k + 1):
            phi = h0 + turn * j / k
            # point on the arc: centre + R * (inward normal rotated back)
            out.append((cx + math.sin(phi) * R * side, cy - math.cos(phi) * R * side))
        n_round += 1
    out.append(pts[-1])
    return _dedupe(out), n_round


def _join_offset(a: list[tuple[float, float]], b: list[tuple[float, float]],
                 max_reach: float = 10.0) -> list[tuple[float, float]]:
    """Concatenate two reference lines that were offset separately from OSM lines meeting at a
    corner: their end points sit on different normals, so a plain concatenation doubles back.
    Replace the two end points by the mitre (intersection of the end segments), or by their
    midpoint when the segments do not meet within ``max_reach``."""
    if len(a) < 2 or len(b) < 2:
        return _dedupe(a + b)
    p1, p2 = np.asarray(a[-2]), np.asarray(a[-1])
    q1, q2 = np.asarray(b[0]), np.asarray(b[1])
    d1, d2 = p2 - p1, q2 - q1
    den = d1[0] * d2[1] - d1[1] * d2[0]
    joint = (p2 + q1) / 2.0
    if abs(den) > 1e-9:
        t = ((q1[0] - p1[0]) * d2[1] - (q1[1] - p1[1]) * d2[0]) / den
        cand = p1 + t * d1
        if np.hypot(*(cand - p2)) <= max_reach and np.hypot(*(cand - q1)) <= max_reach:
            joint = cand
    return _dedupe(a[:-1] + [(float(joint[0]), float(joint[1]))] + b[1:])


_ALL_LANE_TYPES = ("driving", "parking", "biking", "shoulder", "verge", "sidewalk", "median", "none")


def _road_band(road: Road, full: bool) -> BaseGeometry:
    """Flat-capped polygon of the road: carriageway lanes only, or all lanes (``full``).

    ``median`` lanes are never part of the band: the median of a divided arterial lies between
    its own two carriageways, and the street crossing the arterial legitimately runs over it —
    counting it would shorten every arm of the crossing street for nothing."""
    types = tuple(t for t in _ALL_LANE_TYPES if t != "median") if full else (
        "driving", "parking", "biking", "shoulder")
    line2d = _line2d(road.reference_line)
    parts = []
    wl, wr = road.width_left(types), road.width_right(types)
    if wl > 0:
        parts.append(line2d.buffer(wl, single_sided=True))
    if wr > 0:
        parts.append(line2d.buffer(-wr, single_sided=True))
    return unary_union(parts) if parts else Polygon()


def _ring_coords(geom: BaseGeometry) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon):
            out.extend((float(x), float(y)) for x, y in g.exterior.coords)
        elif hasattr(g, "coords"):
            out.extend((float(x), float(y)) for x, y in g.coords)
    return out


def _core_width(road: Road) -> float:
    """Carriageway core: driving + shoulder lanes (parking/biking are add-ons that stay)."""
    return sum(l.width for l in road.lanes if l.type in ("driving", "shoulder"))


def _renumber(left_inner_out: list[Lane], right_inner_out: list[Lane]) -> list[Lane]:
    for i, lane in enumerate(right_inner_out):
        lane.id = -(i + 1)
    for i, lane in enumerate(left_inner_out):
        lane.id = i + 1
    return left_inner_out[::-1] + right_inner_out


def _set_core_width(road: Road, target: float) -> None:
    """Resize the driving lanes (clamped to the profile's [min_width, max_width]) so the core
    carriageway is ``target`` m wide; the remainder goes to the shoulder lane(s) (one is added
    on the right / removed when < 0.5 m). Lane count and order are preserved; the reference
    line is re-centred."""
    P = profiles.get()
    drive = [l for l in road.lanes if l.type == "driving"]
    if not drive or target <= 0:
        return
    shift_before = (road.width_left() - road.width_right()) / 2.0
    lane_w = min(P.lane.max_width, max(P.lane.min_width, target / len(drive)))
    rem = max(0.0, target - lane_w * len(drive))
    for l in drive:
        l.width = lane_w
    left, right = road.lanes_left(), road.lanes_right()
    shoulders = [l for l in road.lanes if l.type == "shoulder"]
    if rem >= 0.5:
        if shoulders:
            for l in shoulders:
                l.width = rem / len(shoulders)
        else:
            idx = max(i for i, l in enumerate(right) if l.type == "driving") + 1 if any(
                l.type == "driving" for l in right) else 0
            right.insert(idx, Lane(id=0, type="shoulder", width=rem, direction="forward"))
    else:
        left = [l for l in left if l.type != "shoulder"]
        right = [l for l in right if l.type != "shoulder"]
    road.lanes = _renumber(left, right)
    shift_after = (road.width_left() - road.width_right()) / 2.0
    if abs(shift_after - shift_before) > 1e-6:
        xy = [(x, y) for x, y, *_ in road.reference_line.coords]
        road.reference_line = _line3d(_dedupe(_offset_polyline(xy, -(shift_after - shift_before))))


# --------------------------------------------------------------------------- tag parsing

def _num(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    m = re.match(r"^\s*([-+]?\d+(?:[.,]\d+)?)", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def parse_length(v: Optional[str]) -> Optional[float]:
    """'12', '12 m', '12.5m', "40'", '40 ft' -> metres."""
    if v is None:
        return None
    s = str(v).strip().lower()
    n = _num(s)
    if n is None:
        return None
    if "ft" in s or s.endswith("'"):
        return n * 0.3048
    if "km" in s:
        return n * 1000.0
    return n


def parse_maxspeed(v: Optional[str]) -> Optional[float]:
    """maxspeed value -> m/s (None for 'none'/unparsable)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("none", "signals", "variable"):
        return None
    if s == "walk":
        return 7.0 / 3.6
    n = _num(s)
    if n is None:
        return None
    if "mph" in s:
        return n * 0.44704
    if "knots" in s:
        return n * 0.514444
    return n / 3.6


def parse_levels(v: Optional[str]) -> Optional[int]:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _is_oneway(tags: dict[str, str]) -> tuple[bool, bool]:
    """-> (oneway, reversed)."""
    v = tags.get("oneway", "").lower()
    if v in ("-1", "reverse"):
        return True, True
    if v in ("yes", "true", "1"):
        return True, False
    if tags.get("junction") in ("roundabout", "circular") and v not in ("no", "false", "0"):
        return True, False
    return False, False


def _sidewalks(tags: dict[str, str], default_w: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """-> (left width, right width), None when absent."""
    present = {"yes", "separate", "both", "left", "right", "lane", "sidepath"}
    left = right = default_w is not None
    sw = tags.get("sidewalk", "").lower()
    if sw:
        left = sw in ("both", "left", "yes", "separate")
        right = sw in ("both", "right", "yes", "separate")
    both = tags.get("sidewalk:both", "").lower()
    if both:
        left = right = both in present
    for side in ("left", "right"):
        v = tags.get(f"sidewalk:{side}", "").lower()
        if v:
            if side == "left":
                left = v in present
            else:
                right = v in present
    P = profiles.get()
    base = default_w if default_w is not None else (P.lane.fallback.sidewalk or P.sidewalk.min_width)
    w_both = parse_length(tags.get("sidewalk:both:width") or tags.get("sidewalk:width"))
    wl = parse_length(tags.get("sidewalk:left:width")) or w_both or base
    wr = parse_length(tags.get("sidewalk:right:width")) or w_both or base
    return (wl if left else None, wr if right else None)


def _cycleways(tags: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    """-> (left, right) cycleway kind: 'lane' | 'track' | 'opposite' | None."""
    def norm(v: str) -> Optional[str]:
        v = v.lower()
        if v in ("lane", "track", "share_busway", "opposite_track", "opposite_lane"):
            return "opposite" if v.startswith("opposite") else ("track" if "track" in v else "lane")
        return None
    left = right = None
    base = tags.get("cycleway", "")
    if base:
        k = norm(base)
        if base.lower().startswith("opposite"):
            left = "opposite"
        elif k:
            right = k
    both = tags.get("cycleway:both")
    if both:
        left = right = norm(both)
    for side in ("left", "right"):
        v = tags.get(f"cycleway:{side}")
        if v is not None:
            k = norm(v)
            if side == "left":
                left = k
            else:
                right = k
    return left, right


def _parking(tags: dict[str, str]) -> tuple[Optional[float], Optional[float]]:
    """-> (left width, right width) of on-street parking lanes, None when absent (widths per
    orientation from the profile's ``lane.parking_width``)."""
    pw = profiles.get().lane.parking_width

    def width_for(scheme_val: Optional[str], orientation: Optional[str]) -> Optional[float]:
        if scheme_val is None:
            return None
        v = scheme_val.lower()
        if v in ("no", "no_parking", "no_stopping", "no_standing", "separate", "none", "fire_lane"):
            return None
        if v in pw:
            return pw[v]
        if v in ("yes", "lane", "street_side", "on_street", "half_on_kerb", "on_kerb", "marked"):
            return pw.get((orientation or "parallel").lower(), pw["parallel"])
        return None
    out: dict[str, Optional[float]] = {"left": None, "right": None}
    for side in ("left", "right"):
        cands = [
            (tags.get(f"parking:{side}"), tags.get(f"parking:{side}:orientation")),
            (tags.get("parking:both"), tags.get("parking:both:orientation")),
            (tags.get(f"parking:lane:{side}"), tags.get(f"parking:lane:{side}:{tags.get(f'parking:lane:{side}', '')}")),
            (tags.get("parking:lane:both"), None),
        ]
        for v, o in cands:
            if v is not None:
                out[side] = width_for(v, o)
                break
    return out["left"], out["right"]


def _turn_lanes(v: Optional[str]) -> Optional[list[list[str]]]:
    if not v:
        return None
    return [[t for t in lane.split(";") if t] for lane in v.split("|")]


def _mirror_turns(turns: list[str]) -> list[str]:
    swap = {"left": "right", "right": "left", "slight_left": "slight_right",
            "slight_right": "slight_left", "sharp_left": "sharp_right", "sharp_right": "sharp_left"}
    return [swap.get(t, t) for t in turns]


# --------------------------------------------------------------------------- lanes for a way

@dataclass
class LaneSpec:
    lanes: list[Lane]
    center_marking: Optional[Marking]
    oneway: bool
    n_forward: int
    n_backward: int


def lanes_for_way(tags: dict[str, str], highway: str) -> LaneSpec:
    """Build the lane list (OpenDRIVE ordering, ids != 0) for one OSM way from the active
    profile's class defaults (``profiles.get().lane.for_class(highway)``) and the tags.

    Right side (negative ids): forward lanes then biking, parking, verge, sidewalk. Left side
    (positive ids): backward driving lanes (two-way) then biking, parking, verge, sidewalk.
    Oneway roads carry all driving lanes on the right; the reference line is then the left
    carriageway edge, so the left side only has biking/parking/verge/sidewalk.

    When the tags are silent the class defaults decide on-street parking
    (``ClassDefaults.parking``: parallel lanes of ``lane.parking_width["parallel"]``), the
    planting strip between curb and sidewalk (``ClassDefaults.verge``, only on sides that have
    a sidewalk) and whether a two-way road gets a centre line (``ClassDefaults.center_marking``).
    Marking colours come from ``profiles.MarkingRules``: the outer driving-lane edge is an edge
    line, lines between driving lanes are lane lines, the two-way centre line is the centre
    line; a oneway road's reference line is its left carriageway edge (edge line).
    """
    P = profiles.get()
    if is_parking_aisle(tags):
        return _parking_aisle_lanes(tags)
    cls = P.lane.for_class(highway)
    mk = P.marking
    oneway, _ = _is_oneway(tags)
    lane_w = float(cls.lane_width)

    n_total = parse_levels(tags.get("lanes"))
    n_fwd = parse_levels(tags.get("lanes:forward"))
    n_bwd = parse_levels(tags.get("lanes:backward"))
    if oneway:
        n_f = n_total or n_fwd or 1
        n_b = 0
    else:
        if n_fwd is not None and n_bwd is not None:
            n_f, n_b = n_fwd, n_bwd
        elif n_total is not None:
            if n_bwd is not None:
                n_f, n_b = max(1, n_total - n_bwd), n_bwd
            elif n_fwd is not None:
                n_f, n_b = n_fwd, max(1, n_total - n_fwd)
            else:
                n_f, n_b = (n_total + 1) // 2, max(1, n_total // 2)
        else:
            n_f = n_fwd or max(1, cls.lanes // 2)
            n_b = n_bwd or max(1, cls.lanes - (cls.lanes // 2))
    n_f = max(1, n_f)
    n_drive = n_f + n_b

    width = parse_length(tags.get("width"))
    shoulder_total = 0.0
    if width and width > 0:
        lane_w = min(P.lane.max_width, max(P.lane.min_width, width / n_drive))
        shoulder_total = max(0.0, width - lane_w * n_drive)

    speed_f = parse_maxspeed(tags.get("maxspeed:forward") or tags.get("maxspeed"))
    speed_b = parse_maxspeed(tags.get("maxspeed:backward") or tags.get("maxspeed"))
    turns_f = _turn_lanes(tags.get("turn:lanes:forward") or (tags.get("turn:lanes") if oneway or n_b == 0 else tags.get("turn:lanes")))
    turns_b = _turn_lanes(tags.get("turn:lanes:backward"))

    right: list[Lane] = []
    left: list[Lane] = []
    # driving lanes: the outermost lane's outer edge is an edge line, the others lane lines
    for i in range(n_f):
        last = i == n_f - 1
        lane = Lane(id=-(i + 1), type="driving", width=lane_w, direction="forward",
                    marking=Marking("solid" if last else "broken", mk.edge_color if last else mk.lane_color),
                    speed_limit=speed_f)
        if turns_f and i < len(turns_f) and turns_f[i]:
            lane.tags["turn"] = turns_f[i]
        right.append(lane)
    for i in range(n_b):
        last = i == n_b - 1
        lane = Lane(id=i + 1, type="driving", width=lane_w, direction="backward",
                    marking=Marking("solid" if last else "broken", mk.edge_color if last else mk.lane_color),
                    speed_limit=speed_b)
        if turns_b and i < len(turns_b) and turns_b[i]:
            lane.tags["turn"] = turns_b[i]
        left.append(lane)
    # leftover OSM width -> shoulder(s) so the carriageway keeps the tagged width
    if shoulder_total >= 1.0:
        if oneway or shoulder_total < 1.5:
            right.append(Lane(id=0, type="shoulder", width=shoulder_total, direction="forward"))
        else:
            right.append(Lane(id=0, type="shoulder", width=shoulder_total / 2, direction="forward"))
            left.append(Lane(id=0, type="shoulder", width=shoulder_total / 2, direction="backward"))
    elif cls.shoulder or cls.shoulder_inner:
        # class default shoulders (freeway / expressway classes, ClassDefaults.shoulder): the
        # outside shoulder sits outboard of the outermost driving lane. On a oneway
        # carriageway the reference line is the left carriageway edge, so the median-side
        # (inner) shoulder goes on the left; an undivided two-way road gets the outside
        # shoulder on both sides and no inner one.
        if cls.shoulder:
            right.append(Lane(id=0, type="shoulder", width=float(cls.shoulder), direction="forward"))
        if oneway:
            if cls.shoulder_inner:
                left.append(Lane(id=0, type="shoulder", width=float(cls.shoulder_inner),
                                 direction="forward"))
        elif cls.shoulder:
            left.append(Lane(id=0, type="shoulder", width=float(cls.shoulder), direction="backward"))
    # cycle lanes
    cl, cr = _cycleways(tags)
    if cr:
        right.append(Lane(id=0, type="biking", width=P.lane.bike_width, direction="forward",
                          marking=Marking("solid", mk.lane_color), tags={"cycleway": cr}))
    if cl:
        left.append(Lane(id=0, type="biking", width=P.lane.bike_width,
                         direction="backward" if (cl == "opposite" or not oneway) else "forward",
                         marking=Marking("solid", mk.lane_color), tags={"cycleway": cl}))
    # parking: tags first; when no parking:* tag says anything, the class default
    pl, pr = _parking(tags)
    if cls.parking != "none" and not any(k.startswith("parking") for k in tags):
        w_park = P.lane.parking_width["parallel"]
        pl = w_park if cls.parking in ("both", "left") else None
        pr = w_park if cls.parking in ("both", "right") else None
    if pr:
        right.append(Lane(id=0, type="parking", width=pr, direction="forward"))
    if pl:
        left.append(Lane(id=0, type="parking", width=pl, direction="backward" if not oneway else "forward"))
    # sidewalks, with the planting strip (verge) between curb and sidewalk when the class has one
    sl, sr = _sidewalks(tags, cls.sidewalk)
    if sr:
        if cls.verge:
            right.append(Lane(id=0, type="verge", width=float(cls.verge), direction="forward"))
        right.append(Lane(id=0, type="sidewalk", width=sr, direction="forward"))
    if sl:
        d_left = "backward" if not oneway else "forward"
        if cls.verge:
            left.append(Lane(id=0, type="verge", width=float(cls.verge), direction=d_left))
        left.append(Lane(id=0, type="sidewalk", width=sl, direction=d_left))
    # assign ids outward
    for i, lane in enumerate(right):
        lane.id = -(i + 1)
    for i, lane in enumerate(left):
        lane.id = i + 1
    if oneway:
        center: Optional[Marking] = Marking("solid", mk.edge_color)  # the left carriageway edge
    else:
        center = Marking("solid", mk.center_color) if cls.center_marking else None
    return LaneSpec(lanes=left[::-1] + right, center_marking=center, oneway=oneway,
                    n_forward=n_f, n_backward=n_b)


def _parking_aisle_lanes(tags: dict[str, str]) -> LaneSpec:
    """Cross section of a parking-lot aisle (``profiles.ParkingAisleRules``): driving lanes
    only, at the profile's aisle width (an OSM ``width`` overrides it), no markings, no
    sidewalk/verge/parking bands, the profile's low aisle speed limit."""
    A = profiles.get().parking_aisle
    oneway, _ = _is_oneway(tags)
    driveway = tags.get("service") == "driveway"
    tagged = parse_length(tags.get("width"))
    if oneway:
        w = tagged or (A.driveway_width if driveway else A.one_way_width)
        lanes = [Lane(id=-1, type="driving", width=w, direction="forward",
                      speed_limit=A.speed_limit, tags={"parking_aisle": True})]
        return LaneSpec(lanes=lanes, center_marking=None, oneway=True, n_forward=1, n_backward=0)
    w = (tagged or A.two_way_width) / 2.0
    lanes = [Lane(id=1, type="driving", width=w, direction="backward",
                  speed_limit=A.speed_limit, tags={"parking_aisle": True}),
             Lane(id=-1, type="driving", width=w, direction="forward",
                  speed_limit=A.speed_limit, tags={"parking_aisle": True})]
    return LaneSpec(lanes=lanes, center_marking=None, oneway=False, n_forward=1, n_backward=1)


def _lane_signature(lanes: list[Lane]) -> tuple:
    """Merge key for chaining ways: turn tags are excluded (they belong to the approach end)."""
    return tuple((l.id, l.type, round(l.width, 2), l.direction)
                 for l in sorted(lanes, key=lambda l: l.id))


def _reversed_lanes(lanes: list[Lane]) -> list[Lane]:
    out = []
    for l in lanes:
        tags = dict(l.tags)
        if "turn" in tags:
            tags["turn"] = _mirror_turns(tags["turn"])
        out.append(Lane(id=-l.id, type=l.type, width=l.width,
                        direction="backward" if l.direction == "forward" else "forward",
                        marking=l.marking, speed_limit=l.speed_limit, tags=tags))
    return sorted(out, key=lambda l: l.id)


# --------------------------------------------------------------------------- internal graph types

@dataclass
class _Piece:
    """A run of a drivable way inside the bbox: node ids (None for synthetic bbox cut points)."""
    way: OsmWay
    nodes: list[Optional[int]]
    xy: list[tuple[float, float]]


@dataclass
class _Segment:
    way: OsmWay
    nodes: list[Optional[int]]
    xy: list[tuple[float, float]]

    @property
    def a(self) -> Optional[int]:
        return self.nodes[0]

    @property
    def b(self) -> Optional[int]:
        return self.nodes[-1]

    @property
    def length(self) -> float:
        return _polyline_length(self.xy)


@dataclass
class _Chain:
    segments: list[_Segment]
    reversed_flags: list[bool]

    @property
    def nodes(self) -> list[Optional[int]]:
        out: list[Optional[int]] = []
        for seg, rev in zip(self.segments, self.reversed_flags):
            ns = seg.nodes[::-1] if rev else seg.nodes
            out.extend(ns if not out else ns[1:])
        return out

    @property
    def xy(self) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for seg, rev in zip(self.segments, self.reversed_flags):
            xs = seg.xy[::-1] if rev else seg.xy
            out.extend(xs if not out else xs[1:])
        return out


@dataclass
class _Cluster:
    id: str
    node_ids: list[int]
    xy: list[tuple[float, float]]
    hull: BaseGeometry
    way_ids: set[int] = field(default_factory=set)
    area: Optional[BaseGeometry] = None
    plaza: Optional[BaseGeometry] = None  # open space between the corner buildings (streetspace)
    way_nodes: dict[int, set[int]] = field(default_factory=dict)  # internal way -> its node ids
    kind: str = "intersection"            # "gore" = freeway merge/diverge, no plaza, no signals
    gore_role: str = ""                   # "diverge_nose" = the nose junction of a taper-model diverge (7k)

    def absorb(self, way_id: int, nodes: Iterable[Optional[int]]) -> None:
        self.way_ids.add(way_id)
        self.way_nodes.setdefault(way_id, set()).update(n for n in nodes if n is not None)


@dataclass
class _Approach:
    """Lanes of ``road`` entering (incoming) or leaving (outgoing) a junction at ``contact``."""
    road: Road
    contact: str                # "start" | "end" of the road touching the junction
    lanes: list[Lane]           # driving lanes in travel order left->right
    incoming: bool

    @property
    def heading(self) -> float:
        line = self.road.reference_line
        h = _heading_along(line, line.length if self.contact == "end" else 0.0)
        travel_forward = (self.contact == "end") == self.incoming
        return h if travel_forward else _wrap(h + math.pi)

    def lane_inner_edge(self, lane: Lane) -> tuple[float, float]:
        """Point on the lane's left edge (in travel direction) at the junction end."""
        line = self.road.reference_line
        s = line.length if self.contact == "end" else 0.0
        same_side = [l for l in self.road.lanes if (l.id > 0) == (lane.id > 0)]
        inner = sum(l.width for l in same_side if abs(l.id) < abs(lane.id))
        t = inner if lane.id > 0 else -inner
        p = point_on_road(self.road, s, t)
        return (p.x, p.y)

    @property
    def group_width(self) -> float:
        return sum(l.width for l in self.lanes)

    def group_centre(self) -> tuple[float, float]:
        """Centre of the lane group at the junction end (lanes are left->right in travel)."""
        x, y = self.lane_inner_edge(self.lanes[0])
        h = self.heading
        w = self.group_width / 2.0
        return (x + math.sin(h) * w, y - math.cos(h) * w)  # right of travel

    def signed_offset(self, other: "_Approach") -> float:
        """Lateral offset of ``other``'s lane group from this one's, positive to the left of
        this approach's travel direction."""
        px, py = self.group_centre()
        qx, qy = other.group_centre()
        h = self.heading
        return -(qx - px) * math.sin(h) + (qy - py) * math.cos(h)

    def lateral_gap(self, other: "_Approach") -> float:
        """Lateral clearance between this arrival's lane group axis and ``other``'s group at its
        start: <= 0 when the groups overlap laterally (a genuine continuation), large when the
        departure is a parallel road (lateral vs. main carriageway)."""
        return abs(self.signed_offset(other)) - (self.group_width + other.group_width) / 2.0


# --------------------------------------------------------------------------- divided carriageways
# A divided (dual) arterial is mapped in OSM as two ``oneway=yes`` ways with the same name (or
# ref) running in opposite directions with a median between them — El Camino Real and South
# Mathilda Avenue in the Sunnyvale fixture, Howard/Folsom in SoMa.  Two things go wrong without
# an explicit model (DESIGN.md "Divided carriageways"):
#
#  * the generic 40–60 m node clustering hops along a carriageway from one intersection to the
#    next and fuses them into one 80–130 m blob, and
#  * each carriageway is given the class default sidewalk / verge / parking on BOTH sides, so
#    the two carriageways' bands overlap each other across the median and the band-overlap rule
#    (build_lanegraph step 7f) grinds the arm between two junctions down to a 1 m sliver.
#
# The model here pairs the carriageways geometrically, drops the median-side furniture, puts an
# explicit ``median`` lane half-way across the gap, and lowers the cluster radius at every node
# of a carriageway to ``junction.dual_carriageway_cluster_m`` so a junction is the median box
# (both carriageways + the crossing street) and never the next block.


@dataclass
class _DualInfo:
    """One carriageway of a divided arterial."""
    key: str             # arterial key (normalised name / ref)
    partner: int         # chain index of the opposite carriageway
    gap: float           # median separation of the two centrelines (m)
    paired_m: float      # length of this chain that runs beside its partner


def _street_key(tags: dict[str, str]) -> str:
    return (tags.get("name") or tags.get("ref") or "").strip().lower()


def _antiparallel_run(a: LineString, b: LineString, *, min_gap: float, max_gap: float,
                      parallel_rad: float, step: float = 5.0) -> tuple[float, float]:
    """``(length of a that runs anti-parallel to b at a separation in [min_gap, max_gap],
    median separation over that run)``. Both lines run in travel direction."""
    n = max(2, int(a.length / step) + 1)
    ds = a.length / (n - 1)
    ok_len = 0.0
    gaps: list[float] = []
    for i in range(n):
        s = min(a.length, i * ds)
        p = a.interpolate(s)
        sb = b.project(p)
        d = p.distance(b.interpolate(sb))
        if not (min_gap <= d <= max_gap):
            continue
        if abs(abs(_wrap(_heading_along(a, s) - _heading_along(b, sb))) - math.pi) > parallel_rad:
            continue
        ok_len += ds
        gaps.append(d)
    return min(ok_len, a.length), (float(np.median(gaps)) if gaps else 0.0)


def _dual_carriageways(lines: dict[int, tuple[str, LineString]]) -> dict[int, _DualInfo]:
    """Pair the one-way carriageways of divided arterials. ``lines`` maps a chain index to
    ``(arterial key, centreline in travel direction)``; the result has one entry per chain that
    is a carriageway of a divided arterial. Empty when the active profile switches the model off
    (``junction.dual_carriageway_max_gap_m == 0``, e.g. ``EU_DENSE``)."""
    P = profiles.get()
    J = P.junction
    if J.dual_carriageway_max_gap_m <= 0.0 or len(lines) < 2:
        return {}
    groups: dict[str, list[int]] = defaultdict(list)
    for i, (key, _line) in lines.items():
        groups[key].append(i)
    rad = math.radians(J.dual_carriageway_parallel_deg)
    out: dict[int, _DualInfo] = {}
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        best: dict[int, _DualInfo] = {}
        for i in idxs:
            a = lines[i][1]
            for jx in idxs:
                if jx == i:
                    continue
                paired, gap = _antiparallel_run(
                    a, lines[jx][1], min_gap=J.dual_carriageway_min_gap_m,
                    max_gap=J.dual_carriageway_max_gap_m, parallel_rad=rad)
                if paired <= 0.0 or paired < J.dual_carriageway_min_fraction * a.length:
                    continue
                cur = best.get(i)
                if cur is None or paired > cur.paired_m:
                    best[i] = _DualInfo(key=key, partner=jx, gap=gap, paired_m=paired)
        # a whole street must be paired over a decent length, otherwise two short one-way stubs
        # that happen to run past each other (a slip road, a bus loop) would count
        if sum(v.paired_m for v in best.values()) >= J.dual_carriageway_min_paired_m:
            out.update(best)
            log.info("divided arterial %r: %d carriageway chain(s), median gap %.1f m",
                     key, len(best), float(np.median([v.gap for v in best.values()])))
    return out


def _apply_median(lanes: list[Lane], gap: float) -> float:
    """Rewrite ``lanes`` (in place) as one carriageway of a divided arterial and return the
    width of the ``median`` lane it gained (0 when the gap leaves no room).

    The median side (left of travel where traffic drives on the right) loses its parking, cycle,
    verge and sidewalk lanes — there is no curb there, the other carriageway is — and gains one
    ``median`` lane reaching half-way across the gap, so the two carriageways' median lanes meet
    and the mesh gets a single contiguous median strip. ``median`` is not a carriageway lane
    type, so the reference line keeps sitting where it did (carriageway centred on the OSM way).
    """
    P = profiles.get()
    left_is_median = P.drives_on == "right"
    left = sorted((l for l in lanes if l.id > 0), key=lambda l: l.id)      # inner -> outer
    right = sorted((l for l in lanes if l.id < 0), key=lambda l: -l.id)    # inner -> outer
    med, oth = (left, right) if left_is_median else (right, left)
    med[:] = [l for l in med if l.type in ("driving", "shoulder")]
    core = sum(l.width for l in med + oth if l.type in ("driving", "parking", "biking", "shoulder"))
    w = min(max(0.0, gap / 2.0 - core / 2.0), P.junction.median_max_width_m)
    if w >= 0.25:
        med.append(Lane(id=0, type="median", width=round(w, 3), direction="forward"))
    else:
        w = 0.0
    lanes[:] = _renumber(left, right)
    return w


# --------------------------------------------------------------------------- selection / clipping

def osm_layer(tags: dict[str, str]) -> int:
    """OSM ``layer`` of a way as an int (0 when absent/unparseable). The vertical stacking
    order at a 2D crossing: two ways with different layers do not meet."""
    v = _num(tags.get("layer"))
    return int(round(v)) if v is not None else 0


def is_bridge(tags: dict[str, str]) -> bool:
    return tags.get("bridge") not in (None, "", "no")


def is_tunnel(tags: dict[str, str]) -> bool:
    """A way that runs underground: ``tunnel=*`` (anything but ``no`` / ``building_passage``)
    or a negative ``layer``. See ``model.road_is_tunnel``."""
    if tags.get("tunnel") not in (None, "", "no", "building_passage"):
        return True
    return osm_layer(tags) < 0


def _is_underground(tags: dict[str, str]) -> bool:
    """Not part of the surface twin: ``tunnel=building_passage`` (a street through a building
    at ground level, no street space of its own), and *service* ways in a tunnel or on
    ``layer < 0`` (the aisles and ramps of an underground car park). A public road in a tunnel
    (``tunnel=yes``, or ``layer=-1`` without a tunnel tag: an underpass) is kept — it is a
    road of the twin whose z comes from its own profile (``cli.apply_tunnel_profiles``)."""
    if tags.get("tunnel") == "building_passage":
        return True
    return tags.get("highway") == "service" and is_tunnel(tags)


def is_parking_aisle(tags: dict[str, str]) -> bool:
    """True when the active profile ingests this way as parking-lot circulation:
    ``highway=service`` + ``service=parking_aisle`` (or ``service=driveway`` when the profile's
    ``ParkingAisleRules.include_driveways`` is set). Aisles are narrow service roads with
    driving lanes only — no sidewalk/verge/parking lanes, no markings, no crossings."""
    A = profiles.get().parking_aisle
    if not A.include or tags.get("highway") != "service" or _is_underground(tags):
        return False
    service = tags.get("service")
    return service == "parking_aisle" or (service == "driveway" and A.include_driveways)


def is_driveway(tags: dict[str, str]) -> bool:
    """True when this way is ingested as a driveway (``service=driveway`` under a profile with
    ``ParkingAisleRules.include_driveways``): the lot's link between the street and its aisles,
    or the run from the street to a building's garage. Same cross section rules as an aisle
    (``driveway_width`` for the one-way case); a free end is a documented dead end
    (``dead_end_<end>_reason = "driveway"``), not a defect."""
    return is_parking_aisle(tags) and tags.get("service") == "driveway"


def _driveway_leads_somewhere(w: OsmWay, shared_nodes: frozenset[int] | set[int],
                              lot_nodes: dict[int, set[int]]) -> bool:
    """A driveway is a road when it leads somewhere: it touches a lot's circulation (an aisle,
    another driveway, an unnamed service road — ``lot_nodes``: node -> ids of such ways) or
    joins two roads (both ends shared with other drivable ways: Tehama Street through to Howard
    Street in SoMa). A driveway off a street to a garage and nothing else is not a road — in a
    subdivision every mapped house driveway would be a dead-end stub with a junction on the
    street. A one-way driveway whose upstream end is free (a garage *exit*) can never be
    entered and is not a road either; a one-way that ends free is the garage entrance, kept
    as a documented dead end."""
    if len(w.nodes) < 2:
        return False
    oneway, rev = _is_oneway(w.tags)
    if oneway and (w.nodes[0] if not rev else w.nodes[-1]) not in shared_nodes:
        return False
    if any(lot_nodes.get(n, set()) - {w.id} for n in w.nodes):
        return True
    return w.nodes[0] in shared_nodes and w.nodes[-1] in shared_nodes


def _way_is_road(w: OsmWay, length_m: float, ramp_nodes: frozenset[int] | set[int] = frozenset(),
                 aisle_nodes: frozenset[int] | set[int] = frozenset(),
                 shared_nodes: frozenset[int] | set[int] = frozenset(),
                 lot_nodes: Optional[dict[int, set[int]]] = None) -> bool:
    """``ramp_nodes``: end nodes of underground drivable ways; an unnamed service way ending on
    one is the ramp into that car park and is dropped with it.
    ``aisle_nodes``: nodes of the parking aisles; a short unnamed service way touching one is
    the lot's link to the street and is kept (the aisles would be an island without it).
    ``shared_nodes``: nodes used by more than one drivable way (see build_lanegraph step 1).
    ``lot_nodes``: node -> ids of the lot-circulation ways there (aisles, driveways, unnamed
    service ways), for the driveway rule (``_driveway_leads_somewhere``)."""
    P = profiles.get()
    hw = w.tags.get("highway")
    if hw not in P.lane.drivable_classes:
        return False
    if w.tags.get("area") == "yes":
        return False
    if _is_underground(w.tags):
        return False  # underground car-park aisles etc. are not part of the surface twin
    if hw == "service":
        if is_parking_aisle(w.tags):
            if is_driveway(w.tags) and not _driveway_leads_somewhere(w, shared_nodes, lot_nodes or {}):
                return False
            # a lot aisle is a road of its own (profiles.ParkingAisleRules), never subject to
            # the unnamed-service-way length rule for through streets
            if length_m >= P.parking_aisle.min_length:
                return True
            # ... but a short aisle whose *both* ends are shared with another drivable way is a
            # connector inside the lot, not a spur. Dropping it severs the lot's circulation:
            # the aisles on either side keep their (now unreachable) junction node and the arm
            # arriving there gets no legal departure at all (Sunnyvale way 1374794719, 4.9 m).
            return bool(w.nodes) and w.nodes[0] in shared_nodes and w.nodes[-1] in shared_nodes
        if w.tags.get("service") in ("parking_aisle", "driveway", "drive-through", "emergency_access"):
            return False
        if (not w.tags.get("name") and length_m < P.lane.service_min_length
                and not (aisle_nodes and aisle_nodes.intersection(w.nodes))):
            return False
        if not w.tags.get("name") and w.nodes and (w.nodes[0] in ramp_nodes or w.nodes[-1] in ramp_nodes):
            return False  # ramp down to an underground aisle
    return True


def _clip_way(way: OsmWay, osm: OsmData, frame: LocalFrame, bbox: tuple[float, float, float, float]
              ) -> list[_Piece]:
    """Split a way into runs inside the bbox (lat/lon test), inserting boundary points."""
    s, w, n, e = bbox
    pts = [(nid, osm.nodes[nid]) for nid in way.nodes if nid in osm.nodes]
    if len(pts) < 2:
        return []

    def inside(nd: OsmNode) -> bool:
        return s <= nd.lat <= n and w <= nd.lon <= e

    def clip_point(a: OsmNode, b: OsmNode) -> tuple[float, float]:
        # Liang–Barsky on the segment a->b, returns the crossing point (lon, lat) nearest to a-inside
        p = [-(b.lon - a.lon), b.lon - a.lon, -(b.lat - a.lat), b.lat - a.lat]
        q = [a.lon - w, e - a.lon, a.lat - s, n - a.lat]
        u1, u2 = 0.0, 1.0
        for pi, qi in zip(p, q):
            if pi == 0:
                continue
            t = qi / pi
            if pi < 0:
                u1 = max(u1, t)
            else:
                u2 = min(u2, t)
        t = u1 if inside(b) else u2
        return (a.lon + t * (b.lon - a.lon), a.lat + t * (b.lat - a.lat))

    runs: list[list[tuple[Optional[int], float, float]]] = []
    cur: list[tuple[Optional[int], float, float]] = []
    for i, (nid, nd) in enumerate(pts):
        if inside(nd):
            if not cur and i > 0 and not inside(pts[i - 1][1]):
                lon, lat = clip_point(pts[i - 1][1], nd)
                cur.append((None, lon, lat))
            cur.append((nid, nd.lon, nd.lat))
        else:
            if cur:
                lon, lat = clip_point(pts[i - 1][1], nd)
                cur.append((None, lon, lat))
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)
    pieces = []
    for run in runs:
        if len(run) < 2:
            continue
        lons = np.array([r[1] for r in run])
        lats = np.array([r[2] for r in run])
        x, y = frame.to_local(lons, lats)
        xy = [(float(a), float(b)) for a, b in zip(np.atleast_1d(x), np.atleast_1d(y))]
        pieces.append(_Piece(way, [r[0] for r in run], xy))
    return pieces


# --------------------------------------------------------------------------- union find

class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# --------------------------------------------------------------------------- service nodes
# A ``highway=service`` way that is not a parking aisle — a frontage road, a lot's access loop,
# a driveway, an alley — meets the street at an ordinary OSM node, and every such node is an
# intersection node. Clustered at ``junction.cluster_m`` (40-60 m in the US profiles) those nodes
# chain: a lot entrance 10 m from the next one pulls it in, that one the frontage road, the
# frontage road the far side of the lot — Sunnyvale's W Olive Ave x S Taaffe St fused ten nodes
# and a 335 m parking loop into one 7100 m2 "junction" with 160 m connecting roads. The rule
# here (``JunctionRules.service_cluster_m``): street junctions are made of street nodes only.
#
#   * a *service node* is an intersection node with at most one street running through it and
#     a non-aisle service way meeting it (``_service_nodes``);
#   * street nodes cluster as before, and a service node sitting on the street *between* two
#     street nodes that fuse is inside that junction (the walk in ``_cluster_service_nodes``
#     looks through service nodes so a lot entrance never splits a median box in two);
#   * a service node joins a street junction only when a chain shorter than
#     ``service_cluster_m`` links it *directly* to one of the junction's street nodes — it can
#     join, it can never bridge from one street node to another;
#   * two service nodes fuse (a frontage road crossing a lot access) only within
#     ``service_cluster_m``, and never so that the service nodes of one junction span more than
#     that: the throat of a driveway is one junction, a row of driveways is a row of junctions.
#
# Whatever the trims leave shorter than ``junction.sliver_m`` between two of these small
# junctions is still merged by the clustering loop (step 7a) — that merge is bounded by real
# overlap, the crawl it replaces was not.

def _service_nodes(intersection: set[int], minor_nodes: set[int], street_degree: dict[int, int],
                   service_degree: dict[int, int]) -> set[int]:
    """Intersection nodes that are one only because a non-aisle service way meets the street
    there (or two service ways meet). Aisle-only ("minor") nodes keep their own rule."""
    if profiles.get().junction.service_cluster_m <= 0:
        return set()
    return {nid for nid in intersection
            if nid not in minor_nodes and service_degree[nid] > 0
            and street_degree[nid] - service_degree[nid] <= 2}


def _cluster_service_nodes(uf: "_UnionFind", chains: list["_Chain"], intersection: set[int],
                           service_nodes: set[int], minor_nodes: set[int],
                           node_xy: dict[int, tuple[float, float]], chain_limit,
                           radius: float) -> int:
    """Union-find step for the chains that touch a service node (see the module comment
    above). ``chain_limit(chain, a, b)`` is the street cluster radius for a chain (gore / dual
    carriageway aware). Returns the number of street-service links that the generic radius
    would have fused and this rule kept apart."""
    def is_street(nid: Optional[int]) -> bool:
        return nid in intersection and nid not in service_nodes and nid not in minor_nodes

    def is_service_chain(ch: _Chain) -> bool:
        return any(s.way.tags.get("highway") == "service" for s in ch.segments)

    at: dict[int, list[tuple[_Chain, int, int, float]]] = defaultdict(list)
    touching: list[tuple[_Chain, int, int, float]] = []
    for ch in chains:
        a, b = ch.nodes[0], ch.nodes[-1]
        if a is None or b is None or a == b or a not in intersection or b not in intersection:
            continue
        if a not in service_nodes and b not in service_nodes:
            continue
        length = _polyline_length(ch.xy)
        at[a].append((ch, a, b, length))
        at[b].append((ch, b, a, length))
        touching.append((ch, a, b, length))

    # 1. street-street links that run *through* service nodes along the street: the service
    #    node is on the carriageway between the two street nodes, so if they fuse it is inside
    for src in sorted(n for n in intersection if is_street(n)):
        stack: list[tuple[int, float, float, list[int]]] = [(src, 0.0, math.inf, [])]
        seen: set[int] = set()
        while stack:
            node, dist_along, limit, via = stack.pop()
            for ch, _here, nxt, length in at.get(node, ()):
                if is_service_chain(ch) or nxt in seen or nxt == src:
                    continue
                total = dist_along + length
                lim = min(limit, chain_limit(ch, node, nxt))
                if total >= lim:
                    continue
                if nxt in service_nodes:
                    seen.add(nxt)
                    stack.append((nxt, total, lim, via + [nxt]))
                elif is_street(nxt) and math.dist(node_xy[src], node_xy[nxt]) < lim:
                    for n in via + [nxt]:
                        uf.union(src, n)

    # 2. a service node directly linked to a street node within the service radius joins it
    n_suppressed = 0
    for ch, a, b, length in touching:
        if (a in service_nodes) == (b in service_nodes):
            continue
        s_node, t_node = (a, b) if a in service_nodes else (b, a)
        if not is_street(t_node):
            continue  # a minor (aisle) node: never merges (see minor_nodes)
        close = length < radius and math.dist(node_xy[a], node_xy[b]) < radius
        if close:
            uf.union(t_node, s_node)
        else:
            lim = chain_limit(ch, a, b)
            if length < lim and math.dist(node_xy[a], node_xy[b]) < lim:
                n_suppressed += 1

    # 3. service-service links within the radius, never spanning more than it
    members: dict[int, list[int]] = {}
    for nid in service_nodes:
        members.setdefault(uf.find(nid), []).append(nid)
    for ch, a, b, length in sorted(touching, key=lambda t: t[3]):
        if a not in service_nodes or b not in service_nodes:
            continue
        if not (length < radius and math.dist(node_xy[a], node_xy[b]) < radius):
            continue
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        group = members.get(ra, []) + members.get(rb, [])
        if any(math.dist(node_xy[p], node_xy[q]) > radius for p in group for q in group):
            continue
        uf.union(a, b)
        root = uf.find(a)
        members.pop(ra, None)
        members.pop(rb, None)
        members[root] = group
    return n_suppressed


# --------------------------------------------------------------------------- the builder

def build_lanegraph(osm: OsmData, frame: LocalFrame, bbox: tuple[float, float, float, float],
                    name: str = "twin") -> TwinModel:
    """OSM -> TwinModel with roads, junctions (polygon None), signals, controllers, buildings,
    objects and metadata. ``bbox`` is (S, W, N, E) in WGS84."""
    t0 = time.perf_counter()
    P = profiles.get()
    # roads with both ends in one cluster shorter than this are internal to the junction
    junction_internal_m = 2.0 * P.junction.cluster_m
    model = TwinModel(name=name, origin_lat=frame.origin_lat, origin_lon=frame.origin_lon,
                      bbox_wgs84=tuple(bbox))
    meta: dict[str, Any] = {"source": "osm", "profile": P.name, "lanegraph": {}}
    stats = meta["lanegraph"]

    # 0. buildings first: they delimit the street space (widths, chamfer trims, plazas)
    model.buildings = _buildings(osm, frame)
    bld = streetspace.building_union(model)
    footways = _footway_index(osm, frame)

    # 1. drivable ways -> pieces inside the bbox
    pieces: list[_Piece] = []
    n_service_dropped = 0
    n_ramps = 0
    ramp_nodes: set[int] = set()  # ends of underground drivable ways: unnamed service ways there are ramps
    aisle_nodes: set[int] = set()  # nodes of the parking aisles: short service ways there are lot links
    ways_at_node: dict[int, int] = defaultdict(int)  # drivable ways touching each node
    for w in osm.ways:
        if w.tags.get("highway") in P.lane.drivable_classes and _is_underground(w.tags) and len(w.nodes) >= 2:
            ramp_nodes.update((w.nodes[0], w.nodes[-1]))
        if is_parking_aisle(w.tags):
            aisle_nodes.update(w.nodes)
        if w.tags.get("highway") in P.lane.drivable_classes and not _is_underground(w.tags):
            for nid in set(w.nodes):
                ways_at_node[nid] += 1
    # a node more than one drivable way uses: a way between two of them carries traffic through,
    # a way with a free end is a spur (see _way_is_road)
    shared_nodes = {nid for nid, n in ways_at_node.items() if n >= 2}
    way_length: dict[int, float] = {}
    for w in osm.ways:
        if w.tags.get("highway") not in P.lane.drivable_classes:
            continue
        coords = osm.way_coords(w)
        if len(coords) < 2:
            continue
        lons, lats = zip(*coords)
        x, y = frame.to_local(np.array(lons), np.array(lats))
        way_length[w.id] = _polyline_length(list(zip(np.atleast_1d(x), np.atleast_1d(y))))
    # lot circulation a driveway can lead into (_driveway_leads_somewhere): the aisles and
    # unnamed service ways that are roads, and every driveway long enough to be one
    lot_nodes: dict[int, set[int]] = defaultdict(set)
    n_driveways_skipped = 0
    for w in osm.ways:
        if w.id not in way_length or w.tags.get("highway") != "service":
            continue
        if is_driveway(w.tags):
            lot = way_length[w.id] >= P.parking_aisle.min_length
        elif is_parking_aisle(w.tags) or not w.tags.get("name"):
            lot = _way_is_road(w, way_length[w.id], ramp_nodes, aisle_nodes, shared_nodes)
        else:
            lot = False
        if lot:
            for nid in w.nodes:
                lot_nodes[nid].add(w.id)
    for w in osm.ways:
        if w.id not in way_length:
            continue
        length = way_length[w.id]
        if not _way_is_road(w, length, ramp_nodes, aisle_nodes, shared_nodes, lot_nodes):
            n_service_dropped += w.tags.get("highway") == "service"
            n_driveways_skipped += is_driveway(w.tags)
            n_ramps += (_way_is_road(w, length, aisle_nodes=aisle_nodes, shared_nodes=shared_nodes,
                                     lot_nodes=lot_nodes)
                        and not _way_is_road(w, length, ramp_nodes, aisle_nodes, shared_nodes, lot_nodes))
            continue
        oneway, rev = _is_oneway(w.tags)
        if rev:  # normalise oneway=-1 by reversing the node order
            w = OsmWay(w.id, list(reversed(w.nodes)), {**w.tags, "oneway": "yes"})
        pieces.extend(_clip_way(w, osm, frame, bbox))
    stats["drivable_ways"] = len({p.way.id for p in pieces})
    stats["service_ways_skipped"] = n_service_dropped
    stats["parking_aisle_ways"] = len({p.way.id for p in pieces if is_parking_aisle(p.way.tags)})
    stats["driveway_ways"] = len({p.way.id for p in pieces if is_driveway(p.way.tags)})
    stats["driveways_skipped"] = n_driveways_skipped

    # 1b. grade separation: a node in the *interior* of two drivable ways whose OSM ``layer``
    #     differs is the 2D crossing of an overpass and the road under it, not an intersection.
    #     Correctly mapped OSM never shares such a node, but plenty of data does; give each
    #     layer its own copy of the node (synthetic negative ids) so the ways never meet and no
    #     junction cluster forms across the grade separation. Nodes where any way *ends* are
    #     left alone: that is an abutment or a genuine junction.
    interior_layers: dict[int, set[int]] = defaultdict(set)
    endpoint_nodes: set[int] = set()
    for p in pieces:
        lay = osm_layer(p.way.tags)
        for nid in p.nodes[1:-1]:
            if nid is not None:
                interior_layers[nid].add(lay)
        for nid in (p.nodes[0], p.nodes[-1]):
            if nid is not None:
                endpoint_nodes.add(nid)
    crossing_only = {nid for nid, lays in interior_layers.items() if len(lays) > 1} - endpoint_nodes
    if crossing_only:
        remap: dict[tuple[int, int], int] = {}
        next_synth = -1
        for nid in sorted(crossing_only):
            for k, lay in enumerate(sorted(interior_layers[nid])):
                if k == 0:
                    remap[(nid, lay)] = nid  # the lowest layer keeps the OSM id
                else:
                    remap[(nid, lay)] = next_synth
                    next_synth -= 1
        for p in pieces:
            lay = osm_layer(p.way.tags)
            p.nodes = [remap.get((n, lay), n) if n in crossing_only else n for n in p.nodes]
    stats["grade_separated_crossings"] = len(crossing_only)

    # 2. node degrees in the drivable graph (street degree = without the parking-lot aisles)
    degree: dict[int, int] = defaultdict(int)
    street_degree: dict[int, int] = defaultdict(int)
    aisle_degree: dict[int, int] = defaultdict(int)
    # non-aisle highway=service ways (frontage roads, lot access, driveways, alleys); they
    # count in street_degree too — service_degree only tells the clustering which
    # intersection nodes exist because of a service way alone (_service_nodes)
    service_degree: dict[int, int] = defaultdict(int)
    endpoint_ways: dict[int, list[_Piece]] = defaultdict(list)
    for p in pieces:
        aisle = is_parking_aisle(p.way.tags)
        service = not aisle and p.way.tags.get("highway") == "service"
        for i, nid in enumerate(p.nodes):
            if nid is None:
                continue
            d = 1 if i in (0, len(p.nodes) - 1) else 2
            degree[nid] += d
            (aisle_degree if aisle else street_degree)[nid] += d
            if service:
                service_degree[nid] += d
            if i in (0, len(p.nodes) - 1):
                endpoint_ways[nid].append(p)
    intersection: set[int] = set()
    for nid, deg in degree.items():
        if deg >= 3:
            intersection.add(nid)
        elif deg == 2 and len(endpoint_ways[nid]) == 2:
            a, b = endpoint_ways[nid]
            if _is_oneway(a.way.tags)[0] != _is_oneway(b.way.tags)[0]:
                intersection.add(nid)  # a oneway meeting a two-way road
    stats["intersection_nodes"] = len(intersection)

    # 3. split pieces at intersection nodes -> segments
    segments: list[_Segment] = []
    for p in pieces:
        start = 0
        for i in range(1, len(p.nodes)):
            if i == len(p.nodes) - 1 or (p.nodes[i] in intersection):
                seg = _Segment(p.way, p.nodes[start:i + 1], p.xy[start:i + 1])
                if len(seg.xy) >= 2 and seg.length > 1e-6:
                    segments.append(seg)
                start = i

    node_xy: dict[int, tuple[float, float]] = {}
    for p in pieces:
        for nid, xy in zip(p.nodes, p.xy):
            if nid is not None:
                node_xy[nid] = xy

    # 4. chain segments through degree-2 nodes (independent of junction clustering)
    seg_at: dict[int, list[tuple[_Segment, str]]] = defaultdict(list)
    for seg in segments:
        if seg.a is not None and seg.a not in intersection:
            seg_at[seg.a].append((seg, "start"))
        if seg.b is not None and seg.b not in intersection:
            seg_at[seg.b].append((seg, "end"))
    specs: dict[int, LaneSpec] = {}

    def spec_for(seg: _Segment) -> LaneSpec:
        if seg.way.id not in specs:
            specs[seg.way.id] = lanes_for_way(seg.way.tags, seg.way.tags.get("highway", ""))
        return specs[seg.way.id]

    def compatible(s1: _Segment, r1: bool, s2: _Segment, r2: bool) -> bool:
        if s1.way.tags.get("name", "") != s2.way.tags.get("name", ""):
            return False
        if is_parking_aisle(s1.way.tags) != is_parking_aisle(s2.way.tags):
            return False  # a lot aisle never chains with a service street of the same width
        if s1.way.tags.get("highway") != s2.way.tags.get("highway"):
            return False
        # a bridge deck is its own road: its z is interpolated between the abutments instead of
        # sampled from the DEM (cli.apply_elevation), and the datum keeps it off the layer below
        if (osm_layer(s1.way.tags), is_bridge(s1.way.tags)) != (osm_layer(s2.way.tags), is_bridge(s2.way.tags)):
            return False
        sp1, sp2 = spec_for(s1), spec_for(s2)
        if sp1.oneway != sp2.oneway:
            return False
        if (r1 or r2) and sp1.oneway:
            return False  # never reverse a oneway
        l1 = _reversed_lanes(sp1.lanes) if r1 else sp1.lanes
        l2 = _reversed_lanes(sp2.lanes) if r2 else sp2.lanes
        return _lane_signature(l1) == _lane_signature(l2)

    used: set[int] = set()
    chains: list[_Chain] = []

    def extend(chain: _Chain, forward: bool) -> None:
        while True:
            seg, rev = chain.segments[-1 if forward else 0], chain.reversed_flags[-1 if forward else 0]
            node = (seg.a if rev else seg.b) if forward else (seg.b if rev else seg.a)
            if node is None or node in intersection:
                return
            cands = [(s, end) for s, end in seg_at[node] if id(s) not in used]
            if len(cands) != 1 or len(seg_at[node]) != 2:
                return
            nxt, end = cands[0]
            # orientation of nxt so that it continues the chain
            if forward:
                nrev = end == "end"       # nxt must start at node
            else:
                nrev = end == "start"     # nxt must end at node
            if not compatible(seg, rev, nxt, nrev):
                return
            used.add(id(nxt))
            if forward:
                chain.segments.append(nxt)
                chain.reversed_flags.append(nrev)
            else:
                chain.segments.insert(0, nxt)
                chain.reversed_flags.insert(0, nrev)

    for seg in segments:
        if id(seg) in used:
            continue
        used.add(id(seg))
        ch = _Chain([seg], [False])
        extend(ch, True)
        extend(ch, False)
        chains.append(ch)
    # a chain may have been reversed as a whole relative to its first way if the seed was placed
    # backwards; make sure oneway chains run along the way direction
    for ch in chains:
        if all(ch.reversed_flags) and any(spec_for(s).oneway for s in ch.segments):
            ch.segments.reverse()
            ch.reversed_flags = [not r for r in reversed(ch.reversed_flags)]
    # other roads' centrelines occlude the building face (the laterals of Passeig de Gracia
    # sit between the main carriageway and the buildings: the main road is not a canyon)
    chain_lines: list[LineString] = []
    chain_index: dict[int, int] = {}
    for ch in chains:
        xy_c = _dedupe(ch.xy)
        if len(xy_c) >= 2:
            chain_index[id(ch)] = len(chain_lines)
            chain_lines.append(LineString(xy_c))
    chain_tree = STRtree(chain_lines) if chain_lines else None

    def blockers_for(ch: _Chain, xy: list[tuple[float, float]]) -> Optional[BaseGeometry]:
        if chain_tree is None:
            return None
        own = LineString(xy)
        own_i = chain_index.get(id(ch), -1)
        others = [chain_lines[int(k)] for k in chain_tree.query(own.buffer(P.streetspace.max_face_dist_m))
                  if int(k) != own_i]
        return unary_union(others) if others else None

    # 4b. divided arterials: pair the one-way carriageways (see _dual_carriageways)
    dual_lines: dict[int, tuple[str, LineString]] = {}
    for i, ch in enumerate(chains):
        if not spec_for(ch.segments[0]).oneway:
            continue
        key = _street_key(ch.segments[0].way.tags)
        if not key:
            continue
        xy_d = _dedupe(ch.xy)
        if len(xy_d) >= 2:
            line = LineString(xy_d)
            if line.length > 1.0:
                dual_lines[i] = (key, line)
    dual = _dual_carriageways(dual_lines)
    dual_by_chain: dict[int, _DualInfo] = {id(chains[i]): info for i, info in dual.items()}
    dual_nodes: set[int] = set()
    for i in dual:
        dual_nodes.update(n for n in chains[i].nodes if n is not None)
    stats["dual_carriageway_chains"] = len(dual)
    stats["dual_carriageway_pairs"] = len({info.key for info in dual.values()})

    # 5. cluster intersection nodes -> junctions. Two intersection nodes join one cluster when a
    #    chain shorter than junction.cluster_m links them and they are closer than that. Clusters
    #    whose linking road is completely swallowed by the trim are merged and the pass repeated.
    #    At a node of a divided carriageway the radius drops to
    #    junction.dual_carriageway_cluster_m: the median box (both carriageways plus the street
    #    crossing between them) is one junction, but the crawl along a carriageway to the next
    #    intersection — and side streets that hit the two carriageways at offset nodes — are not.
    uf = _UnionFind()
    for nid in intersection:
        uf.find(nid)
    # minor junctions: a node that is only an intersection because a parking-lot aisle joins it
    # (at most a street running through, or aisles meeting each other inside a lot). They never
    # merge with a neighbouring cluster — two lot entrances 50 m apart on the same street would
    # otherwise pull the street between them into one 60 m cluster and pave it.
    minor_nodes = {nid for nid in intersection
                   if aisle_degree[nid] > 0 and street_degree[nid] <= 2}
    stats["minor_junction_nodes"] = len(minor_nodes)
    # service nodes: intersection nodes that exist because a frontage road / lot access /
    # driveway meets the street (see _service_nodes and _cluster_service_nodes below). They
    # never seed or extend a street junction; the generic loop skips their chains.
    service_nodes = _service_nodes(intersection, minor_nodes, street_degree, service_degree)
    stats["service_junction_nodes"] = len(service_nodes)

    def chain_limit(ch: _Chain, a: int, b: int) -> float:
        """Cluster radius for the chain ``ch`` between intersection nodes ``a`` and ``b``."""
        # a chain of grade-separated (freeway) ways links two gores, not two halves of one
        # intersection: clustering them would swallow the whole speed-change lane into a
        # single "junction" the width of the freeway (P.junction.gore_cluster_m, 0 by
        # default). A bridge deck is never inside a junction either: the two ramp terminals
        # of a diamond interchange are one cluster radius apart, and swallowing the deck
        # between them would put the overpass on the junction's plane, i.e. on the freeway.
        grade_separated = all(s.way.tags.get("highway") in P.lane.grade_separated_classes
                              for s in ch.segments)
        spans_levels = any(is_bridge(s.way.tags) or osm_layer(s.way.tags) != 0
                           for s in ch.segments)
        limit = (P.junction.gore_cluster_m if (grade_separated or spans_levels)
                 else P.junction.cluster_m)
        if dual_nodes and (a in dual_nodes or b in dual_nodes):
            limit = min(limit, P.junction.dual_carriageway_cluster_m)
        return limit

    n_dual_suppressed = 0
    for ch in chains:
        nodes = ch.nodes
        a, b = nodes[0], nodes[-1]
        if a in service_nodes or b in service_nodes:
            continue  # _cluster_service_nodes
        if a in minor_nodes or b in minor_nodes:
            # A lot entrance never merges with the intersection up the street — that would pull
            # the block between them into one cluster and pave it. Two nodes joined by a chain
            # shorter than junction.sliver_m are a different matter: whatever is left of that
            # chain after the trims can carry no lane link at all, so it is one junction (the
            # 8 m aisle crossing an aisle entrance in SoMa, whose two halves are otherwise both
            # swallowed by the trim and leave the entrance with no exit).
            if (P.junction.sliver_m > 0 and a in intersection and b in intersection and a != b
                    and _polyline_length(ch.xy) < P.junction.sliver_m
                    and math.dist(node_xy[a], node_xy[b]) < P.junction.sliver_m):
                uf.union(a, b)
            continue
        if a in intersection and b in intersection and a != b:
            limit = chain_limit(ch, a, b)
            if (_polyline_length(ch.xy) < limit
                    and math.dist(node_xy[a], node_xy[b]) < limit):
                uf.union(a, b)
            elif (limit < P.junction.cluster_m
                  and _polyline_length(ch.xy) < P.junction.cluster_m
                  and math.dist(node_xy[a], node_xy[b]) < P.junction.cluster_m):
                n_dual_suppressed += 1
    stats["dual_merges_suppressed"] = n_dual_suppressed
    if service_nodes:
        stats["service_merges_suppressed"] = _cluster_service_nodes(
            uf, chains, intersection, service_nodes, minor_nodes, node_xy, chain_limit,
            P.junction.service_cluster_m)

    def make_clusters() -> tuple[list[_Cluster], dict[int, _Cluster]]:
        groups: dict[int, list[int]] = defaultdict(list)
        for nid in intersection:
            groups[uf.find(nid)].append(nid)
        clusters: list[_Cluster] = []
        node_cluster: dict[int, _Cluster] = {}
        for gi, (_root, nids) in enumerate(sorted(groups.items(),
                                                  key=lambda kv: min(node_xy[n] for n in kv[1]))):
            nids = sorted(nids)
            xy = [node_xy[n] for n in nids]
            c = _Cluster(id=f"j{gi + 1}", node_ids=nids, xy=xy, hull=MultiPoint(xy).convex_hull)
            clusters.append(c)
            for n in nids:
                node_cluster[n] = c
        return clusters, node_cluster

    _ROAD_TAG_KEYS = ("oneway", "lanes", "width", "maxspeed", "surface", "lit", "sidewalk",
                      "sidewalk:both", "sidewalk:left", "sidewalk:right", "cycleway", "cycleway:left",
                      "cycleway:right", "cycleway:both", "turn:lanes", "lanes:forward",
                      "lanes:backward", "placement", "junction", "service", "access",
                      # vertical position: needed by the elevation (a bridge deck interpolates
                      # between its abutments) and by the layer-aware road datum
                      "layer", "bridge", "tunnel")

    # 6. roads from chains (rebuilt per clustering iteration)
    def roads_from_chains(node_cluster: dict[int, _Cluster]):
        roads: list[Road] = []
        road_end_cluster: dict[str, dict[str, Optional[_Cluster]]] = {}
        road_end_node: dict[str, dict[str, Optional[int]]] = {}
        road_nodes: dict[str, list[Optional[int]]] = {}
        n_internal = 0
        n_jogs = 0
        n_fillets = 0
        n_canyon = 0
        n_dual_roads = [0]
        canyon_faces: dict[str, tuple[float, float]] = {}
        canyon_key: dict[str, tuple] = {}
        canyon_tags: dict[str, dict[str, str]] = {}
        for ch in chains:
            nodes = ch.nodes
            xy, nj = _remove_jogs(_dedupe(ch.xy))
            n_jogs += nj
            xy, nr = _fillet_corners(xy)
            n_fillets += nr
            if len(xy) < 2:
                continue
            head = ch.segments[0]
            # lanes: the first segment's spec, reversed if that segment is reversed in the chain
            spec = spec_for(head)
            lanes = (_reversed_lanes(spec.lanes) if ch.reversed_flags[0]
                     else [Lane(**l.__dict__) for l in spec.lanes])
            # forward lanes take their turn:lanes from the last segment (approach to the successor)
            tail, tail_rev = ch.segments[-1], ch.reversed_flags[-1]
            if tail is not head:
                tail_spec = spec_for(tail)
                tail_lanes = {l.id: l for l in (_reversed_lanes(tail_spec.lanes) if tail_rev
                                                else tail_spec.lanes)}
                for l in lanes:
                    if l.id < 0 and l.type == "driving" and l.id in tail_lanes:
                        turn = tail_lanes[l.id].tags.get("turn")
                        l.tags = {k: v for k, v in l.tags.items() if k != "turn"}
                        if turn:
                            l.tags["turn"] = turn
            c_start = node_cluster.get(nodes[0]) if nodes[0] is not None else None
            c_end = node_cluster.get(nodes[-1]) if nodes[-1] is not None else None
            length = _polyline_length(xy)
            # a bridge deck is a road of its own even inside a cluster: absorbing it would put
            # the deck on the junction's plane, i.e. on the road it crosses over
            on_bridge = any(is_bridge(sg.way.tags) or osm_layer(sg.way.tags) != 0
                            for sg in ch.segments)
            # a chain that starts and ends on the *same* OSM node is a ring (a parking-lot
            # loop off one entrance), not a link between two nodes of one cluster: absorbing
            # it paves the loop and leaves the entrance with a single arm, so the road that
            # reaches the lot dead-ends there. 7j splits such a ring into two roads instead.
            ring = nodes[0] is not None and nodes[0] == nodes[-1]
            if (c_start is not None and c_start is c_end and length < junction_internal_m
                    and not on_bridge and not ring):
                for sg in ch.segments:
                    c_start.absorb(sg.way.id, sg.nodes)
                n_internal += 1
                continue
            road = Road(id=f"r{len(roads) + 1}", reference_line=_line3d(xy), lanes=lanes,
                        name=head.way.tags.get("name", ""), highway=head.way.tags.get("highway", ""),
                        osm_way_ids=sorted({sg.way.id for sg in ch.segments}),
                        center_marking=spec.center_marking,
                        tags={k: v for k, v in head.way.tags.items() if k in _ROAD_TAG_KEYS})
            road.tags["oneway_road"] = spec.oneway
            road.tags["reversed"] = bool(ch.reversed_flags[0])  # runs against the head way
            aisle = is_parking_aisle(head.way.tags)
            if aisle:
                road.tags["parking_aisle"] = True
            driveway = is_driveway(head.way.tags)
            if driveway:
                road.tags["driveway"] = True
            for end, nid in (("start", nodes[0]), ("end", nodes[-1])):
                if nid is not None and degree.get(nid, 0) == 1:
                    road.tags[f"dead_end_{end}"] = True  # a real cul-de-sac, not a trim/bbox cut
                    # a driveway's free end is the garage / the lot it serves; every other
                    # degree-1 OSM node is a cul-de-sac (Jennifer Place, the last stall row)
                    road.tags[f"dead_end_{end}_reason"] = "driveway" if driveway else "cul_de_sac"
            # divided arterial: median-side furniture off, explicit median lane (see _apply_median)
            info = dual_by_chain.get(id(ch))
            if info is not None and spec.oneway:
                mw = _apply_median(road.lanes, info.gap)
                road.tags["dual_carriageway"] = info.key
                road.tags["median_gap_m"] = round(info.gap, 2)
                road.tags["median_width_m"] = round(mw, 2)
                road.tags["median_side"] = "left" if P.drives_on == "right" else "right"
                n_dual_roads[0] += 1
            # reference line between forward and backward carriageway lanes
            wl, wr = road.width_left(), road.width_right()
            shift = (wl - wr) / 2.0  # positive: move right (carriageway centre stays on the OSM line)
            if abs(shift) > 1e-3:
                xy = _dedupe(_offset_polyline(xy, -shift))
                road.reference_line = _line3d(xy)
            # aisles keep their profile cross section: a lot between two buildings is not a
            # street canyon, and an aisle never gets sidewalks or parking bands from the faces.
            # A carriageway of a divided arterial has no building face on the median side, and a
            # freeway has no street space (buildings sit behind the right of way), so the canyon
            # cross-section (which needs both faces) is not measured for them either.
            # ... and a tunnel runs *under* the buildings: no faces, no canyon
            faces = (None if (aisle or info is not None
                              or road.highway in P.lane.grade_separated_classes
                              or is_tunnel(head.way.tags))
                     else _measure_faces(road, bld, blockers_for(ch, xy)))
            if faces is not None:
                canyon_faces[road.id] = faces
                canyon_key[road.id] = (road.name, road.highway, spec.oneway,
                                       sum(1 for l in lanes if l.type == "driving"))
                canyon_tags[road.id] = head.way.tags
            roads.append(road)
            road_end_cluster[road.id] = {"start": c_start, "end": c_end}
            road_end_node[road.id] = {"start": nodes[0], "end": nodes[-1]}
            road_nodes[road.id] = nodes
        # street canyon: cross section from the building faces (widths guarded per street)
        for rid, (fl, fr, guarded) in _street_width_guard(canyon_faces, canyon_key).items():
            r = next(r for r in roads if r.id == rid)
            n_canyon += _building_cross_section(r, canyon_tags[rid], (fl, fr), footways)
            if guarded:
                r.tags["street_width_guarded"] = True
        stats["canyon_roads"] = n_canyon
        stats["dual_carriageways"] = n_dual_roads[0]
        return roads, road_end_cluster, road_end_node, road_nodes, n_internal, n_jogs, n_fillets

    # 7. trim roads at junctions (cluster hull buffered by half the road width + margin). Trims
    #    are s-intervals on the shifted, untrimmed line so an end can be re-cut (a neighbour
    #    handed to a junction) or restored (a junction dissolved) later.
    crossing_nodes = {nid for nid, n in osm.nodes.items() if n.tags.get("highway") == "crossing"}
    orig_line: dict[str, LineString] = {}
    max_half: dict[str, float] = defaultdict(float)

    def half_width(r: Road) -> float:
        return (r.width_left() + r.width_right()) / 2.0

    def cut_interval(line2d: LineString, cut: BaseGeometry, keep_end: str
                     ) -> Optional[tuple[float, float]]:
        """s-interval of ``line2d`` outside ``cut``: the piece attached to ``keep_end``."""
        rest = line2d.difference(cut)
        if rest.is_empty:
            return None
        parts = list(rest.geoms) if hasattr(rest, "geoms") else [rest]
        parts = [pp for pp in parts if isinstance(pp, LineString) and pp.length > 1e-6]
        if not parts:
            return None
        anchor = Point(line2d.coords[0]) if keep_end == "start" else Point(line2d.coords[-1])
        parts.sort(key=lambda pp: (pp.distance(anchor), -pp.length))
        best = parts[0]
        if best.distance(anchor) > 0.5:  # anchor swallowed by another cut: keep the longest piece
            best = max(parts, key=lambda pp: pp.length)
        s0 = line2d.project(Point(best.coords[0]))
        s1 = line2d.project(Point(best.coords[-1]))
        return (min(s0, s1), max(s0, s1))

    n_chamfer = [0]

    def retrim(r: Road) -> bool:
        """Cut ``r`` at the clusters on its ends, from the untrimmed line; False when nothing
        drivable is left. A canyon arm is cut at the chamfer line (where its building faces
        end, ``streetspace.canyon_extent``), the others at the hull-based cut. A crossing node
        within P.crossing.near_cut_m of a cut pulls the cut back to P.crossing.keep_m past the
        node so the crossing stays whole on the road (never into the node cluster itself).
        The result is Douglas-Peucker simplified (P.geometry.simplify_m)."""
        line2d = orig_line[r.id]
        L = line2d.length
        lo, hi = 0.0, L
        xnodes = [nid for nid in road_nodes[r.id] if nid in crossing_nodes and nid in node_xy]
        chamfer: tuple[Optional[float], Optional[float]] = (None, None)
        if r.tags.get("cross_section_source") == "buildings" and any(
                road_end_cluster[r.id][e] is not None for e in ("start", "end")):
            chamfer = streetspace.canyon_extent(
                line2d, bld, (float(r.tags["face_left_m"]), float(r.tags["face_right_m"])),
                scan=P.junction.chamfer_scan_m)
        r.tags.pop("trim_source", None)
        for end in ("start", "end"):
            c = road_end_cluster[r.id][end]
            if c is None:
                continue
            # every arm of a cluster is cut at the same distance (the widest arm's half width):
            # arms end on a common line, so the (convex) junction cover of a wide arm's end
            # section cannot swallow the last metres, and the crossing, of a narrow parallel
            # arm. Cutting each arm at the half width of the street it runs into would keep
            # more crossings at their OSM node, but needs a concave junction cover first.
            hw = max(half_width(r), max_half[c.id])
            cut = c.hull.buffer(hw + P.junction.trim_margin_m, join_style="round")
            c.area = cut if c.area is None else c.area.union(cut)
            keep = "end" if end == "start" else "start"
            iv = cut_interval(line2d, cut, keep)
            if iv is None:
                return False
            core = cut_interval(line2d, c.hull.buffer(max(1.0, hw)), keep)
            s_ch = chamfer[0] if end == "start" else chamfer[1]
            if s_ch is not None and core is not None:
                # the faces end at the chamfer: cut there (never inside the hull core)
                s_ch = max(s_ch, core[0]) if end == "start" else min(s_ch, core[1])
                if (end == "start" and s_ch < L - P.geometry.min_road_length) or (end == "end" and s_ch > P.geometry.min_road_length):
                    iv = (s_ch, iv[1]) if end == "start" else (iv[0], s_ch)
                    r.tags["trim_source"] = "chamfer"
                    n_chamfer[0] += 1
            if end == "start":
                s_cut = iv[0]
                for nid in xnodes:
                    s_n = line2d.project(Point(node_xy[nid]))
                    if s_cut - P.crossing.near_cut_m < s_n < s_cut + P.crossing.keep_m:
                        s_cut = min(s_cut, max(core[0] if core else 0.0, s_n - P.crossing.keep_m))
                lo = max(lo, s_cut)
            else:
                s_cut = iv[1]
                for nid in xnodes:
                    s_n = line2d.project(Point(node_xy[nid]))
                    if s_cut - P.crossing.keep_m < s_n < s_cut + P.crossing.near_cut_m:
                        s_cut = max(s_cut, min(core[1] if core else L, s_n + P.crossing.keep_m))
                hi = min(hi, s_cut)
        if hi - lo < P.geometry.min_road_length:
            return False
        piece = substring(line2d, lo, hi).simplify(P.geometry.simplify_m, preserve_topology=False)
        r.reference_line = _line3d([(x, y) for x, y in piece.coords])
        return True

    n_iter = 0
    dropped: list[Road] = []
    while True:
        n_iter += 1
        clusters, node_cluster = make_clusters()
        roads, road_end_cluster, road_end_node, road_nodes, n_internal, n_jogs, n_fillets = roads_from_chains(node_cluster)
        orig_line = {r.id: _line2d(r.reference_line) for r in roads}
        max_half = defaultdict(float)
        for r in roads:
            for end in ("start", "end"):
                c = road_end_cluster[r.id][end]
                if c is not None:
                    max_half[c.id] = max(max_half[c.id], half_width(r))

        kept: list[Road] = []
        dropped = []
        merges: list[tuple[_Cluster, _Cluster]] = []
        for r in roads:
            cs, ce = road_end_cluster[r.id]["start"], road_end_cluster[r.id]["end"]
            if retrim(r):
                if cs is not None:
                    r.predecessor = RoadLink("junction", cs.id)
                if ce is not None:
                    r.successor = RoadLink("junction", ce.id)
                kept.append(r)
                # a sliver: what is left between the two junctions is too short to hold a lane
                # link, so no vehicle can ever traverse it — the junctions belong together
                if (cs is not None and ce is not None and cs is not ce
                        and r.length < P.junction.sliver_m):
                    log.info("%s: %.1f m sliver between %s and %s: merging the junctions",
                             r.id, r.length, cs.id, ce.id)
                    merges.append((cs, ce))
                continue
            if cs is not None and ce is not None and cs is not ce:
                merges.append((cs, ce))  # the junctions overlap: merge them and redo
                continue
            dropped.append(r)
        if not merges or n_iter >= 10:
            if merges:
                log.warning("cluster merging did not converge; %d roads still swallowed", len(merges))
            break
        for ca, cb in merges:
            log.info("merging junctions %s and %s (their linking road is shorter than the trims)",
                     ca.id, cb.id)
            uf.union(ca.node_ids[0], cb.node_ids[0])
    roads = kept
    stats["cluster_iterations"] = n_iter
    stats["junctions_clustered_nodes"] = {c.id: len(c.node_ids) for c in clusters if len(c.node_ids) > 1}
    stats["roads_internal_to_junctions"] = n_internal
    stats["roads_dropped_by_trim"] = len(dropped)
    stats["jogs_removed"] = n_jogs
    stats["corners_rounded"] = n_fillets
    stats["ramps_skipped"] = n_ramps
    stats["chamfer_trims"] = n_chamfer[0]
    by_id = {r.id: r for r in roads}

    def set_link(r: Road, end: str, link: Optional[RoadLink]) -> None:
        if end == "end":
            r.successor = link
        else:
            r.predecessor = link

    def attach(r: Road, end: str, c: Optional[_Cluster]) -> None:
        road_end_cluster[r.id][end] = c
        set_link(r, end, RoadLink("junction", c.id) if c is not None else None)

    def free_ends() -> dict[int, list[tuple[Road, str]]]:
        out: dict[int, list[tuple[Road, str]]] = defaultdict(list)
        for r in roads:
            for end in ("start", "end"):
                nid = road_end_node[r.id][end]
                if nid is not None and road_end_cluster[r.id][end] is None:
                    out[nid].append((r, end))
        return out

    # 7b. roads swallowed by the trim, and stubs (< P.junction.stub_m with a junction at one end only),
    #     are absorbed into that junction; the road continuing from their free end is handed
    #     over to the junction so a street reached through a short lateral does not dangle
    for r in list(roads):
        ends = road_end_cluster[r.id]
        if (ends["start"] is None) == (ends["end"] is None):
            continue
        free_end = "end" if ends["start"] is not None else "start"
        nid = road_end_node[r.id][free_end]
        others = [rb for rb, _e in free_ends().get(nid, []) if rb is not r] if nid is not None else []
        # a stub: shorter than P.junction.stub_m, or a dead end shorter than P.junction.dead_end_stub_m that no
        # other road continues (the entrance to a pedestrian passage): cars go nowhere there
        if r.length < P.junction.stub_m or (r.length < P.junction.dead_end_stub_m and nid is not None and not others):
            dropped.append(r)
            roads.remove(r)
            del by_id[r.id]
    # 7b-bis. a junction must keep at least one departure. The roads dropped above are the
    #     remnants of a way that continues outside the bbox, of a short link inside a parking
    #     lot, or of an arm the trim swallowed whole. When such a road carried the only lanes
    #     *leaving* one of its junctions, dropping it turns every arrival there into a dead end:
    #     step 8 finds no departure to connect the arriving lanes to, the xodr lane gets no
    #     link, and the traffic manager deletes whatever it routed onto it. Put the road back —
    #     re-cut if that leaves something drivable, untrimmed if it does not (a 2 m stub at the
    #     bbox edge is still a legal exit; the validator excludes lanes that end at the edge).
    def _end_lanes_leaving(r: Road, end: str) -> int:
        """Driving lanes of ``r`` that travel *away* from a junction attached at ``end``."""
        return sum(1 for l in r.lanes if l.type == "driving" and (l.id < 0) == (end == "start"))

    def starves_an_arrival(c: _Cluster, ignore: Optional[Road] = None) -> bool:
        """True when some arm arriving at ``c`` would have nowhere to go but back down itself
        (step 8 never connects an arrival to its own road: that is a u-turn). ``ignore`` is a
        road about to be removed."""
        outs, ins = set(), set()
        for r in roads:
            if r is ignore:
                continue
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is not c:
                    continue
                if _end_lanes_leaving(r, end):
                    outs.add(r.id)
                if any(l.type == "driving" and (l.id < 0) == (end == "end") for l in r.lanes):
                    ins.add(r.id)
        return any(not (outs - {rid}) for rid in ins)

    n_outlets_restored = 0
    for r in list(dropped):
        # only a road the *bbox* cut short is put back: it is the stub of a way that carries on
        # outside the map, so the network leaves the map there instead of dead-ending inside it.
        # An arm the trim swallowed between two interior nodes really is smaller than the
        # junction (an 8 m cross aisle between two private driveways) — restoring it would only
        # add two lanes that start and end on the junction node; step 8 tags that dead end.
        clipped = any(road_end_cluster[r.id][end] is None and road_end_node[r.id][end] is None
                      for end in ("start", "end"))
        if not clipped or orig_line[r.id].length < P.geometry.min_road_length:
            continue
        starved = [c for end in ("start", "end")
                   for c in (road_end_cluster[r.id][end],)
                   if c is not None and c in clusters and _end_lanes_leaving(r, end)
                   and starves_an_arrival(c)]
        if not starved:
            continue
        dropped.remove(r)
        roads.append(r)
        by_id[r.id] = r
        r.tags["junction_outlet"] = True  # never absorbed again by the sliver rules below
        for end in ("start", "end"):
            attach(r, end, road_end_cluster[r.id][end])
        if not retrim(r):
            r.reference_line = _line3d([(x, y) for x, y in orig_line[r.id].coords])
        n_outlets_restored += 1
        log.info("%s: %.1f m arm restored — the only exit from %s",
                 r.id, r.length, ", ".join(sorted(c.id for c in starved)))
    stats["junction_outlets_restored"] = n_outlets_restored

    n_reattached = 0
    for r in dropped:
        ends = road_end_cluster[r.id]
        for c in (ends["start"], ends["end"]):
            if c is not None:
                for wid in r.osm_way_ids:
                    c.absorb(wid, road_nodes[r.id])
        if (ends["start"] is None) == (ends["end"] is None):
            continue
        c = ends["start"] or ends["end"]
        free_end = "end" if ends["start"] is not None else "start"
        nid = road_end_node[r.id][free_end]
        others = free_ends().get(nid, []) if nid is not None else []
        if len(others) != 1:
            continue
        rb, eb = others[0]
        attach(rb, eb, c)
        max_half[c.id] = max(max_half[c.id], half_width(rb))
        if not retrim(rb):
            log.warning("%s: %s would vanish when re-cut at the junction; kept untrimmed", c.id, rb.id)
        n_reattached += 1
    stats["stubs_absorbed"] = len(dropped)
    stats["roads_reattached"] = n_reattached

    # 7c. clusters that are not junctions: no arm, one arm, or two arms that continue each
    #     other (the lateral / ramp stub that made the node an intersection is gone). Their
    #     roads are restored to the node and linked road<->road in 7d.
    def arms_of(c: _Cluster) -> list[tuple[Road, str]]:
        return [(r, end) for r in roads for end in ("start", "end") if road_end_cluster[r.id][end] is c]

    def end_lanes(r: Road, end: str) -> tuple[int, int]:
        """(driving lanes leaving the road through this end, lanes entering through it)."""
        fwd = sum(1 for l in r.lanes if l.type == "driving" and l.id < 0)
        bwd = sum(1 for l in r.lanes if l.type == "driving" and l.id > 0)
        return (fwd, bwd) if end == "end" else (bwd, fwd)

    def out_heading(r: Road, end: str) -> float:
        line = orig_line[r.id]
        h = _heading_along(line, line.length if end == "end" else 0.0)
        return h if end == "end" else _wrap(h + math.pi)

    n_dissolved = 0
    for c in list(clusters):
        arms = arms_of(c)
        if len(arms) > 2:
            continue
        if len(arms) == 2:
            (ra, ea), (rb, eb) = arms
            na, nb = road_end_node[ra.id][ea], road_end_node[rb.id][eb]
            if na is None or na != nb:
                continue
            if abs(_wrap(out_heading(ra, ea) - out_heading(rb, eb) - math.pi)) > math.radians(45.0):
                continue  # a real turn (e.g. the other arms are clipped by the bbox): junction
            la, lb = end_lanes(ra, ea), end_lanes(rb, eb)
            if la != (lb[1], lb[0]):
                continue
        log.info("%s: %d arm(s), not a junction — dissolved", c.id, len(arms))
        for r, end in arms:
            if len(arms) == 1 and any(l.type == "driving" and (l.id < 0) == (end == "end")
                                      for l in r.lanes):
                # nothing else is left at this OSM node: every other way there is outside the
                # twin's scope (a private driveway, an underground ramp) or was clipped away.
                # The lanes arriving here have nowhere to go — a dead end of the modelled
                # network, the same documented kind as a degree-1 cul-de-sac (step 6).
                r.tags[f"dead_end_{end}"] = True
                r.tags[f"dead_end_{end}_reason"] = "no_continuation"
            attach(r, end, None)
            retrim(r)
        clusters.remove(c)
        n_dissolved += 1
    stats["junctions_dissolved"] = n_dissolved

    # 7d. road<->road links at degree-2 nodes that did not merge into one chain
    for nid, lst in free_ends().items():
        if len(lst) != 2:
            continue
        (ra, ea), (rb, eb) = lst
        set_link(ra, ea, RoadLink("road", rb.id, eb))
        set_link(rb, eb, RoadLink("road", ra.id, ea))

    # 7e. sidewalk=separate: width from the parallel footway=sidewalk ways (canyon roads
    #     already took theirs from the faces)
    stats.update(_sidewalk_widths_from_footways(
        [r for r in roads if r.tags.get("cross_section_source") != "buildings"], footways))

    # 7e-bis. ramp gores: a cluster whose arms are all grade-separated (freeway) classes and of
    #     which at least one is a ramp (link class) is a merge/diverge gore, not an
    #     intersection. It gets no band-overlap cut (7f), no plaza (7i), no chamfer, no sidewalk
    #     band and no signals; the lane count simply changes along the mainline (7h tapers the
    #     width step). ``surfaces`` and ``_build_signals`` read this through ``_Cluster.kind`` /
    #     ``Junction.tags["kind"]``.
    for c in clusters:
        arms = arms_of(c)
        hws = [r.highway for r, _e in arms]
        if arms and all(h in P.lane.grade_separated_classes for h in hws) \
                and any(h in P.lane.link_classes for h in hws):
            c.kind = "gore"
    stats["gore_junctions"] = sum(1 for c in clusters if c.kind == "gore")

    # 7f. a road's full band (carriageway + sidewalks) must not cover another road's carriageway
    #     at a junction: with 6 m sidewalks the carriageway-only trim leaves a raised sidewalk
    #     slab across the crossing street's lanes. Shorten the offending arm by exactly the
    #     overlap (not every arm by the full width, which would pave the chamfer corners).
    #     The two carriageways of one divided arterial are exempt: they run beside each other by
    #     construction, their bands overlap along their whole length, and cutting one against the
    #     other grinds the arm between two junctions down to a 1 m sliver (the SoMa "Howard
    #     Street" dead ends). The overlap between them is the median, which 7d/_apply_median owns.
    n_band_cuts = [0]

    def same_arterial(a: Road, b: Road) -> bool:
        key = a.tags.get("dual_carriageway")
        return bool(key) and b.tags.get("dual_carriageway") == key

    def band_overlap_pass() -> list[tuple[_Cluster, _Cluster]]:
        """Shorten arms whose full band covers a neighbour's carriageway. Returns the junction
        pairs that could not be separated without leaving a sliver between them."""
        slivers: list[tuple[_Cluster, _Cluster]] = []
        for _pass in range(3):
            changed = False
            for c in clusters:
                if c.kind == "gore":
                    # at a ramp gore the arms *are* meant to overlap: the speed-change lane runs
                    # beside the mainline for 100-200 m before the nose. Cutting the arms back by
                    # the overlap would turn the whole taper into one giant junction.
                    continue
                arms = arms_of(c)
                carriage = {r.id: _road_band(r, full=False) for r, _e in arms}
                for r, end in arms:
                    band = _road_band(r, full=True)
                    line2d = _line2d(r.reference_line)
                    s_cut: Optional[float] = None
                    for rb, _eb in arms:
                        if rb is r or same_arterial(r, rb):
                            continue
                        ov = band.intersection(carriage[rb.id])
                        if ov.is_empty or ov.area <= P.junction.band_overlap_m2:
                            continue
                        ss = [line2d.project(Point(x, y)) for x, y in _ring_coords(ov)]
                        reach = (max(ss) if end == "start" else min(ss))
                        s_cut = reach if s_cut is None else (max(s_cut, reach) if end == "start" else min(s_cut, reach))
                    if s_cut is None:
                        continue
                    # stop P.junction.trim_margin_m short of the corner: two arms cut exactly to the corner
                    # where their sidewalks meet leave a zero-length turn between them
                    if end == "start":
                        lo, hi = min(line2d.length - P.geometry.min_road_length, s_cut + P.junction.trim_margin_m), line2d.length
                    else:
                        lo, hi = 0.0, max(P.geometry.min_road_length, s_cut - P.junction.trim_margin_m)
                    other = road_end_cluster[r.id]["end" if end == "start" else "start"]
                    if (hi - lo < P.junction.sliver_m and other is not None and other is not c):
                        # Cutting would leave a sliver between two junctions — a road too short
                        # to carry a lane link, which the traffic manager routes into and then
                        # deletes the vehicle. Two neighbouring nodes really are one junction;
                        # two junctions a block apart are not, and merging them would pave the
                        # block, so there the band overlap (a sidewalk slab over a lane) stays.
                        if c.hull.centroid.distance(other.hull.centroid) <= P.junction.cluster_m:
                            slivers.append((c, other))
                        else:
                            log.warning("%s: %s would be a %.1f m sliver between %s and %s; "
                                        "keeping the band overlap instead",
                                        c.id, r.id, hi - lo, c.id, other.id)
                        continue
                    if hi - lo < P.geometry.min_road_length or (end == "start" and lo <= 1e-6) or (end == "end" and hi >= line2d.length - 1e-6):
                        log.warning("%s: %s cannot be shortened enough to clear a neighbour's carriageway", c.id, r.id)
                        continue
                    r.reference_line = _line3d([(x, y) for x, y in substring(line2d, lo, hi).coords])
                    n_band_cuts[0] += 1
                    changed = True
            if not changed:
                break
        return slivers

    def merge_clusters(ca: _Cluster, cb: _Cluster) -> None:
        """Fold ``cb`` into ``ca`` after the clustering loop has finished: the arms change hands,
        the hull grows, and every arm of the result is re-cut from its untrimmed line."""
        ca.node_ids = sorted(set(ca.node_ids) | set(cb.node_ids))
        pts = [node_xy[n] for n in ca.node_ids if n in node_xy]
        if pts:
            ca.xy = pts
            ca.hull = MultiPoint(pts).convex_hull
        ca.way_ids |= cb.way_ids
        for wid, ns in cb.way_nodes.items():
            ca.way_nodes.setdefault(wid, set()).update(ns)
        if cb.area is not None:
            ca.area = cb.area if ca.area is None else ca.area.union(cb.area)
        ca.plaza = None
        max_half[ca.id] = max(max_half[ca.id], max_half[cb.id])
        for r in list(roads):
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is cb:
                    attach(r, end, ca)
        if cb in clusters:
            clusters.remove(cb)
        for r in list(roads):
            ends = road_end_cluster[r.id]
            if ends["start"] is ca and ends["end"] is ca:
                # the sliver itself: now internal to the merged junction
                for wid in r.osm_way_ids:
                    ca.absorb(wid, road_nodes[r.id])
                roads.remove(r)
                by_id.pop(r.id, None)
            elif ends["start"] is ca or ends["end"] is ca:
                if not retrim(r):
                    log.warning("%s: %s vanished when re-cut at the merged junction", ca.id, r.id)

    n_sliver_merges = 0
    for _round in range(3):
        slivers = band_overlap_pass()
        pairs_left = [(ca, cb) for ca, cb in slivers if ca is not cb and ca in clusters and cb in clusters]
        if not pairs_left:
            break
        for ca, cb in pairs_left:
            if ca is cb or ca not in clusters or cb not in clusters:
                continue
            log.info("merging %s and %s: the arm between them would be a sliver", ca.id, cb.id)
            merge_clusters(ca, cb)
            n_sliver_merges += 1
    # a sliver with a junction at one end only cannot be merged away — it is absorbed into that
    # junction (7b does this before the band cuts; the band cuts make new ones)
    n_sliver_absorbed = 0
    for r in list(roads):
        ends = road_end_cluster[r.id]
        if (ends["start"] is None) == (ends["end"] is None) or r.length >= P.junction.sliver_m:
            continue
        c = ends["start"] or ends["end"]
        free_end = "end" if ends["start"] is not None else "start"
        if r.tags.get("junction_outlet") or (road_end_node[r.id][free_end] is None
                                             and starves_an_arrival(c, ignore=r)):
            continue  # the stub the bbox cut short is this junction's only exit (7b-bis)
        nid = road_end_node[r.id][free_end]
        others = [(rb, eb) for rb, eb in free_ends().get(nid, []) if rb is not r] if nid is not None else []
        if len(others) > 1:
            continue  # a fork: dropping the road would orphan more than one neighbour
        for wid in r.osm_way_ids:
            c.absorb(wid, road_nodes[r.id])
        roads.remove(r)
        by_id.pop(r.id, None)
        n_sliver_absorbed += 1
        log.info("%s: %.1f m sliver off %s absorbed into the junction", r.id, r.length, c.id)
        if others:
            rb, eb = others[0]
            attach(rb, eb, c)
            max_half[c.id] = max(max_half[c.id], half_width(rb))
            if not retrim(rb):
                log.warning("%s: %s would vanish when re-cut at the junction; kept untrimmed", c.id, rb.id)
    # every pass since the clustering loop (7b's re-cuts, the band-overlap cuts, the sliver
    # merges themselves) can shorten an arm; sweep once more so no road between two junctions
    # is left below sliver_m (validate.junction_slivers)
    if P.junction.sliver_m > 0:
        for _round in range(3):
            left = [r for r in roads
                    if r.length < P.junction.sliver_m
                    and road_end_cluster[r.id]["start"] is not None
                    and road_end_cluster[r.id]["end"] is not None
                    and road_end_cluster[r.id]["start"] is not road_end_cluster[r.id]["end"]]
            if not left:
                break
            for r in left:
                ca, cb = road_end_cluster[r.id]["start"], road_end_cluster[r.id]["end"]
                if ca is cb or ca not in clusters or cb not in clusters:
                    continue
                log.info("%s: %.1f m sliver left between %s and %s: merging the junctions",
                         r.id, r.length, ca.id, cb.id)
                merge_clusters(ca, cb)
                n_sliver_merges += 1
    stats["band_overlap_cuts"] = n_band_cuts[0]
    stats["sliver_junction_merges"] = n_sliver_merges
    stats["slivers_absorbed"] = n_sliver_absorbed

    # 7g. non-junction roads shorter than P.junction.short_road_m merge into the road-linked neighbour
    #     they continue when the lane configuration matches
    n_short = 0
    for r in list(roads):
        if r.length >= P.junction.short_road_m:
            continue
        for link, end in ((r.predecessor, "start"), (r.successor, "end")):
            if link is None or link.element != "road" or link.id not in by_id:
                continue
            nb = by_id[link.id]
            same_dir = (end == "start" and link.contact == "end") or (end == "end" and link.contact == "start")
            if not same_dir or _lane_signature(nb.lanes) != _lane_signature(r.lanes):
                continue
            far = "end" if end == "start" else "start"  # r's other end becomes nb's
            a = [c[:2] for c in nb.reference_line.coords]
            b = [c[:2] for c in r.reference_line.coords]
            nb.reference_line = _line3d(_join_offset(a, b) if end == "start" else _join_offset(b, a))
            nb.osm_way_ids = sorted(set(nb.osm_way_ids) | set(r.osm_way_ids))
            far_link = r.successor if far == "end" else r.predecessor
            set_link(nb, far, far_link)
            road_end_cluster[nb.id][far] = road_end_cluster[r.id][far]
            road_end_node[nb.id][far] = road_end_node[r.id][far]
            road_nodes[nb.id] = (road_nodes[nb.id] + road_nodes[r.id][1:] if end == "start"
                                 else road_nodes[r.id] + road_nodes[nb.id][1:])
            if far_link is not None and far_link.element == "road" and far_link.id in by_id:
                set_link(by_id[far_link.id], far_link.contact, RoadLink("road", nb.id, far))
            orig_line[nb.id] = _line2d(nb.reference_line)
            roads.remove(r)
            del by_id[r.id]
            n_short += 1
            log.info("merged %.1f m road %s into %s", r.length, r.id, nb.id)
            break
    stats["short_roads_merged"] = n_short

    # 7h. carriageway width steps (> P.geometry.width_step_m) at road<->road links: a road without
    #     width/lanes tags adopts the narrowest tagged neighbour's core width; a step between
    #     tagged roads gets a taper split off the wider road (<= P.geometry.taper_max_m, in at most
    #     P.geometry.taper_pieces_max constant-width pieces, each lane constant per piece)
    def road_links(r: Road):
        for link, end in ((r.predecessor, "start"), (r.successor, "end")):
            if link is not None and link.element == "road" and link.id in by_id:
                yield by_id[link.id], link.contact, end

    def has_width_tags(r: Road) -> bool:
        return "width" in r.tags or "lanes" in r.tags or r.tags.get("cross_section_source") == "buildings"

    n_reconciled = 0
    for r in roads:
        if has_width_tags(r):
            continue
        targets = [_core_width(nb) for nb, _c, _e in road_links(r)
                   if has_width_tags(nb) and abs(_core_width(nb) - _core_width(r)) > P.geometry.width_step_m]
        if targets:
            log.info("%s: no width tags, adopting %.1f m from its tagged neighbour", r.id, min(targets))
            _set_core_width(r, min(targets))
            orig_line[r.id] = _line2d(r.reference_line)
            r.tags["width_source"] = "neighbour"
            n_reconciled += 1
    stats["widths_reconciled"] = n_reconciled

    n_taper = 0
    next_num = max((int(r.id[1:]) for r in roads if r.id[1:].isdigit()), default=0) + 1
    seen_pairs: set[tuple] = set()
    pairs: list[tuple[Road, str, Road, str]] = []
    for r in list(roads):
        for nb, contact, end in road_links(r):
            key = tuple(sorted([(r.id, end), (nb.id, contact)]))
            if key not in seen_pairs:
                seen_pairs.add(key)
                pairs.append((r, end, nb, contact))
    for ra, ea, rb, eb in pairs:
        wa, wb = _core_width(ra), _core_width(rb)
        delta = abs(wa - wb)
        if delta <= P.geometry.width_step_m:
            continue
        wide, wend, narrow, nend = (ra, ea, rb, eb) if wa > wb else (rb, eb, ra, ea)
        if wide.length < 3 * P.geometry.min_road_length:
            continue
        taper_len = min(P.geometry.taper_max_m, wide.length / 2.0)
        k = max(1, min(P.geometry.taper_pieces_max, math.ceil(delta / P.geometry.width_step_m - 1e-9)))
        step = delta / (k + 1)
        line2d = _line2d(wide.reference_line)
        L = line2d.length
        pieces: list[Road] = []
        for i in range(1, k + 1):
            if wend == "end":
                s0, s1 = L - taper_len + (i - 1) * taper_len / k, L - taper_len + i * taper_len / k
            else:
                s0, s1 = taper_len - i * taper_len / k, taper_len - (i - 1) * taper_len / k
            seg = substring(line2d, s0, s1)
            piece = Road(id=f"r{next_num}", reference_line=_line3d([(x, y) for x, y in seg.coords]),
                         lanes=[Lane(id=l.id, type=l.type, width=l.width, direction=l.direction,
                                     marking=l.marking, speed_limit=l.speed_limit, tags=dict(l.tags))
                                for l in wide.lanes],
                         name=wide.name, highway=wide.highway, osm_way_ids=list(wide.osm_way_ids),
                         center_marking=wide.center_marking,
                         tags={**wide.tags, "taper": True, "width_source": "taper"})
            next_num += 1
            _set_core_width(piece, max(wa, wb) - step * i)
            pieces.append(piece)
            roads.append(piece)
            by_id[piece.id] = piece
            road_end_cluster[piece.id] = {"start": None, "end": None}
            road_end_node[piece.id] = {"start": None, "end": None}
            road_nodes[piece.id] = []
        kept_seg = substring(line2d, 0.0, L - taper_len) if wend == "end" else substring(line2d, taper_len, L)
        wide.reference_line = _line3d([(x, y) for x, y in kept_seg.coords])
        seq: list[Road] = [wide] + pieces + [narrow]
        for a, b in zip(seq, seq[1:]):
            if wend == "end":
                ea_, eb_ = "end", ("start" if b is not narrow else nend)
            else:
                ea_, eb_ = "start", ("end" if b is not narrow else nend)
            set_link(a, ea_, RoadLink("road", b.id, eb_))
            set_link(b, eb_, RoadLink("road", a.id, ea_))
        log.info("taper %s (%.1f m) -> %s (%.1f m): %d piece(s) over %.1f m",
                 wide.id, max(wa, wb), narrow.id, min(wa, wb), k, taper_len)
        n_taper += 1
    stats["tapers_inserted"] = n_taper


    # 7i. junction plazas: the open space between the corner buildings, clipped to the hull of
    #     the arms' street corridors (each from its trimmed end into the junction)
    n_plaza = 0
    for c in clusters:
        if c.kind == "gore":
            continue
        centre = (float(np.mean([p[0] for p in c.xy])), float(np.mean([p[1] for p in c.xy])))
        corridors = []
        reach = 0.0
        for r, end in arms_of(c):
            line2d = _line2d(r.reference_line)
            p = line2d.coords[-1] if end == "end" else line2d.coords[0]
            h = _heading_along(line2d, line2d.length if end == "end" else 0.0)
            h_in = h if end == "end" else _wrap(h + math.pi)
            if r.tags.get("cross_section_source") == "buildings":
                width = float(r.tags["street_width_m"])
                t_c = (float(r.tags["face_left_m"]) - float(r.tags["face_right_m"])) / 2.0
            else:
                wl, wr = r.width_left(_ALL_LANE_TYPES), r.width_right(_ALL_LANE_TYPES)
                width, t_c = wl + wr, (wl - wr) / 2.0
            d = math.dist(p, centre)
            reach = max(reach, d)
            corridors.append(streetspace.arm_corridor(
                (p[0], p[1]), h_in, width / 2.0, d + max_half[c.id] + P.junction.trim_margin_m,
                offset=t_c if end == "end" else -t_c))
        if not corridors:
            continue
        plaza = streetspace.junction_plaza(Point(centre), bld, corridors,
                                           radius=max(P.junction.plaza_radius_m, reach + 5.0))
        if plaza is not None and not plaza.is_empty:
            c.plaza = plaza
            n_plaza += 1
    stats["plazas"] = n_plaza

    # 7j. a road with the same junction at both ends is a loop (a parking-lot ring off one
    #     driveway). OpenDRIVE's <connection> names the incoming road by id only, so CARLA
    #     cannot tell the two ends apart and half the movements into the loop dead-end. Split
    #     it in the middle into two roads joined road<->road.
    n_loops_split = 0
    for r in list(roads):
        cs, ce = road_end_cluster[r.id]["start"], road_end_cluster[r.id]["end"]
        if cs is None or cs is not ce or r.length < 4 * P.geometry.min_road_length:
            continue
        line2d = _line2d(r.reference_line)
        half = line2d.length / 2.0
        tail = Road(id=f"r{next_num}",
                    reference_line=_line3d([(x, y) for x, y in substring(line2d, half, line2d.length).coords]),
                    lanes=[Lane(id=l.id, type=l.type, width=l.width, direction=l.direction,
                                marking=l.marking, speed_limit=l.speed_limit, tags=dict(l.tags))
                           for l in r.lanes],
                    name=r.name, highway=r.highway, osm_way_ids=list(r.osm_way_ids),
                    center_marking=r.center_marking, tags={**r.tags, "loop_split": True})
        next_num += 1
        r.reference_line = _line3d([(x, y) for x, y in substring(line2d, 0.0, half).coords])
        tail.successor = r.successor
        tail.predecessor = RoadLink("road", r.id, "end")
        r.successor = RoadLink("road", tail.id, "start")
        road_end_cluster[tail.id] = {"start": None, "end": ce}
        road_end_cluster[r.id]["end"] = None
        road_end_node[tail.id] = {"start": None, "end": road_end_node[r.id]["end"]}
        road_end_node[r.id]["end"] = None
        road_nodes[tail.id] = []
        orig_line[tail.id] = _line2d(tail.reference_line)
        roads.append(tail)
        by_id[tail.id] = tail
        n_loops_split += 1
        log.info("%s: %.1f m loop with both ends at %s; split into %s + %s",
                 r.id, line2d.length, cs.id, r.id, tail.id)
    stats["junction_loops_split"] = n_loops_split

    # 7k. ramp gores as speed-change lanes (P.junction.gore_model == "taper", DESIGN.md "Ramp
    #     gores"). A merge dissolves its gore cluster: the mainline runs through as one road
    #     link, the ramp ends at the nose and links road-to-road into an auxiliary lane of the
    #     downstream mainline road that tapers out (aux lane tags, see model.aux_span). A
    #     diverge gets the deceleration lane tapering in on the upstream road and keeps a
    #     compact nose junction (2 * gore_nose_m long): a road with two successors must be a
    #     junction in OpenDRIVE, and CARLA follows only one road-level successor.
    n_gore_merge = n_gore_diverge = n_gore_kept = 0
    if P.junction.gore_model == "taper":
        def extend_to_node(r: Road, end: str) -> None:
            """Undo the junction trim at ``end``: put back the untrimmed line up to the node."""
            line2d = _line2d(r.reference_line)
            orig = orig_line[r.id]
            if end == "end":
                s0 = orig.project(Point(line2d.coords[-1]))
                if orig.length - s0 < 1e-3:
                    return
                tail = substring(orig, s0, orig.length)
                coords = list(line2d.coords) + list(tail.coords)[1:]
            else:
                s1 = orig.project(Point(line2d.coords[0]))
                if s1 < 1e-3:
                    return
                head = substring(orig, 0.0, s1)
                coords = list(head.coords)[:-1] + list(line2d.coords)
            r.reference_line = _line3d(_dedupe([(x, y) for x, y, *_ in coords]))

        def gore_approaches(c: _Cluster) -> list[_Approach]:
            out: list[_Approach] = []
            for r, end in arms_of(c):
                fwd = [l for l in r.lanes_right() if l.type == "driving" and lane_present_at(l, r, end)]
                bwd = [l for l in r.lanes_left() if l.type == "driving" and lane_present_at(l, r, end)]
                if fwd:
                    out.append(_Approach(r, end, fwd, end == "end"))
                if bwd:
                    out.append(_Approach(r, end, bwd, end == "start"))
            return out

        for c in list(clusters):
            if c.kind != "gore":
                continue
            result = _taper_gore(c, gore_approaches(c), extend_to_node, attach, set_link)
            if result == "merge":
                clusters.remove(c)
                n_gore_merge += 1
            elif result == "diverge":
                c.gore_role = "diverge_nose"
                n_gore_diverge += 1
            else:
                n_gore_kept += 1
                log.info("%s: gore kept as a junction (%s)", c.id, result)
    stats["gore_tapers_merge"] = n_gore_merge
    stats["gore_tapers_diverge"] = n_gore_diverge
    stats["gore_junctions_kept"] = n_gore_kept
    stats["gore_junctions"] = sum(1 for c in clusters if c.kind == "gore")

    # 8. junctions with connecting roads
    junctions: list[Junction] = []
    restrictions = _restriction_index(osm)
    way_names = {w.id: w.tags.get("name", "") for w in osm.ways}
    n_conn_total = 0
    n_restricted = 0
    n_rules_unresolved = 0
    n_parallel_dropped = 0
    n_lane_fallbacks = 0
    n_dead_arms_rescued = 0
    n_dead_departures_rescued = 0
    n_arms_pulled_back = 0
    n_funnel_dead_ends = 0
    plain_roads = list(roads)
    for c in clusters:
        approaches: list[_Approach] = []
        for r in plain_roads:
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is not c:
                    continue
                # left->right in travel; an auxiliary lane (7k) that has tapered out before
                # this end is not an approach lane
                fwd = [l for l in r.lanes_right() if l.type == "driving" and lane_present_at(l, r, end)]
                bwd = [l for l in r.lanes_left() if l.type == "driving" and lane_present_at(l, r, end)]  # +1 first = leftmost
                if end == "end":
                    if fwd:
                        approaches.append(_Approach(r, "end", fwd, True))
                    if bwd:
                        approaches.append(_Approach(r, "end", bwd, False))
                else:
                    if bwd:
                        approaches.append(_Approach(r, "start", bwd, True))
                    if fwd:
                        approaches.append(_Approach(r, "start", fwd, False))
        incoming = [a for a in approaches if a.incoming]
        outgoing = [a for a in approaches if not a.incoming]
        junction = Junction(id=c.id, polygon=None, osm_node_ids=list(c.node_ids),
                            osm_way_ids=sorted(c.way_ids),
                            tags={"kind": c.kind,
                                  **({"gore_role": c.gore_role, "gore_model": "taper"} if c.gore_role else {}),
                                  "centre": [float(np.mean([p[0] for p in c.xy])),
                                             float(np.mean([p[1] for p in c.xy]))],
                                  # a hull smaller than the widest arm's width squared (single
                                  # node, collinear nodes) is widened to that arm's half width
                                  "hull_wkt": (c.hull if c.hull.area >= (2 * max_half[c.id]) ** 2
                                               else c.hull.buffer(max(1.0, max_half[c.id]))).wkt,
                                  # area_wkt: the plaza (open space between the corner
                                  # buildings) when buildings delimit it, else the trim area
                                  "area_wkt": (c.plaza.wkt if c.plaza is not None else
                                               c.area.wkt if c.area is not None else None),
                                  "area_source": "plaza" if c.plaza is not None else "trim",
                                  "plaza_wkt": (c.plaza.wkt if c.plaza is not None else None),
                                  "trim_area_wkt": (c.area.wkt if c.area is not None else None),
                                  "n_incoming": len(incoming), "n_outgoing": len(outgoing)})
        m = 0
        # (arrival, its lane, departure, its lane, turn) for every movement of this junction
        movements: list[tuple[_Approach, Lane, _Approach, Lane, str]] = []
        def make_connection(inc: _Approach, out: _Approach, turn: str,
                            in_lane: Lane, out_lane: Lane) -> bool:
            nonlocal m
            m += 1
            p0 = inc.lane_inner_edge(in_lane)
            p1 = out.lane_inner_edge(out_lane)
            coords = _hermite(p0, inc.heading, p1, out.heading)
            if _polyline_length(coords) < P.geometry.min_road_length:
                coords = [p0, p1] if math.dist(p0, p1) >= P.geometry.min_road_length else coords
                if _polyline_length(coords) < P.geometry.min_road_length:
                    log.warning("%s: skipping degenerate connection %s->%s", c.id, inc.road.id, out.road.id)
                    return False
            cid = f"{c.id}c{m}"
            cr = Road(id=cid, reference_line=_line3d(coords),
                      lanes=[Lane(id=-1, type="driving", width=in_lane.width, direction="forward",
                                  speed_limit=in_lane.speed_limit, tags={"turn": turn})],
                      name=inc.road.name, highway=inc.road.highway, junction_id=c.id,
                      predecessor=RoadLink("road", inc.road.id, inc.contact),
                      successor=RoadLink("road", out.road.id, out.contact),
                      tags={"turn": turn, "from_lane": in_lane.id, "to_lane": out_lane.id,
                            "to_road": out.road.id})
            roads.append(cr)
            junction.connections.append(Connection(
                id=cid, incoming_road=inc.road.id, connecting_road=cid, contact_point="start",
                lane_links=[LaneLink(from_lane=in_lane.id, to_lane=-1)]))
            return True

        # a junction whose other arms only *arrive* (a one-way funnel: Jessie Street into Mint
        # Street, two one-way service ways converging on a two-way one) offers an arrival on
        # the remaining arm no departure at all. That is a dead end in the OSM data, the same
        # kind as a cul-de-sac at a degree-1 node, not a defect of the twin: tag the road end
        # so validate.terminal_lanes / junction_lane_links treat it like one instead of
        # reporting a lane the traffic manager must not be routed onto.
        for inc in (() if c.kind == "gore" else incoming):
            if any(out.road is not inc.road for out in outgoing):
                continue
            inc.road.tags[f"dead_end_{inc.contact}"] = True
            inc.road.tags[f"dead_end_{inc.contact}_reason"] = "oneway_funnel"
            n_funnel_dead_ends += 1
            log.info("%s: %s arrives where every other arm is a one-way into the junction; "
                     "its approach is a dead end", c.id, inc.road.id)

        # room for a connecting road: at a sharp turn between two narrow arms (a one-way lot
        # aisle hairpinning into the next row, both arms cut a few metres from the node) the
        # inner lane edges of the two arms end within P.geometry.min_road_length of each
        # other, the connection would be degenerate and the movement lost. Pull both arms back
        # a metre at a time until every lane pair of the movement has room.
        if c.kind != "gore":
            for inc in incoming:
                for out in outgoing:
                    if out.road is inc.road:
                        continue
                    if abs(_wrap(out.heading - inc.heading)) > math.radians(P.junction.uturn_deg):
                        continue
                    for _ in range(4):
                        if _connection_room(inc, out) >= P.geometry.min_road_length:
                            break
                        pulled = _pull_back(inc.road, inc.contact, 1.0, P.geometry.min_road_length)
                        pulled = _pull_back(out.road, out.contact, 1.0, P.geometry.min_road_length) or pulled
                        if not pulled:
                            break
                        n_arms_pulled_back += 1
                        log.info("%s: %s -> %s had no room for a connecting road; arms pulled back",
                                 c.id, inc.road.id, out.road.id)

        legal_by_inc: dict[int, list[tuple[_Approach, str, bool]]] = {}
        rules_by_inc: dict[int, list] = {}
        for inc in (() if c.kind == "gore" else incoming):
            rules = []
            for rule in _rules_for(restrictions, inc, c):
                targets = _resolve_to(rule, c, outgoing, road_end_node, way_names)
                if targets:
                    rules.append((rule, targets))
                else:
                    n_rules_unresolved += 1
                    log.debug("%s: restriction %s from %s: 'to' ways %s not found among departures",
                              c.id, rule.kind, inc.road.id, sorted(rule.to_ways))
            legal: list[tuple[_Approach, str, bool]] = []
            for out in outgoing:
                if out.road is inc.road:
                    continue  # no u-turns
                delta = _wrap(out.heading - inc.heading)
                if abs(delta) > math.radians(P.junction.uturn_deg):
                    continue
                turn = "through" if abs(delta) < math.radians(P.junction.through_deg) else ("left" if delta > 0 else "right")
                allowed, forced = _apply_rules(rules, out.road)
                if not allowed:
                    n_restricted += 1
                    continue
                legal.append((out, turn, forced))
            # several through departures (a lateral beside the main carriageway): only the
            # laterally aligned one(s) continue this arrival; the lane fan into the parallel
            # road is not a movement
            if not legal:
                # every departure was excluded (turn restrictions that leave nothing, or only
                # near-u-turn departures): the whole arm would dead-end into the junction. Keep
                # the straightest departure on another road so the approach still leads out.
                alt = [(out, _wrap(out.heading - inc.heading)) for out in outgoing
                       if out.road is not inc.road]
                if alt:
                    out, delta = min(alt, key=lambda t: abs(t[1]))
                    turn = ("through" if abs(delta) < math.radians(P.junction.through_deg)
                            else ("left" if delta > 0 else "right"))
                    legal = [(out, turn, True)]
                    n_dead_arms_rescued += 1
                    log.info("%s: %s has no legal departure; keeping the straightest one (%s, %s)",
                             c.id, inc.road.id, out.road.id, turn)
            throughs = [t for t in legal if t[1] == "through"]
            if len(throughs) > 1:
                aligned = [t for t in throughs if t[2] or inc.lateral_gap(t[0]) <= P.junction.through_align_m]
                if aligned and len(aligned) < len(throughs):
                    n_parallel_dropped += len(throughs) - len(aligned)
                    legal = [t for t in legal if t[1] != "through" or t in aligned]
            legal_by_inc[id(inc)] = legal
            rules_by_inc[id(inc)] = rules

        # A lot entrance no arrival leads into. The rules above serve the *arrivals*: each
        # arrival keeps a departure, but a departure may be left that no arrival feeds — the
        # aisle re-attached beside the street when its 20 m link road was absorbed into the
        # junction (its through from the street is then a "parallel through" and dropped), an
        # aisle behind a hairpin. For a street that is the data (turn restrictions are law);
        # for a parking aisle or driveway it leaves the whole lot unreachable, so the
        # straightest arrival the restrictions allow feeds it.
        for out in (() if c.kind == "gore" else outgoing):
            if not out.road.tags.get("parking_aisle"):
                continue
            if any(any(o is out for o, _, _ in legal) for legal in legal_by_inc.values()):
                continue
            cands = []
            for inc in incoming:
                if inc.road is out.road:
                    continue
                allowed, _forced = _apply_rules(rules_by_inc.get(id(inc), []), out.road)
                if not allowed:
                    continue
                delta = _wrap(out.heading - inc.heading)
                cands.append((abs(delta) > math.radians(P.junction.uturn_deg), abs(delta), inc, delta))
            if not cands:
                continue
            _, _, inc, delta = min(cands, key=lambda t: (t[0], t[1]))
            turn = ("through" if abs(delta) < math.radians(P.junction.through_deg)
                    else ("left" if delta > 0 else "right"))
            legal_by_inc.setdefault(id(inc), []).append((out, turn, True))
            n_dead_departures_rescued += 1
            log.info("%s: no arrival led into the lot aisle %s; fed from %s (%s)",
                     c.id, out.road.id, inc.road.id, turn)

        for inc in (() if c.kind == "gore" else incoming):
            legal = legal_by_inc.get(id(inc), [])
            mapped: set[int] = set()
            for out, turn, forced in legal:
                # a single legal departure is a continuation: every lane feeds it
                mapping_turn = "through" if len(legal) == 1 else turn
                for in_lane, out_lane in _lane_pairs(inc.lanes, out.lanes, mapping_turn,
                                                     forced or len(legal) == 1):
                    if make_connection(inc, out, turn, in_lane, out_lane):
                        mapped.add(in_lane.id)
            # every driving lane of an approach must feed at least one connection. An outer lane
            # of a lane-count taper that no movement picks up is a dead end in the xodr: the
            # traffic manager routes a vehicle onto it and then deletes it (the "terrain wedge"
            # dead ends on S Mathilda Ave / Howard St). Give it the nearest legal departure.
            for k_lane, in_lane in enumerate(inc.lanes):
                if in_lane.id in mapped or not legal:
                    continue
                if k_lane == 0:
                    order = ("left", "through", "right")
                elif k_lane == len(inc.lanes) - 1:
                    order = ("right", "through", "left")
                else:
                    order = ("through", "right", "left")
                pick = next((t for want in order for t in legal if t[1] == want), legal[0])
                out, turn = pick[0], pick[1]
                out_lane = (out.lanes[-1] if turn == "right"
                            else out.lanes[0] if turn == "left"
                            else out.lanes[min(k_lane, len(out.lanes) - 1)])
                if make_connection(inc, out, turn, in_lane, out_lane):
                    mapped.add(in_lane.id)
                    n_lane_fallbacks += 1
                    log.debug("%s: lane %d of %s had no movement; added a %s connection to %s",
                              c.id, in_lane.id, inc.road.id, turn, out.road.id)
        if c.kind == "gore":
            # a gore has no turns: the lane count simply changes along the mainline
            for inc, in_lane, out, out_lane, turn in _gore_movements(incoming, outgoing):
                make_connection(inc, out, turn, in_lane, out_lane)
        n_conn_total += len(junction.connections)
        junctions.append(junction)
    stats["connections"] = n_conn_total
    stats["restricted_pairs"] = n_restricted
    stats["restrictions_unresolved"] = n_rules_unresolved
    stats["parallel_throughs_dropped"] = n_parallel_dropped
    stats["lane_link_fallbacks"] = n_lane_fallbacks
    stats["dead_arms_rescued"] = n_dead_arms_rescued
    stats["dead_departures_rescued"] = n_dead_departures_rescued
    stats["arms_pulled_back"] = n_arms_pulled_back
    stats["oneway_funnel_dead_ends"] = n_funnel_dead_ends

    model.roads = roads
    model.junctions = junctions

    # 9. signals + controllers
    _build_signals(model, osm, node_xy, clusters, road_end_cluster, road_nodes, by_id, stats)

    # 10. point objects (buildings were built first); surface parking lots go to the metadata
    #     (surfaces.build_surfaces turns them into ``parking`` surfaces)
    model.objects = _objects(osm, frame)
    meta["parking_lots_wkt"] = _parking_lots(osm, frame)
    stats["parking_lots"] = len(meta["parking_lots_wkt"])

    stats.update({
        "roads": sum(1 for r in roads if r.junction_id is None),
        "connecting_roads": sum(1 for r in roads if r.junction_id is not None),
        "junctions": len(junctions), "signals": len(model.signals),
        "controllers": len(model.controllers), "buildings": len(model.buildings),
        "objects": len(model.objects),
        "parking_lanes": sum(1 for r in roads if r.junction_id is None
                             for l in r.lanes if l.type == "parking"),
        "parking_aisle_roads": sum(1 for r in roads if r.junction_id is None
                                   and r.tags.get("parking_aisle")),
        "driveway_roads": sum(1 for r in roads if r.junction_id is None and r.tags.get("driveway")),
        "profile": P.name,
        "params": {"junction_cluster_m": P.junction.cluster_m, "trim_margin_m": P.junction.trim_margin_m,
                   "service_min_length": P.lane.service_min_length,
                   "parking_aisles": P.parking_aisle.include,
                   "parking_aisle_two_way_m": P.parking_aisle.two_way_width,
                   "parking_aisle_one_way_m": P.parking_aisle.one_way_width,
                   "connect_sample_m": P.geometry.connect_sample_m,
                   "canyon_min_fraction": P.streetspace.canyon_min_fraction,
                   "sidewalk_canyon_fraction": P.sidewalk.canyon_fraction,
                   "min_lane_width": P.lane.min_width, "canyon_max_width": P.lane.canyon_max_width},
        "seconds": round(time.perf_counter() - t0, 2),
    })
    model.metadata.update(meta)
    log.info("lanegraph: %s", {k: v for k, v in stats.items() if not isinstance(v, dict)})
    return model


# --------------------------------------------------------------------------- restrictions

@dataclass
class _Restriction:
    kind: str                # no_left_turn, only_straight_on, ...
    from_ways: set[int]
    to_ways: set[int]
    via_nodes: set[int]
    via_ways: set[int]


def _restriction_index(osm: OsmData) -> list[_Restriction]:
    out = []
    for rel in osm.relations:
        if rel.tags.get("type") != "restriction":
            continue
        kind = rel.tags.get("restriction") or rel.tags.get("restriction:motorcar") or ""
        if not kind:
            continue
        fw = {m.ref for m in rel.members if m.type == "way" and m.role == "from"}
        tw = {m.ref for m in rel.members if m.type == "way" and m.role == "to"}
        vn = {m.ref for m in rel.members if m.type == "node" and m.role == "via"}
        vw = {m.ref for m in rel.members if m.type == "way" and m.role == "via"}
        if fw and tw:
            out.append(_Restriction(kind, fw, tw, vn, vw))
    return out


def _rules_for(restrictions: list[_Restriction], inc: _Approach, c: _Cluster) -> list[_Restriction]:
    """Restrictions whose ``from`` way is on this approach and whose ``via`` is in this junction."""
    ways = set(inc.road.osm_way_ids)
    cluster_nodes = set(c.node_ids)
    rules = []
    for r in restrictions:
        if not (r.from_ways & ways):
            continue
        if r.via_nodes and not (r.via_nodes & cluster_nodes):
            continue
        if r.via_ways and not (r.via_ways & c.way_ids) and not r.via_nodes:
            continue
        rules.append(r)
    return rules


def _resolve_to(rule: _Restriction, c: _Cluster, outgoing: list[_Approach],
                road_end_node: dict, way_names: dict[int, str]) -> set[str]:
    """Departure road ids a restriction's ``to`` ways stand for. A ``to`` way swallowed by the
    junction cluster is followed to the departures leaving from its nodes with the same name."""
    direct = {o.road.id for o in outgoing if set(o.road.osm_way_ids) & rule.to_ways}
    if direct:
        return direct
    out: set[str] = set()
    for wid in rule.to_ways & set(c.way_ids):
        wname = way_names.get(wid, "")
        # walk the internal pieces of the same street that share nodes with the 'to' way
        nodes = set(c.way_nodes.get(wid, set()))
        frontier = [wid]
        seen = {wid}
        while frontier:
            cur = frontier.pop()
            for other, onodes in c.way_nodes.items():
                if other in seen or way_names.get(other, "") != wname:
                    continue
                if onodes & c.way_nodes.get(cur, set()):
                    seen.add(other)
                    nodes |= onodes
                    frontier.append(other)
        for o in outgoing:
            if road_end_node[o.road.id][o.contact] in nodes and o.road.name == wname:
                out.add(o.road.id)
    return out


def _connection_room(inc: _Approach, out: _Approach) -> float:
    """Length of the shortest connecting road any lane pair of the movement ``inc -> out``
    would get (what ``make_connection`` builds: the hermite between the lanes' inner edges,
    or the straight segment when that is longer)."""
    best = math.inf
    for li in inc.lanes:
        p0 = inc.lane_inner_edge(li)
        for lo in out.lanes:
            p1 = out.lane_inner_edge(lo)
            n = max(_polyline_length(_hermite(p0, inc.heading, p1, out.heading)), math.dist(p0, p1))
            best = min(best, n)
    return best


def _pull_back(r: Road, contact: str, d: float, min_length: float) -> bool:
    """Cut ``d`` metres off ``r`` at ``contact`` (its junction end); False when the road would
    drop below twice ``min_length``."""
    line2d = _line2d(r.reference_line)
    if line2d.length - d < 2.0 * min_length:
        return False
    piece = (substring(line2d, d, line2d.length) if contact == "start"
             else substring(line2d, 0.0, line2d.length - d))
    r.reference_line = _line3d([(x, y) for x, y in piece.coords])
    return True


def _apply_rules(rules: list[tuple[_Restriction, set[str]]], out_road: Road) -> tuple[bool, bool]:
    """-> (pair allowed, pair forced by an only_* rule)."""
    allowed, forced = True, False
    for r, targets in rules:
        hits = out_road.id in targets
        if r.kind.startswith("no_") and hits:
            allowed = False
        elif r.kind.startswith("only_"):
            if hits:
                forced = True
            else:
                allowed = False
    return (allowed or forced), forced


def _lane_allows(lane: Lane, turn: str) -> Optional[bool]:
    turns = lane.tags.get("turn")
    if not turns:
        return None
    wanted = {"through": _THROUGH_TURNS, "left": _LEFT_TURNS, "right": _RIGHT_TURNS}[turn]
    return bool(set(turns) & wanted) or "none" in turns


def _lane_pairs(in_lanes: list[Lane], out_lanes: list[Lane], turn: str, forced: bool
                ) -> list[tuple[Lane, Lane]]:
    """Which incoming lane feeds which outgoing lane for a movement. Lanes are ordered
    left->right in travel direction."""
    if not in_lanes or not out_lanes:
        return []
    flags = [_lane_allows(l, turn) for l in in_lanes]
    if any(f is True for f in flags):
        src = [l for l, f in zip(in_lanes, flags) if f]
    elif all(f is False for f in flags) and not forced:
        return []  # turn:lanes forbids this movement from every lane
    else:
        if turn == "through":
            src = list(in_lanes)
        elif turn == "left":
            src = [in_lanes[0]]
        else:
            src = [in_lanes[-1]]
    n_out = len(out_lanes)
    pairs = []
    if turn == "right":
        for k, l in enumerate(reversed(src)):
            pairs.append((l, out_lanes[max(0, n_out - 1 - k)]))
        pairs.reverse()
    else:
        for k, l in enumerate(src):
            pairs.append((l, out_lanes[min(k, n_out - 1)]))
    return pairs


# --------------------------------------------------------------------------- signals

def _gore_movements(incoming: list[_Approach], outgoing: list[_Approach]
                    ) -> list[tuple[_Approach, Lane, _Approach, Lane, str]]:
    """Lane mapping at a freeway gore (merge / diverge): there is no turn there, only a lane
    count change along the mainline.

    Both sides' lane groups are laid out in their lateral order across the mainline and matched
    one to one from the left, so a 5-lane arrival that splits into 4 mainline lanes plus a
    1-lane off-ramp maps straight across (no lane is left without a successor and the ramp is
    fed by the outermost lane only), and an on-ramp merges into the outermost lane of the road
    it joins. ``_lane_pairs``' turn logic would classify a diverging ramp as a "right turn" and
    connect one lane out of five."""
    if not incoming or not outgoing:
        return []
    ref = max(incoming, key=lambda a: len(a.lanes))
    ins = sorted(incoming, key=lambda a: -ref.signed_offset(a))
    outs = sorted(outgoing, key=lambda a: -ref.signed_offset(a))
    in_lanes = [(a, l) for a in ins for l in a.lanes]
    out_lanes = [(a, l) for a in outs for l in a.lanes]
    if not in_lanes or not out_lanes:
        return []
    moves: list[tuple[_Approach, Lane, _Approach, Lane, str]] = []
    seen: set[tuple] = set()
    for k in range(max(len(in_lanes), len(out_lanes))):
        ia, il = in_lanes[min(k, len(in_lanes) - 1)]
        oa, ol = out_lanes[min(k, len(out_lanes) - 1)]
        if ia.road is oa.road:
            continue  # a u-turn back onto the arrival (a clipped arm): never a gore movement
        key = (id(ia), il.id, id(oa), ol.id)
        if key in seen:
            continue
        seen.add(key)
        moves.append((ia, il, oa, ol, "through"))
    return moves


# --------------------------------------------------------------------------- ramp gores as tapers (7k)

def _aux_plan(kind: str, length: float, lane_m: float, taper_m: float, margin: float
              ) -> dict[str, Any]:
    """Lane tags for an auxiliary lane on a mainline road of ``length``: ``kind`` "merge"
    (acceleration lane from the road start: full width over ``lane_m``, then tapering to 0
    over ``taper_m``) or "diverge" (deceleration lane up to the road end: tapering in from 0
    over ``taper_m``, then full width over ``lane_m``). Both are capped so the lane closes at
    least ``margin`` before the far end of the road; a road too short for even a half taper
    carries the lane over its whole length as a weaving lane."""
    avail = length - margin
    total = min(lane_m + taper_m, avail)
    if avail < max(10.0, 0.5 * taper_m):
        return {"aux": "weave", "aux_s0": 0.0, "aux_s1": float(length)}
    taper = min(taper_m, total)
    if kind == "merge":
        return {"aux": "merge", "aux_s0": 0.0, "aux_s1": float(total),
                "taper_s0": float(total - taper), "taper_s1": float(total)}
    s0 = length - total
    return {"aux": "diverge", "aux_s0": float(s0), "aux_s1": float(length),
            "taper_s0": float(s0), "taper_s1": float(s0 + taper)}


def _add_aux_lanes(road: Road, n: int, width: float, tags: dict[str, Any]) -> list[Lane]:
    """Insert ``n`` auxiliary driving lanes outboard of the right-hand driving lanes (inside
    the shoulder) and renumber. The reference line is not moved: the through lanes keep their
    lateral position across the gore."""
    right = road.lanes_right()
    left = road.lanes_left()
    drv = [i for i, l in enumerate(right) if l.type == "driving"]
    idx = drv[-1] + 1 if drv else 0
    outer = right[drv[-1]] if drv else None
    new = [Lane(id=0, type="driving", width=width, direction="forward",
                speed_limit=outer.speed_limit if outer else None, tags=dict(tags))
           for _ in range(n)]
    right[idx:idx] = new
    road.lanes = _renumber(left, right)
    return new


def _merge_overlapping_aux(road: Road) -> None:
    """A merge lane and a diverge lane on one road whose spans overlap are one weaving lane."""
    aux = [l for l in road.lanes if l.tags.get("aux") and l.id < 0]
    merges = [l for l in aux if l.tags["aux"] == "merge"]
    diverges = [l for l in aux if l.tags["aux"] == "diverge"]
    for m in merges:
        for d in diverges:
            if d not in road.lanes or m not in road.lanes:
                continue
            if float(d.tags["aux_s0"]) < float(m.tags["aux_s1"]):
                m.tags.pop("taper_s0", None)
                m.tags.pop("taper_s1", None)
                m.tags.update({"aux": "weave", "aux_s0": 0.0, "aux_s1": float(road.length),
                               "gore_out": d.tags.get("gore"), "ramp_out": d.tags.get("ramp")})
                right = [l for l in road.lanes_right() if l is not d]
                road.lanes = _renumber(road.lanes_left(), right)
                break


def _ramp_to_nose(ramp2d: LineString, main2d: LineString, offset: float, p_nose: tuple[float, float],
                  h_nose: float, *, at_end: bool, blend: float, clear: float, min_keep: float
                  ) -> list[tuple[float, float]]:
    """Re-lay the ramp's end (``at_end``) or start as a Hermite into the nose pose
    ``(p_nose, h_nose)``. The blend starts where the ramp's reference line is at least
    ``offset + clear`` from the mainline and is at least ``blend`` long."""
    L = ramp2d.length
    far: Optional[float] = None
    if at_end:
        s = L - 5.0
        while s >= min_keep:
            if main2d.distance(ramp2d.interpolate(s)) >= offset + clear:
                far = s
                break
            s -= 2.0
        s_c = min(far, L - blend) if far is not None else L - blend
        s_c = max(min_keep, min(s_c, L - 5.0))
        h_c = _heading_along(ramp2d, s_c)
        head = substring(ramp2d, 0.0, s_c)
        q = ramp2d.interpolate(s_c)
        tail = _hermite((q.x, q.y), h_c, p_nose, h_nose)
        return _dedupe([(x, y) for x, y in head.coords][:-1] + tail)
    s = 5.0
    while s <= L - min_keep:
        if main2d.distance(ramp2d.interpolate(s)) >= offset + clear:
            far = s
            break
        s += 2.0
    s_c = max(far, blend) if far is not None else blend
    s_c = min(L - min_keep, max(s_c, 5.0))
    h_c = _heading_along(ramp2d, s_c)
    q = ramp2d.interpolate(s_c)
    head = _hermite(p_nose, h_nose, (q.x, q.y), h_c)
    rest = substring(ramp2d, s_c, L)
    return _dedupe(head + [(x, y) for x, y in rest.coords][1:])


def _taper_gore(c: _Cluster, approaches: list[_Approach], extend_to_node, attach, set_link) -> str:
    """Turn gore cluster ``c`` into a speed-change lane. Returns "merge" (cluster dissolved),
    "diverge" (cluster kept as the nose junction) or the reason it was left as a junction."""
    P = profiles.get()
    incoming = [a for a in approaches if a.incoming]
    outgoing = [a for a in approaches if not a.incoming]
    is_link = lambda a: a.road.highway in P.lane.link_classes
    mains_in = [a for a in incoming if not is_link(a)]
    mains_out = [a for a in outgoing if not is_link(a)]
    ramps = [a for a in approaches if is_link(a)]
    if len(mains_in) != 1 or len(mains_out) != 1 or len(ramps) != 1:
        return f"arms: {len(mains_in)} mainline in, {len(mains_out)} out, {len(ramps)} ramp(s)"
    A, B, R = mains_in[0], mains_out[0], ramps[0]
    for a in (A, B, R):
        if any(l.type == "driving" and l.id > 0 for l in a.road.lanes):
            return f"{a.road.id} is not a one-way carriageway"
    if A.contact != "end" or B.contact != "start" or A.road is B.road or R.road is A.road or R.road is B.road:
        return "mainline orientation"
    if A.signed_offset(R) > 0:
        return f"ramp {R.road.id} is on the left"
    merge = R.incoming
    if merge and R.contact != "end" or not merge and R.contact != "start":
        return "ramp orientation"
    lane_w = A.lanes[-1].width
    k = len(R.lanes)
    n_in, n_out = len(A.lanes), len(B.lanes)

    # untrimmed arms: the nose is the OSM node
    extend_to_node(A.road, "end")
    extend_to_node(B.road, "start")
    extend_to_node(R.road, R.contact)
    main2d = LineString(list(_line2d(A.road.reference_line).coords) + list(_line2d(B.road.reference_line).coords)[1:])

    def right_driving_width(road: Road, upto: Optional[Lane] = None) -> float:
        w = 0.0
        for l in road.lanes_right():
            if l is upto:
                break
            if l.type == "driving":
                w += l.width
        return w

    if merge:
        need = n_in + k - n_out
        if need > 0:
            plan = _aux_plan("merge", B.road.length, P.junction.gore_merge_lane_m,
                             P.junction.gore_merge_taper_m, P.junction.gore_nose_m)
            plan.update({"gore": c.id, "ramp": R.road.id})
            aux = _add_aux_lanes(B.road, need, lane_w, plan)
            _merge_overlapping_aux(B.road)
            first = aux[0]
        else:
            # OSM already gives the downstream road the lane(s): the ramp feeds the outer k
            first = [l for l in B.road.lanes_right() if l.type == "driving"][-k]
        t_ref = -right_driving_width(B.road, first)
        b2d = _line2d(B.road.reference_line)
        p0 = point_on_road(B.road, 0.0, t_ref)
        h0 = _heading_along(b2d, 0.0)
        coords = _ramp_to_nose(_line2d(R.road.reference_line), main2d, -t_ref, (p0.x, p0.y), h0,
                               at_end=True, blend=P.junction.gore_blend_m, clear=P.junction.gore_clear_m,
                               min_keep=P.geometry.min_road_length)
        R.road.reference_line = _line3d(coords)
        for a, end in ((A, "end"), (B, "start"), (R, "end")):
            attach(a.road, end, None)
        set_link(A.road, "end", RoadLink("road", B.road.id, "start"))
        set_link(B.road, "start", RoadLink("road", A.road.id, "end"))
        set_link(R.road, "end", RoadLink("road", B.road.id, "start"))
        R.road.tags.update({"gore_model": "taper", "gore_kind": "merge", "gore_mainline": B.road.id,
                            "gore": c.id})
        B.road.tags.setdefault("gore_merge_ramps", []).append(R.road.id)
        log.info("%s: merge gore -> taper: %s ends into %s (%d lane(s) in + %d ramp -> %d, %d aux)",
                 c.id, R.road.id, B.road.id, n_in, k, n_out, max(0, need))
        return "merge"

    need = n_out + k - n_in
    d = P.junction.gore_nose_m
    a2d, b2d = _line2d(A.road.reference_line), _line2d(B.road.reference_line)
    if a2d.length <= 2 * d + P.geometry.min_road_length or b2d.length <= 2 * d + P.geometry.min_road_length:
        return "mainline too short for a nose junction"
    A.road.reference_line = _line3d([(x, y) for x, y in substring(a2d, 0.0, a2d.length - d).coords])
    B.road.reference_line = _line3d([(x, y) for x, y in substring(b2d, d, b2d.length).coords])
    if need > 0:
        plan = _aux_plan("diverge", A.road.length, P.junction.gore_diverge_lane_m,
                         P.junction.gore_diverge_taper_m, P.junction.gore_nose_m)
        plan.update({"gore": c.id, "ramp": R.road.id})
        aux = _add_aux_lanes(A.road, need, lane_w, plan)
        _merge_overlapping_aux(A.road)
    # the ramp is fed by the outermost k driving lanes of the arrival (auxiliary or not)
    feeders = [l for l in A.road.lanes_right() if l.type == "driving"][-k:]
    t_ref = -right_driving_width(A.road, feeders[0])
    p0 = point_on_road(B.road, 0.0, t_ref)
    h0 = _heading_along(_line2d(B.road.reference_line), 0.0)
    coords = _ramp_to_nose(_line2d(R.road.reference_line), main2d, -t_ref, (p0.x, p0.y), h0,
                           at_end=False, blend=P.junction.gore_blend_m, clear=P.junction.gore_clear_m,
                           min_keep=P.geometry.min_road_length)
    R.road.reference_line = _line3d(coords)
    R.road.tags.update({"gore_model": "taper", "gore_kind": "diverge", "gore_mainline": A.road.id,
                        "gore": c.id})
    A.road.tags.setdefault("gore_diverge_ramps", []).append(R.road.id)
    log.info("%s: diverge gore -> taper + nose junction: %s leaves %s (%d lane(s) in -> %d + %d ramp, %d aux)",
             c.id, R.road.id, A.road.id, n_in, n_out, k, max(0, need))
    return "diverge"


def _signal_side(road: Road, forward: bool) -> float:
    """t of a signal pole just outside the carriageway on the right of travel."""
    P = profiles.get()
    if forward:
        return -(road.width_right() + P.junction.signal_lateral_m)
    return road.width_left() + P.junction.signal_lateral_m


def _lane_runs(ids: Iterable[int]) -> list[tuple[int, int]]:
    """Contiguous inclusive runs of lane ids, e.g. ``[-3, -2, -1]`` -> ``[(-3, -1)]``.

    Lane 0 is never part of a run (OpenDRIVE's centre lane; a ``(0, 0)`` validity is dropped
    outright by ``MapBuilder::RemoveZeroLaneValiditySignalReferences``).
    """
    out: list[tuple[int, int]] = []
    for i in sorted({int(x) for x in ids if int(x) != 0}):
        if out and i == out[-1][1] + 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def _stage_plan(headings: list[float]) -> list[list[int]]:
    """Split a junction's approaches into signal stages, as lists of indices into ``headings``.

    ``headings`` are travel directions of the *incoming* approaches (the direction traffic
    moves in as it reaches the junction), so two approaches facing each other differ by pi.

    Two movements may share a stage only when they cannot conflict. Approaches pointing the
    same way never conflict (the two carriageways of a divided street, or a one-way pair), and
    two opposing approaches conflict only in their left turns -- the classic "N/S green, then
    E/W green" plan, and what stock CARLA maps do (Town10HD junction 23 = 3 controllers,
    junction 189 = 4).

    So: group approaches by direction, then pair opposing groups. If any group has no opposite
    -- a T-junction, a one-way grid, a skewed cluster -- pairing is abandoned and every
    direction becomes its own stage, which is conservative and always conflict-free.

    Stage count is capped at ``JunctionRules.signal_max_stages`` by merging the smallest
    stages; the emitted order is the runtime order (``JunctionParser`` discards ``sequence``).
    """
    P = profiles.get()
    tol = math.radians(P.junction.through_deg)
    n = len(headings)
    if n <= 1:
        return [list(range(n))]

    # ---- same-direction groups
    groups: list[list[int]] = []
    for i, h in enumerate(headings):
        for g in groups:
            if abs(_wrap(h - headings[g[0]])) <= tol:
                g.append(i)
                break
        else:
            groups.append([i])

    stages: list[list[int]] = [list(g) for g in groups]
    if P.junction.signal_stages == "opposing_pairs" and len(groups) > 1:
        paired: dict[int, int] = {}
        used: set[int] = set()
        for a in range(len(groups)):
            if a in used:
                continue
            best, best_err = None, tol
            for b in range(a + 1, len(groups)):
                if b in used:
                    continue
                err = abs(abs(_wrap(headings[groups[a][0]] - headings[groups[b][0]])) - math.pi)
                if err <= best_err:
                    best, best_err = b, err
            if best is not None:
                paired[a] = best
                used.update((a, best))
        if len(used) == len(groups):  # every direction has an opposite
            stages = [groups[a] + groups[b] for a, b in paired.items()]

    # ---- cap: repeatedly fold the smallest stage into the next-smallest
    while len(stages) > max(1, P.junction.signal_max_stages):
        order = sorted(range(len(stages)), key=lambda k: len(stages[k]))
        a, b = sorted(order[:2])
        stages[a] = stages[a] + stages[b]
        del stages[b]

    stages.sort(key=lambda st: _wrap(headings[min(st)]))
    return stages


def _turn_movements(model: TwinModel, junction_id: str) -> dict[tuple[str, int], set[str]]:
    """``(approach road id, its lane id)`` -> the movements that lane makes through ``junction_id``.

    The turn graph is already in the model: ``make_connection`` writes ``turn`` / ``from_lane``
    / ``to_lane`` / ``to_road`` onto every connecting road, and the connecting road's
    predecessor is the approach it leaves. This is the twin's *own* labelling, not a heading
    comparison recomputed from the exported geometry.
    """
    out: dict[tuple[str, int], set[str]] = defaultdict(set)
    for r in model.roads:
        if r.junction_id != junction_id:
            continue
        pred = r.predecessor
        turn = r.tags.get("turn")
        from_lane = r.tags.get("from_lane")
        if pred is None or pred.element != "road" or turn is None or from_lane is None:
            continue
        out[(pred.id, int(from_lane))].add(str(turn))
    return out


def _dedicated_turn_lanes(moves: dict[tuple[str, int], set[str]], road_id: str,
                          lane_ids: Iterable[int], turns: Iterable[str]) -> list[int]:
    """The lanes of one approach whose only movements are in ``turns`` -- a *dedicated* turn
    lane, as opposed to a shared left+through lane.

    Empty unless at least one other lane of the same approach goes somewhere else: an approach
    that turns as a whole (a one-lane slip road) needs no separate arrow, its through signal
    already governs the movement.
    """
    want = set(turns)
    ded, other = [], False
    for lid in lane_ids:
        mv = moves.get((road_id, int(lid)))
        if not mv:
            continue  # a lane with no connection through this junction says nothing
        if mv <= want:
            ded.append(int(lid))
        else:
            other = True
    return ded if (ded and other) else []


# Priority order of the OSM highway classes, most major first. Used to pick the major road of
# an unsignalised junction; a ``*_link`` ranks just below its parent class.
_HIGHWAY_RANK: dict[str, int] = {"motorway": 90, "trunk": 80, "primary": 70, "secondary": 60,
                                 "tertiary": 50, "unclassified": 40, "residential": 30,
                                 "living_street": 20, "service": 10}
_HIGHWAY_RANK.update({f"{k}_link": v - 5 for k, v in list(_HIGHWAY_RANK.items())
                      if k in ("motorway", "trunk", "primary", "secondary", "tertiary")})


def _highway_rank(highway: str) -> int:
    return _HIGHWAY_RANK.get(highway or "", 0)


def _make_signal(sid: str, kind: str, road: Road, s: float, t: float, forward: bool, **kw) -> Signal:
    pos = point_on_road(road, s, t)
    h = _heading_along(road.reference_line, s)
    if not forward:
        h = _wrap(h + math.pi)
    return Signal(id=sid, kind=kind, road_id=road.id, s=float(s), t=float(t), position=pos,
                  heading=float(h), orientation="+" if forward else "-", **kw)


def _unsignalised_control(model: TwinModel, clusters: list[_Cluster], road_end_cluster: dict,
                          plain_roads: list[Road], signalised: set[str],
                          osm_regulated: set[tuple[str, bool]], all_way_clusters: set[str],
                          next_id, stats: dict) -> list[Signal]:
    """Regulatory signs on the approaches of every junction that has no traffic light.

    Without one the Traffic Manager treats the crossing as free: ``LocalizationStage`` only
    stops for a ``1000001`` junction entry and ``TrafficLightStage`` for a stop/yield
    ``RoadInfoSignal``, so an unsigned intersection is one nobody yields at.

    The rule is ``JunctionRules.unsignalised_control`` (US profiles: an all-way stop, MUTCD
    2B.07; EU: give way on the minor approaches and a priority road through). An OSM
    ``highway=stop`` / ``highway=give_way`` node on an approach always wins, and a ``stop=all``
    node makes its junction an all-way stop whatever the profile says.
    """
    P = profiles.get()
    default_rule = P.junction.unsignalised_control
    out: list[Signal] = []
    n_junctions = 0
    per_rule: dict[str, int] = defaultdict(int)
    per_kind: dict[str, int] = defaultdict(int)
    for c in clusters:
        if c.id in signalised or c.kind == "gore":
            continue
        approaches: list[tuple[Road, bool, list[Lane]]] = []
        for r in plain_roads:
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is not c:
                    continue
                forward = end == "end"
                lanes = [l for l in r.lanes if l.type == "driving"
                         and (l.direction == "forward") == forward]
                if lanes:
                    approaches.append((r, forward, lanes))
        if len(approaches) < 2:
            continue  # a dead end or a single arm: nothing to give way to
        # A cluster that is only a road split / continuation -- two approaches and no movement
        # that crosses another -- is not an intersection and gets nothing.
        turns = {r.tags.get("turn") for r in model.roads if r.junction_id == c.id}
        if len(approaches) <= 2 and not (turns - {"through", None}):
            continue
        rule = "all_way_stop" if c.id in all_way_clusters else default_rule
        n_junctions += 1
        per_rule[rule] += 1
        if rule in ("priority_right", "osm"):
            continue  # the default rule of the road, or whatever OSM already said
        # major = the highest (highway class, lane count); an all-way stop has no major road,
        # and when every approach ties there is no priority road either -- everyone gives way
        weight = {(r.id, fwd): (_highway_rank(r.highway), len(lanes))
                  for r, fwd, lanes in approaches}
        best = max(weight.values())
        major = {k for k, v in weight.items() if v == best}
        if rule == "all_way_stop" or len(major) == len(weight):
            major = set()
        minor_kind = "stop" if rule in ("all_way_stop", "minor_stop") else "yield"
        for r, forward, lanes in approaches:
            if (r.id, forward) in osm_regulated:
                per_kind["osm"] += 1
                continue
            kind = "priority_road" if (r.id, forward) in major else minor_kind
            s = r.length if forward else 0.0
            out.append(_make_signal(next_id(), kind, r, s, _signal_side(r, forward), forward,
                                    validities=_lane_runs(l.id for l in lanes),
                                    tags={"junction_id": c.id, "control": rule,
                                          "source": "unsignalised_control"}))
            per_kind[kind] += 1
    stats["unsignalised_junctions"] = n_junctions
    stats["unsignalised_control_rules"] = dict(per_rule)
    stats["unsignalised_control_signals"] = dict(per_kind)
    return out


def _build_signals(model: TwinModel, osm: OsmData, node_xy: dict, clusters: list[_Cluster],
                   road_end_cluster: dict, road_nodes: dict, by_id: dict[str, Road], stats: dict) -> None:
    P = profiles.get()
    signals: list[Signal] = []
    controllers: list[Controller] = []
    counter = [0]

    def next_id() -> str:
        counter[0] += 1
        return f"sig{counter[0]}"

    # parking-lot aisles carry no signals: no traffic lights on a lot entrance approach and
    # no crossings (a zebra node on the street must never land on the aisle beside it)
    plain_roads = [r for r in model.roads
                   if r.junction_id is None and not r.tags.get("parking_aisle")]
    tagged = {nid: n for nid, n in osm.nodes.items() if n.tags}
    xy_of = {}
    for nid, n in tagged.items():
        if nid in node_xy:
            xy_of[nid] = node_xy[nid]
        else:
            xy_of[nid] = _frame_xy(model, n)

    # traffic lights: one per incoming approach of every junction that has a traffic_signals node
    tl_nodes = [nid for nid, n in tagged.items() if n.tags.get("highway") == "traffic_signals"]
    tl_pts = MultiPoint([xy_of[n] for n in tl_nodes]) if tl_nodes else None
    n_tl_junctions = 0
    # filled per signalised junction, read by the pedestrian heads further down
    signalised: dict[str, _Cluster] = {}
    stage_roads: dict[str, list[tuple[str, set[str]]]] = {}
    by_ctl_id: dict[str, Controller] = {}
    for c in clusters:
        if tl_pts is None:
            break
        if c.kind == "gore":
            continue  # a freeway merge/diverge is never signalised (the ramp terminal beyond it is)
        near = [nid for nid in tl_nodes if Point(xy_of[nid]).distance(c.hull) <= P.junction.signal_search_m]
        if not near:
            continue
        n_tl_junctions += 1
        approach_sigs: list[Signal] = []
        arrow_of: dict[int, Signal] = {}   # index into approach_sigs -> its protected-turn signal
        moves = _turn_movements(model, c.id)
        for r in plain_roads:
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is not c:
                    continue
                forward = end == "end"
                lanes = [l for l in r.lanes if l.type == "driving" and
                         (l.direction == "forward") == forward]
                if not lanes:
                    continue
                s = r.length if forward else 0.0
                anchor = point_on_road(r, s, 0.0)
                nearest = min(near, key=lambda nid: anchor.distance(Point(xy_of[nid])))
                # <validity> over exactly this approach's own driving lanes. Under right-hand
                # traffic those are the negative (right) lanes for a forward approach and the
                # positive ones for a backward approach -- the opposite of the side CARLA
                # synthesises when a signal carries no validity at all.
                ded = _dedicated_turn_lanes(moves, r.id, [l.id for l in lanes],
                                            P.junction.signal_arrow_turns)
                through_lanes = [l.id for l in lanes if l.id not in ded]
                base = _make_signal(next_id(), "traffic_light", r, s, _signal_side(r, forward),
                                    forward, osm_node_id=nearest,
                                    validities=_lane_runs(through_lanes or [l.id for l in lanes]),
                                    tags={"junction_id": c.id})
                approach_sigs.append(base)
                if not ded:
                    continue
                # A protected turn cannot be a second head of the through signal: one
                # ATrafficLightBase carries one ETrafficLightState and every client and TM call
                # is per actor. It gets its own signal, its own validity over exactly the
                # dedicated lane(s), and (below) its own leading stage.
                off = P.junction.signal_arrow_offset_m
                s_arrow = min(max(s - off if forward else s + off, 0.0), r.length)
                arrow = _make_signal(base.id + "a", "traffic_light_arrow", r, s_arrow,
                                     _signal_side(r, forward), forward, osm_node_id=nearest,
                                     validities=_lane_runs(ded),
                                     tags={"junction_id": c.id, "through_signal": base.id,
                                           "arrow_turn": ",".join(sorted(P.junction.signal_arrow_turns))})
                arrow_of[len(approach_sigs) - 1] = arrow
        if not approach_sigs:
            continue
        # One <controller> per stage. The runtime ticks exactly one controller per group and
        # round-robins them (ATrafficLightGroup::Tick / NextController), so this is the whole
        # signal plan; a single controller per junction meant every approach went green at once.
        stages = _stage_plan([sig.heading for sig in approach_sigs])
        # A stage's protected turns run as a *leading* stage of their own, in which nothing else
        # is green. Opposing and parallel left turns never conflict with each other, so the
        # arrows of one through stage share one arrow stage (the standard dual protected left).
        plan: list[list[Signal]] = []
        for members in stages:
            if not members:
                continue
            arrows = [arrow_of[i] for i in members if i in arrow_of]
            if arrows:
                plan.append(arrows)
            plan.append([approach_sigs[i] for i in members])
        for k, members_sig in enumerate(plan):
            ctl = Controller(id=f"ctl{c.id[1:]}_p{k}", junction_id=c.id, sequence=k)
            for sig in members_sig:
                sig.controller_id = ctl.id
                ctl.signal_ids.append(sig.id)
            controllers.append(ctl)
            by_ctl_id[ctl.id] = ctl
        signals.extend(approach_sigs)
        signals.extend(arrow_of[i] for i in sorted(arrow_of))
        # the stage plan of this junction, for the pedestrian heads below: which approach roads
        # are green in each stage, in stage order
        stage_roads[c.id] = [(ctl.id, {sig.road_id for sig in members_sig})
                             for ctl, members_sig in zip(controllers[-len(plan):], plan)]
        signalised[c.id] = c
        stats.setdefault("signal_stages_hist", {})
        n_stages = len(plan)
        stats["signal_stages_hist"][n_stages] = stats["signal_stages_hist"].get(n_stages, 0) + 1
        if arrow_of:
            stats["junctions_with_protected_turns"] = stats.get("junctions_with_protected_turns", 0) + 1
            stats["protected_turn_signals"] = stats.get("protected_turn_signals", 0) + len(arrow_of)
    stats["junctions_with_traffic_lights"] = n_tl_junctions

    # crossings, stop, give_way: attach to the road that carries the node (else nearest road)
    node_roads: dict[int, list[str]] = defaultdict(list)
    for rid, nodes in road_nodes.items():
        if rid in by_id:
            for nid in nodes:
                if nid is not None:
                    node_roads[nid].append(rid)
    n_unplaced = 0
    n_clamped = 0
    n_in_junction = 0
    n_ped_heads = 0
    n_ped_merged = 0
    ped_at: list[tuple[str, float]] = []
    # approaches an OSM highway=stop / give_way node already governs: those always win over
    # JunctionRules.unsignalised_control
    osm_regulated: set[tuple[str, bool]] = set()
    all_way_clusters: set[str] = set()
    for nid, n in tagged.items():
        hw = n.tags.get("highway")
        if hw not in ("crossing", "stop", "give_way"):
            continue
        pt = Point(xy_of[nid])
        cands = [by_id[r] for r in node_roads.get(nid, [])
                 if r in by_id and not by_id[r].tags.get("parking_aisle")]
        road = min(cands, key=lambda r: r.reference_line.distance(pt)) if cands else None
        if (road is None or road.reference_line.distance(pt) > 1.0) and plain_roads:
            # the node's own road was shortened past it (taper split, merge): the road that
            # now runs through the node takes it; otherwise the node keeps its own road
            alt = min(plain_roads, key=lambda r: r.reference_line.distance(pt))
            if road is None or alt.reference_line.distance(pt) < 1.0:
                road = alt
        in_junction = [c.id for c in clusters
                       if (c.plaza if c.plaza is not None else c.area) is not None
                       and (c.plaza if c.plaza is not None else c.area).contains(pt)]
        # inside a junction plaza (arms cut at the chamfer line) a crossing can be well past
        # its road's end; it stays attached to that road, flagged for the surface builder
        if road is None or road.reference_line.distance(pt) > (P.junction.plaza_radius_m if in_junction else 15.0):
            n_unplaced += 1
            continue
        line2d = LineString([(x, y) for x, y, *_ in road.reference_line.coords])
        s = float(line2d.project(pt))
        h = _heading_along(line2d, s)
        base = line2d.interpolate(s)
        t = float(-(pt.x - base.x) * math.sin(h) + (pt.y - base.y) * math.cos(h))
        # crossings mapped inside the junction box (typically a cycle lane crossing the
        # carriageway at the intersection node, beyond the trimmed road end): kept, flagged
        # for the surface builder
        along = abs((pt.x - base.x) * math.cos(h) + (pt.y - base.y) * math.sin(h))
        if hw == "crossing" and in_junction and along > P.crossing.keep_m:
            n_in_junction += 1
        else:
            in_junction = []
        if hw == "crossing":
            # the crossing rectangle must stay on the road (not overlap the junction polygon that
            # starts at the road end): clamp s away from the ends
            keep = min(P.crossing.keep_m, line2d.length / 2.0)
            s_c = min(max(s, keep), line2d.length - keep)
            if abs(s_c - s) > 1e-6:
                n_clamped += 1
                s = s_c
            signals.append(_make_signal(next_id(), "crosswalk", road, s, t, True, osm_node_id=nid,
                                        tags={k: v for k, v in n.tags.items() if k.startswith("crossing")}
                                        | {"node_xy": [pt.x, pt.y],
                                           "width": parse_length(n.tags.get("crossing:width")) or P.crossing.width}
                                        | ({"in_junction": in_junction[0]} if in_junction else {})))
            # A pedestrian head at every crossing of a signalised junction. Type 1000002 is a
            # type CARLA does not know: no ATrafficLightBase is generated, MatchSignalAndActor
            # never matches it and TrafficSignsModels has no entry, so it never shows up as a
            # traffic.traffic_light actor -- and it would get no trigger box anyway, since
            # UTrafficLightComponent::InitializeSign skips every non-Driving lane. It carries
            # the phase it *would* run in, over the road's sidewalk lanes.
            if not P.junction.signal_ped_heads:
                continue
            cl = min((cc for cc in signalised.values()
                      if pt.distance(cc.hull) <= P.junction.signal_ped_search_m),
                     key=lambda cc: pt.distance(cc.hull), default=None)
            sidewalks = _lane_runs(l.id for l in road.lanes if l.type == "sidewalk")
            if cl is None or not sidewalks:
                continue
            # OSM maps a crossing as one node per direction of travel, and two of them can land
            # on the same road at the same s: one head per spot, or the baked rigs coincide
            if any(abs(q[1] - s) < P.junction.signal_ped_merge_m
                   for q in ped_at if q[0] == road.id):
                n_ped_merged += 1
                continue
            ped_at.append((road.id, s))
            # derived from the crossing's own id (never from ``next_id``) so that adding
            # pedestrian heads does not renumber every crosswalk <object> after them
            ped = _make_signal(signals[-1].id + "p", "traffic_light_ped", road, s,
                               _signal_side(road, True), True, osm_node_id=nid,
                               validities=sidewalks,
                               tags={"junction_id": cl.id, "crossing_signal": signals[-1].id,
                                     "node_xy": [pt.x, pt.y]})
            # walk when the street being crossed is red: the first stage that greens no
            # approach of this road. A one-stage junction leaves the head uncontrolled.
            for cid, roads_green in stage_roads.get(cl.id, []):
                if road.id not in roads_green:
                    ped.controller_id = cid
                    by_ctl_id[cid].signal_ids.append(ped.id)
                    break
            signals.append(ped)
            n_ped_heads += 1
        else:
            direction = n.tags.get("direction", "").lower()
            forward = direction != "backward"
            if direction not in ("forward", "backward"):
                # unsigned: put it at the road end nearest to the node (stop lines sit at the end)
                forward = s > line2d.length / 2 or not any(l.id > 0 and l.type == "driving" for l in road.lanes)
            # the same correctness fix the traffic lights got: without a <validity> CARLA
            # synthesises one for the oncoming side (MapBuilder::GenerateDefaultValidities-
            # ForSignalReferences), so the sign governs lanes nobody drives on
            appr = [l.id for l in road.lanes if l.type == "driving"
                    and (l.direction == "forward") == forward]
            signals.append(_make_signal(next_id(), "stop" if hw == "stop" else "yield", road, s,
                                        _signal_side(road, forward), forward, osm_node_id=nid,
                                        validities=_lane_runs(appr),
                                        tags={"node_xy": [pt.x, pt.y], "source": "osm"}))
            osm_regulated.add((road.id, forward))
            if hw == "stop" and str(n.tags.get("stop", "")).lower() == "all":
                cl = min((cc for cc in clusters if cc.kind != "gore"),
                         key=lambda cc: pt.distance(cc.hull), default=None)
                if cl is not None and pt.distance(cl.hull) <= P.junction.signal_search_m:
                    all_way_clusters.add(cl.id)
    stats["signal_nodes_unplaced"] = n_unplaced
    stats["crossings_clamped"] = n_clamped
    stats["crossings_in_junction"] = n_in_junction
    stats["pedestrian_heads"] = n_ped_heads
    stats["pedestrian_heads_merged"] = n_ped_merged

    # ---- unsignalised junctions: something must govern them (Phase 5b)
    signals.extend(_unsignalised_control(model, clusters, road_end_cluster, plain_roads,
                                         set(signalised), osm_regulated, all_way_clusters,
                                         next_id, stats))

    # speed limits at road start (forward) / end (backward)
    for r in plain_roads:
        for forward in (True, False):
            lanes = [l for l in r.lanes if l.type == "driving" and (l.direction == "forward") == forward]
            speeds = {l.speed_limit for l in lanes if l.speed_limit}
            if not speeds:
                continue
            v = min(speeds)
            s = 0.0 if forward else r.length
            signals.append(_make_signal(next_id(), "speed_limit", r, s, _signal_side(r, forward), forward,
                                        value=float(v), tags={"maxspeed": r.tags.get("maxspeed")}))
    model.signals = signals
    model.controllers = controllers


def _frame_xy(model: TwinModel, n: OsmNode) -> tuple[float, float]:
    fr = LocalFrame(model.origin_lat, model.origin_lon)
    x, y = fr.to_local(n.lon, n.lat)
    return float(x), float(y)


# --------------------------------------------------------------------------- sidewalk widths

def _separate_sides(tags: dict[str, Any]) -> tuple[bool, bool]:
    """(left, right) sides tagged ``sidewalk=separate`` (in road direction; the road's
    ``reversed`` tag says it runs against its OSM way)."""
    both = str(tags.get("sidewalk") or tags.get("sidewalk:both") or "").lower() == "separate"
    left = right = both
    for side in ("left", "right"):
        v = str(tags.get(f"sidewalk:{side}") or "").lower()
        if v:
            if side == "left":
                left = v == "separate"
            else:
                right = v == "separate"
    if tags.get("reversed"):
        left, right = right, left
    return left, right


@dataclass
class _FootwayIndex:
    lines: list[LineString]
    tree: Optional[STRtree]


def _footway_index(osm: OsmData, frame: LocalFrame) -> _FootwayIndex:
    """Spatial index of the ``highway=footway`` + ``footway=sidewalk`` ways (model space)."""
    lines: list[LineString] = []
    for w in osm.ways:
        if w.tags.get("highway") != "footway" or w.tags.get("footway") != "sidewalk":
            continue
        coords = osm.way_coords(w)
        if len(coords) < 2:
            continue
        lons, lats = zip(*coords)
        x, y = frame.to_local(np.array(lons), np.array(lats))
        lines.append(LineString(list(zip(np.atleast_1d(x), np.atleast_1d(y)))))
    return _FootwayIndex(lines, STRtree(lines) if lines else None)


def _footway_offsets(line2d: LineString, side: str, t_from: float, fw: _FootwayIndex,
                     search: Optional[float] = None, ss: Optional[list[float]] = None) -> list[float]:
    """Outward distance from ``t_from`` (an offset of ``line2d``, +left) to the nearest roughly
    parallel (``sidewalk.parallel_deg``) sidewalk footway within ``search`` m (default
    ``sidewalk.search_m``), per sample."""
    P = profiles.get()
    if search is None:
        search = P.sidewalk.search_m
    if fw.tree is None:
        return []
    L = line2d.length
    if ss is None:
        ss = [float(v) for v in np.arange(2.0, L - 2.0, P.sidewalk.sample_m)] or [L / 2.0]
    par = math.radians(P.sidewalk.parallel_deg)
    sign = 1.0 if side == "left" else -1.0
    out: list[float] = []
    for s in ss:
        p = line2d.interpolate(s)
        h = _heading_along(line2d, s)
        nx, ny = -math.sin(h), math.cos(h)  # left normal
        ex, ey = p.x + t_from * nx, p.y + t_from * ny
        ox, oy = sign * nx, sign * ny  # outward
        best: Optional[float] = None
        for k in fw.tree.query(Point(ex, ey).buffer(search)):
            f = fw.lines[int(k)]
            sf = f.project(Point(ex, ey))
            q = f.interpolate(sf)
            d = (q.x - ex) * ox + (q.y - ey) * oy
            along = abs(-(q.x - ex) * oy + (q.y - ey) * ox)
            if d < 0.3 or d > search or along > 2.0:
                continue  # behind us, too far, or only its end is near
            dh = abs(_wrap(_heading_along(f, sf) - h))
            if min(dh, math.pi - dh) > par:
                continue
            if best is None or d < best:
                best = d
        if best is not None:
            out.append(best)
    return out


def _sidewalk_widths_from_footways(roads: list[Road], fw: _FootwayIndex) -> dict[str, Any]:
    """``sidewalk=separate`` means the sidewalk is mapped as its own ``highway=footway`` +
    ``footway=sidewalk`` way, drawn down the *middle* of the sidewalk. Per side, sample the
    carriageway edge every P.sidewalk.sample_m, take the perpendicular distance to the nearest
    roughly parallel (P.sidewalk.parallel_deg) sidewalk way within P.sidewalk.search_m, and set
    the sidewalk lane width to twice the median of it, clamped to [P.sidewalk.min_width,
    P.sidewalk.max_width]. Sides without a match keep the default. Mutates the lanes in place."""
    P = profiles.get()
    n_set = n_sides = 0
    widths: list[float] = []
    if fw.tree is not None:
        for r in roads:
            if r.junction_id is not None:
                continue
            sep_l, sep_r = _separate_sides(r.tags)
            line2d = _line2d(r.reference_line)
            L = line2d.length
            ss = [float(s) for s in np.arange(2.0, L - 2.0, P.sidewalk.sample_m)] or [L / 2.0]
            for side, sep in (("left", sep_l), ("right", sep_r)):
                if not sep:
                    continue
                lanes = [l for l in r.lanes if l.type == "sidewalk" and (l.id > 0) == (side == "left")]
                if not lanes:
                    continue
                n_sides += 1
                t_edge = r.width_left() if side == "left" else -r.width_right()
                ds = _footway_offsets(line2d, side, t_edge, fw, ss=ss)
                if len(ds) >= max(1, len(ss) // 2):
                    w = min(P.sidewalk.max_width, max(P.sidewalk.min_width, 2.0 * float(np.median(ds))))
                    lanes[0].width = round(w, 2)
                    lanes[0].tags["width_source"] = "footway"
                    widths.append(lanes[0].width)
                    n_set += 1
    if widths:
        log.info("sidewalk widths from %d footway-mapped sides: p10/p50/p90 = %s m", n_set,
                 np.round(np.percentile(widths, [10, 50, 90]), 2).tolist())
    return {"sidewalk_separate_sides": n_sides, "sidewalks_from_footways": n_set}


# --------------------------------------------------------------------------- street canyon

def _clone_lane(l: Lane, **kw) -> Lane:
    d = dict(id=l.id, type=l.type, width=l.width, direction=l.direction, marking=l.marking,
             speed_limit=l.speed_limit, tags=dict(l.tags))
    d.update(kw)
    return Lane(**d)


def _measure_faces(road: Road, bld, blockers) -> Optional[tuple[float, float]]:
    """(left, right) building-face distance from the reference line when the road runs in a
    street canyon (``canyon_fraction`` >= P.streetspace.canyon_min_fraction on both sides), else None.
    Tags ``canyon_fraction`` either way."""
    P = profiles.get()
    line2d = _line2d(road.reference_line)
    _, dl = streetspace.face_distances(line2d, bld, "left", blockers=blockers)
    _, dr = streetspace.face_distances(line2d, bld, "right", blockers=blockers)
    cf_l, cf_r = streetspace.canyon_fraction(dl), streetspace.canyon_fraction(dr)
    road.tags["canyon_fraction"] = [round(cf_l, 2), round(cf_r, 2)]
    road.tags["cross_section_source"] = "tags"
    if min(cf_l, cf_r) < P.streetspace.canyon_min_fraction:
        return None
    fl, fr = streetspace.robust_width(dl, math.nan), streetspace.robust_width(dr, math.nan)
    if not (math.isfinite(fl) and math.isfinite(fr)):
        return None
    return fl, fr


def _street_width_guard(faces: dict[str, tuple[float, float]], key_of: dict[str, tuple]
                        ) -> dict[str, tuple[float, float, bool]]:
    """Pieces of one street (same name, lane count, direction) share a width in a planned
    grid: a piece whose W deviates more than P.geometry.street_width_outlier from the street's median
    (a set-back building, a garden, a corner piece at the bbox edge) is scaled to it."""
    P = profiles.get()
    groups: dict[tuple, list[float]] = defaultdict(list)
    for rid, (fl, fr) in faces.items():
        groups[key_of[rid]].append(fl + fr)
    out = {}
    for rid, (fl, fr) in faces.items():
        ws = groups[key_of[rid]]
        if len(ws) >= 2 and key_of[rid][0]:
            med = float(np.median(ws))
            w = fl + fr
            if w <= 0.5 or med <= 0.5:  # a building drawn over the road (garage, overpass): no street width here
                out[rid] = (fl, fr, False)
                continue
            if abs(w - med) > P.geometry.street_width_outlier * med:
                k = med / w
                log.info("%s: street width %.1f m is off its street's median %.1f m; scaled", rid, w, med)
                out[rid] = (fl * k, fr * k, True)
                continue
        out[rid] = (fl, fr, False)
    return out


def _building_cross_section(road: Road, way_tags: dict[str, str], faces: tuple[float, float],
                            fw: _FootwayIndex) -> bool:
    """Street-canyon cross section: when building faces flank the road on both sides
    (``canyon_fraction`` >= P.streetspace.canyon_min_fraction, nothing in between), the street width W is
    the sum of the two face distances. Sidewalk per side = 2 x (face - footway centreline) when
    a ``sidewalk=separate`` footway runs along it, else P.sidewalk.canyon_fraction x W (clamped to
    [P.sidewalk.min_width, P.sidewalk.max_width]); the carriageway C = W - sidewalks is centred between the
    faces: driving lanes keep their count and widen to min(P.lane.canyon_max_width, C / n) (floor
    P.lane.min_width), what is left becomes parking lanes (P.lane.parking_min..P.lane.parking_max) on the
    sides OSM allows (``parking:*`` tags; else both sides when >= 2 x P.lane.parking_min, else the
    right side of a oneway), any remainder widens the sidewalks. Where the class has a verge
    (US planting strip) it takes its width out of the pedestrian band, between the curb and
    the sidewalk, leaving the sidewalk at least ``sidewalk.min_width``. Tags the road; True
    when the canyon regime applied. Roads with ``sidewalk=no`` keep no sidewalk lane (living
    streets)."""
    P = profiles.get()
    cls = P.lane.for_class(road.highway)
    xy = [(x, y) for x, y, *_ in road.reference_line.coords]
    line2d = LineString(xy)
    fl, fr = faces
    width = fl + fr
    drive = [l for l in road.lanes if l.type == "driving"]
    if not drive:
        return False
    oneway = bool(road.tags.get("oneway_road"))
    no_sidewalk = str(way_tags.get("sidewalk", "")).lower() in ("no", "none")

    # sidewalks along the faces
    sep = _separate_sides(road.tags)
    sw: dict[str, float] = {}
    sw_src: dict[str, str] = {}
    for side, face, has_sep in (("left", fl, sep[0]), ("right", fr, sep[1])):
        est = None
        if has_sep:
            ds = _footway_offsets(line2d, side, 0.0, fw, search=face)
            if len(ds) >= 3:
                est = 2.0 * (face - float(np.median(ds)))
                sw_src[side] = "footway"
        if est is None:
            est = P.sidewalk.canyon_fraction * width
            sw_src[side] = "fraction"
        sw[side] = 0.0 if no_sidewalk else min(P.sidewalk.max_width, max(P.sidewalk.min_width, est))
    carriage = width - sw["left"] - sw["right"]

    # carriageway: keep biking + tagged parking lanes, drop shoulders, widen the driving lanes
    keep_left = [l for l in road.lanes_left() if l.type in ("biking", "parking")]
    keep_right = [l for l in road.lanes_right() if l.type in ("biking", "parking")]
    fixed = sum(l.width for l in keep_left + keep_right)
    n = len(drive)
    lane_w = min(P.lane.canyon_max_width, (carriage - fixed) / n)
    # driving lanes get at least their class width before the sidewalks: a footway drawn
    # close to the face must not squeeze the carriageway
    floor = max(P.lane.min_width, float(cls.lane_width))
    tagged_w = parse_length(way_tags.get("width"))
    if tagged_w and road.highway in ("living_street", "pedestrian", "service"):
        # a level living street keeps its tagged carriageway (Barcelona superblock axes)
        cap = max(P.lane.min_width, (tagged_w - fixed) / n)
        lane_w, floor = min(lane_w, cap), min(floor, cap)
    if lane_w < floor:
        need = (floor - lane_w) * n
        excess = {k: max(0.0, v - P.sidewalk.min_width) for k, v in sw.items()}
        avail = sum(excess.values())
        give = min(need, avail)
        if give > 0:
            for k in sw:
                sw[k] -= give * excess[k] / avail
            carriage += give
        lane_w = max(P.lane.min_width, min(floor, (carriage - fixed) / n))
    rem = max(0.0, carriage - fixed - lane_w * n)
    # parking lanes from the leftover width
    # tagged, or already carrying parking lanes (the class default when the tags are silent)
    tagged_parking = (any(k.startswith("parking") for k in way_tags)
                      or any(l.type == "parking" for l in keep_left + keep_right))
    new_park: list[str] = []
    if (not tagged_parking and rem >= P.lane.parking_min
            and road.highway not in ("living_street", "pedestrian")):  # level streets: no bays
        if rem >= 2 * P.lane.parking_min:
            new_park = ["left", "right"]
        elif oneway:
            new_park = ["right"]
    park_w = 0.0
    if new_park:
        park_w = min(P.lane.parking_max, rem / len(new_park))
        rem -= park_w * len(new_park)
    else:
        existing = [l for l in keep_left + keep_right if l.type == "parking"]
        if existing and rem > 0:
            add = min(rem / len(existing), P.lane.parking_max - min(l.width for l in existing))
            if add > 0:
                for l in existing:
                    l.width += add
                rem -= add * len(existing)
    if rem > 0 and not no_sidewalk:  # the rest widens the sidewalks (up to P.sidewalk.max_width;
        for k in sw:                  # beyond that the space to the face stays unassigned)
            add = min(rem / 2.0, max(0.0, P.sidewalk.max_width - sw[k]))
            sw[k] += add
            rem -= add

    # the planting strip takes its class width out of the pedestrian band
    verge_w = {"left": 0.0, "right": 0.0}
    if cls.verge and not no_sidewalk:
        for k in sw:
            verge_w[k] = min(float(cls.verge), max(0.0, sw[k] - P.sidewalk.min_width)) if sw[k] > 0 else 0.0

    def build_side(side: str) -> list[Lane]:
        out: list[Lane] = []
        for l in (road.lanes_left() if side == "left" else road.lanes_right()):
            if l.type == "driving":
                out.append(_clone_lane(l, width=lane_w))
        out.extend(_clone_lane(l) for l in (keep_left if side == "left" else keep_right))
        if side in new_park:
            out.append(Lane(id=0, type="parking", width=park_w,
                            direction="forward" if (oneway or side == "right") else "backward",
                            tags={"width_source": "buildings"}))
        if sw[side] > 0:
            if verge_w[side] > 0:
                out.append(Lane(id=0, type="verge", width=round(verge_w[side], 2),
                                direction="forward" if (oneway or side == "right") else "backward",
                                tags={"width_source": "buildings"}))
            old = [l for l in road.lanes if l.type == "sidewalk" and (l.id > 0) == (side == "left")]
            w_sw = round(sw[side] - verge_w[side], 2)
            lane = _clone_lane(old[0], width=w_sw) if old else Lane(
                id=0, type="sidewalk", width=w_sw,
                direction="forward" if (oneway or side == "right") else "backward")
            lane.tags["width_source"] = sw_src[side]
            out.append(lane)
        return out

    left, right = build_side("left"), build_side("right")
    road.lanes = _renumber(left, right)
    # centre the carriageway between the faces (the reference line keeps its lane convention)
    t_centre = (fl - fr) / 2.0
    delta = t_centre - (road.width_left() - road.width_right()) / 2.0
    if abs(delta) > 1e-3:
        xy = _dedupe(_offset_polyline(xy, delta))
        road.reference_line = _line3d(xy)
    # the sidewalk lanes fill from the carriageway edge to the face (the footway estimate only
    # decided how much of the street the carriageway takes)
    for l in road.lanes:
        if l.type == "sidewalk":
            face = (fl - delta) if l.id > 0 else (fr + delta)
            edge = (road.width_left() + verge_w["left"]) if l.id > 0 else (road.width_right() + verge_w["right"])
            l.width = round(min(P.sidewalk.max_width, max(P.sidewalk.min_width, face - edge)), 2)
    road.tags.update({
        "cross_section_source": "buildings", "street_width_m": round(width, 2),
        "face_left_m": round(fl - delta, 2), "face_right_m": round(fr + delta, 2),
        "carriageway_m": round(road.width_left() + road.width_right(), 2),
        "width_source": "buildings",
    })
    return True


# --------------------------------------------------------------------------- buildings / objects

def _ring_xy(osm: OsmData, way: OsmWay, frame: LocalFrame) -> Optional[list[tuple[float, float]]]:
    coords = osm.way_coords(way)
    if len(coords) < 3:
        return None
    lons, lats = zip(*coords)
    x, y = frame.to_local(np.array(lons), np.array(lats))
    return [(float(a), float(b)) for a, b in zip(np.atleast_1d(x), np.atleast_1d(y))]


def _assemble_rings(osm: OsmData, way_ids: Iterable[int], frame: LocalFrame) -> list[Polygon]:
    """Join member ways sharing endpoints into closed rings, then polygons."""
    lines = []
    for wid in way_ids:
        try:
            w = osm.way(wid)
        except KeyError:
            continue
        xy = _ring_xy(osm, w, frame)
        if xy and len(xy) >= 2:
            lines.append(LineString(xy))
    if not lines:
        return []
    merged = unary_union(lines)
    polys = [p for p in polygonize(merged) if p.is_valid and p.area > 0.5]
    return polys


def _building_from(tags: dict[str, str], geom: Polygon | MultiPolygon, osm_id: int, prefix: str) -> Building:
    height = parse_length(tags.get("height") or tags.get("building:height") or tags.get("est_height"))
    levels = parse_levels(tags.get("building:levels"))
    keep = {k: v for k, v in tags.items()
            if k in ("building", "name", "roof:shape", "roof:levels", "building:colour", "addr:street",
                     "addr:housenumber", "amenity", "shop", "tourism", "historic", "min_height",
                     "building:min_level", "wikidata")}
    return Building(id=f"{prefix}{osm_id}", footprint=geom, height=height, levels=levels,
                    osm_id=osm_id, tags=keep)


def _buildings(osm: OsmData, frame: LocalFrame) -> list[Building]:
    out: list[Building] = []
    rel_member_ways: set[int] = set()
    for rel in osm.relations:
        if "building" not in rel.tags or rel.tags.get("type") not in ("multipolygon", None, "building"):
            continue
        outers = [m.ref for m in rel.members if m.type == "way" and m.role in ("outer", "")]
        inners = [m.ref for m in rel.members if m.type == "way" and m.role == "inner"]
        if rel.tags.get("type") == "building":
            outers = [m.ref for m in rel.members if m.type == "way" and m.role in ("outline", "outer")]
        rel_member_ways.update(outers)
        rel_member_ways.update(inners)
        outer_polys = _assemble_rings(osm, outers, frame)
        if not outer_polys:
            continue
        geom = unary_union(outer_polys)
        holes = _assemble_rings(osm, inners, frame)
        if holes:
            geom = geom.difference(unary_union(holes))
        if geom.is_empty:
            continue
        if not isinstance(geom, (Polygon, MultiPolygon)):
            geom = unary_union([g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon)])
            if geom.is_empty:
                continue
        out.append(_building_from(rel.tags, geom, rel.id, "br"))
    for w in osm.ways:
        if "building" not in w.tags or w.tags.get("building") == "no":
            continue
        if len(w.nodes) < 4 or w.nodes[0] != w.nodes[-1]:
            continue
        xy = _ring_xy(osm, w, frame)
        if not xy or len(xy) < 4:
            continue
        poly = Polygon(xy)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < 0.5:
            continue
        out.append(_building_from(w.tags, poly, w.id, "bw"))
    return out


def _objects(osm: OsmData, frame: LocalFrame) -> list[PointObject]:
    out: list[PointObject] = []
    for nid, n in osm.nodes.items():
        kind = None
        if n.tags.get("natural") == "tree":
            kind = "tree"
        elif "traffic_sign" in n.tags:
            kind = "traffic_sign"
        if kind is None:
            continue
        x, y = frame.to_local(n.lon, n.lat)
        keep = {k: v for k, v in n.tags.items()
                if k in ("traffic_sign", "direction", "leaf_type", "genus", "species", "height",
                         "diameter_crown", "circumference")}
        out.append(PointObject(id=f"{kind}{nid}", kind=kind, position=Point(float(x), float(y)),
                               osm_id=nid, tags=keep))
    return out


_PARKING_EXCLUDED = ("underground", "multi-storey", "rooftop")


def _parking_lots(osm: OsmData, frame: LocalFrame) -> list[str]:
    """WKT (model space) of the surface parking lots: closed ways and multipolygon relations
    tagged ``amenity=parking`` whose ``parking=*`` is ``surface`` or absent (underground,
    multi-storey and rooftop lots are not ground surfaces)."""
    def wanted(tags: dict[str, str]) -> bool:
        return tags.get("amenity") == "parking" and tags.get("parking", "surface") not in _PARKING_EXCLUDED

    out: list[str] = []
    member_ways: set[int] = set()
    for rel in osm.relations:
        if not wanted(rel.tags) or rel.tags.get("type", "multipolygon") != "multipolygon":
            continue
        outers = [m.ref for m in rel.members if m.type == "way" and m.role in ("outer", "")]
        inners = [m.ref for m in rel.members if m.type == "way" and m.role == "inner"]
        member_ways.update(outers)
        outer_polys = _assemble_rings(osm, outers, frame)
        if not outer_polys:
            continue
        geom = unary_union(outer_polys)
        holes = _assemble_rings(osm, inners, frame)
        if holes:
            geom = geom.difference(unary_union(holes))
        geom = unary_union([g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon) and not g.is_empty])
        if not geom.is_empty:
            out.append(geom.wkt)
    for w in osm.ways:
        if not wanted(w.tags) or w.id in member_ways or len(w.nodes) < 4 or w.nodes[0] != w.nodes[-1]:
            continue
        xy = _ring_xy(osm, w, frame)
        if not xy or len(xy) < 4:
            continue
        poly = Polygon(xy)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.area >= 0.5:
            out.append(poly.wkt)
    return out


# --------------------------------------------------------------------------- quick look

_LANE_COLORS = {"driving": "#555555", "sidewalk": "#2a9d8f", "parking": "#3a86ff", "verge": "#8bc34a",
                "biking": "#d62d9b", "shoulder": "#c8a96e", "median": "#888888", "none": "#bbbbbb"}


def plot_lanegraph(model: TwinModel, out_png: str, dpi: int = 160, show_ids: bool = False,
                   window: Optional[tuple[float, float, float, float]] = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely import wkt as shapely_wkt

    fig, ax = plt.subplots(figsize=(18, 16))
    for b in model.buildings:
        geoms = b.footprint.geoms if isinstance(b.footprint, MultiPolygon) else [b.footprint]
        for g in geoms:
            ax.fill(*g.exterior.xy, color="#ececec", zorder=0)
    for j in model.junctions:
        area = j.tags.get("area_wkt")
        if area:
            g = shapely_wkt.loads(area)
            for poly in (g.geoms if hasattr(g, "geoms") else [g]):
                ax.fill(*poly.exterior.xy, color="#ffd166", alpha=0.35, zorder=1)
        hull = shapely_wkt.loads(j.tags["hull_wkt"])
        if hull.geom_type == "Polygon":
            ax.plot(*hull.exterior.xy, color="#e63946", lw=1.0, zorder=3)
        cx, cy = j.tags["centre"]
        ax.plot(cx, cy, "o", color="#e63946", ms=3, zorder=4)
        if show_ids:
            ax.annotate(j.id, (cx, cy), color="#e63946", fontsize=6, zorder=6)
    for r in model.roads:
        xy = [(x, y) for x, y, *_ in r.reference_line.coords]
        if r.junction_id is not None:
            ax.plot(*zip(*xy), color="#f77f00", lw=0.8, alpha=0.9, zorder=5)
            continue
        ax.plot(*zip(*xy), color="black", lw=1.2, zorder=4)
        for side in (r.lanes_left(), r.lanes_right()):
            off = 0.0
            for lane in side:
                sign = 1.0 if lane.id > 0 else -1.0
                inner, outer = off, off + lane.width
                edge = _offset_polyline(xy, sign * outer)
                mid = _offset_polyline(xy, sign * (inner + outer) / 2)
                ax.plot(*zip(*edge), color=_LANE_COLORS.get(lane.type, "#999999"),
                        lw=0.6 if lane.type != "driving" else 0.8,
                        ls="-" if lane.type != "driving" or (lane.marking and lane.marking.kind == "solid") else "--",
                        zorder=2)
                if lane.type == "driving":
                    ax.plot(*zip(*mid), color="#9aa0a6", lw=0.3, zorder=2)
                    pts = _resample(mid, 25.0)
                    for p, q in zip(pts, pts[1:]):
                        if lane.direction == "backward":
                            p, q = q, p
                        ax.annotate("", xy=q, xytext=p,
                                    arrowprops=dict(arrowstyle="->", color="#9aa0a6", lw=0.4), zorder=2)
                off = outer
        if show_ids:
            m = r.reference_line.interpolate(0.5, normalized=True)
            ax.annotate(r.id, (m.x, m.y), fontsize=5, color="black")
    kinds = {"traffic_light": ("^", "#06d6a0"), "crosswalk": ("s", "#118ab2"), "stop": ("v", "#ef476f"),
             "yield": ("v", "#ffd166"), "speed_limit": ("d", "#8338ec")}
    for s in model.signals:
        mk, col = kinds.get(s.kind, ("x", "k"))
        ax.plot(s.position.x, s.position.y, mk, color=col, ms=3, zorder=6)
    for o in model.objects:
        if o.kind == "tree":
            ax.plot(o.position.x, o.position.y, ".", color="#4caf50", ms=2, zorder=1)
    if window is not None:
        ax.set_xlim(window[0], window[2])
        ax.set_ylim(window[1], window[3])
    ax.set_aspect("equal")
    ax.set_title(f"{model.name}: {sum(1 for r in model.roads if r.junction_id is None)} roads, "
                 f"{len(model.junctions)} junctions, "
                 f"{sum(1 for r in model.roads if r.junction_id)} connecting roads, "
                 f"{len(model.signals)} signals")
    ax.set_xlabel("x east [m]")
    ax.set_ylabel("y north [m]")
    ax.grid(True, lw=0.3, alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_png, dpi=dpi)
    plt.close(fig)
    log.info("wrote %s", out_png)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    from pathlib import Path
    from .ingest.osm import load_fixture, fetch_overpass, parse_osm

    ap = argparse.ArgumentParser(description="lane graph quick look")
    ap.add_argument("--fixture", help="cached Overpass JSON (no network)")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"),
                    default=[41.3905, 2.1630, 41.3945, 2.1690])
    ap.add_argument("--out", default="out/eixample_lanes.png")
    ap.add_argument("--save", help="also save the twin model directory here")
    ap.add_argument("--ids", action="store_true", help="annotate road/junction ids")
    ap.add_argument("--name", default="eixample")
    ap.add_argument("--window", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="zoom the plot to this model-space window")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    bbox = tuple(args.bbox)
    if args.fixture:
        osm = load_fixture(args.fixture)
        if osm.bbox_swne:
            bbox = tuple(osm.bbox_swne)
    else:
        osm = parse_osm(fetch_overpass(bbox, "data"))
    frame = LocalFrame.from_bbox(*bbox)
    model = build_lanegraph(osm, frame, bbox, name=args.name)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    plot_lanegraph(model, args.out, show_ids=args.ids,
                   window=tuple(args.window) if args.window else None)
    print(json.dumps(model.metadata["lanegraph"], indent=1, default=str))
    if args.save:
        model.save(args.save)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
