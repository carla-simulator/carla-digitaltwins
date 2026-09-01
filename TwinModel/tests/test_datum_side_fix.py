"""Regression for RoadDatum.nearest's side test at a road's start (SoMa r28, z max 1.206 m).

Shapely measures a negative ``line_interpolate_point`` distance from the END of the line, so for
a point projecting within 0.1 m of a road's start the tangent came out reversed and the road's
reach on the *wrong* side was applied: 4th Street's 17.7 m right-hand reach was used for a point
6.6 m off its 4.3 m left side, beating the service road whose lane the point is on (1.2 m z jump
at one waypoint)."""
from __future__ import annotations

import pytest
from shapely.geometry import LineString

from twinmodel.datum import RoadDatum
from twinmodel.model import Lane, Road


def _road(rid, coords, lanes):
    return Road(id=rid, reference_line=LineString(coords), lanes=lanes)


def test_side_test_is_right_at_a_road_start():
    # a wide one-way street (reference line at its left curb: 2 m sidewalk left, 16 m of lanes
    # right) starting at x=0, and a narrow service road passing 6.6 m off its *left* side,
    # 1.2 m lower
    wide = _road("wide", [(0, 0, 6.3), (60, 0, 6.3)],
                 [Lane(id=1, type="sidewalk", width=2.0)]
                 + [Lane(id=-i, type="driving", width=4.0) for i in range(1, 5)])
    narrow = _road("narrow", [(-30, 6.6, 5.1), (30, 6.6, 5.1)],
                   [Lane(id=1, type="driving", width=3.0), Lane(id=-1, type="driving", width=3.0)])
    d = RoadDatum([wide, narrow], None, max_dist=25.0)
    # on the narrow road's carriageway, near the wide road's start: the wide road does not
    # reach 6.6 m to its left, whatever s the query projects to
    for x in (0.02, 0.09, 0.5, 3.0):
        assert d.z(x, 5.2) == pytest.approx(5.1, abs=1e-6), x
    # mirrored onto the wide road's lanes: the wide road wins there
    assert d.z(0.05, -5.2) == pytest.approx(6.3, abs=1e-6)
    assert d.z(20.0, -5.2) == pytest.approx(6.3, abs=1e-6)
