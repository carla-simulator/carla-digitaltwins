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
from shapely.ops import unary_union, polygonize

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
SERVICE_MIN_LENGTH_M = 30.0    # unnamed service ways shorter than this are not roads
CONNECT_SAMPLE_M = 1.0         # connecting road sampling step
THROUGH_DEG = 30.0             # |heading change| below this = through movement
UTURN_DEG = 150.0              # |heading change| above this = u-turn (never connected)
SIGNAL_SEARCH_M = 25.0         # traffic_signals node within this of a junction hull -> lights
SIGNAL_LATERAL_M = 0.5         # signal placed this far outside the carriageway edge
BIKE_LANE_WIDTH = 1.5
PARKING_WIDTH = {"parallel": 2.0, "diagonal": 4.5, "perpendicular": 5.0}
MIN_LANE_WIDTH, MAX_LANE_WIDTH = 2.5, 3.75

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

def _way_is_road(w: OsmWay, length_m: float) -> bool:
    hw = w.tags.get("highway")
    if hw not in DRIVABLE:
        return False
    if w.tags.get("area") == "yes":
        return False
    if w.tags.get("tunnel") in ("yes", "building_passage") or (_num(w.tags.get("layer")) or 0) < 0:
        return False  # underground car-park ramps etc. are not part of the surface twin
    if hw == "service":
        if w.tags.get("service") in ("parking_aisle", "driveway", "drive-through", "emergency_access"):
            return False
        if not w.tags.get("name") and length_m < SERVICE_MIN_LENGTH_M:
            return False
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
    for w in osm.ways:
        if w.tags.get("highway") not in DRIVABLE:
            continue
        coords = osm.way_coords(w)
        if len(coords) < 2:
            continue
        lons, lats = zip(*coords)
        x, y = frame.to_local(np.array(lons), np.array(lats))
        length = _polyline_length(list(zip(np.atleast_1d(x), np.atleast_1d(y))))
        if not _way_is_road(w, length):
            n_service_dropped += w.tags.get("highway") == "service"
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
        for ch in chains:
            nodes = ch.nodes
            xy = _dedupe(ch.xy)
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
        return roads, road_end_cluster, road_end_node, road_nodes, n_internal

    # 7. trim roads at junctions (cluster hull buffered by half the road width + margin)
    def half_width(r: Road) -> float:
        return (r.width_left() + r.width_right()) / 2.0

    def trim(line: LineString, cut: BaseGeometry, keep_end: str) -> Optional[LineString]:
        """Remove the part of ``line`` inside ``cut``; keep the piece attached to ``keep_end``."""
        rest = LineString([(x, y) for x, y, *_ in line.coords]).difference(cut)
        if rest.is_empty:
            return None
        parts = list(rest.geoms) if isinstance(rest, MultiLineString) else [rest]
        parts = [pp for pp in parts if isinstance(pp, LineString) and pp.length > 1e-6]
        if not parts:
            return None
        anchor = Point(line.coords[0][:2]) if keep_end == "start" else Point(line.coords[-1][:2])
        parts.sort(key=lambda pp: (pp.distance(anchor), -pp.length))
        best = parts[0]
        if best.distance(anchor) > 0.5:  # anchor swallowed by another cut: keep the longest piece
            best = max(parts, key=lambda pp: pp.length)
        return _line3d([(x, y) for x, y in best.coords])

    n_iter = 0
    while True:
        n_iter += 1
        clusters, node_cluster = make_clusters()
        roads, road_end_cluster, road_end_node, road_nodes, n_internal = roads_from_chains(node_cluster)
        max_half_in_cluster: dict[str, float] = defaultdict(float)
        for r in roads:
            for end in ("start", "end"):
                c = road_end_cluster[r.id][end]
                if c is not None:
                    max_half_in_cluster[c.id] = max(max_half_in_cluster[c.id], half_width(r))

        def cut_polygon(c: _Cluster, r: Road) -> BaseGeometry:
            hw = half_width(r)
            if c.hull.area < 1.0:  # single node / collinear pair: use the widest road at the node
                hw = max(hw, max_half_in_cluster[c.id])
            return c.hull.buffer(hw + TRIM_MARGIN_M, join_style="round")

        kept: list[Road] = []
        n_trim_dropped = 0
        merges: list[tuple[_Cluster, _Cluster]] = []
        for r in roads:
            line = r.reference_line
            ok = True
            for end in ("start", "end"):
                c = road_end_cluster[r.id][end]
                if c is None:
                    continue
                cut = cut_polygon(c, r)
                c.area = cut if c.area is None else c.area.union(cut)
                keep = "end" if end == "start" else "start"
                new = trim(line, cut, keep)
                if new is None or new.length < MIN_ROAD_LENGTH_M:
                    ok = False
                    break
                line = new
            if not ok or line.length < MIN_ROAD_LENGTH_M:
                cs, ce = road_end_cluster[r.id]["start"], road_end_cluster[r.id]["end"]
                if cs is not None and ce is not None and cs is not ce:
                    merges.append((cs, ce))  # the junctions overlap: merge them and redo
                    continue
                n_trim_dropped += 1
                for c in (cs, ce):
                    if c is not None:
                        for wid in r.osm_way_ids:
                            c.absorb(wid, road_nodes[r.id])
                continue
            r.reference_line = line
            cs, ce = road_end_cluster[r.id]["start"], road_end_cluster[r.id]["end"]
            if cs is not None:
                r.predecessor = RoadLink("junction", cs.id)
            if ce is not None:
                r.successor = RoadLink("junction", ce.id)
            kept.append(r)
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
    stats["roads_dropped_by_trim"] = n_trim_dropped
    by_id = {r.id: r for r in roads}

    # road<->road links at degree-2 nodes that did not merge into one chain
    ends_at_node: dict[int, list[tuple[Road, str]]] = defaultdict(list)
    for r in roads:
        for end in ("start", "end"):
            nid = road_end_node[r.id][end]
            if nid is not None and road_end_cluster[r.id][end] is None:
                ends_at_node[nid].append((r, end))
    # absorb trimmed stubs (< STUB_M, junction at one end) into the neighbour / the junction
    n_stubs = 0
    for nid, lst in list(ends_at_node.items()):
        for r, end in list(lst):
            other = "start" if end == "end" else "end"
            c = road_end_cluster[r.id][other]
            if r.length >= STUB_M or c is None:
                continue
            others = [(rb, eb) for rb, eb in lst if rb is not r]
            if len(others) == 1:
                rb, eb = others[0]
                road_end_cluster[rb.id][eb] = c
                if eb == "end":
                    rb.successor = RoadLink("junction", c.id)
                else:
                    rb.predecessor = RoadLink("junction", c.id)
                for wid in r.osm_way_ids:
                    c.absorb(wid, road_nodes[r.id])
                roads.remove(r)
                del by_id[r.id]
                ends_at_node[nid] = [(rb2, eb2) for rb2, eb2 in lst if rb2 is not r and rb2 is not rb]
                n_stubs += 1
    for r in list(roads):
        if r.length < STUB_M and r.junction_id is None:
            ends = road_end_cluster[r.id]
            if (ends["start"] is None) != (ends["end"] is None):
                c = ends["start"] or ends["end"]
                other = "end" if ends["start"] is not None else "start"
                if len(ends_at_node.get(road_end_node[r.id][other], [])) <= 1:  # dead-end stub
                    for wid in r.osm_way_ids:
                        c.absorb(wid, road_nodes[r.id])
                    roads.remove(r)
                    del by_id[r.id]
                    n_stubs += 1
    stats["stubs_absorbed"] = n_stubs
    for nid, lst in ends_at_node.items():
        if len(lst) != 2:
            continue
        (ra, ea), (rb, eb) = lst
        link_a = RoadLink("road", rb.id, eb)
        link_b = RoadLink("road", ra.id, ea)
        if ea == "end":
            ra.successor = link_a
        else:
            ra.predecessor = link_a
        if eb == "end":
            rb.successor = link_b
        else:
            rb.predecessor = link_b

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
                                  "hull_wkt": c.hull.wkt,
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
    for nid, n in tagged.items():
        hw = n.tags.get("highway")
        if hw not in ("crossing", "stop", "give_way"):
            continue
        pt = Point(xy_of[nid])
        cands = [by_id[r] for r in node_roads.get(nid, [])] or plain_roads
        if not cands:
            n_unplaced += 1
            continue
        road = min(cands, key=lambda r: r.reference_line.distance(pt))
        if road.reference_line.distance(pt) > 15.0:
            n_unplaced += 1
            continue
        line2d = LineString([(x, y) for x, y, *_ in road.reference_line.coords])
        s = float(line2d.project(pt))
        h = _heading_along(line2d, s)
        base = line2d.interpolate(s)
        t = float(-(pt.x - base.x) * math.sin(h) + (pt.y - base.y) * math.cos(h))
        if hw == "crossing":
            signals.append(_make_signal(next_id(), "crosswalk", road, s, t, True, osm_node_id=nid,
                                        tags={k: v for k, v in n.tags.items() if k.startswith("crossing")}
                                        | {"node_xy": [pt.x, pt.y]}))
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
