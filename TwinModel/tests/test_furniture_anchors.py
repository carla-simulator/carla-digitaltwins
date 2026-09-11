import pytest
from pyproj import Transformer
from shapely.geometry import Point,shape
from .test_furniture import street
from twinmodel.furniture import plan,configuration
from twinmodel.furniture_anchors import import_osm


def test_shelter_uses_source_anchor_and_preserves_glass_attachment():
    a={'id':'stop1','kind':'bus_shelter','position':[30,.7],'source':'manual','layer':0}
    p=plan(street(),{'anchors':[a],'infer_rest_groups':False})
    assert p['summary']['groups']==1
    group=p['review'][0];root,glass=group['members']
    assert group['anchor']['position']==[30,.7]
    assert group['position']==[30,2.92]
    assert root['kind']=='bus_shelter' and glass['kind']=='bus_glass'
    assert glass['parent_id']==root['id'] and glass['attachment_m']==[0,0,0]
    assert glass['z']==root['z'] and glass['min_m'][2]>.4


def test_no_shelter_tag_gets_supported_panel_only():
    a={'id':'stop1','kind':'bus_stop','position':[30,.7],'source':'osm','tags':{'shelter':'no'}}
    p=plan(street(),{'anchors':[a],'infer_rest_groups':False})
    assert {m['kind'] for m in p['points']}=={'bus_pole','bus_panel'}
    panel=next(m for m in p['points'] if m['kind']=='bus_panel')
    assert panel['attachment_m']==[0,.025,2.05]
    pole=next(m for m in p['points'] if m['kind']=='bus_pole')
    assert panel['yaw']==pole['yaw']
    with pytest.raises(ValueError):configuration({'anchors':[{**a,'kind':'bus_shelter'}]})


def test_boarding_area_and_network_reject_narrow_platform():
    a={'id':'stop1','kind':'bus_shelter','position':[30,.7],'source':'manual'}
    p=plan(street(3.2),{'anchors':[a],'infer_rest_groups':False})
    assert p['points']==[]


def test_banner_includes_real_pole_and_checked_mount_height():
    a={'id':'b1','kind':'banner','position':[30,3],'source':'manual','yaw':0}
    p=plan(street(10),{'anchors':[a],'infer_rest_groups':False})
    assert len(p['points'])==2
    pole,banner=p['points']
    assert banner['parent_id']==pole['id']
    assert banner['attachment_m'][2]+banner['min_m'][2]-pole['min_m'][2]>2.5
    with pytest.raises(ValueError):configuration({'anchors':[{k:v for k,v in a.items() if k!='yaw'}]})


def test_observed_anchors_win_over_inferred_rest_groups():
    a={'id':'stop1','kind':'bus_shelter','position':[30,.7],'source':'manual'}
    p=plan(street(),{'anchors':[a]})
    stop=next(g for g in p['review'] if g['id']=='anchor:stop1')
    assert stop['status']=='accepted'
    assert all(not shape(stop['occupied']).intersects(shape(g['occupied'])) for g in p['review']
               if g['status']=='accepted' and g['id']!=stop['id'])


def test_osm_import_deduplicates_and_reports_unknown_shelter():
    m=street();m.bbox_wgs84=(40,1,42,3)
    node={'id':123,'type':'node','lat':41,'lon':2,'tags':{'highway':'bus_stop','shelter':'yes','name':'Test'}}
    unknown={**node,'id':124,'tags':{'highway':'bus_stop'}}
    result=import_osm(m,{'elements':[node,node,unknown]})
    assert len(result['anchors'])==1 and len(result['review'])==1
    assert result['anchors'][0]['source_url'].endswith('/node/123')
    assert result['anchors'][0]['position']==[0,0]


def test_anchor_move_still_checks_entire_group():
    a={'id':'stop1','kind':'bus_shelter','position':[30,.7],'source':'manual'}
    p=plan(street(),{'anchors':[a],'infer_rest_groups':False,'overrides':{'anchor:stop1':{'position':[30,-5]}}})
    assert not p['points']


def test_boarding_space_is_walkable_but_reserved_from_other_furniture():
    a={'id':'stop','kind':'bus_shelter','position':[30,.7],'source':'manual'}
    p=plan(street(4.8),{'anchors':[a],'infer_rest_groups':False})
    assert len(p['points'])==2


def test_bounded_fit_preserves_source_and_avoids_existing_pole():
    a={'id':'stop','kind':'bus_stop','position':[30,-.3],'source':'manual'}
    p=plan(street(),{'anchors':[a],'infer_rest_groups':False},poles=[{'x':30,'y':-.85}])
    assert len(p['points'])==2
    g=p['review'][0]
    assert g['anchor']['position']==[30,-.3]
    assert 0<g['anchor']['fit_displacement_m']<=6
    assert g['anchor']['fit_station_offset_m']!=0
    assert len({r['id'] for r in p['review']})==len(p['review'])


def test_unknown_layer_produces_one_review_record_per_anchor():
    a={'id':'stop','kind':'bus_stop','position':[30,.7],'source':'manual','layer':99}
    p=plan(street(),{'anchors':[a],'infer_rest_groups':False})
    assert len(p['review'])==1 and not p['points']
