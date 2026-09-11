import math
from shapely.geometry import LineString, Point
from twinmodel.model import TwinModel,Road,Lane,Signal,Junction,Connection
from twinmodel.signal_coverage import complete_wide_crossings


def model():
    m=TwinModel('test',0,0,(0,0,1,1))
    m.junctions=[Junction('j1',tags={'kind':'intersection'},connections=[
        Connection('c1','a','ca'),Connection('c2','b','cb')])]
    for rid,h in [('a',0),('b',math.pi/2)]:
        m.roads.append(Road(rid,LineString([(0,0),(10,0)]),[Lane(-1),Lane(-2)]))
        m.signals.append(Signal('s'+rid,'yield',rid,10,-7,Point(10,-7),heading=h,
                               validities=[[-2,-1]],tags={'junction_id':'j1','source':'unsignalised_control'}))
    return m


def test_inferred_crossing_is_complete_separate_stages_and_idempotent():
    m=model();positions=[s.position for s in m.signals]
    assert complete_wide_crossings(m)==[]
    report=complete_wide_crossings(m,True)
    assert len(report)==1
    assert all(s.kind=='traffic_light' and s.tags['inferred'] for s in m.signals)
    assert [s.position for s in m.signals]==positions
    assert len(m.controllers)==2
    assert all(len(c.signal_ids)==1 for c in m.controllers)
    assert complete_wide_crossings(m,True)==[]


def test_explicit_controls_narrow_roads_and_continuations_are_preserved():
    for change in ('explicit','narrow','parallel','missing','gore'):
        m=model()
        if change=='explicit':m.signals[0].tags['source']='osm'
        if change=='narrow':m.roads[0].lanes=m.roads[0].lanes[:1]
        if change=='parallel':m.signals[1].heading=math.pi
        if change=='missing':m.signals.pop()
        if change=='gore':m.junctions[0].tags['kind']='gore'
        assert complete_wide_crossings(m,True)==[],change
        assert not m.controllers
