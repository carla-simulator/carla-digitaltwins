"""streetspace helpers on synthetic blocks (no fixture, no network)."""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from twinmodel import streetspace as ss


def _eixample_corner(street=20.0, chamfer=14.0, block=100.0):
    """Four blocks around the origin, streets ``street`` wide, 45 deg chamfers of ``chamfer``
    m along each face (a 20 m diagonal for 14 m)."""
    h = street / 2.0
    blocks = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            x0, y0 = sx * h, sy * h
            x1, y1 = sx * (h + block), sy * (h + block)
            pts = [(x0 + sx * chamfer, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0 + sy * chamfer)]
            blocks.append(Polygon(pts).buffer(0))
    return unary_union(blocks)


def test_face_distances_and_canyon_fraction():
    bld = _eixample_corner()
    line = LineString([(30, 0), (100, 0)])  # mid-street, faces 10 m either side
    s, dl = ss.face_distances(line, bld, "left", step=4.0)
    _, dr = ss.face_distances(line, bld, "right", step=4.0)
    assert ss.canyon_fraction(dl) == 1.0 and ss.canyon_fraction(dr) == 1.0
    assert ss.robust_width(dl, 0.0) == pytest.approx(10.0, abs=1e-6)
    assert ss.robust_width(dr, 0.0) == pytest.approx(10.0, abs=1e-6)
    # off-centre line: asymmetric faces, same street width
    line2 = LineString([(30, -4), (100, -4)])
    _, dl2 = ss.face_distances(line2, bld, "left")
    _, dr2 = ss.face_distances(line2, bld, "right")
    assert ss.robust_width(dl2, 0) == pytest.approx(14.0) and ss.robust_width(dr2, 0) == pytest.approx(6.0)
    # nothing within MAX_FACE_DIST: nan, fraction 0
    _, d = ss.face_distances(LineString([(0, 300), (50, 300)]), bld, "left")
    assert np.isnan(d).all() and ss.canyon_fraction(d) == 0.0
    assert ss.robust_width(d, 3.3) == 3.3


def test_blockers_occlude_the_face():
    """A parallel road between the line and the face claims that side (Passeig de Gracia's
    laterals): the ray returns nan there."""
    bld = _eixample_corner(street=60.0)
    main = LineString([(40, 0), (100, 0)])
    lateral = LineString([(30, 18), (110, 18)])
    _, dl = ss.face_distances(main, bld, "left")
    assert ss.robust_width(dl, 0) == pytest.approx(30.0)
    _, dl_b = ss.face_distances(main, bld, "left", blockers=lateral)
    assert ss.canyon_fraction(dl_b) == 0.0
    _, dr_b = ss.face_distances(main, bld, "right", blockers=lateral)
    assert ss.robust_width(dr_b, 0) == pytest.approx(30.0)  # nothing between on the right
    # the line itself (a hit closer than BLOCKER_MIN_DIST_M) never blocks
    _, d_self = ss.face_distances(main, bld, "left", blockers=main)
    assert ss.robust_width(d_self, 0) == pytest.approx(30.0)


def test_canyon_extent_finds_the_chamfer_start():
    bld = _eixample_corner(street=20.0, chamfer=14.0)
    # a road along +x from x=100 down to the node at the origin (reference line mid-street)
    line = LineString([(100, 0), (0, 0)])
    lo, hi = ss.canyon_extent(line, bld, (10.0, 10.0), step=1.0, tol=1.5)
    assert lo == pytest.approx(0.0)
    # faces end at x = 10 + 14 = 24 -> s = 76; the tolerance lets it run ~1.5 m further
    assert hi is not None and 75.5 <= hi <= 78.0, hi
    # scanning only the far end
    lo2, hi2 = ss.canyon_extent(line, bld, (10.0, 10.0), scan=10.0)
    assert lo2 == pytest.approx(0.0) and hi2 is None
    # no faces at all -> (None, None)
    assert ss.canyon_extent(LineString([(0, 300), (50, 300)]), bld, (10.0, 10.0)) == (None, None)


def test_arm_corridor_and_junction_plaza_is_the_chamfered_octagon():
    bld = _eixample_corner(street=20.0, chamfer=14.0)
    centre = Point(0, 0)
    mouth = 24.0  # arms cut at the chamfer line
    corridors = []
    for hdg, end in ((math.pi, (mouth, 0)), (0.0, (-mouth, 0)), (-math.pi / 2, (0, mouth)),
                     (math.pi / 2, (0, -mouth))):
        c = ss.arm_corridor(end, hdg, 10.0, mouth + 12.0)
        assert c.area == pytest.approx(20.0 * (mouth + 12.0))
        corridors.append(c)
    plaza = ss.junction_plaza(centre, bld, corridors, radius=45.0)
    assert isinstance(plaza, Polygon) and plaza.is_valid
    # octagon: 48 x 48 square minus the four 14 m corner triangles
    assert plaza.area == pytest.approx(48.0 ** 2 - 4 * 0.5 * 14.0 ** 2, rel=0.02)
    assert plaza.intersection(bld).area < 1e-6
    # the corner triangle in front of the chamfer is in
    assert plaza.contains(Point(15.0, 15.0))
    assert not plaza.contains(Point(30.0, 5.0))  # beyond the mouth: the arm's, not the plaza's
    # with a lateral offset the corridor shifts to the street centre
    off = ss.arm_corridor((mouth, 3.0), math.pi, 10.0, 10.0, offset=3.0)  # left of heading pi is -y
    assert off.bounds[1] == pytest.approx(-10.0) and off.bounds[3] == pytest.approx(10.0)


def test_corner_void_without_buildings_is_the_disc():
    v = ss.corner_void(Point(0, 0), None, radius=10.0)
    assert v.area == pytest.approx(math.pi * 100, rel=0.01)
    assert ss.junction_plaza(Point(0, 0), Polygon(), [], radius=10.0).area == pytest.approx(v.area)
