import copy
import json
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box, mapping

from twinmodel.model import TwinModel, Road, Surface, PointObject, Building
from twinmodel.vegetation import configuration, plan


def model():
    m=TwinModel(name='test',origin_lat=41,origin_lon=2,bbox_wgs84=(40,1,42,3))
    m.roads=[Road(id='r',reference_line=LineString([(0,0),(100,0)]),lanes=[])]
    m.surfaces=[Surface('soil','ground',box(0,10,100,100)),Surface('road','drivable',box(0,-4,100,4))]
    m.objects=[PointObject('tree1','tree',Point(20,20),1),PointObject('tree2','tree',Point(40,40),2)]
    return m


def test_repeatable_and_preserves_source_geometry():
    m=model();before=[o.position.wkt for o in m.objects]
    a,b=plan(m),plan(m)
    assert a==b
    assert len(a['points'])==2
    assert [o.position.wkt for o in m.objects]==before
    assert a['points'][0]['x']==20 and a['points'][0]['y']==-20
    assert {r['source'] for r in a['points']}=={'osm'}


def test_existing_points_keep_variation_when_unrelated_tree_added():
    m=model();a={r['id']:r for r in plan(m)['points']}
    m.objects.append(PointObject('tree9','tree',Point(70,70),9))
    b={r['id']:r for r in plan(m)['points']}
    assert a['tree1']==b['tree1'] and a['tree2']==b['tree2']


def test_road_conflict_is_reported_not_snapped():
    m=model();m.objects[0].position=Point(20,0)
    p=plan(m);r=next(r for r in p['review'] if r['id']=='tree1')
    assert r['status']=='rejected' and r['position']==[20,0]
    assert m.objects[0].position==Point(20,0)


def test_signal_clearance_and_crossing_access():
    m=model();m.surfaces.append(Surface('cross','crossing',box(38,39,42,41)))
    p=plan(m,poles=[{'x':20,'y':-20,'layer':0}])
    assert {r['reason'] for r in p['review']}=={'signal_or_pole_clearance','crossing_access'}


def test_building_canopy_not_only_trunk():
    m=model();m.buildings=[Building(id='b',footprint=box(22,18,25,22))]
    p=plan(m);assert p['review'][0]['reason']=='building_or_crown_clearance'


def test_manual_move_disable_and_exclusion_use_same_validation():
    m=model();cfg={'overrides':{'tree1':{'position':[20,0]},'tree2':{'disabled':True},
                              'manual:1':{'position':[70,70]}},'exclusions':[mapping(box(69,69,71,71))]}
    p=plan(m,cfg);assert not p['points']
    assert {r['reason'] for r in p['review']}=={'insufficient_planting_surface','disabled_by_user','user_exclusion'}
    assert p['review'][0]['original_position']==[20,20]


def test_zone_sampling_bounded_deterministic_and_within_zone():
    m=model();g=box(50,50,60,60)
    cfg={'include_observed':False,'zones':[{'id':'g','preset':'grass','density':1,'geometry':mapping(g)}]}
    a=plan(m,cfg);assert a==plan(m,cfg) and len(a['points'])>30
    for r in a['points']:assert g.covers(Point(r['position']).buffer(r['pit_radius_m']))
    for i,a1 in enumerate(a['points']):
        for b1 in a['points'][i+1:]:assert Point(a1['position']).distance(Point(b1['position']))>=.7
    cfg['zones'][0]['density']=0
    assert not plan(m,cfg)['points']


def test_ground_cover_can_grow_under_tree_crown_but_not_trunk():
    m=model();m.objects=m.objects[:1]
    cfg={'zones':[{'id':'g','preset':'grass','geometry':mapping(box(17,17,23,23))}]}
    p=plan(m,cfg);grass=[r for r in p['points'] if r['kind']=='grass'];assert grass
    assert min(Point(r['position']).distance(Point(20,20)) for r in grass)<3
    assert all(Point(r['position']).distance(Point(20,20))>=.6+.15+.25 for r in grass)


def test_layer_separation():
    m=model();m.objects[0].tags['layer']=1
    r=plan(m)['review'][0];assert r['reason']=='no_surface_on_layer'


def test_narrow_sidewalk_preserves_pedestrian_access():
    m=model();m.surfaces=[Surface('walk','sidewalk',box(0,4,100,6)),Surface('road','drivable',box(0,-4,100,4))]
    m.objects=[PointObject('t','tree',Point(20,5.2))]
    assert plan(m,{'sidewalk_footing':None})['review'][0]['reason']=='pedestrian_corridor'


@pytest.mark.parametrize('cfg',[{'row_spacing_m':0},{'road_clearance_m':-1},{'max_candidates':float('inf')},
                               {'zones':[{'id':'z','preset':'grass','geometry':mapping(box(0,0,10,10)),'density':2}]}])
def test_invalid_settings_fail(cfg):
    with pytest.raises(ValueError):configuration(cfg)


def test_candidate_budget_not_silently_truncated():
    with pytest.raises(ValueError,match='budget'):
        plan(model(),{'max_candidates':10,'zones':[{'id':'big','preset':'grass','geometry':mapping(box(10,10,100,100))}]})


def test_street_rows_use_sidewalk_and_leave_walking_corridor():
    from twinmodel.model import CurbLine
    m=model();m.objects=[]
    m.surfaces=[Surface('walk','sidewalk',box(0,4,100,10)),Surface('road','drivable',box(0,-4,100,4))]
    m.curbs=[CurbLine('c',LineString([(0,4),(100,4)]))]
    a=plan(m,{'street_rows':True})
    assert len(a['points'])>=9
    assert all(p['source']=='inferred_row' and p['position'][1]>4 for p in a['points'])
    assert a==plan(m,{'street_rows':True})


def test_custom_large_pits_reject_mixed_kind_overlap_across_cells():
    from twinmodel.vegetation import Planner
    m=model();m.objects=[]
    cfg=configuration()
    cfg['presets']['wide']=dict(cfg['presets']['deciduous'],pit_radius_m=12,spacing_m=1,scale=[1,1])
    cfg['presets']['wide_grass']=dict(cfg['presets']['grass'],pit_radius_m=12,spacing_m=1,scale=[1,1])
    p=Planner(m,cfg)
    p.consider('a',Point(20,40),'wide','manual')
    p.consider('b',Point(43,40),'wide_grass','inferred_zone',zone=box(0,10,100,100))
    assert p.records[0]['status']=='accepted'
    assert p.records[1]['reason']=='vegetation_spacing'


def test_sidewalk_footing_reserves_full_size_and_skips_natural_ground():
    m=model();m.surfaces.append(Surface('walk','sidewalk',box(0,4,100,10)))
    m.objects=[PointObject('street','tree',Point(20,5.5)),PointObject('park','tree',Point(40,40))]
    p=plan(m);by_id={r['id']:r for r in p['points']}
    assert by_id['street']['footing']['scale']==.8
    assert by_id['street']['pit_radius_m']==pytest.approx(1.12*.8)
    assert 'footing' not in by_id['park']
    assert p['summary']['footings']==1


def test_footing_clearance_rejects_location_that_only_fits_trunk():
    m=model();m.surfaces.append(Surface('walk','sidewalk',box(0,4,100,10)))
    m.objects=[PointObject('street','tree',Point(20,5.1))]
    assert plan(m,{'sidewalk_footing':None})['points']
    assert plan(m)['review'][0]['reason']=='road_or_cycle_lane'


def test_footing_not_scaled_by_random_tree_growth():
    m=model();m.surfaces.append(Surface('walk','sidewalk',box(0,4,100,10)))
    m.objects=[PointObject('street','tree',Point(20,5.5))]
    a=plan(m,{'seed':1})['points'][0];b=plan(m,{'seed':2})['points'][0]
    assert a['scale']!=b['scale'] and a['footing']==b['footing']
