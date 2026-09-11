"""Explicit, surveyed street-space annotations, independent of OSM centre lines.

Both boundaries run from section start to section end, in WGS84 [lon, lat].
They may have different vertex counts. Never repair invalid survey geometry with
buffer(0): doing so silently loses the boundaries the reviewer specified.
"""
from __future__ import annotations

import copy
import math

from shapely.geometry import Polygon

KINDS = ("driving", "parking", "biking", "bus", "taxi", "bike_crosswalk", "sidewalk", "shoulder", "median", "verge")


def ring(op):
    return op["left"] + list(reversed(op["right"])) + [op["left"][0]]


def validate(op):
    errors = []
    if op.get("kind") not in KINDS:
        errors.append("space type must be one of " + ", ".join(KINDS))
    for edge in ("left", "right"):
        points = op.get(edge)
        if not isinstance(points, list) or len(points) < 2:
            errors.append(f"{edge} boundary needs at least two points")
            continue
        for p in points:
            if (not isinstance(p, list) or len(p) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in p)
                    or abs(p[0]) > 180 or abs(p[1]) > 90):
                errors.append(f"{edge} boundary has an invalid [longitude, latitude] point")
                break
    if not errors:
        polygon = Polygon(ring(op))
        if not polygon.is_valid or polygon.area <= 1e-16:
            errors.append("boundaries must enclose a nonzero area without crossing")
    return errors


def feature_collection(ops):
    """Lossless WGS84 annotation export; not a claim of OpenDRIVE lane fitting."""
    features = []
    for op in ops:
        if op.get("op") != "space.set" or op.get("disabled"):
            continue
        problems = validate(op)
        if problems:
            raise ValueError("; ".join(problems))
        features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring(op)]},
                         "properties": copy.deepcopy({k: v for k, v in op.items() if k != "op"})})
    return {"type": "FeatureCollection", "features": features}
