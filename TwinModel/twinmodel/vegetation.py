"""Deterministic, reviewable vegetation plans from final baked TwinModel surfaces.

Geometry/config zones use local ENU metres. Output transforms use CARLA metres.
Source tree coordinates are never snapped. All placements, including manual
positions, go through the same exclusion tests. PCG consumes only accepted points.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import shapely
from shapely.geometry import Point, Polygon, shape
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

_REPO_DEFAULTS = Path(__file__).resolve().parents[1] / 'vegetation/defaults.json'
_PACKAGED_DEFAULTS = Path(__file__).resolve().parent / 'data/vegetation/defaults.json'
DEFAULTS = _REPO_DEFAULTS if _REPO_DEFAULTS.exists() else _PACKAGED_DEFAULTS
SCHEMA = 'twin-vegetation-plan/1'


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse_layer(raw) -> int:
    if raw is None or raw == '':
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0


def _flatten_parts(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if hasattr(geom, 'geoms'):
        out = []
        for g in geom.geoms:
            out.extend(_flatten_parts(g))
        return out
    return [geom]


def _dist_lt(tree: STRtree | None, parts: list, point: Point, thresh: float) -> bool:
    if tree is None or thresh <= 0.0:
        return False
    hits = tree.query(point, predicate='dwithin', distance=thresh)
    if len(hits) == 0:
        return False
    return any(parts[int(idx)].distance(point) < thresh for idx in hits)


def stable_seed(seed, key):
    return int.from_bytes(hashlib.blake2s(f'{seed}:{key}'.encode('utf-8'), digest_size=4).digest(), 'big') & 0x7fffffff


def fraction(seed, key):
    return stable_seed(seed, key) / 2147483648.0


def configuration(overrides=None):
    c = json.loads(DEFAULTS.read_text(encoding='utf-8'))
    unknown = set(overrides or {}) - set(c)
    if unknown:
        raise ValueError(f'Unknown vegetation settings: {sorted(unknown)}')
    for k, v in (overrides or {}).items():
        if k == 'presets':
            if not isinstance(v, dict):
                raise ValueError('presets must be a mapping')
            c[k].update(copy.deepcopy(v))
        else:
            c[k] = copy.deepcopy(v)
    if not isinstance(c['seed'], int) or isinstance(c['seed'], bool):
        raise ValueError('seed must be an integer')
    for key in ('include_observed', 'street_rows'):
        if not isinstance(c[key], bool):
            raise ValueError(f'{key} must be boolean')
    for key in ('row_spacing_m', 'row_inset_m', 'pedestrian_width_m', 'max_candidates'):
        if not _is_num(c[key]) or c[key] <= 0:
            raise ValueError(f'{key} must be finite and positive')
    for key in ('road_clearance_m', 'crossing_clearance_m', 'pole_clearance_m', 'building_clearance_m'):
        if not _is_num(c[key]) or c[key] < 0:
            raise ValueError(f'{key} must be finite and nonnegative')
    if not _is_num(c['max_slope_degrees']) or not 0 < c['max_slope_degrees'] < 90:
        raise ValueError('max_slope_degrees must be between 0 and 90')
    for name, p in c['presets'].items():
        if not isinstance(p, dict) or p.get('kind') not in {'tree', 'shrub', 'grass'} or not p.get('meshes'):
            raise ValueError(f'Invalid preset {name}')
        if not all(isinstance(m, str) and m.startswith('/Game/Carla/Static/Vegetation/') for m in p['meshes']):
            raise ValueError(f'Expected CARLA vegetation mesh paths in {name}')
        if len(p.get('scale', ())) != 2 or not all(_is_num(v) for v in p['scale']) or not 0 < p['scale'][0] <= p['scale'][1]:
            raise ValueError(f'Invalid scale range in {name}')
        for key in ('spacing_m', 'pit_radius_m', 'crown_radius_m'):
            if not _is_num(p.get(key)) or p[key] <= 0:
                raise ValueError(f'Invalid {key} in {name}')
    if c['tree_preset'] not in c['presets']:
        raise ValueError('Unknown tree_preset')
    footing = c.get('sidewalk_footing')
    if footing is not None:
        if not isinstance(footing, dict) or footing.get('mesh') != '/Game/Carla/Static/Static/SM_TreeBase02':
            raise ValueError('Unsupported sidewalk footing asset')
        for key in ('radius_m', 'scale'):
            if not _is_num(footing.get(key)) or footing[key] <= 0:
                raise ValueError(f'Invalid footing {key}')
        if not _is_num(footing.get('soil_height_m')) or footing['soil_height_m'] < 0:
            raise ValueError('Invalid footing soil height')
        if not _is_num(footing.get('ground_offset_m')):
            raise ValueError('Invalid footing ground offset')
        if footing['scale'] > 3 or not -.04 <= footing['ground_offset_m'] <= -.005 or abs(footing['soil_height_m']-.14744362) > .0001:
            raise ValueError('Unsupported footing scale or soil offset')
    if not isinstance(c.get('exclusions'), list):
        raise ValueError('exclusions must be a list')
    for excl in c['exclusions']:
        eg = shape(excl)
        if eg.is_empty or not eg.is_valid or eg.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Invalid exclusion polygon')
    if not isinstance(c.get('overrides'), dict):
        raise ValueError('overrides must be a mapping')
    for okey, ov in c['overrides'].items():
        if not isinstance(ov, dict):
            raise ValueError(f'Invalid override for {okey}')
        if 'position' in ov and (len(ov['position']) != 2 or not all(_is_num(x) for x in ov['position'])):
            raise ValueError(f'Invalid override position for {okey}')
    seen = set()
    for z in c['zones']:
        if z['id'] in seen or z['preset'] not in c['presets']:
            raise ValueError('Zone IDs must be unique and presets must exist')
        seen.add(z['id'])
        g = shape(z['geometry'])
        if g.is_empty or not g.is_valid or g.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError(f'Invalid polygon in zone {z["id"]}')
        d = z.get('density', 1.0)
        if not _is_num(d) or not 0 <= d <= 1:
            raise ValueError('Zone density must be in [0,1]')
    return c


class Planner:
    def __init__(self, model, config=None, poles=()):
        self.model, self.config = model, configuration(config)
        self.records, self.occupied = [], {}
        self.layers = {}
        self.poles_by_layer: dict[int, list[tuple[float, float]]] = {}
        for p in poles:
            lyr = _parse_layer(p.get('layer', 0))
            self.poles_by_layer.setdefault(lyr, []).append((float(p['x']), -float(p['y'])))
        bldg_parts = [b.footprint for b in model.buildings if not b.footprint.is_empty]
        self.buildings = unary_union(bldg_parts) if bldg_parts else Polygon()
        shapely.prepare(self.buildings)
        self.bldg_parts = _flatten_parts(self.buildings)
        self.bldg_tree = STRtree(self.bldg_parts) if self.bldg_parts else None
        excl_parts = [shape(g) for g in self.config['exclusions']]
        self.exclusions = unary_union(excl_parts) if excl_parts else Polygon()
        shapely.prepare(self.exclusions)
        self.excl_parts = _flatten_parts(self.exclusions)
        self.excl_tree = STRtree(self.excl_parts) if self.excl_parts else None
        layers = {_parse_layer(s.tags.get('layer')) for s in model.surfaces}
        for layer in layers:
            ss = [s for s in model.surfaces if _parse_layer(s.tags.get('layer')) == layer]
            def union(kinds, _ss=ss):
                geoms = [s.geometry for s in _ss if s.kind in kinds and not s.geometry.is_empty]
                return unary_union(geoms) if geoms else Polygon()
            road = union({'drivable', 'parking', 'biking'})
            crossing = union({'crossing'})
            support = union({'sidewalk', 'island', 'median', 'verge', 'grass', 'ground'})
            walk = union({'sidewalk'})
            for g_u in (road, crossing, support, walk):
                shapely.prepare(g_u)
            road_parts = _flatten_parts(road)
            cross_parts = _flatten_parts(crossing)
            support_bparts = _flatten_parts(support.boundary) if not support.is_empty else []
            surf_geoms = [s.geometry for s in ss]
            for sg in surf_geoms:
                shapely.prepare(sg)
            self.layers[layer] = {
                'surfaces': ss,
                'surf_geoms': surf_geoms,
                'surf_tree': STRtree(surf_geoms) if surf_geoms else None,
                'road': road,
                'road_parts': road_parts,
                'road_tree': STRtree(road_parts) if road_parts else None,
                'crossing': crossing,
                'cross_parts': cross_parts,
                'cross_tree': STRtree(cross_parts) if cross_parts else None,
                'support': support,
                'support_bparts': support_bparts,
                'support_btree': STRtree(support_bparts) if support_bparts else None,
                'walk': walk,
            }
        # Mixed vegetation uses pit-to-pit clearance, which can exceed the
        # nominal spacing for custom presets. One neighboring cell must cover both.
        self.max_spacing = max(max(p['spacing_m'], 2*p['pit_radius_m']*max(1, p['scale'][1])+.25)
                               for p in self.config['presets'].values())
        if self.config.get('sidewalk_footing'):
            f = self.config['sidewalk_footing']
            self.max_spacing = max(self.max_spacing, 2*f['radius_m']*f['scale']+.25)

    def _near(self, px: float, py: float, layer: int):
        cell = self.max_spacing
        i, j = math.floor(px / cell), math.floor(py / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield from self.occupied.get((layer, i + dx, j + dy), ())

    def _reserve(self, px: float, py: float, layer: int, spacing: float, kind: str, pit: float):
        key = (layer, math.floor(px / self.max_spacing), math.floor(py / self.max_spacing))
        self.occupied.setdefault(key, []).append((px, py, spacing, kind, pit))

    @staticmethod
    def _covers_disk(poly, btree: STRtree | None, point: Point, radius: float) -> bool:
        if poly.is_empty or not poly.covers(point):
            return False
        if btree is not None and len(btree.query(point, predicate='dwithin', distance=radius + 1e-6)) == 0:
            return True
        return poly.covers(point.buffer(radius))

    def consider(self, key, point, preset, provenance, layer=0, zone=None, zone_btree: STRtree | None = None):
        c = self.config
        if len(self.records) >= c['max_candidates']:
            raise ValueError('Candidate budget exceeded; reduce region size or density')
        override = c['overrides'].get(key, {})
        original = [point.x, point.y]
        if 'position' in override:
            point = Point(*override['position'])
        px, py = point.x, point.y
        preset = override.get('preset', preset)
        if preset not in c['presets']:
            raise ValueError(f'Unknown preset for {key}')
        spec = c['presets'][preset]
        sd = stable_seed(c['seed'], key)
        scale = spec['scale'][0] + (spec['scale'][1] - spec['scale'][0]) * fraction(sd, 'scale')
        mesh = spec['meshes'][stable_seed(sd, 'mesh') % len(spec['meshes'])]
        rec = dict(id=key, source=provenance, original_position=original,
                   position=[px, py], kind=spec['kind'], preset=preset,
                   layer=layer, seed=sd, mesh=mesh, scale=scale, yaw=360*fraction(sd, 'yaw'),
                   species_proxy=spec.get('species_proxy', True), manual_override=bool(override))
        reason = None
        geom = self.layers.get(layer)
        pit = spec['pit_radius_m'] * max(1.0, scale)
        crown = spec['crown_radius_m'] * scale
        footing = None
        if spec['kind'] == 'tree' and geom and geom['walk'].covers(point) and c.get('sidewalk_footing'):
            footing = copy.deepcopy(c['sidewalk_footing'])
            # Street furniture has its own real-world size, independent of the
            # tree's randomized growth scale. Reserve the complete surround.
            pit = max(pit, footing['radius_m']*footing['scale'])
            footing['yaw'] = 0.0
        pole_r2 = (pit + c['pole_clearance_m']) ** 2
        if override.get('disabled'):
            reason = 'disabled_by_user'
        elif not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError(f'Non-finite point {key}')
        elif geom is None:
            reason = 'no_surface_on_layer'
        elif not self._covers_disk(geom['support'], geom['support_btree'], point, pit):
            reason = 'insufficient_planting_surface'
        elif _dist_lt(geom['road_tree'], geom['road_parts'], point, pit + c['road_clearance_m']):
            reason = 'road_or_cycle_lane'
        elif _dist_lt(geom['cross_tree'], geom['cross_parts'], point, pit + c['crossing_clearance_m']):
            reason = 'crossing_access'
        elif _dist_lt(self.bldg_tree, self.bldg_parts, point, max(pit, crown) + c['building_clearance_m']):
            reason = 'building_or_crown_clearance'
        elif _dist_lt(self.excl_tree, self.excl_parts, point, pit):
            reason = 'user_exclusion'
        elif any((qx - px) ** 2 + (qy - py) ** 2 < pole_r2 for qx, qy in self.poles_by_layer.get(layer, ())):
            reason = 'signal_or_pole_clearance'
        elif zone is not None and not self._covers_disk(zone, zone_btree, point, pit):
            reason = 'zone_boundary'
        elif spec['kind'] != 'tree' and zone is None:
            reason = 'ground_cover_requires_zone'
        elif geom['walk'].covers(point) and not self._pedestrian_clearance(point, pit, geom):
            reason = 'pedestrian_corridor'
        elif any(
            (qx - px) ** 2 + (qy - py) ** 2 < (max(spacing, spec['spacing_m']) if kind == spec['kind'] else old_pit + pit + .25) ** 2
            for qx, qy, spacing, kind, old_pit in self._near(px, py, layer)
        ):
            reason = 'vegetation_spacing'
        z = 0.0
        if reason is None:
            surface = None
            if geom['surf_tree'] is not None:
                hits = sorted(int(i) for i in geom['surf_tree'].query(point, predicate='intersects'))
                for i in hits:
                    if geom['surf_geoms'][i].covers(point):
                        surface = geom['surfaces'][i]
                        break
            if surface is None:
                surface = min(geom['surfaces'], key=lambda s: s.geometry.distance(point))
            z = float(self.model.sample_z(px, py, layer=layer)) + surface.z_offset
            delta = 0.5
            dzx = float(self.model.sample_z(px + delta, py, layer=layer)) - float(self.model.sample_z(px - delta, py, layer=layer))
            dzy = float(self.model.sample_z(px, py + delta, layer=layer)) - float(self.model.sample_z(px, py - delta, layer=layer))
            if not all(math.isfinite(v) for v in (z, dzx, dzy)) or math.degrees(math.atan(math.hypot(dzx, dzy))) > c['max_slope_degrees']:
                reason = 'terrain_slope'
        if reason is None:
            self._reserve(px, py, layer, spec['spacing_m'], spec['kind'], pit)
        rec.update(status='rejected' if reason else 'accepted', reason=reason,
                   x=px, y=-py, z=z, pit_radius_m=pit, crown_radius_m=crown)
        if footing:
            rec['footing'] = footing
        self.records.append(rec)

    def _pedestrian_clearance(self, p, pit, geom):
        # Preserve a walkable strip on at least one side of the tree pit. This is
        # a local corridor check, not a substitute for a complete pedestrian network.
        if geom['road'].is_empty:
            return False
        if geom['road_tree'] is not None:
            nearest_idx = int(geom['road_tree'].nearest(p))
            edge = nearest_points(p, geom['road_parts'][nearest_idx])[1]
        else:
            edge = nearest_points(p, geom['road'])[1]
        d = math.hypot(p.x - edge.x, p.y - edge.y)
        if d < 1e-6:
            return False
        n = ((p.x-edge.x)/d, (p.y-edge.y)/d)
        width = self.config['pedestrian_width_m']
        for side in (1, -1):
            def xy(u, v, _side=side):
                return (p.x + _side*n[0]*u - n[1]*v, p.y+_side*n[1]*u+n[0]*v)
            strip = Polygon([xy(pit+.05,-1),xy(pit+width+.05,-1),xy(pit+width+.05,1),xy(pit+.05,1)])
            if geom['walk'].covers(strip) and not self.buildings.intersects(strip) and not self.exclusions.intersects(strip):
                return True
        return False

    def run(self):
        c = self.config
        if c['include_observed']:
            for obj in sorted(self.model.objects, key=lambda o:o.id):
                if obj.kind == 'tree':
                    self.consider(obj.id, obj.position, c['tree_preset'], 'osm', _parse_layer(obj.tags.get('layer')))
        for key, value in sorted(c['overrides'].items()):
            if key.startswith('manual:'):
                self.consider(key, Point(*value['position']), value.get('preset', c['tree_preset']), 'manual', _parse_layer(value.get('layer', 0)))
        if c['street_rows']:
            for curb in sorted(self.model.curbs, key=lambda x:x.id):
                layer = _parse_layer(curb.layer)
                if layer not in self.layers:
                    continue
                line = curb.geometry
                count = int(line.length / c['row_spacing_m'])
                for i in range(count):
                    s = (i+.5)*c['row_spacing_m']
                    a,b = line.interpolate(max(0,s-.1)),line.interpolate(min(line.length,s+.1))
                    h = math.atan2(b.y-a.y,b.x-a.x)
                    p = line.interpolate(s)
                    for side in (-1,1):
                        q = Point(p.x-side*math.sin(h)*c['row_inset_m'],p.y+side*math.cos(h)*c['row_inset_m'])
                        if self.layers[layer]['walk'].covers(q):
                            self.consider(f'row:{curb.id}:{i}:{side}',q,c['tree_preset'],'inferred_row',layer)
        for z in sorted(c['zones'], key=lambda z: ({'tree':0,'shrub':1,'grass':2}[c['presets'][z['preset']]['kind']],z['id'])):
            polygon = shape(z['geometry'])
            shapely.prepare(polygon)
            zb_parts = _flatten_parts(polygon.boundary)
            zb_tree = STRtree(zb_parts) if zb_parts else None
            spec = c['presets'][z['preset']]
            cell = spec['spacing_m']
            x0,y0,x1,y1 = polygon.bounds
            imin,imax = math.floor(x0/cell),math.ceil(x1/cell)
            jmin,jmax = math.floor(y0/cell),math.ceil(y1/cell)
            if (imax-imin)*(jmax-jmin) > c['max_candidates']:
                raise ValueError(f'Zone {z["id"]} exceeds candidate budget')
            z_layer = _parse_layer(z.get('layer', 0))
            for i in range(imin,imax):
                for j in range(jmin,jmax):
                    key=f'zone:{z["id"]}:{i}:{j}'
                    if fraction(c['seed'],key+':density') >= z.get('density',1):
                        continue
                    p=Point((i+.2+.6*fraction(c['seed'],key+':x'))*cell,(j+.2+.6*fraction(c['seed'],key+':y'))*cell)
                    if polygon.covers(p):
                        self.consider(key,p,z['preset'],'inferred_zone',z_layer,polygon,zb_tree)
        accepted=[r for r in self.records if r['status']=='accepted']
        return {'schema':SCHEMA,'map':self.model.name,'origin':[self.model.origin_lat,self.model.origin_lon],
                'coordinate_system':'carla-metres','config':c,'points':accepted,'review':self.records,
                'summary':{'accepted':len(accepted),'rejected':len(self.records)-len(accepted),
                           'footings':sum('footing' in r for r in accepted),
                           'by_kind':dict(Counter(r['kind'] for r in accepted)),
                           'reasons':dict(Counter(r['reason'] for r in self.records if r['reason']))}}


def plan(model, config=None, poles=()):
    return Planner(model, config, poles).run()
