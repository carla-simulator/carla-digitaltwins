"""Left-hand traffic by reflection.

The lane graph is written for right-hand traffic: forward lanes on the right of the reference
line (negative OpenDRIVE ids), the median on the left, signals on the right kerb, protected
turns across the oncoming traffic to the left. A left-hand traffic city is the mirror image of
a right-hand one, so ``profiles.StreetProfile.drives_on == "left"`` is built as

    reflect the OSM data about the frame's central meridian   (``mirror_osm``)
    build the lane graph for right-hand traffic                (``right_handed``)
    reflect the model back                                     (``mirror_model``)

The transverse Mercator of ``frame.LocalFrame`` is symmetric about its central meridian, so a
longitude reflected about ``origin_lon`` lands at exactly ``-x``. Reflecting the model negates
every lane id and keeps every lane's direction along ``s``: the forward lanes end up on the left
of the reference line with positive ids, which is what OpenDRIVE's ``rule="LHT"`` means (the
exporter writes it). Lateral offsets (signal ``t``, ``hOffset``) change sign, headings ``h``
become ``pi - h``, and "left" and "right" swap wherever OSM or the lane graph name a side or a
turn.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Optional

from shapely import wkt as _wkt
from shapely.affinity import scale
from shapely.geometry.base import BaseGeometry

from .ingest.osm import OsmData
from .lanegraph import _wrap
from .model import TwinModel
from .profiles import swap_side

# keys whose *values* name a side or a turn (``sidewalk=left``, ``turn:lanes=left|through``,
# ``restriction=no_left_turn``, ``change:lanes=not_left|yes``, the lane graph's ``median_side``)
_SIDED_VALUE_PREFIXES = ("turn", "change", "restriction", "sidewalk", "cycleway", "parking",
                         "side", "driving_side", "median_side")


def mirror_osm_tags(tags: dict[str, Any]) -> dict[str, Any]:
    """Tags as the mirror image reads them: ``sidewalk:left`` is ``sidewalk:right``, a
    ``*:lanes`` list (left to right across the carriageway) runs the other way, and sided
    values swap. Its own inverse. Values that are not strings pass through."""
    out: dict[str, Any] = {}
    for k, v in tags.items():
        key = swap_side(k)
        if isinstance(v, str):
            parts = key.split(":")
            if "lanes" in parts[1:] and "|" in v:
                v = "|".join(reversed(v.split("|")))
            if parts[0] in _SIDED_VALUE_PREFIXES:
                v = swap_side(v)
        out[key] = v
    return out


def mirror_bbox(bbox: tuple[float, float, float, float], lon0: float) -> tuple[float, float, float, float]:
    s, w, n, e = bbox
    return (s, 2.0 * lon0 - e, n, 2.0 * lon0 - w)


def mirror_osm(osm: OsmData, lon0: float) -> OsmData:
    """The OSM data reflected about the meridian ``lon0``."""
    nodes = {nid: replace(n, lon=2.0 * lon0 - n.lon, tags=mirror_osm_tags(n.tags))
             for nid, n in osm.nodes.items()}
    ways = [replace(w, tags=mirror_osm_tags(w.tags)) for w in osm.ways]
    rels = [replace(r, tags=mirror_osm_tags(r.tags)) for r in osm.relations]
    bbox = mirror_bbox(osm.bbox_swne, lon0) if osm.bbox_swne else None
    return OsmData(nodes=nodes, ways=ways, relations=rels, bbox_swne=bbox)


# --------------------------------------------------------------------------- the model

def _geom(g: Optional[BaseGeometry]) -> Optional[BaseGeometry]:
    return None if g is None else scale(g, xfact=-1.0, yfact=1.0, origin=(0.0, 0.0))


def _tags(tags: dict[str, Any]) -> dict[str, Any]:
    """Tags of a model element reflected back: the OSM tags it copied (``mirror_osm_tags``),
    lane ids (``from_lane`` / ``to_lane``), turn lists, and geometry -- shapely objects,
    ``*_wkt`` strings and ``[x, y]`` points (``centre``, ``node_xy``)."""
    out = {}
    for k, v in mirror_osm_tags(tags).items():
        if isinstance(v, BaseGeometry):
            v = _geom(v)
        elif k.endswith("_wkt") and isinstance(v, str):
            v = _geom(_wkt.loads(v)).wkt
        elif k in ("centre", "node_xy") and isinstance(v, (list, tuple)) and len(v) >= 2:
            v = type(v)([-v[0], *v[1:]])
        elif k in ("from_lane", "to_lane") and isinstance(v, int):
            v = -v
        elif k == "turn" and isinstance(v, (list, tuple)):
            v = type(v)(swap_side(t) for t in v)
        out[k] = v
    return out


def _metadata(v: Any, key: str = "") -> Any:
    if isinstance(v, dict):
        return {k: _metadata(x, k) for k, x in v.items()}
    if isinstance(key, str) and key.endswith("_wkt"):
        if isinstance(v, str):
            return _geom(_wkt.loads(v)).wkt
        if isinstance(v, list):
            return [_geom(_wkt.loads(x)).wkt if isinstance(x, str) else x for x in v]
    return v


def mirror_model(model: TwinModel) -> TwinModel:
    """Reflect a lane-graph model about x = 0, in place (see the module doc)."""
    for r in model.roads:
        r.reference_line = _geom(r.reference_line)
        for l in r.lanes:
            l.id = -l.id
            l.tags = _tags(l.tags)
            if l.marking is not None and l.marking.geometry is not None:
                l.marking.geometry = _geom(l.marking.geometry)
        r.lanes.sort(key=lambda l: -l.id)
        r.tags = _tags(r.tags)
    for j in model.junctions:
        j.polygon = _geom(j.polygon)
        j.tags = _tags(j.tags)
        for c in j.connections:
            for ll in c.lane_links:
                ll.from_lane, ll.to_lane = -ll.from_lane, -ll.to_lane
    for s in model.signals:
        s.t = -s.t
        s.position = _geom(s.position)
        s.heading = _wrap(math.pi - s.heading)
        s.h_offset = -s.h_offset
        s.validities = [(-b, -a) for a, b in s.validities]
        s.tags = _tags(s.tags)
    for su in model.surfaces:
        su.geometry = _geom(su.geometry)
        su.tags = _tags(su.tags)
    for c in model.curbs:
        c.geometry = _geom(c.geometry)
    for m in model.markings:
        m.geometry = _geom(m.geometry)
    for b in model.buildings:
        b.footprint = _geom(b.footprint)
        b.tags = _tags(b.tags)
    for o in model.objects:
        o.position = _geom(o.position)
        o.tags = _tags(o.tags)
    model.metadata = _metadata(model.metadata)
    return model
