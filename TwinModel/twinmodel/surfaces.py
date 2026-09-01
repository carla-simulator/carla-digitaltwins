"""Lane graph (+ optional refined mask) -> Surface polygons, junction polygons, curbs, markings.

Implements DESIGN.md §Surfaces. ``build_surfaces`` is idempotent: it clears the surfaces, curbs,
free-standing markings and (recomputed) junction polygons of the model before rebuilding them.

Geometry conventions: model space, metres. A road's reference line has lanes with positive ids on
its *left* (looking along the line) and negative ids on its right. The carriageway on one side is
the band between the reference line and the outer edge of the outermost carriageway-type lane
(driving/parking/biking/shoulder); sidewalk lanes are bands at their own cumulative offset.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import shapely
from shapely.geometry import (GeometryCollection, LineString, MultiLineString, MultiPolygon,
                              Point, Polygon)
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union

from .model import CurbLine, Junction, Lane, Marking, Road, Signal, Surface, TwinModel

log = logging.getLogger("twinmodel.surfaces")

CARRIAGEWAY_TYPES: tuple[str, ...] = ("driving", "parking", "biking", "shoulder")
RAISED_TYPES: tuple[str, ...] = ("sidewalk", "median")
SIDEWALK_Z = 0.15
CURB_HEIGHT = 0.15
CROSSING_Z = 0.003
DEFAULT_CROSSING_WIDTH = 4.0
SIMPLIFY_TOL = 0.05
GRID = 0.001            # precision grid for all overlays (mm) -> robust shared boundaries
MITRE_LIMIT = 2.0
MIN_SURFACE_AREA = 0.5  # m^2, drop slivers below this
MIN_ISLAND_AREA = 2.0   # m^2, smaller drivable holes are filled instead of becoming islands
MAX_ISLAND_AREA = 400.0  # m^2, larger holes are city blocks (kept as holes, no island surface)
EDGE_MARKING_INSET = 0.10  # outermost edge lines are drawn this far inside the carriageway


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


def _sidewalk_widths(road: Road) -> tuple[float, float]:
    """Total sidewalk width per side (left, right)."""
    wl = sum(l.width for l in road.lanes if l.id > 0 and l.type == "sidewalk")
    wr = sum(l.width for l in road.lanes if l.id < 0 and l.type == "sidewalk")
    return wl, wr


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


def junction_polygon(model: TwinModel, j: Junction,
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


# --------------------------------------------------------------------------- markings

def _default_marking(bands: list[_LaneBand], b: _LaneBand) -> Optional[Marking]:
    """Default marking on the outer edge of lane ``b`` when the lane graph gave none."""
    if b.lane.type != "driving":
        return None
    outer_neighbours = [o for o in bands if o.left == b.left and abs(o.inner - b.outer) < 1e-6]
    if outer_neighbours:
        o = outer_neighbours[0].lane
        if o.type == "driving":
            kind = "broken" if o.direction == b.lane.direction else "solid"
            return Marking(kind, "white")
        if o.type in ("parking", "biking", "shoulder"):
            return Marking("solid", "white")
        return Marking("solid", "white")  # against sidewalk/median/none: edge line
    return Marking("solid", "white")  # road edge


def _default_center_marking(road: Road) -> Optional[Marking]:
    left = [l for l in road.lanes_left() if l.type in CARRIAGEWAY_TYPES]
    right = [l for l in road.lanes_right() if l.type in CARRIAGEWAY_TYPES]
    if not left and not right:
        return None
    if not left or not right:
        return Marking("solid", "white")  # reference line is the carriageway edge
    li, ri = left[0], right[0]
    if li.type == "driving" and ri.type == "driving":
        return Marking("broken" if li.direction == ri.direction else "solid", "white")
    return Marking("solid", "white")


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
    width = float(sig.tags.get("crossing:width", sig.tags.get("width", DEFAULT_CROSSING_WIDTH)) or
                  DEFAULT_CROSSING_WIDTH)
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

    # junction polygons ----------------------------------------------------------------------
    junction_polys: dict[str, Polygon] = {}
    for j in model.junctions:
        poly = junction_polygon(model, j, carriageways, cover=junction_cover)
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
    buildings = _clean(unary_union([b.footprint for b in model.buildings])) if model.buildings else Polygon()
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
    # 3. sidewalks / medians -----------------------------------------------------------------
    raised_parts: dict[str, list[tuple[BaseGeometry, str]]] = {"sidewalk": [], "median": []}
    for r in model.roads:
        if r.id in connecting_ids:
            continue
        ref = _ref2d(r)
        for b in lane_bands(r):
            if b.lane.type not in RAISED_TYPES:
                continue
            band = _side_band(ref, b.inner, b.outer, b.left)
            if not band.is_empty:
                raised_parts[b.lane.type].append((band, r.id))

    # sidewalks wrapping around junction polygons
    for j in model.junctions:
        poly = junction_polys.get(j.id)
        if poly is None:
            continue
        _, incoming = _junction_roads(model, j)
        widths = [w for r, _ in incoming for w in _sidewalk_widths(r) if w > 0]
        if not widths:
            continue
        w = max(widths)
        wrap = _clean(poly.buffer(w, join_style="mitre", mitre_limit=MITRE_LIMIT))
        raised_parts["sidewalk"].append((wrap, f"junction:{j.id}"))

    raised_union_parts: list[BaseGeometry] = []
    for kind in ("sidewalk", "median"):
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
        for k, part in enumerate(_parts(geom)):
            rids = [rid for g, rid in items if not rid.startswith("junction:") and g.intersects(part)]
            jids = [rid.split(":", 1)[1] for g, rid in items if rid.startswith("junction:") and g.intersects(part)]
            model.surfaces.append(Surface(
                id=f"{kind}_{k}", kind=kind, geometry=part, z_offset=SIDEWALK_Z, source="osm_tags",
                road_ids=rids, junction_id=jids[0] if len(jids) == 1 else None,
                tags={"junction_ids": jids} if len(jids) > 1 else {}))
            raised_union_parts.append(part)

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
                                      z_offset=SIDEWALK_Z, source=source))
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
                                      z_offset=CROSSING_Z, source="osm_tags",
                                      road_ids=[sig.road_id], tags={"signal_id": sig.id}))
        k += 1

    # 5. curbs -------------------------------------------------------------------------------
    raised_all = _clean(unary_union(raised_union_parts + islands)) if (raised_union_parts or islands) else Polygon()
    for k, line in enumerate(curb_lines(drivable, raised_all)):
        mid = line.interpolate(0.5, normalized=True)
        high_kind = "sidewalk"
        for isl in islands:
            if isl.distance(mid) < 0.01:
                high_kind = "island"
                break
        model.curbs.append(CurbLine(id=f"curb_{k}", geometry=line, height=CURB_HEIGHT,
                                    low_side_kind="drivable", high_side_kind=high_kind))

    # 6. markings (never inside junctions) ---------------------------------------------------
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
        "drivable_area": float(drivable.area),
        "sidewalk_area": float(sum(s.geometry.area for s in model.surfaces_of("sidewalk"))),
        "island_count": len(islands),
        "curb_length": float(sum(c.geometry.length for c in model.curbs)),
        "marking_count": len(model.markings),
        "junctions_with_polygon": len(junction_polys),
        "drivable_source": source,
    })
    model.metadata.setdefault("surfaces", {}).update(stats)
    log.info("surfaces: drivable %.0f m2, sidewalk %.0f m2, %d islands, curbs %.0f m, %d markings",
             stats["drivable_area"], stats["sidewalk_area"], stats["island_count"],
             stats["curb_length"], stats["marking_count"])
    return model
