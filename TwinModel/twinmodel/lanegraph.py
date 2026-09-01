"""OSM ways -> Roads / Lanes / Junctions / Signals (the lane graph).

Pipeline (all pure functions over :mod:`twinmodel.model` dataclasses):

1. select drivable ``highway=*`` ways, normalise ``oneway=-1``, clip to the bbox
2. node degrees in the drivable graph -> intersection nodes
3. cluster intersection nodes (<= ``JUNCTION_CLUSTER_M`` apart *and* joined by a way shorter
   than that) into junctions — the Eixample chamfer octagons collapse into one junction each
4. split ways at intersection nodes, chain compatible pieces through degree-2 nodes -> roads
5. lanes from the DEFAULTS table + tag overrides; reference line = OSM centreline shifted so
   that it sits between forward and backward carriageway lanes (oneway: left carriageway edge)
6. trim roads at the junction area (cluster hull buffered by half the road width + 2 m)
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

from .frame import LocalFrame
from .ingest.osm import OsmData, OsmNode, OsmRelation, OsmWay
from .model import (Building, Connection, Controller, Junction, Lane, LaneLink, Marking,
                    PointObject, Road, RoadLink, Signal, TwinModel)

log = logging.getLogger("twinmodel.lanegraph")

# --------------------------------------------------------------------------- parameters

JUNCTION_CLUSTER_M = 30.0      # cluster intersection nodes closer than this joined by short ways
JUNCTION_INTERNAL_M = 2.0 * JUNCTION_CLUSTER_M  # roads with both ends in one cluster shorter -> internal
TRIM_MARGIN_M = 2.0            # extra distance outside the cluster hull where roads are cut
MIN_ROAD_LENGTH_M = 1.0
STUB_M = 3.0                   # trimmed remnants shorter than this are absorbed into their neighbour
DEAD_END_STUB_M = 10.0         # dead-end stubs off a junction shorter than this are absorbed too
BAND_OVERLAP_M2 = 0.5          # a road's full band may not cover another road's carriageway more
SERVICE_MIN_LENGTH_M = 30.0    # unnamed service ways shorter than this are not roads
CONNECT_SAMPLE_M = 1.0         # connecting road sampling step
THROUGH_DEG = 30.0             # |heading change| below this = through movement
UTURN_DEG = 150.0              # |heading change| above this = u-turn (never connected)
SIGNAL_SEARCH_M = 25.0         # traffic_signals node within this of a junction hull -> lights
SIGNAL_LATERAL_M = 0.5         # signal placed this far outside the carriageway edge
BIKE_LANE_WIDTH = 1.5
PARKING_WIDTH = {"parallel": 2.0, "diagonal": 4.5, "perpendicular": 5.0}
MIN_LANE_WIDTH, MAX_LANE_WIDTH = 2.5, 3.75
CROSSING_KEEP_M = 2.5          # a crossing (4 m) stays whole on its road: cut >= this past the node
CROSSING_NEAR_CUT_M = 5.0      # crossing nodes this close to a trim cut pull the cut back
SIMPLIFY_M = 0.1               # Douglas-Peucker tolerance on trimmed reference lines
JOG_MAX_M = 5.0                # a lateral jog: segment shorter than this ...
JOG_MIN_TURN_DEG = 45.0        # ... turning at least this at both ends, same heading after
JOG_TRANSITION_M = 10.0        # the jog is spread over this much line on either side
SHORT_ROAD_M = 5.0             # non-junction roads shorter than this merge into a neighbour
WIDTH_STEP_M = 1.0             # carriageway width jumps larger than this get reconciled/tapered
TAPER_MAX_M = 15.0             # taper length split off the wider road
TAPER_PIECES_MAX = 3           # ... in at most this many constant-width pieces
SIDEWALK_SEARCH_M = 12.0       # sidewalk=separate: look for footway=sidewalk ways this far out
SIDEWALK_PARALLEL_DEG = 15.0   # ... roughly parallel to the road
SIDEWALK_SAMPLE_M = 5.0
SIDEWALK_MIN_M, SIDEWALK_MAX_M = 1.5, 6.0

DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
            "residential", "living_street", "service",
            "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}

# lane width, default lane count (two-way total), default sidewalk width per side (None = none)
DEFAULTS: dict[str, dict[str, Any]] = {
    "motorway":       {"lane_width": 3.5,  "lanes": 2, "sidewalk": None},
    "motorway_link":  {"lane_width": 3.5,  "lanes": 1, "sidewalk": None},
    "trunk":          {"lane_width": 3.5,  "lanes": 2, "sidewalk": None},
    "trunk_link":     {"lane_width": 3.5,  "lanes": 1, "sidewalk": None},
    "primary":        {"lane_width": 3.5,  "lanes": 2, "sidewalk": 2.0},
    "primary_link":   {"lane_width": 3.5,  "lanes": 1, "sidewalk": 2.0},
    "secondary":      {"lane_width": 3.25, "lanes": 2, "sidewalk": 2.0},
    "secondary_link": {"lane_width": 3.25, "lanes": 1, "sidewalk": 2.0},
    "tertiary":       {"lane_width": 3.25, "lanes": 2, "sidewalk": 2.0},
    "tertiary_link":  {"lane_width": 3.25, "lanes": 1, "sidewalk": 2.0},
    "unclassified":   {"lane_width": 3.25, "lanes": 2, "sidewalk": 2.0},
    "residential":    {"lane_width": 3.0,  "lanes": 2, "sidewalk": 2.0},
    "living_street":  {"lane_width": 3.0,  "lanes": 2, "sidewalk": 2.0},
    "pedestrian":     {"lane_width": 3.0,  "lanes": 1, "sidewalk": 2.0},
    "service":        {"lane_width": 3.0,  "lanes": 2, "sidewalk": None},
}
_FALLBACK_DEFAULT = {"lane_width": 3.25, "lanes": 2, "sidewalk": 2.0}

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


def _hermite(p0, h0, p1, h1, step: float = CONNECT_SAMPLE_M) -> list[tuple[float, float]]:
    """Cubic Hermite from p0 (heading h0) to p1 (heading h1), resampled every ``step`` m."""
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


def _remove_jogs(coords: list[tuple[float, float]], max_len: float = JOG_MAX_M,
                 min_turn_deg: float = JOG_MIN_TURN_DEG, transition: float = JOG_TRANSITION_M
                 ) -> tuple[list[tuple[float, float]], int]:
    """Replace lateral jogs (a segment shorter than ``max_len`` that turns sharply at both ends
    and comes back to the previous heading — OSM mappers draw a 3 m sideways step this way) by a
    gradual shift spread over ``transition`` m on either side. -> (coords, jogs removed)."""
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


_ALL_LANE_TYPES = ("driving", "parking", "biking", "shoulder", "sidewalk", "median", "none")


def _road_band(road: Road, full: bool) -> BaseGeometry:
    """Flat-capped polygon of the road: carriageway lanes only, or all lanes (``full``)."""
    types = _ALL_LANE_TYPES if full else ("driving", "parking", "biking", "shoulder")
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
    """Resize the driving lanes (clamped to [MIN, MAX]) so the core carriageway is ``target`` m
    wide; the remainder goes to the shoulder lane(s) (one is added on the right / removed when
    < 0.5 m). Lane count and order are preserved; the reference line is re-centred."""
    drive = [l for l in road.lanes if l.type == "driving"]
    if not drive or target <= 0:
        return
    shift_before = (road.width_left() - road.width_right()) / 2.0
    lane_w = min(MAX_LANE_WIDTH, max(MIN_LANE_WIDTH, target / len(drive)))
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
    base = default_w if default_w is not None else 2.0
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
    """-> (left width, right width) of on-street parking lanes, None when absent."""
    def width_for(scheme_val: Optional[str], orientation: Optional[str]) -> Optional[float]:
        if scheme_val is None:
            return None
        v = scheme_val.lower()
        if v in ("no", "no_parking", "no_stopping", "no_standing", "separate", "none", "fire_lane"):
            return None
        if v in PARKING_WIDTH:
            return PARKING_WIDTH[v]
        if v in ("yes", "lane", "street_side", "on_street", "half_on_kerb", "on_kerb", "marked"):
            return PARKING_WIDTH.get((orientation or "parallel").lower(), 2.0)
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
    """Build the lane list (OpenDRIVE ordering, ids != 0) for one OSM way from DEFAULTS and tags.

    Right side (negative ids): forward lanes then biking, parking, sidewalk. Left side
    (positive ids): backward driving lanes (two-way) then biking, parking, sidewalk. Oneway
    roads carry all driving lanes on the right; the reference line is then the left carriageway
    edge, so the left side only has biking/parking/sidewalk.
    """
    d = DEFAULTS.get(highway, _FALLBACK_DEFAULT)
    oneway, _ = _is_oneway(tags)
    lane_w = float(d["lane_width"])

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
            n_f = n_fwd or max(1, d["lanes"] // 2)
            n_b = n_bwd or max(1, d["lanes"] - (d["lanes"] // 2))
    n_f = max(1, n_f)
    n_drive = n_f + n_b

    width = parse_length(tags.get("width"))
    shoulder_total = 0.0
    if width and width > 0:
        lane_w = min(MAX_LANE_WIDTH, max(MIN_LANE_WIDTH, width / n_drive))
        shoulder_total = max(0.0, width - lane_w * n_drive)

    speed_f = parse_maxspeed(tags.get("maxspeed:forward") or tags.get("maxspeed"))
    speed_b = parse_maxspeed(tags.get("maxspeed:backward") or tags.get("maxspeed"))
    turns_f = _turn_lanes(tags.get("turn:lanes:forward") or (tags.get("turn:lanes") if oneway or n_b == 0 else tags.get("turn:lanes")))
    turns_b = _turn_lanes(tags.get("turn:lanes:backward"))

    right: list[Lane] = []
    left: list[Lane] = []
    # driving lanes
    for i in range(n_f):
        last = i == n_f - 1
        lane = Lane(id=-(i + 1), type="driving", width=lane_w, direction="forward",
                    marking=Marking("solid" if last else "broken", "white"), speed_limit=speed_f)
        if turns_f and i < len(turns_f) and turns_f[i]:
            lane.tags["turn"] = turns_f[i]
        right.append(lane)
    for i in range(n_b):
        last = i == n_b - 1
        lane = Lane(id=i + 1, type="driving", width=lane_w, direction="backward",
                    marking=Marking("solid" if last else "broken", "white"), speed_limit=speed_b)
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
    # cycle lanes
    cl, cr = _cycleways(tags)
    if cr:
        right.append(Lane(id=0, type="biking", width=BIKE_LANE_WIDTH, direction="forward",
                          marking=Marking("solid", "white"), tags={"cycleway": cr}))
    if cl:
        left.append(Lane(id=0, type="biking", width=BIKE_LANE_WIDTH,
                         direction="backward" if (cl == "opposite" or not oneway) else "forward",
                         marking=Marking("solid", "white"), tags={"cycleway": cl}))
    # parking
    pl, pr = _parking(tags)
    if pr:
        right.append(Lane(id=0, type="parking", width=pr, direction="forward"))
    if pl:
        left.append(Lane(id=0, type="parking", width=pl, direction="backward" if not oneway else "forward"))
    # sidewalks
    sl, sr = _sidewalks(tags, d["sidewalk"])
    if sr:
        right.append(Lane(id=0, type="sidewalk", width=sr, direction="forward"))
    if sl:
        left.append(Lane(id=0, type="sidewalk", width=sl, direction="backward" if not oneway else "forward"))
    # assign ids outward
    for i, lane in enumerate(right):
        lane.id = -(i + 1)
    for i, lane in enumerate(left):
        lane.id = i + 1
    center = Marking("solid", "white")  # two-way: centre line; oneway: left carriageway edge
    return LaneSpec(lanes=left[::-1] + right, center_marking=center, oneway=oneway,
                    n_forward=n_f, n_backward=n_b)


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
    way_nodes: dict[int, set[int]] = field(default_factory=dict)  # internal way -> its node ids

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


# --------------------------------------------------------------------------- selection / clipping

def _is_underground(tags: dict[str, str]) -> bool:
    return tags.get("tunnel") in ("yes", "building_passage") or (_num(tags.get("layer")) or 0) < 0


def _way_is_road(w: OsmWay, length_m: float, ramp_nodes: frozenset[int] | set[int] = frozenset()
                 ) -> bool:
    """``ramp_nodes``: end nodes of underground drivable ways; an unnamed service way ending on
    one is the ramp into that car park and is dropped with it."""
    hw = w.tags.get("highway")
    if hw not in DRIVABLE:
        return False
    if w.tags.get("area") == "yes":
        return False
    if _is_underground(w.tags):
        return False  # underground car-park aisles etc. are not part of the surface twin
    if hw == "service":
        if w.tags.get("service") in ("parking_aisle", "driveway", "drive-through", "emergency_access"):
            return False
        if not w.tags.get("name") and length_m < SERVICE_MIN_LENGTH_M:
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


# --------------------------------------------------------------------------- the builder

def build_lanegraph(osm: OsmData, frame: LocalFrame, bbox: tuple[float, float, float, float],
                    name: str = "twin") -> TwinModel:
    """OSM -> TwinModel with roads, junctions (polygon None), signals, controllers, buildings,
    objects and metadata. ``bbox`` is (S, W, N, E) in WGS84."""
    t0 = time.perf_counter()
    model = TwinModel(name=name, origin_lat=frame.origin_lat, origin_lon=frame.origin_lon,
                      bbox_wgs84=tuple(bbox))
    meta: dict[str, Any] = {"source": "osm", "lanegraph": {}}
    stats = meta["lanegraph"]

    # 1. drivable ways -> pieces inside the bbox
    pieces: list[_Piece] = []
    n_service_dropped = 0
    n_ramps = 0
    ramp_nodes: set[int] = set()  # ends of underground drivable ways: unnamed service ways there are ramps
    for w in osm.ways:
        if w.tags.get("highway") in DRIVABLE and _is_underground(w.tags) and len(w.nodes) >= 2:
            ramp_nodes.update((w.nodes[0], w.nodes[-1]))
    for w in osm.ways:
        if w.tags.get("highway") not in DRIVABLE:
            continue
        coords = osm.way_coords(w)
        if len(coords) < 2:
            continue
        lons, lats = zip(*coords)
        x, y = frame.to_local(np.array(lons), np.array(lats))
        length = _polyline_length(list(zip(np.atleast_1d(x), np.atleast_1d(y))))
        if not _way_is_road(w, length, ramp_nodes):
            n_service_dropped += w.tags.get("highway") == "service"
            n_ramps += _way_is_road(w, length) and not _way_is_road(w, length, ramp_nodes)
            continue
        oneway, rev = _is_oneway(w.tags)
        if rev:  # normalise oneway=-1 by reversing the node order
            w = OsmWay(w.id, list(reversed(w.nodes)), {**w.tags, "oneway": "yes"})
        pieces.extend(_clip_way(w, osm, frame, bbox))
    stats["drivable_ways"] = len({p.way.id for p in pieces})
    stats["service_ways_skipped"] = n_service_dropped

    # 2. node degrees in the drivable graph
    degree: dict[int, int] = defaultdict(int)
    endpoint_ways: dict[int, list[_Piece]] = defaultdict(list)
    for p in pieces:
        for i, nid in enumerate(p.nodes):
            if nid is None:
                continue
            degree[nid] += 1 if i in (0, len(p.nodes) - 1) else 2
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
        if s1.way.tags.get("highway") != s2.way.tags.get("highway"):
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

    # 5. cluster intersection nodes -> junctions. Two intersection nodes join one cluster when a
    #    chain shorter than JUNCTION_CLUSTER_M links them and they are closer than that. Clusters
    #    whose linking road is completely swallowed by the trim are merged and the pass repeated.
    uf = _UnionFind()
    for nid in intersection:
        uf.find(nid)
    for ch in chains:
        nodes = ch.nodes
        a, b = nodes[0], nodes[-1]
        if a in intersection and b in intersection and a != b:
            if (_polyline_length(ch.xy) < JUNCTION_CLUSTER_M
                    and math.dist(node_xy[a], node_xy[b]) < JUNCTION_CLUSTER_M):
                uf.union(a, b)

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
                      "lanes:backward", "placement", "junction", "service", "access")

    # 6. roads from chains (rebuilt per clustering iteration)
    def roads_from_chains(node_cluster: dict[int, _Cluster]):
        roads: list[Road] = []
        road_end_cluster: dict[str, dict[str, Optional[_Cluster]]] = {}
        road_end_node: dict[str, dict[str, Optional[int]]] = {}
        road_nodes: dict[str, list[Optional[int]]] = {}
        n_internal = 0
        n_jogs = 0
        for ch in chains:
            nodes = ch.nodes
            xy, nj = _remove_jogs(_dedupe(ch.xy))
            n_jogs += nj
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
            if c_start is not None and c_start is c_end and length < JUNCTION_INTERNAL_M:
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
            # reference line between forward and backward carriageway lanes
            wl, wr = road.width_left(), road.width_right()
            shift = (wl - wr) / 2.0  # positive: move right (carriageway centre stays on the OSM line)
            if abs(shift) > 1e-3:
                xy = _dedupe(_offset_polyline(xy, -shift))
                road.reference_line = _line3d(xy)
            roads.append(road)
            road_end_cluster[road.id] = {"start": c_start, "end": c_end}
            road_end_node[road.id] = {"start": nodes[0], "end": nodes[-1]}
            road_nodes[road.id] = nodes
        return roads, road_end_cluster, road_end_node, road_nodes, n_internal, n_jogs

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

    def retrim(r: Road) -> bool:
        """Cut ``r`` at the clusters on its ends, from the untrimmed line; False when nothing
        drivable is left. A crossing node within CROSSING_NEAR_CUT_M of a cut pulls the cut back
        to CROSSING_KEEP_M past the node so the crossing stays whole on the road (never into
        the node cluster itself). The result is Douglas-Peucker simplified (SIMPLIFY_M)."""
        line2d = orig_line[r.id]
        L = line2d.length
        lo, hi = 0.0, L
        xnodes = [nid for nid in road_nodes[r.id] if nid in crossing_nodes and nid in node_xy]
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
            cut = c.hull.buffer(hw + TRIM_MARGIN_M, join_style="round")
            c.area = cut if c.area is None else c.area.union(cut)
            keep = "end" if end == "start" else "start"
            iv = cut_interval(line2d, cut, keep)
            if iv is None:
                return False
            core = cut_interval(line2d, c.hull.buffer(max(1.0, hw)), keep)
            if end == "start":
                s_cut = iv[0]
                for nid in xnodes:
                    s_n = line2d.project(Point(node_xy[nid]))
                    if s_cut - CROSSING_NEAR_CUT_M < s_n < s_cut + CROSSING_KEEP_M:
                        s_cut = min(s_cut, max(core[0] if core else 0.0, s_n - CROSSING_KEEP_M))
                lo = max(lo, s_cut)
            else:
                s_cut = iv[1]
                for nid in xnodes:
                    s_n = line2d.project(Point(node_xy[nid]))
                    if s_cut - CROSSING_KEEP_M < s_n < s_cut + CROSSING_NEAR_CUT_M:
                        s_cut = max(s_cut, min(core[1] if core else L, s_n + CROSSING_KEEP_M))
                hi = min(hi, s_cut)
        if hi - lo < MIN_ROAD_LENGTH_M:
            return False
        piece = substring(line2d, lo, hi).simplify(SIMPLIFY_M, preserve_topology=False)
        r.reference_line = _line3d([(x, y) for x, y in piece.coords])
        return True

    n_iter = 0
    dropped: list[Road] = []
    while True:
        n_iter += 1
        clusters, node_cluster = make_clusters()
        roads, road_end_cluster, road_end_node, road_nodes, n_internal, n_jogs = roads_from_chains(node_cluster)
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
    stats["ramps_skipped"] = n_ramps
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

    # 7b. roads swallowed by the trim, and stubs (< STUB_M with a junction at one end only),
    #     are absorbed into that junction; the road continuing from their free end is handed
    #     over to the junction so a street reached through a short lateral does not dangle
    for r in list(roads):
        ends = road_end_cluster[r.id]
        if (ends["start"] is None) == (ends["end"] is None):
            continue
        free_end = "end" if ends["start"] is not None else "start"
        nid = road_end_node[r.id][free_end]
        others = [rb for rb, _e in free_ends().get(nid, []) if rb is not r] if nid is not None else []
        # a stub: shorter than STUB_M, or a dead end shorter than DEAD_END_STUB_M that no
        # other road continues (the entrance to a pedestrian passage): cars go nowhere there
        if r.length < STUB_M or (r.length < DEAD_END_STUB_M and nid is not None and not others):
            dropped.append(r)
            roads.remove(r)
            del by_id[r.id]
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

    # 7e. sidewalk=separate: width from the parallel footway=sidewalk ways
    stats.update(_sidewalk_widths_from_footways(roads, osm, frame))

    # 7f. a road's full band (carriageway + sidewalks) must not cover another road's carriageway
    #     at a junction: with 6 m sidewalks the carriageway-only trim leaves a raised sidewalk
    #     slab across the crossing street's lanes. Shorten the offending arm by exactly the
    #     overlap (not every arm by the full width, which would pave the chamfer corners).
    n_band_cuts = 0
    for _pass in range(3):
        changed = False
        for c in clusters:
            arms = arms_of(c)
            carriage = {r.id: _road_band(r, full=False) for r, _e in arms}
            for r, end in arms:
                band = _road_band(r, full=True)
                line2d = _line2d(r.reference_line)
                s_cut: Optional[float] = None
                for rb, _eb in arms:
                    if rb is r:
                        continue
                    ov = band.intersection(carriage[rb.id])
                    if ov.is_empty or ov.area <= BAND_OVERLAP_M2:
                        continue
                    ss = [line2d.project(Point(x, y)) for x, y in _ring_coords(ov)]
                    reach = (max(ss) if end == "start" else min(ss))
                    s_cut = reach if s_cut is None else (max(s_cut, reach) if end == "start" else min(s_cut, reach))
                if s_cut is None:
                    continue
                # stop TRIM_MARGIN_M short of the corner: two arms cut exactly to the corner
                # where their sidewalks meet leave a zero-length turn between them
                if end == "start":
                    lo, hi = min(line2d.length - MIN_ROAD_LENGTH_M, s_cut + TRIM_MARGIN_M), line2d.length
                else:
                    lo, hi = 0.0, max(MIN_ROAD_LENGTH_M, s_cut - TRIM_MARGIN_M)
                if hi - lo < MIN_ROAD_LENGTH_M or (end == "start" and lo <= 1e-6) or (end == "end" and hi >= line2d.length - 1e-6):
                    log.warning("%s: %s cannot be shortened enough to clear a neighbour's carriageway", c.id, r.id)
                    continue
                r.reference_line = _line3d([(x, y) for x, y in substring(line2d, lo, hi).coords])
                n_band_cuts += 1
                changed = True
        if not changed:
            break
    stats["band_overlap_cuts"] = n_band_cuts

    # 7g. non-junction roads shorter than SHORT_ROAD_M merge into the road-linked neighbour
    #     they continue when the lane configuration matches
    n_short = 0
    for r in list(roads):
        if r.length >= SHORT_ROAD_M:
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

    # 7h. carriageway width steps (> WIDTH_STEP_M) at road<->road links: a road without
    #     width/lanes tags adopts the narrowest tagged neighbour's core width; a step between
    #     tagged roads gets a taper split off the wider road (<= TAPER_MAX_M, in at most
    #     TAPER_PIECES_MAX constant-width pieces, each lane constant per piece)
    def road_links(r: Road):
        for link, end in ((r.predecessor, "start"), (r.successor, "end")):
            if link is not None and link.element == "road" and link.id in by_id:
                yield by_id[link.id], link.contact, end

    def has_width_tags(r: Road) -> bool:
        return "width" in r.tags or "lanes" in r.tags

    n_reconciled = 0
    for r in roads:
        if has_width_tags(r):
            continue
        targets = [_core_width(nb) for nb, _c, _e in road_links(r)
                   if has_width_tags(nb) and abs(_core_width(nb) - _core_width(r)) > WIDTH_STEP_M]
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
        if delta <= WIDTH_STEP_M:
            continue
        wide, wend, narrow, nend = (ra, ea, rb, eb) if wa > wb else (rb, eb, ra, ea)
        if wide.length < 3 * MIN_ROAD_LENGTH_M:
            continue
        taper_len = min(TAPER_MAX_M, wide.length / 2.0)
        k = max(1, min(TAPER_PIECES_MAX, math.ceil(delta / WIDTH_STEP_M - 1e-9)))
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


    # 8. junctions with connecting roads
    junctions: list[Junction] = []
    restrictions = _restriction_index(osm)
    way_names = {w.id: w.tags.get("name", "") for w in osm.ways}
    n_conn_total = 0
    n_restricted = 0
    n_rules_unresolved = 0
    plain_roads = list(roads)
    for c in clusters:
        approaches: list[_Approach] = []
        for r in plain_roads:
            for end in ("start", "end"):
                if road_end_cluster[r.id][end] is not c:
                    continue
                fwd = [l for l in r.lanes_right() if l.type == "driving"]        # left->right in travel
                bwd = [l for l in r.lanes_left() if l.type == "driving"]         # +1 first = leftmost
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
                            tags={"centre": [float(np.mean([p[0] for p in c.xy])),
                                             float(np.mean([p[1] for p in c.xy]))],
                                  # a hull smaller than the widest arm's width squared (single
                                  # node, collinear nodes) is widened to that arm's half width
                                  "hull_wkt": (c.hull if c.hull.area >= (2 * max_half[c.id]) ** 2
                                               else c.hull.buffer(max(1.0, max_half[c.id]))).wkt,
                                  "area_wkt": (c.area.wkt if c.area is not None else None),
                                  "n_incoming": len(incoming), "n_outgoing": len(outgoing)})
        m = 0
        for inc in incoming:
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
                if abs(delta) > math.radians(UTURN_DEG):
                    continue
                turn = "through" if abs(delta) < math.radians(THROUGH_DEG) else ("left" if delta > 0 else "right")
                allowed, forced = _apply_rules(rules, out.road)
                if not allowed:
                    n_restricted += 1
                    continue
                legal.append((out, turn, forced))
            for out, turn, forced in legal:
                # a single legal departure is a continuation: every lane feeds it
                mapping_turn = "through" if len(legal) == 1 else turn
                for in_lane, out_lane in _lane_pairs(inc.lanes, out.lanes, mapping_turn,
                                                     forced or len(legal) == 1):
                    m += 1
                    p0 = inc.lane_inner_edge(in_lane)
                    p1 = out.lane_inner_edge(out_lane)
                    coords = _hermite(p0, inc.heading, p1, out.heading)
                    if _polyline_length(coords) < MIN_ROAD_LENGTH_M:
                        coords = [p0, p1] if math.dist(p0, p1) >= MIN_ROAD_LENGTH_M else coords
                        if _polyline_length(coords) < MIN_ROAD_LENGTH_M:
                            log.warning("%s: skipping degenerate connection %s->%s", c.id, inc.road.id, out.road.id)
                            continue
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
        n_conn_total += len(junction.connections)
        junctions.append(junction)
    stats["connections"] = n_conn_total
    stats["restricted_pairs"] = n_restricted
    stats["restrictions_unresolved"] = n_rules_unresolved

    model.roads = roads
    model.junctions = junctions

    # 9. signals + controllers
    _build_signals(model, osm, node_xy, clusters, road_end_cluster, road_nodes, by_id, stats)

    # 10. buildings and point objects
    model.buildings = _buildings(osm, frame)
    model.objects = _objects(osm, frame)

    stats.update({
        "roads": sum(1 for r in roads if r.junction_id is None),
        "connecting_roads": sum(1 for r in roads if r.junction_id is not None),
        "junctions": len(junctions), "signals": len(model.signals),
        "controllers": len(model.controllers), "buildings": len(model.buildings),
        "objects": len(model.objects),
        "params": {"JUNCTION_CLUSTER_M": JUNCTION_CLUSTER_M, "TRIM_MARGIN_M": TRIM_MARGIN_M,
                   "SERVICE_MIN_LENGTH_M": SERVICE_MIN_LENGTH_M, "CONNECT_SAMPLE_M": CONNECT_SAMPLE_M},
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

def _signal_side(road: Road, forward: bool) -> float:
    """t of a signal pole just outside the carriageway on the right of travel."""
    if forward:
        return -(road.width_right() + SIGNAL_LATERAL_M)
    return road.width_left() + SIGNAL_LATERAL_M


def _make_signal(sid: str, kind: str, road: Road, s: float, t: float, forward: bool, **kw) -> Signal:
    pos = point_on_road(road, s, t)
    h = _heading_along(road.reference_line, s)
    if not forward:
        h = _wrap(h + math.pi)
    return Signal(id=sid, kind=kind, road_id=road.id, s=float(s), t=float(t), position=pos,
                  heading=float(h), orientation="+" if forward else "-", **kw)


def _build_signals(model: TwinModel, osm: OsmData, node_xy: dict, clusters: list[_Cluster],
                   road_end_cluster: dict, road_nodes: dict, by_id: dict[str, Road], stats: dict) -> None:
    signals: list[Signal] = []
    controllers: list[Controller] = []
    counter = [0]

    def next_id() -> str:
        counter[0] += 1
        return f"sig{counter[0]}"

    plain_roads = [r for r in model.roads if r.junction_id is None]
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
    for c in clusters:
        if tl_pts is None:
            break
        near = [nid for nid in tl_nodes if Point(xy_of[nid]).distance(c.hull) <= SIGNAL_SEARCH_M]
        if not near:
            continue
        n_tl_junctions += 1
        ctl = Controller(id=f"ctl{c.id[1:]}", junction_id=c.id)
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
                sig = _make_signal(next_id(), "traffic_light", r, s, _signal_side(r, forward), forward,
                                   controller_id=ctl.id, osm_node_id=nearest,
                                   tags={"junction_id": c.id})
                signals.append(sig)
                ctl.signal_ids.append(sig.id)
        if ctl.signal_ids:
            controllers.append(ctl)
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
    for nid, n in tagged.items():
        hw = n.tags.get("highway")
        if hw not in ("crossing", "stop", "give_way"):
            continue
        pt = Point(xy_of[nid])
        cands = [by_id[r] for r in node_roads.get(nid, []) if r in by_id]
        road = min(cands, key=lambda r: r.reference_line.distance(pt)) if cands else None
        if (road is None or road.reference_line.distance(pt) > 1.0) and plain_roads:
            # the node's own road was shortened past it (taper split, merge): the road that
            # now runs through the node takes it; otherwise the node keeps its own road
            alt = min(plain_roads, key=lambda r: r.reference_line.distance(pt))
            if road is None or alt.reference_line.distance(pt) < 1.0:
                road = alt
        if road is None or road.reference_line.distance(pt) > 15.0:
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
        in_junction = [c.id for c in clusters if c.area is not None and c.area.contains(pt)]
        if hw == "crossing" and in_junction and along > CROSSING_KEEP_M:
            n_in_junction += 1
        else:
            in_junction = []
        if hw == "crossing":
            # the 4 m rectangle must stay on the road (not overlap the junction polygon that
            # starts at the road end): clamp s away from the ends
            keep = min(CROSSING_KEEP_M, line2d.length / 2.0)
            s_c = min(max(s, keep), line2d.length - keep)
            if abs(s_c - s) > 1e-6:
                n_clamped += 1
                s = s_c
            signals.append(_make_signal(next_id(), "crosswalk", road, s, t, True, osm_node_id=nid,
                                        tags={k: v for k, v in n.tags.items() if k.startswith("crossing")}
                                        | {"node_xy": [pt.x, pt.y]}
                                        | ({"in_junction": in_junction[0]} if in_junction else {})))
        else:
            direction = n.tags.get("direction", "").lower()
            forward = direction != "backward"
            if direction not in ("forward", "backward"):
                # unsigned: put it at the road end nearest to the node (stop lines sit at the end)
                forward = s > line2d.length / 2 or not any(l.id > 0 and l.type == "driving" for l in road.lanes)
            signals.append(_make_signal(next_id(), "stop" if hw == "stop" else "yield", road, s,
                                        _signal_side(road, forward), forward, osm_node_id=nid,
                                        tags={"node_xy": [pt.x, pt.y]}))
    stats["signal_nodes_unplaced"] = n_unplaced
    stats["crossings_clamped"] = n_clamped
    stats["crossings_in_junction"] = n_in_junction

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


def _sidewalk_widths_from_footways(roads: list[Road], osm: OsmData, frame: LocalFrame) -> dict[str, Any]:
    """``sidewalk=separate`` means the sidewalk is mapped as its own ``highway=footway`` +
    ``footway=sidewalk`` way, drawn down the *middle* of the sidewalk. Per side, sample the
    carriageway edge every SIDEWALK_SAMPLE_M, take the perpendicular distance to the nearest
    roughly parallel (SIDEWALK_PARALLEL_DEG) sidewalk way within SIDEWALK_SEARCH_M, and set
    the sidewalk lane width to twice the median of it, clamped to [SIDEWALK_MIN_M,
    SIDEWALK_MAX_M]. Sides without a match keep the default. Mutates the lanes in place."""
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
    n_set = n_sides = 0
    widths: list[float] = []
    if lines:
        tree = STRtree(lines)
        par = math.radians(SIDEWALK_PARALLEL_DEG)
        for r in roads:
            if r.junction_id is not None:
                continue
            sep_l, sep_r = _separate_sides(r.tags)
            line2d = _line2d(r.reference_line)
            L = line2d.length
            ss = [float(s) for s in np.arange(2.0, L - 2.0, SIDEWALK_SAMPLE_M)] or [L / 2.0]
            for side, sep in (("left", sep_l), ("right", sep_r)):
                if not sep:
                    continue
                lanes = [l for l in r.lanes if l.type == "sidewalk" and (l.id > 0) == (side == "left")]
                if not lanes:
                    continue
                n_sides += 1
                sign = 1.0 if side == "left" else -1.0
                t_edge = r.width_left() if side == "left" else -r.width_right()
                ds: list[float] = []
                for s in ss:
                    p = line2d.interpolate(s)
                    h = _heading_along(line2d, s)
                    nx, ny = -math.sin(h), math.cos(h)  # left normal
                    ex, ey = p.x + t_edge * nx, p.y + t_edge * ny  # carriageway edge
                    ox, oy = sign * nx, sign * ny  # outward
                    best: Optional[float] = None
                    for k in tree.query(Point(ex, ey).buffer(SIDEWALK_SEARCH_M)):
                        f = lines[int(k)]
                        sf = f.project(Point(ex, ey))
                        q = f.interpolate(sf)
                        d = (q.x - ex) * ox + (q.y - ey) * oy
                        along = abs(-(q.x - ex) * oy + (q.y - ey) * ox)
                        if d < 0.3 or d > SIDEWALK_SEARCH_M or along > 2.0:
                            continue  # behind us, too far, or only its end is near
                        dh = abs(_wrap(_heading_along(f, sf) - h))
                        if min(dh, math.pi - dh) > par:
                            continue
                        if best is None or d < best:
                            best = d
                    if best is not None:
                        ds.append(best)
                if len(ds) >= max(1, len(ss) // 2):
                    w = min(SIDEWALK_MAX_M, max(SIDEWALK_MIN_M, 2.0 * float(np.median(ds))))
                    lanes[0].width = round(w, 2)
                    lanes[0].tags["width_source"] = "footway"
                    widths.append(lanes[0].width)
                    n_set += 1
    if widths:
        log.info("sidewalk widths from %d footway-mapped sides: p10/p50/p90 = %s m", n_set,
                 np.round(np.percentile(widths, [10, 50, 90]), 2).tolist())
    return {"sidewalk_separate_sides": n_sides, "sidewalks_from_footways": n_set}


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


# --------------------------------------------------------------------------- quick look

_LANE_COLORS = {"driving": "#555555", "sidewalk": "#2a9d8f", "parking": "#3a86ff",
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
