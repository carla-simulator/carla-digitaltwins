"""Support geometry is distinct from the lane control anchor."""
from dataclasses import asdict

import pytest
from shapely.geometry import LineString, Point, box

from twinmodel.model import Road, Signal, Surface, TwinModel
from twinmodel.pole_placement import PolePlacer


def model():
    m = TwinModel(name='test', origin_lat=0, origin_lon=0, bbox_wgs84=(0, 0, 1, 1))
    m.roads = [Road(id='r', reference_line=LineString([(0, 0), (40, 0)]), lanes=[])]
    m.signals = [Signal(id='s', kind='traffic_light', road_id='r', s=20, t=-4,
                        position=Point(20, -4), validities=[(-2, -1)])]
    # Chamfer/plaza road is wider than the nominal 4 m offset.
    m.surfaces = [Surface('road', 'drivable', box(0, -7, 40, 7)),
                  Surface('walk', 'sidewalk', box(0, -11, 40, -7), z_offset=.15)]
    return m


def test_chamfer_pole_clears_finished_road_without_moving_control():
    m = model()
    before = asdict(m.signals[0])
    a = PolePlacer(m).place('s')
    assert a['status'] == 'ok'
    assert a['x'] == pytest.approx(20)
    assert a['y'] >= 7.449
    assert a['z'] == pytest.approx(.15)
    assert asdict(m.signals[0]) == before


def test_parking_and_crossings_are_not_supports():
    m = model()
    m.surfaces += [Surface('parking', 'parking', box(10, -8, 30, -7)),
                   Surface('zebra', 'crossing', box(18, -11, 22, -7))]
    a = PolePlacer(m).place('s')
    p = Point(a['x'], -a['y'])
    for s in m.surfaces:
        if s.kind in {'drivable', 'parking', 'crossing'}:
            assert p.distance(s.geometry) >= .449


def test_never_falls_back_to_road_ground_or_opposite_side():
    m = model()
    m.surfaces[1] = Surface('other-side', 'sidewalk', box(0, 7, 40, 11))
    m.surfaces.append(Surface('slab', 'ground', box(0, -20, 40, 20)))
    assert PolePlacer(m).place('s')['status'] == 'unresolved'


def test_does_not_snap_to_another_elevation_layer():
    m = model()
    m.surfaces[1].tags['layer'] = 1
    assert PolePlacer(m).place('s')['status'] == 'unresolved'


def test_separate_poles_do_not_collapse_to_one_corner():
    m = model()
    m.signals.append(Signal(id='s2', kind='speed_limit', road_id='r', s=20, t=-4,
                            position=Point(20, -4)))
    placer = PolePlacer(m)
    a, b = placer.place('s'), placer.place('s2')
    assert Point(a['x'], a['y']).distance(Point(b['x'], b['y'])) >= .599


def test_left_side_and_safe_existing_position_are_preserved():
    m = model()
    m.signals[0].t = 8
    m.signals[0].position = Point(20, 8)
    m.surfaces[1].geometry = box(0, 7, 40, 11)
    a = PolePlacer(m).place('s')
    assert a['move_m'] == 0
    assert a['y'] == -8


def test_buildings_exclude_supports():
    from types import SimpleNamespace
    m = model()
    m.buildings = [SimpleNamespace(footprint=box(19, -11, 21, -7))]
    a = PolePlacer(m).place('s')
    assert Point(a['x'], -a['y']).distance(m.buildings[0].footprint) >= .499


def test_too_narrow_support_and_distant_sidewalk_fail():
    m = model()
    m.surfaces[1].geometry = box(0, -7.1, 40, -7)
    assert PolePlacer(m).place('s')['status'] == 'unresolved'
    m.surfaces[1].geometry = box(0, -30, 40, -25)
    assert PolePlacer(m).place('s')['status'] == 'unresolved'
