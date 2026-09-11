"""Explicit traffic-control annotations in WGS84, with face bearing clockwise from north.

These preserve authoring intent, including target road/lane. They are not implicitly
converted into signal controllers or lane triggers by assigning a nearest road.
"""
import copy
import math
from shapely.geometry import LineString, Polygon

TYPES = {
    'stop_line': ('stop', 'yield'),
    'bike_crosswalk': ('marked', 'unmarked', 'signalized'),
    'crosswalk': ('zebra', 'unmarked', 'signalized'),
    'traffic_light': ('vehicle', 'pedestrian', 'left_arrow', 'right_arrow', 'directional'),
    'traffic_sign': ('stop', 'yield', 'speed_limit', 'no_entry', 'priority_road', 'custom'),
}


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _point(p):
    return (isinstance(p, list) and len(p) == 2 and all(_number(v) for v in p)
            and abs(p[0]) <= 180 and abs(p[1]) <= 90)


def validate(op):
    kind = op.get('kind')
    if kind not in TYPES:
        return ['unknown control kind']
    errors = []
    if op.get('type') not in TYPES[kind]:
        errors.append('unsupported control type')
    geometry = op.get('geometry', {})
    if not isinstance(geometry, dict):
        return errors + ['geometry must be a GeoJSON geometry']
    coords = geometry.get('coordinates')
    if kind in ('crosswalk', 'bike_crosswalk') and geometry.get('type') == 'Polygon':
        if (not isinstance(coords, list) or not coords or any(
                not isinstance(ring, list) or len(ring) < 4 or not all(_point(p) for p in ring)
                or ring[0] != ring[-1] for ring in coords)):
            errors.append('crosswalk outline needs closed rings of valid coordinates')
        else:
            polygon = Polygon(coords[0], coords[1:])
            if not polygon.is_valid or polygon.area == 0:
                errors.append('crosswalk outline must not cross itself or collapse')
    elif kind in ('stop_line', 'crosswalk', 'bike_crosswalk'):
        if (geometry.get('type') != 'LineString' or not isinstance(coords, list)
                or len(coords) < 2 or not all(_point(p) for p in coords)):
            errors.append('segment needs at least two valid [longitude, latitude] points')
        else:
            line = LineString(coords)
            if line.length == 0 or not line.is_simple:
                errors.append('segment must have length and must not cross itself')
        if not _number(op.get('width_m')) or not 0 < op['width_m'] <= 30:
            errors.append('width must be greater than zero and at most 30 metres')
    else:
        if geometry.get('type') != 'Point' or not _point(coords):
            errors.append('control needs a valid point position')
        if not _number(op.get('bearing')) or not 0 <= op['bearing'] < 360:
            errors.append('face bearing must be 0 <= degrees < 360, clockwise from north')
        if op.get('type') == 'speed_limit' and (not _number(op.get('value')) or not 0 < op['value'] <= 200):
            errors.append('speed limit must be greater than zero and at most 200 km/h')
        if op.get('type') == 'custom' and not str(op.get('label', '')).strip():
            errors.append('custom sign needs a label')
    target = op.get('target', {})
    if not isinstance(target, dict):
        errors.append('target must be an object')
    elif 'lane_id' in target and (not isinstance(target['lane_id'], int) or isinstance(target['lane_id'], bool) or target['lane_id'] == 0):
        errors.append('target lane id must be a nonzero integer')
    return errors


def feature_collection(ops):
    features = []
    for op in ops:
        if op.get('op') != 'control.set' or op.get('disabled'):
            continue
        errors = validate(op)
        if errors:
            raise ValueError('; '.join(errors))
        features.append({'type': 'Feature', 'geometry': copy.deepcopy(op['geometry']),
                         'properties': copy.deepcopy({k: v for k, v in op.items() if k not in ('geometry', 'op')})})
    return {'type': 'FeatureCollection', 'features': features}
