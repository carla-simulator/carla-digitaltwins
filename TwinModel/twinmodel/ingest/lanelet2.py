"""Autoware Lanelet2 map -> Twin Model lane graph.

An Autoware vector map (``lanelet2_map.osm``) replaces the OSM road network as the source of the roads, lanes, junctions and traffic
control; everything downstream (surfaces, OpenDRIVE, meshes, the Unreal bake) is unchanged.
Buildings, trees and the region profile still come from OpenStreetMap for the same area
(``cli.build --lanelet2``).

Mapping
-------
* Every vehicle lanelet (``subtype`` road / highway / bus_lane / play_street) becomes a
  :class:`~twinmodel.model.Road` with one driving lane ``-1``: the reference line is the
  lanelet's left bound (in travel direction) and the lane's width follows the distance to the
  right bound (``Lane.tags["reviewed_widths"]``, a piecewise linear width the OpenDRIVE writer
  emits), so the OpenDRIVE lane *is* the lanelet. A ``road_border`` bound gets a sidewalk lane
  outside it.
* Lanelets inside an intersection (``turn_direction``, inside an ``intersection_area``) and the
  branches of a split or a merge are *junction lanelets*. Overlapping or chained junction
  lanelets form one :class:`~twinmodel.model.Junction`; every path through it from an entry
  lanelet to an exit lanelet becomes one connecting road (the chained lanelets' geometry
  concatenated), because an OpenDRIVE connecting road links an incoming road to an outgoing one.
* Lanelet succession (shared bound end points) becomes road links and junction connections.
* ``traffic_light`` regulatory elements become traffic-light signals at the stop line of every
  lanelet that references them, grouped into stages per junction (``lanegraph._stage_plan``:
  Lanelet2 has no signal phases); stop signs become ``stop`` signals; crosswalk lanelets become
  ``crosswalk`` signals on the roads they cross, with the crosswalk's own outline as the
  crossing surface; ``speed_limit`` becomes the lane speed limit.
* The drivable surface is the union of the vehicle and shoulder lanelets (passed to
  ``surfaces.build_surfaces`` as the drivable polygon) and the lane markings are the
  lanelets' painted bounds and stop lines, so the mesh shows the map rather than a
  reconstruction from lane widths.
* The node elevations (``ele``) become the elevation grid (:func:`elevation_from_lanelet2`).

Coordinates: the nodes' ``lat``/``lon`` (which Autoware writes for every geodetic projector:
MGRS, LocalCartesianUTM, TransverseMercator) are projected into the twin's local frame,
centred on the map. ``projector_type: Local`` maps (``local_x``/``local_y`` only) are not read.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import shapely
from lxml import etree
from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

from .. import profiles
from ..frame import LocalFrame
from ..model import (Connection, Controller, Elevation, Junction, Lane, LaneLink, Marking, Road,
                     RoadLink, Signal, TwinModel)

log = logging.getLogger("twinmodel.ingest.lanelet2")

VEHICLE_SUBTYPES = {"road", "highway", "bus_lane", "play_street"}
SHOULDER_SUBTYPES = {"road_shoulder"}
CROSSWALK_SUBTYPES = {"crosswalk"}
# painted bound types -> marking width (m)
PAINTED = {"line_thin": 0.15, "line_thick": 0.30}
STOP_LINE_WIDTH = 0.45
# two lanelet ends closer than this (both bounds) are the same point even without shared nodes
END_TOL = 0.10
# at most this many connecting roads from one entry lanelet into a junction (paths through
# chained junction lanelets can multiply)
MAX_PATHS_PER_ENTRY = 64
WIDTH_STEP = 2.0          # sampling of the lane width along the reference line (m)
MIN_LANELET_LENGTH = 0.2
SLIVER_M = 5.0            # a plain lanelet shorter than this between two junctions merges them
LEVEL_DZ = 2.5            # lanelets overlapping in plan this far apart in z are on two levels
TL_SEARCH_M = 120.0       # a light's lanelet can be this far before its junction


# ------------------------------------------------------------------------------ reading

@dataclass
class Lanelet2Map:
    """A Lanelet2 OSM file in model coordinates."""
    nodes: dict[int, tuple[float, float, float]]
    ways: dict[int, tuple[list[int], dict[str, str]]]
    relations: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]]
    frame: LocalFrame
    bbox: tuple[float, float, float, float]          # S W N E (WGS84)

    def coords(self, way_id: int) -> np.ndarray:
        return np.array([self.nodes[n] for n in self.ways[way_id][0]], dtype=np.float64)


def load_lanelet2(path: Path | str) -> Lanelet2Map:
    """Read ``path`` into model coordinates (a local frame centred on the map)."""
    root = etree.parse(str(Path(path))).getroot()
    raw_nodes: dict[int, tuple[float, float, float]] = {}
    for n in root.iter("node"):
        tags = {t.get("k"): t.get("v") for t in n.iter("tag")}
        raw_nodes[int(n.get("id"))] = (float(n.get("lat", 0.0)), float(n.get("lon", 0.0)),
                                       float(tags.get("ele", 0.0) or 0.0))
    ways = {int(w.get("id")): ([int(nd.get("ref")) for nd in w.iter("nd")],
                               {t.get("k"): t.get("v") for t in w.iter("tag")})
            for w in root.iter("way")}
    relations = {int(r.get("id")): ([(m.get("type"), int(m.get("ref")), m.get("role") or "")
                                     for m in r.iter("member")],
                                    {t.get("k"): t.get("v") for t in r.iter("tag")})
                 for r in root.iter("relation")}
    lat = np.array([v[0] for v in raw_nodes.values()])
    lon = np.array([v[1] for v in raw_nodes.values()])
    if not len(lat) or (np.allclose(lat, 0.0) and np.allclose(lon, 0.0)):
        raise ValueError("the nodes carry no lat/lon (projector_type Local maps are not supported)")
    bbox = (float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max()))
    frame = LocalFrame.from_bbox(*bbox)
    x, y = frame.to_local(lon, lat)
    nodes = {i: (float(xx), float(yy), v[2]) for (i, v), xx, yy in zip(raw_nodes.items(), x, y)}
    return Lanelet2Map(nodes=nodes, ways=ways, relations=relations, frame=frame, bbox=bbox)


# ------------------------------------------------------------------------------ lanelets

@dataclass
class _Lanelet:
    id: int
    tags: dict[str, str]
    left: np.ndarray           # (N, 3) in travel direction
    right: np.ndarray          # (M, 3) in travel direction
    left_way: int
    right_way: int
    regelems: list[int]
    polygon: Polygon = None
    next: list[int] = field(default_factory=list)
    prev: list[int] = field(default_factory=list)

    @property
    def length(self) -> float:
        return float(LineString(self.left[:, :2]).length)


def _signed_side(line: np.ndarray, p: np.ndarray) -> float:
    """> 0 when ``p`` is left of the polyline ``line`` (nearest segment)."""
    best, side = np.inf, 0.0
    for a, b in zip(line[:-1, :2], line[1:, :2]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 < 1e-12 else min(max(float((p - a) @ ab) / L2, 0.0), 1.0)
        q = a + t * ab
        d = float(np.hypot(*(p - q)))
        if d < best:
            best, side = d, float(ab[0] * (p - a)[1] - ab[1] * (p - a)[0])
    return side


def _mid(line: np.ndarray) -> np.ndarray:
    return line[len(line) // 2, :2] if len(line) > 2 else (line[0, :2] + line[-1, :2]) / 2.0


def _align(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """lanelet2's ``geometry::align``: the right bound must lie right of the left one and vice
    versa; the left bound's direction is the travel direction."""
    if len(left) > 1 and _signed_side(left, _mid(right)) > 0:
        left = left[::-1]
    if len(right) > 1 and _signed_side(right, _mid(left)) < 0:
        right = right[::-1]
    return left, right


def _lanelets(lm: Lanelet2Map, subtypes: set[str]) -> dict[int, _Lanelet]:
    out = {}
    for rid, (members, tags) in lm.relations.items():
        if tags.get("type") != "lanelet" or tags.get("subtype") not in subtypes:
            continue
        if subtypes is VEHICLE_SUBTYPES and tags.get("participant:vehicle") == "no":
            continue
        lw = [ref for t, ref, role in members if t == "way" and role == "left"]
        rw = [ref for t, ref, role in members if t == "way" and role == "right"]
        if not lw or not rw or lw[0] not in lm.ways or rw[0] not in lm.ways:
            continue
        left, right = _align(lm.coords(lw[0]), lm.coords(rw[0]))
        if len(left) < 2 or len(right) < 2 or LineString(left[:, :2]).length < MIN_LANELET_LENGTH:
            continue
        ll = _Lanelet(rid, tags, left, right, lw[0], rw[0],
                      [ref for t, ref, role in members if t == "relation" and role == "regulatory_element"])
        poly = shapely.make_valid(Polygon(np.vstack([left[:, :2], right[::-1, :2]])))
        ll.polygon = poly if not poly.is_empty else Polygon()
        out[rid] = ll
    return out


def _link(lanelets: dict[int, _Lanelet]) -> None:
    """``next``/``prev``: B follows A when A's bounds end where B's start (lanelet2's rule,
    with a small tolerance for maps that duplicate the nodes)."""
    ids = list(lanelets)
    if not ids:
        return
    starts = np.array([np.r_[lanelets[i].left[0, :2], lanelets[i].right[0, :2]] for i in ids])
    from scipy.spatial import cKDTree
    tree = cKDTree(starts)
    for a in ids:
        la = lanelets[a]
        q = np.r_[la.left[-1, :2], la.right[-1, :2]]
        for k in tree.query_ball_point(q, r=END_TOL * 2.0):
            b = ids[k]
            if b == a:
                continue
            if (np.hypot(*(starts[k][:2] - q[:2])) <= END_TOL
                    and np.hypot(*(starts[k][2:] - q[2:])) <= END_TOL):
                la.next.append(b)
                lanelets[b].prev.append(a)


# ------------------------------------------------------------------------------ geometry helpers

def _widths(ll_left: np.ndarray, ll_right: np.ndarray, s_offset: float = 0.0
            ) -> list[tuple[float, float]]:
    """``[(s, width)]`` along the left bound: the distance to the right bound."""
    ref = LineString(ll_left[:, :2])
    rb = LineString(ll_right[:, :2])
    n = max(2, int(math.ceil(ref.length / WIDTH_STEP)) + 1)
    out = []
    for s in np.linspace(0.0, ref.length, n):
        w = float(rb.distance(ref.interpolate(s)))
        out.append((round(float(s + s_offset), 4), round(max(w, 0.05), 4)))
    return out


def _concat(lines: list[np.ndarray]) -> np.ndarray:
    pts = [lines[0]]
    for ln in lines[1:]:
        pts.append(ln[1:] if np.hypot(*(ln[0, :2] - pts[-1][-1, :2])) < END_TOL else ln)
    return np.vstack(pts)


def _union(polys: list) -> Polygon:
    polys = [p for p in polys if p is not None and not p.is_empty]
    if not polys:
        return Polygon()
    u = shapely.union_all([p.buffer(0.05, join_style="mitre") for p in polys]).buffer(-0.05, join_style="mitre")
    return shapely.make_valid(u)


def _levels(L: dict[int, "_Lanelet"]) -> dict[int, int]:
    """Vertical level per lanelet (an OSM ``layer``). Only a lanelet that overlaps another in plan
    more than ``LEVEL_DZ`` apart in z leaves level 0: +1 when it is that far above the lanes
    around it (a deck), -1 when below them (an underpass, which the surfaces then enclose)."""
    ids = list(L)
    level = {i: 0 for i in ids}
    if not ids:
        return level
    bounds = {i: (LineString(L[i].left), LineString(L[i].right)) for i in ids}

    def z_near(i: int, p: np.ndarray) -> float:
        """z of lanelet ``i`` where it covers ``p``: both bounds' z at the projection of ``p``."""
        q = Point(*p)
        return float(np.mean([b.interpolate(LineString(np.asarray(b.coords)[:, :2]).project(q)).z
                              for b in bounds[i]]))

    tree = STRtree([L[i].polygon for i in ids])
    pairs: list[tuple[int, int]] = []        # (upper, lower)
    for i in ids:
        for j in tree.query(L[i].polygon, predicate="intersects"):
            o = ids[int(j)]
            if o <= i:
                continue
            inter = L[i].polygon.intersection(L[o].polygon)
            if inter.area <= 1.0:
                continue
            c = np.asarray(inter.representative_point().coords[0][:2])
            dz = z_near(i, c) - z_near(o, c)
            if dz > LEVEL_DZ:
                pairs.append((i, o))
            elif dz < -LEVEL_DZ:
                pairs.append((o, i))
    if not pairs:
        return level
    from scipy.spatial import cKDTree
    allv = np.vstack([np.vstack([L[i].left, L[i].right]) for i in ids])
    vtree = cKDTree(allv[:, :2])
    for i in {x for p in pairs for x in p}:
        v = np.vstack([L[i].left, L[i].right])
        c = v[:, :2].mean(axis=0)
        around = allv[vtree.query_ball_point(c, r=60.0), 2]
        dz = float(np.median(v[:, 2]) - np.median(around))
        level[i] = 1 if dz > LEVEL_DZ else (-1 if dz < -LEVEL_DZ else 0)
    # a pair the local reference could not split: the upper one is the deck
    for up, lo in pairs:
        if level[up] <= level[lo]:
            level[up] = level[lo] + 1
    return level


def _junction_ahead(roads: dict[str, Road], rid: str) -> Optional[str]:
    """The junction ``rid`` leads into, following single road links up to ``TL_SEARCH_M``."""
    dist, seen = 0.0, set()
    while rid in roads and rid not in seen:
        seen.add(rid)
        r = roads[rid]
        if r.successor is None:
            return None
        if r.successor.element == "junction":
            return r.successor.id
        dist += r.length
        if dist > TL_SEARCH_M:
            return None
        rid = r.successor.id
    return None


def _project(line: LineString, p: Point) -> tuple[float, float]:
    """(s, signed t) of ``p`` against ``line`` (t > 0 left)."""
    s = float(line.project(p))
    q = np.asarray(line.interpolate(s).coords[0][:2])
    q2 = np.asarray(line.interpolate(min(line.length, s + 0.5)).coords[0][:2])
    q1 = np.asarray(line.interpolate(max(0.0, s - 0.5)).coords[0][:2])
    d = q2 - q1
    v = np.asarray(p.coords[0][:2]) - q
    t = float(np.hypot(*v)) * (1.0 if d[0] * v[1] - d[1] * v[0] >= 0 else -1.0)
    return s, t


def _heading_at(line: LineString, s: float) -> float:
    a = np.asarray(line.interpolate(max(0.0, s - 0.5)).coords[0][:2])
    b = np.asarray(line.interpolate(min(line.length, s + 0.5)).coords[0][:2])
    return math.atan2(b[1] - a[1], b[0] - a[0])


# ------------------------------------------------------------------------------ conversion

@dataclass
class Lanelet2Conversion:
    model: TwinModel
    drivable: dict[int, Polygon]  # level -> union of the vehicle and shoulder lanelets on it
    ref_lines: dict[str, LineString]  # road id -> 3D reference line with the lanelet elevations
    markings: list[Marking]      # painted bounds and stop lines
    stats: dict[str, Any]
    ground_xyz: np.ndarray = None     # bound vertices of the ground-level lanelets (the terrain)


def lanelet2_to_model(lm: Lanelet2Map, name: str) -> Lanelet2Conversion:
    P = profiles.get()
    model = TwinModel(name=name, origin_lat=lm.frame.origin_lat, origin_lon=lm.frame.origin_lon,
                      bbox_wgs84=tuple(lm.bbox))
    stats: dict[str, Any] = {}
    L = _lanelets(lm, VEHICLE_SUBTYPES)
    _link(L)
    stats["vehicle_lanelets"] = len(L)
    level = _levels(L)
    stats["levels"] = sorted(set(level.values()))

    # -- junction lanelets ----------------------------------------------------------------------
    areas = [shapely.make_valid(Polygon(lm.coords(wid)[:, :2])) for wid, (ids, t) in lm.ways.items()
             if t.get("type") == "intersection_area" and len(ids) >= 3]
    areas += [shapely.make_valid(Polygon(lm.coords(ref)[:, :2]))
              for rid, (m, t) in lm.relations.items() if t.get("type") == "intersection_area"
              for typ, ref, role in m if typ == "way" and ref in lm.ways and len(lm.ways[ref][0]) >= 3]
    member: set[int] = set()
    for i, ll in L.items():
        if "turn_direction" in ll.tags:
            member.add(i)
        elif any(a.contains(ll.polygon.representative_point()) for a in areas):
            member.add(i)
        elif any(len(L[p].next) > 1 for p in ll.prev) or any(len(L[n].prev) > 1 for n in ll.next):
            member.add(i)            # a branch of a split or a merge
    # a junction lanelet needs something to come from: demote those at the start of the map
    changed = True
    while changed:
        changed = False
        for i in list(member):
            if not L[i].prev:
                member.discard(i)
                changed = True
    # a short plain lanelet between junction lanelets (< SLIVER_M) joins them: two junctions a
    # few metres apart are one intersection
    for i, ll in L.items():
        if (i not in member and ll.prev and ll.next and ll.length < SLIVER_M
                and all(p in member for p in ll.prev) and all(n in member for n in ll.next)):
            member.add(i)
    # clusters: chained, sharing an entry/exit, or overlapping junction lanelets
    parent = {i: i for i in member}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in member:
        for n in L[i].next + L[i].prev:
            if n in member:
                union(i, n)
        for p in L[i].prev:
            for sib in L[p].next:
                if sib in member:
                    union(i, sib)
        for n in L[i].next:
            for sib in L[n].prev:
                if sib in member:
                    union(i, sib)
    mlist = sorted(member)
    if mlist:
        tree = STRtree([L[i].polygon for i in mlist])
        for k, i in enumerate(mlist):
            for j in tree.query(L[i].polygon, predicate="intersects"):
                other = mlist[int(j)]
                if other != i and L[i].polygon.intersection(L[other].polygon).area > 0.5:
                    union(i, other)
    clusters: dict[int, list[int]] = {}
    for i in mlist:
        clusters.setdefault(find(i), []).append(i)
    jid_of: dict[int, str] = {}
    for k, root in enumerate(sorted(clusters)):
        for i in clusters[root]:
            jid_of[i] = f"j{k + 1}"
    stats["junction_lanelets"] = len(member)
    stats["junctions"] = len(clusters)

    # -- plain roads ------------------------------------------------------------------------------
    sidewalk_w = P.lane.fallback.sidewalk or P.sidewalk.min_width
    roads: dict[str, Road] = {}
    road_of: dict[int, str] = {}

    def speed(ll: _Lanelet) -> Optional[float]:
        try:
            return float(ll.tags["speed_limit"]) / 3.6
        except (KeyError, ValueError):
            return None

    def bound_type(way: int) -> str:
        return lm.ways[way][1].get("type", "")

    SH = _lanelets(lm, SHOULDER_SUBTYPES)
    use: dict[int, int] = {}
    for ll in list(L.values()) + list(SH.values()):
        for way in {ll.left_way, ll.right_way}:
            use[way] = use.get(way, 0) + 1

    def edge(way: int) -> bool:
        """A carriageway edge: a kerb, or a bound no other vehicle/shoulder lanelet shares."""
        return bound_type(way) == "road_border" or use.get(way, 0) == 1

    for i, ll in L.items():
        if i in member:
            continue
        widths = _widths(ll.left, ll.right)
        w = float(np.median([x for _s, x in widths]))
        lanes = [Lane(id=-1, type="driving", width=w, direction="forward", speed_limit=speed(ll),
                      tags={"reviewed_widths": widths, "lanelet_id": i})]
        if edge(ll.right_way) and level[i] == 0:
            lanes.append(Lane(id=-2, type="sidewalk", width=sidewalk_w, direction="forward"))
        if edge(ll.left_way) and level[i] == 0:
            lanes.append(Lane(id=1, type="sidewalk", width=sidewalk_w, direction="backward"))
        rid = f"l{i}"
        tags = {"lanelet_id": i, "source": "lanelet2"}
        if level[i]:
            tags["layer"] = str(level[i])
        roads[rid] = Road(id=rid, reference_line=LineString(ll.left), lanes=lanes,
                          name=ll.tags.get("name", ""), highway="tertiary", tags=tags)
        road_of[i] = rid

    # road-to-road links between plain lanelets
    for i, rid in road_of.items():
        ll = L[i]
        nxt = [n for n in ll.next if n in road_of]
        if len(ll.next) == 1 and nxt:
            roads[rid].successor = RoadLink("road", road_of[nxt[0]], "start")
            roads[road_of[nxt[0]]].predecessor = RoadLink("road", rid, "end")

    # -- junctions and connecting roads ---------------------------------------------------------
    junctions: dict[str, Junction] = {}
    for root, ids in clusters.items():
        jid = jid_of[ids[0]]
        junctions[jid] = Junction(id=jid, name=jid, tags={"lanelet_ids": sorted(ids)})
    n_paths = 0
    truncated = 0
    plain_roads = dict(road_of)
    for i, rid in plain_roads.items():
        ll = L[i]
        entries = [n for n in ll.next if n in member]
        if not entries:
            continue
        jids = {jid_of[n] for n in entries}
        for jid in sorted(jids):
            roads[rid].successor = RoadLink("junction", jid)
            paths: list[tuple[list[int], Optional[int]]] = []

            def walk(path: list[int]) -> None:
                if len(paths) >= MAX_PATHS_PER_ENTRY:
                    return
                last = L[path[-1]]
                exits = [n for n in last.next if n in plain_roads]
                inner = [n for n in last.next if n in member and jid_of[n] == jid and n not in path]
                for o in exits:
                    paths.append((list(path), o))
                for m in inner:
                    walk(path + [m])
                if not exits and not inner:
                    paths.append((list(path), None))

            for e in entries:
                if jid_of[e] == jid:
                    walk([e])
            if len(paths) >= MAX_PATHS_PER_ENTRY:
                truncated += 1
            for k, (path, out) in enumerate(paths):
                left = _concat([L[m].left for m in path])
                widths, s0 = [], 0.0
                for m in path:
                    ws = _widths(L[m].left, L[m].right, s0)
                    widths += ws if not widths else ws[1:]
                    s0 += L[m].length
                w = float(np.median([x for _s, x in widths]))
                sp = [v for v in (speed(L[m]) for m in path) if v]
                cid = f"c{i}_{k}"
                croad = Road(id=cid, reference_line=LineString(left),
                             lanes=[Lane(id=-1, type="driving", width=w, direction="forward",
                                         speed_limit=min(sp) if sp else None,
                                         tags={"reviewed_widths": widths, "lanelet_ids": path})],
                             highway="tertiary", junction_id=jid,
                             predecessor=RoadLink("road", rid, "end"),
                             tags={"lanelet_ids": path, "source": "lanelet2",
                                   "turn_direction": L[path[0]].tags.get("turn_direction", "")})
                lv = max(level[m] for m in path)
                if lv:
                    croad.tags["layer"] = str(lv)
                if out is not None:
                    croad.successor = RoadLink("road", plain_roads[out], "start")
                    roads[plain_roads[out]].predecessor = RoadLink("junction", jid)
                roads[cid] = croad
                junctions[jid].connections.append(Connection(
                    id=f"{rid}->{cid}", incoming_road=rid, connecting_road=cid,
                    contact_point="start", lane_links=[LaneLink(-1, -1)]))
                n_paths += 1
                for m in path:
                    road_of.setdefault(m, cid)   # first path through a junction lanelet
    stats["connecting_roads"] = n_paths
    stats["entries_truncated"] = truncated
    model.roads = list(roads.values())
    model.junctions = [j for j in junctions.values() if j.connections]

    # -- signals ----------------------------------------------------------------------------------
    plain = {i: rid for i, rid in road_of.items() if roads[rid].junction_id is None}
    signals: list[Signal] = []
    tl_by_junction: dict[str, list[Signal]] = {}
    skipped_tl = 0

    def stop_line_of(members: list[tuple[str, int, str]]) -> Optional[LineString]:
        for t, ref, role in members:
            if t == "way" and role == "ref_line" and ref in lm.ways:
                return LineString(lm.coords(ref)[:, :2])
        return None

    placed: set[tuple[int, str]] = set()
    # a light referenced by junction lanelets governs the roads entering them
    refs: list[tuple[int, str]] = [(i, rid) for i, rid in plain.items()]
    for road in roads.values():
        if road.junction_id is None or road.predecessor is None:
            continue
        for m in road.tags.get("lanelet_ids", []):
            refs.append((m, road.predecessor.id))
    seen_ref: set[tuple[int, str]] = set()
    for i, rid in refs:
        if (i, rid) in seen_ref:
            continue
        seen_ref.add((i, rid))
        ll, road = L[i], roads[rid]
        ref = LineString(np.asarray(road.reference_line.coords)[:, :2])
        w = road.lanes[0].width
        for reg in ll.regelems:
            if reg not in lm.relations:
                continue
            members, tags = lm.relations[reg]
            sub = tags.get("subtype")
            refers = [ref_ for t, ref_, role in members if t == "way" and role == "refers" and ref_ in lm.ways]
            sl = stop_line_of(members)
            if sub == "traffic_light":
                if (reg, rid) in placed:
                    continue
                jid = _junction_ahead(roads, rid)
                if jid is None:
                    skipped_tl += 1
                    continue
                s = ref.length if sl is None else _project(ref, sl.interpolate(0.5, normalized=True))[0]
                s = min(max(s, 0.0), ref.length)
                h = _heading_at(ref, s)
                n = np.array([-math.sin(h), math.cos(h)])
                p = np.asarray(ref.interpolate(s).coords[0][:2]) - n * (w + 0.5)
                lights = [lm.coords(x).mean(axis=0).tolist() for x in refers
                          if lm.ways[x][1].get("type") == "traffic_light"]
                sig = Signal(id=f"tl{reg}_{i}", kind="traffic_light", road_id=rid, s=float(s),
                             t=-(w + 0.5), position=Point(*p), heading=h, orientation="+",
                             validities=[(-1, -1)],
                             tags={"junction_id": jid, "rig_anchor": f"tl{reg}_{i}",
                                   "controlled_lane": -1, "lanelet2_regulatory_element": reg,
                                   "lanelet2_lights": lights})
                signals.append(sig)
                tl_by_junction.setdefault(jid, []).append(sig)
                placed.add((reg, rid))
            elif sub == "traffic_sign" and (tags.get("sign_type") == "stop_sign" or any(
                    lm.ways[x][1].get("subtype") == "stop_sign" for x in refers)):
                s = ref.length if sl is None else _project(ref, sl.interpolate(0.5, normalized=True))[0]
                s = min(max(s, 0.0), ref.length)
                h = _heading_at(ref, s)
                n = np.array([-math.sin(h), math.cos(h)])
                p = np.asarray(ref.interpolate(s).coords[0][:2]) - n * (w + 0.5)
                signals.append(Signal(id=f"stop{reg}_{i}", kind="stop", road_id=rid, s=float(s),
                                      t=-(w + 0.5), position=Point(*p), heading=h, orientation="+",
                                      validities=[(-1, -1)], tags={"lanelet2_regulatory_element": reg}))
    stats["traffic_light_regelems_without_junction"] = skipped_tl

    # stages per junction (Lanelet2 carries no phases)
    from ..lanegraph import _stage_plan
    controllers: list[Controller] = []
    for jid, sigs in sorted(tl_by_junction.items()):
        stages = _stage_plan([s.heading for s in sigs])
        for k, st in enumerate(stages):
            cid = f"{jid}_stage{k}"
            for idx in st:
                sigs[idx].controller_id = cid
            controllers.append(Controller(id=cid, junction_id=jid,
                                          signal_ids=[sigs[idx].id for idx in st], sequence=k))
    model.controllers = controllers

    # crosswalks: one signal per road lanelet the crosswalk crosses; the first carries the outline
    CW = _lanelets(lm, CROSSWALK_SUBTYPES)
    road_ids = list(road_of)
    rtree = STRtree([L[i].polygon for i in road_ids]) if road_ids else None
    n_cw = 0
    for c, cw in CW.items():
        if rtree is None or cw.polygon.is_empty:
            continue
        hits = []
        for k in rtree.query(cw.polygon, predicate="intersects"):
            i = road_ids[int(k)]
            inter = L[i].polygon.intersection(cw.polygon)
            if inter.area > 0.2:
                hits.append((i in plain, inter.area, i, inter))
        if not hits:
            continue
        hits.sort(key=lambda h: (not h[0], -h[1]))
        width = float(np.mean([LineString(cw.left[:, :2]).distance(Point(p)) for p in cw.right[:, :2]]))
        for k, (_plain, _a, i, inter) in enumerate(hits):
            rid = road_of[i]
            road = roads[rid]
            ref = LineString(np.asarray(road.reference_line.coords)[:, :2])
            s, _t = _project(ref, inter.centroid)
            tags = {"width": max(width, 1.0), "lanelet_id": c}
            if k == 0:
                tags["polygon_wkt"] = cw.polygon.wkt
            else:
                tags["crossing_surface"] = "none"
            signals.append(Signal(id=f"cw{c}_{k}", kind="crosswalk", road_id=rid, s=float(s), t=0.0,
                                  position=inter.centroid, heading=_heading_at(ref, s), tags=tags))
        n_cw += 1
    stats["crosswalks"] = n_cw
    model.signals = signals
    stats["traffic_lights"] = sum(1 for s in signals if s.kind == "traffic_light")
    stats["stop_signs"] = sum(1 for s in signals if s.kind == "stop")
    stats["controllers"] = len(controllers)

    # -- drivable surface and markings -------------------------------------------------------------
    by_level: dict[int, list] = {}
    for i, ll in L.items():
        by_level.setdefault(level[i], []).append(ll.polygon)
    for ll in SH.values():
        by_level.setdefault(0, []).append(ll.polygon)
    drivable = {lv: _union(polys) for lv, polys in by_level.items()}
    markings: list[Marking] = []
    seen: set[int] = set()
    for ll in L.values():
        for way in (ll.left_way, ll.right_way):
            if way in seen:
                continue
            seen.add(way)
            t = lm.ways[way][1]
            if t.get("type") not in PAINTED:
                continue
            sub = t.get("subtype", "solid")
            kind = "broken" if sub.startswith("dashed") else "solid"
            color = "yellow" if t.get("color") == "yellow" else "white"
            markings.append(Marking(kind=kind, color=color, width=PAINTED[t["type"]],
                                    geometry=LineString(lm.coords(way)[:, :2])))
    for wid, (ids, t) in lm.ways.items():
        if t.get("type") == "stop_line" and len(ids) >= 2:
            markings.append(Marking(kind="solid", color="white", width=STOP_LINE_WIDTH,
                                    geometry=LineString(lm.coords(wid)[:, :2])))
    stats["markings"] = len(markings)

    model.metadata["source"] = "lanelet2"
    model.metadata["lanelet2"] = {"stats": stats}
    log.info("lanelet2: %s", stats)
    return Lanelet2Conversion(model=model, drivable=drivable, markings=markings, stats=stats,
                              ref_lines={r.id: r.reference_line for r in model.roads},
                              ground_xyz=np.vstack([np.vstack([ll.left, ll.right]) for i, ll in L.items()
                                                    if level[i] == 0]) if L else np.zeros((0, 3)))


def elevation_from_lanelet2(pts: np.ndarray, margin: float = 200.0, res: float = 2.0) -> Optional[Elevation]:
    """A regular elevation grid interpolated from lanelet vertices ``(N, 3)`` (nearest outside
    their convex hull): the ground-level lanelets of :class:`Lanelet2Conversion`, so a deck does
    not lift the terrain. None when they are flat."""
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) < 3 or np.ptp(pts[:, 2]) < 1e-6:
        return None
    from scipy.interpolate import griddata
    x0, y0 = pts[:, 0].min() - margin, pts[:, 1].min() - margin
    x1, y1 = pts[:, 0].max() + margin, pts[:, 1].max() + margin
    nx, ny = int(math.ceil((x1 - x0) / res)) + 1, int(math.ceil((y1 - y0) / res)) + 1
    gx, gy = np.meshgrid(x0 + np.arange(nx) * res, y0 + np.arange(ny) * res)
    # thin the samples: the bound vertices are dense along the roads
    key = np.round(pts[:, :2] / res).astype(np.int64)
    _, idx = np.unique(key, axis=0, return_index=True)
    s = pts[idx]
    z = griddata(s[:, :2], s[:, 2], (gx, gy), method="linear")
    hole = np.isnan(z)
    if hole.any():
        z[hole] = griddata(s[:, :2], s[:, 2], (gx[hole], gy[hole]), method="nearest")
    return Elevation(z, x0, y0, res, res, source="lanelet2")
