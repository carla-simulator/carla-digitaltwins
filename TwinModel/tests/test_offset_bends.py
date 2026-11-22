"""``lanegraph._offset_polyline`` on the inside of a bend tighter than the offset."""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point

from twinmodel.lanegraph import _offset_polyline


def test_an_offset_into_a_tight_bend_never_runs_backwards():
    # a 90-degree bend of 1 m radius, offset 3 m to its inside
    bend = [(0.0, 0.0), (10.0, 0.0)] + [
        (10.0 + math.sin(a), 1.0 - math.cos(a)) for a in [k * math.pi / 16 for k in range(1, 8)]
    ] + [(11.0, 1.0), (11.0, 11.0)]
    out = _offset_polyline(bend, 3.0)
    # no loop: the line never crosses or doubles back over itself
    assert LineString(out).is_simple, out
    assert out[0] == (0.0, 3.0) and out[-1] == (8.0, 11.0)
    # every vertex moves forward along the original line
    for (ax, ay), (bx, by) in zip(out, out[1:]):
        assert LineString(bend).project(Point(bx, by)) >= LineString(bend).project(Point(ax, ay)) - 1e-6
