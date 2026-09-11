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

from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import nearest_points, unary_union

from .model import road_osm_layer

DEFAULTS = Path(__file__).resolve().parents[1] / 'vegetation/defaults.json'
SCHEMA = 'twin-vegetation-plan/1'


def stable_seed(seed, key):
    return int.from_bytes(hashlib.blake2s(f'{seed}:{key}'.encode(), digest_size=4).digest(), 'big') & 0x7fffffff


def fraction(seed, key):
    return stable_seed(seed, key) / 2147483648.0


def configuration(overrides=None):
    c = json.loads(DEFAULTS.read_text())
    for k, v in (overrides or {}).items():
        if k == 'presets':
            c[k].update(v)
        else:
            c[k] = copy.deepcopy(v)
    for key in ('row_spacing_m', 'row_inset_m', 'pedestrian_width_m', 'max_candidates'):
        if not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f'{key} must be finite and positive')
    for key in ('road_clearance_m', 'crossing_clearance_m', 'pole_clearance_m', 'building_clearance_m'):
        if not math.isfinite(c[key]) or c[key] < 0:
            raise ValueError(f'{key} must be finite and nonnegative')
    if not 0 < c['max_slope_degrees'] < 90:
        raise ValueError('max_slope_degrees must be between 0 and 90')
    for name, p in c['presets'].items():
        if p['kind'] not in {'tree', 'shrub', 'grass'} or not p['meshes']:
            raise ValueError(f'Invalid preset {name}')
        if not all(isinstance(m, str) and m.startswith('/Game/Carla/Static/Vegetation/') for m in p['meshes']):
            raise ValueError(f'Expected CARLA vegetation mesh paths in {name}')
        if not 0 < p['scale'][0] <= p['scale'][1] or not all(math.isfinite(v) for v in p['scale']):
            raise ValueError(f'Invalid scale range in {name}')
        for key in ('spacing_m', 'pit_radius_m', 'crown_radius_m'):
            if not math.isfinite(p[key]) or p[key] <= 0:
                raise ValueError(f'Invalid {key} in {name}')
    if c['tree_preset'] not in c['presets']:
        raise ValueError('Unknown tree_preset')
    footing = c.get('sidewalk_footing')
    if footing is not None:
        if footing['mesh'] != '/Game/Carla/Static/Static/SM_TreeBase02':
            raise ValueError('Unsupported sidewalk footing asset')
        for key in ('radius_m', 'scale'):
            if not math.isfinite(footing[key]) or footing[key] <= 0:
                raise ValueError(f'Invalid footing {key}')
        if not math.isfinite(footing['soil_height_m']) or footing['soil_height_m'] < 0:
            raise ValueError('Invalid footing soil height')
        if footing['scale'] > 3 or not -.04 <= footing['ground_offset_m'] <= -.005 or abs(footing['soil_height_m']-.14744362) > .0001:
            raise ValueError('Unsupported footing scale or soil offset')
    seen = set()
    for z in c['zones']:
        if z['id'] in seen or z['preset'] not in c['presets']:
            raise ValueError('Zone IDs must be unique and presets must exist')
        seen.add(z['id'])
        g = shape(z['geometry'])
        if g.is_empty or not g.is_valid or g.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError(f'Invalid polygon in zone {z["id"]}')
        d = z.get('density', 1.0)
        if not math.isfinite(d) or not 0 <= d <= 1:
            raise ValueError('Zone density must be in [0,1]')
    return c


class Planner:
    def __init__(self, model, config=None, poles=()):
        self.model, self.config = model, configuration(config)
        self.records, self.occupied = [], {}
        self.layers = {}
        self.poles = [(Point(p['x'], -p['y']), int(p.get('layer', 0))) for p in poles]
        self.buildings = unary_union([b.footprint for b in model.buildings])
        self.exclusions = unary_union([shape(g) for g in self.config['exclusions']])
        layers = {int(s.tags.get('layer') or 0) for s in model.surfaces}
        for layer in layers:
            ss = [s for s in model.surfaces if int(s.tags.get('layer') or 0) == layer]
            def union(kinds):
                return unary_union([s.geometry for s in ss if s.kind in kinds])
            self.layers[layer] = {
                'surfaces': ss,
                'road': union({'drivable', 'parking', 'biking'}),
                'crossing': union({'crossing'}),
                'support': union({'sidewalk', 'island', 'median', 'verge', 'grass', 'ground'}),
                'walk': union({'sidewalk'}),
            }
        # Mixed vegetation uses pit-to-pit clearance, which can exceed the
        # nominal spacing for custom presets. One neighboring cell must cover both.
        self.max_spacing = max(max(p['spacing_m'], 2*p['pit_radius_m']*max(1, p['scale'][1])+.25)
                               for p in self.config['presets'].values())
        if self.config.get('sidewalk_footing'):
            f = self.config['sidewalk_footing']
            self.max_spacing = max(self.max_spacing, 2*f['radius_m']*f['scale']+.25)

    def _near(self, p, layer):
        cell = self.max_spacing
        i, j = math.floor(p.x / cell), math.floor(p.y / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield from self.occupied.get((layer, i + dx, j + dy), ())

    def _reserve(self, p, layer, spacing, kind, pit):
        key = (layer, math.floor(p.x/self.max_spacing), math.floor(p.y/self.max_spacing))
        self.occupied.setdefault(key, []).append((p, spacing, kind, pit))

    def consider(self, key, point, preset, provenance, layer=0, zone=None):
        c = self.config
        if len(self.records) >= c['max_candidates']:
            raise ValueError('Candidate budget exceeded; reduce region size or density')
        override = c['overrides'].get(key, {})
        original = [point.x, point.y]
        if 'position' in override:
            point = Point(*override['position'])
        preset = override.get('preset', preset)
        if preset not in c['presets']:
            raise ValueError(f'Unknown preset for {key}')
        spec = c['presets'][preset]
        sd = stable_seed(c['seed'], key)
        scale = spec['scale'][0] + (spec['scale'][1] - spec['scale'][0]) * fraction(sd, 'scale')
        mesh = spec['meshes'][stable_seed(sd, 'mesh') % len(spec['meshes'])]
        rec = dict(id=key, source=provenance, original_position=original,
                   position=[point.x, point.y], kind=spec['kind'], preset=preset,
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
        if override.get('disabled'):
            reason = 'disabled_by_user'
        elif not all(math.isfinite(v) for v in (point.x, point.y)):
            raise ValueError(f'Non-finite point {key}')
        elif geom is None:
            reason = 'no_surface_on_layer'
        elif not geom['support'].covers(point.buffer(pit)):
            reason = 'insufficient_planting_surface'
        elif geom['road'].distance(point) < pit + c['road_clearance_m']:
            reason = 'road_or_cycle_lane'
        elif not geom['crossing'].is_empty and geom['crossing'].distance(point) < pit + c['crossing_clearance_m']:
            reason = 'crossing_access'
        elif not self.buildings.is_empty and self.buildings.distance(point) < max(pit, crown) + c['building_clearance_m']:
            reason = 'building_or_crown_clearance'
        elif not self.exclusions.is_empty and self.exclusions.distance(point) < pit:
            reason = 'user_exclusion'
        elif any(l == layer and p.distance(point) < pit + c['pole_clearance_m'] for p, l in self.poles):
            reason = 'signal_or_pole_clearance'
        elif zone is not None and not zone.covers(point.buffer(pit)):
            reason = 'zone_boundary'
        elif spec['kind'] != 'tree' and zone is None:
            reason = 'ground_cover_requires_zone'
        elif geom['walk'].covers(point) and not self._pedestrian_clearance(point, pit, geom):
            reason = 'pedestrian_corridor'
        elif any(p.distance(point) < (max(spacing, spec['spacing_m']) if kind == spec['kind'] else old_pit + pit + .25)
                 for p, spacing, kind, old_pit in self._near(point, layer)):
            reason = 'vegetation_spacing'
        z = 0.0
        if reason is None:
            surface = min(geom['surfaces'], key=lambda s: s.geometry.distance(point))
            z = float(self.model.sample_z(point.x, point.y, layer=layer)) + surface.z_offset
            delta = 0.5
            dzx = float(self.model.sample_z(point.x+delta, point.y, layer=layer)) - float(self.model.sample_z(point.x-delta, point.y, layer=layer))
            dzy = float(self.model.sample_z(point.x, point.y+delta, layer=layer)) - float(self.model.sample_z(point.x, point.y-delta, layer=layer))
            if not all(math.isfinite(v) for v in (z, dzx, dzy)) or math.degrees(math.atan(math.hypot(dzx, dzy))) > c['max_slope_degrees']:
                reason = 'terrain_slope'
        if reason is None:
            self._reserve(point, layer, spec['spacing_m'], spec['kind'], pit)
        rec.update(status='rejected' if reason else 'accepted', reason=reason,
                   x=point.x, y=-point.y, z=z, pit_radius_m=pit, crown_radius_m=crown)
        if footing:
            rec['footing'] = footing
        self.records.append(rec)

    def _pedestrian_clearance(self, p, pit, geom):
        # Preserve a walkable strip on at least one side of the tree pit. This is
        # a local corridor check, not a substitute for a complete pedestrian network.
        if geom['road'].is_empty:
            return False
        edge = nearest_points(p, geom['road'])[1]
        d = p.distance(edge)
        if d < 1e-6:
            return False
        n = ((p.x-edge.x)/d, (p.y-edge.y)/d)
        width = self.config['pedestrian_width_m']
        for side in (1, -1):
            def xy(u,v):
                return (p.x + side*n[0]*u - n[1]*v, p.y+side*n[1]*u+n[0]*v)
            strip = Polygon([xy(pit+.05,-1),xy(pit+width+.05,-1),xy(pit+width+.05,1),xy(pit+.05,1)])
            if geom['walk'].covers(strip) and not self.buildings.intersects(strip) and not self.exclusions.intersects(strip):
                return True
        return False

    def run(self):
        c = self.config
        if c['include_observed']:
            for obj in sorted(self.model.objects, key=lambda o:o.id):
                if obj.kind == 'tree':
                    self.consider(obj.id, obj.position, c['tree_preset'], 'osm', int(obj.tags.get('layer') or 0))
        for key, value in sorted(c['overrides'].items()):
            if key.startswith('manual:'):
                self.consider(key, Point(*value['position']), value.get('preset', c['tree_preset']), 'manual', int(value.get('layer', 0)))
        if c['street_rows']:
            for curb in sorted(self.model.curbs, key=lambda x:x.id):
                layer = curb.layer or 0
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
            spec = c['presets'][z['preset']]
            cell = spec['spacing_m']
            x0,y0,x1,y1 = polygon.bounds
            imin,imax = math.floor(x0/cell),math.ceil(x1/cell)
            jmin,jmax = math.floor(y0/cell),math.ceil(y1/cell)
            if (imax-imin)*(jmax-jmin) > c['max_candidates']:
                raise ValueError(f'Zone {z["id"]} exceeds candidate budget')
            for i in range(imin,imax):
                for j in range(jmin,jmax):
                    key=f'zone:{z["id"]}:{i}:{j}'
                    if fraction(c['seed'],key+':density') >= z.get('density',1):
                        continue
                    p=Point((i+.2+.6*fraction(c['seed'],key+':x'))*cell,(j+.2+.6*fraction(c['seed'],key+':y'))*cell)
                    if polygon.covers(p):
                        self.consider(key,p,z['preset'],'inferred_zone',int(z.get('layer',0)),polygon)
        accepted=[r for r in self.records if r['status']=='accepted']
        return {'schema':SCHEMA,'map':self.model.name,'origin':[self.model.origin_lat,self.model.origin_lon],
                'coordinate_system':'carla-metres','config':c,'points':accepted,'review':self.records,
                'summary':{'accepted':len(accepted),'rejected':len(self.records)-len(accepted),
                           'footings':sum('footing' in r for r in accepted),
                           'by_kind':dict(Counter(r['kind'] for r in accepted)),
                           'reasons':dict(Counter(r['reason'] for r in self.records if r['reason']))}}


def plan(model, config=None, poles=()):
    return Planner(model, config, poles).run()
