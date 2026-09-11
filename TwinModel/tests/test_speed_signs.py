from shapely.geometry import LineString,Point
from twinmodel.model import TwinModel,Road,RoadLink,Lane,Signal
from twinmodel.speed_signs import redundant_speed_signals


def chain():
    m=TwinModel('test',0,0,(0,0,1,1))
    for i in range(3):
        rid='r'+str(i)
        r=Road(rid,LineString([(i*4,0),(i*4+4,0)]),[Lane(-1,speed_limit=10/3.6)],name='Street')
        if i:r.predecessor=RoadLink('road','r'+str(i-1),'end')
        if i<2:r.successor=RoadLink('road','r'+str(i+1),'start')
        m.roads.append(r);m.signals.append(Signal('s'+str(i),'speed_limit',rid,0,-4,Point(i*4,-4),value=10/3.6))
    return m


def test_same_limit_continuation_keeps_entry_and_is_idempotent():
    m=chain();assert redundant_speed_signals(m)==['s1','s2']
    m.signals=m.signals[:1];assert redundant_speed_signals(m)==[]


def test_speed_changes_junction_entries_and_explicit_signs_are_retained():
    m=chain();m.roads[0].lanes[0].speed_limit=30/3.6
    assert redundant_speed_signals(m)==['s2']
    m=chain();m.roads[1].predecessor=RoadLink('junction','j1')
    assert redundant_speed_signals(m)==['s2']
    m=chain();m.signals[1].osm_node_id=123
    assert redundant_speed_signals(m)==['s2']


def test_closed_loop_retains_one_sign_on_repeated_runs():
    m=chain();m.roads[0].predecessor=RoadLink('road','r2','end');m.roads[2].successor=RoadLink('road','r0','start')
    removed=redundant_speed_signals(m);assert removed==['s1','s2']
    m.signals=[s for s in m.signals if s.id not in removed]
    assert redundant_speed_signals(m)==[]
