"""Physical roadside supports, independent of OpenDRIVE control/stop positions.

All geometry is final baked model-space ENU metres. A missing safe location is a
reported failure, never permission to leave a support on a carriageway.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union

from .lanegraph import _heading_along
from .model import road_osm_layer


@dataclass(frozen=True)
class PlacementRules:
    road_clearance_m: float = 0.45
    edge_clearance_m: float = 0.20
    building_clearance_m: float = 0.50
    pole_spacing_m: float = 0.60
    max_move_m: float = 12.0
    max_along_m: float = 8.0


class PolePlacer:
    """Deterministic nearest safe position on the original side and vertical layer.

    Keep the pole within a bounded neighbourhood of its control point. Carriageway
    includes parking and crossings; raised surfaces must actually exist. Ground
    slabs are not a substitute for a sidewalk. Positions reserve space so successive
    poles do not collapse onto the same corner. Clearances are design defaults,
    not a claim of regional regulatory compliance.
    """
    def __init__(self, model, rules=PlacementRules()):
        self.model, self.rules = model, rules
        self.occupied = {}
        self.layers = {}
        for layer in {road_osm_layer(r) for r in model.roads}:
            surfaces = [s for s in model.surfaces if int(s.tags.get('layer') or 0) == layer]
            support = [s for s in surfaces if s.kind in {'sidewalk', 'island', 'median', 'verge'}]
            roads = unary_union([s.geometry for s in surfaces
                                 if s.kind in {'drivable', 'parking', 'crossing', 'biking'}])
            buildings = unary_union([b.footprint for b in model.buildings])
            safe = unary_union([s.geometry for s in support]).buffer(-rules.edge_clearance_m)
            safe = safe.difference(roads.buffer(rules.road_clearance_m))
            safe = safe.difference(buildings.buffer(rules.building_clearance_m))
            self.layers[layer] = safe, support

    def place(self, signal_id, anchor=None):
        sig = next(s for s in self.model.signals if s.id == signal_id)
        road = self.model.road(sig.road_id)
        layer = road_osm_layer(road)
        safe, supports = self.layers[layer]
        p = anchor if anchor is not None else Point(sig.position.x, sig.position.y)
        h = _heading_along(road.reference_line, sig.s)
        tangent = (math.cos(h), math.sin(h))
        side = 1 if sig.t >= 0 else -1
        normal = (-tangent[1] * side, tangent[0] * side)
        ref = road.reference_line.interpolate(sig.s)
        # Restrict to the original side of the road, and avoid moving along an
        # approach to an unrelated junction. Allow inward moves to actual sidewalks.
        def xy(along, across):
            return (ref.x + tangent[0] * along + normal[0] * across,
                    ref.y + tangent[1] * along + normal[1] * across)
        r = self.rules
        bound = max(abs(sig.t) + r.max_move_m + 1, 1)
        side_box = Polygon([xy(-r.max_along_m, 0), xy(r.max_along_m, 0),
                            xy(r.max_along_m, bound), xy(-r.max_along_m, bound)])
        candidates = safe.intersection(side_box).intersection(p.buffer(r.max_move_m))
        for other in self.occupied.get(layer, []):
            candidates = candidates.difference(other.buffer(r.pole_spacing_m))
        if candidates.is_empty:
            return {'status': 'unresolved', 'reason': 'no safe support on the same road side/layer within search limits'}
        q = p if candidates.covers(p) else nearest_points(p, candidates)[1]
        surface = min(supports, key=lambda s: s.geometry.distance(q))
        z = float(self.model.sample_z(q.x, q.y, layer=layer)) + surface.z_offset
        self.occupied.setdefault(layer, []).append(q)
        return {'status': 'ok', 'x': q.x, 'y': -q.y, 'z': z,
                'move_m': p.distance(q), 'surface_id': surface.id, 'layer': layer,
                'road_clearance_m': r.road_clearance_m}
