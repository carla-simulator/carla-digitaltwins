import copy
import pytest
from shapely.geometry import Point
from twinmodel.model import TwinModel, Signal, Controller
from twinmodel.pedestrian_signals import connect_pedestrian_signals
from ue.pedestrian_logic import connect_logic


def fixture():
    m = TwinModel('test', 0, 0, (0, 0, 1, 1))
    m.signals = [Signal(id=sid, kind=kind, road_id='r1', s=5, t=t,
                        position=Point(5, t), heading=0, controller_id='c1')
                 for sid, kind, t in [('v', 'traffic_light', 0),
                                      ('pa', 'traffic_light_ped', -6),
                                      ('pb', 'traffic_light_ped', 6)]]
    m.controllers = [Controller('c1', 'j1', ['v', 'pa', 'pb'])]
    return m


def test_exclusive_paired_walk_stage_and_idempotence():
    m = fixture()
    assert connect_pedestrian_signals(m) == 1
    assert m.controllers[0].signal_ids == ['v']
    assert m.controllers[1].signal_ids == ['pa', 'pb']
    assert all(s.controller_id == 'ped_j1' for s in m.signals[1:])
    before = copy.deepcopy(m.controllers)
    connect_pedestrian_signals(m)
    assert before == m.controllers


def test_missing_controller_rejected_before_mutation():
    m = fixture(); m.signals[-1].controller_id = 'missing'
    before = copy.deepcopy(m.controllers)
    with pytest.raises(ValueError): connect_pedestrian_signals(m)
    assert m.controllers == before


def test_clearance_and_vehicle_timing_preserved():
    signals = [dict(id=sid, kind='ped', road_id=1, s=5, x=0, y=y)
               for sid,y in [('pa',-6),('pb',6)]]
    logic = {'TrafficLights': [dict(SignalID=sid, Timing={'GreenDuration': 7})
                              for sid in ['v','pa','pb']]}
    ctls = {'v':'c1', 'pa':'ped_j1', 'pb':'ped_j1'}
    assert connect_logic(logic, signals, ctls, {'ped_j1':'1'}) == {'ped_j1':12.0}
    assert logic['TrafficLights'][0]['Timing'] == {'GreenDuration':7}
    for entry in logic['TrafficLights'][1:]:
        assert entry['Timing'] == dict(GreenDuration=10., AmberDuration=0., RedDuration=12.)
    with pytest.raises(ValueError):
        connect_logic(logic, signals, dict(ctls, v='ped_j1'), {'ped_j1':'1'})


def test_unassigned_head_recovers_signalized_junction_from_metadata():
    m = fixture()
    for signal in m.signals[1:]:
        signal.controller_id = None
        signal.tags['junction_id'] = 'j1'
    m.controllers[0].signal_ids = ['v']
    connect_pedestrian_signals(m)
    assert all(s.controller_id == 'ped_j1' for s in m.signals[1:])
