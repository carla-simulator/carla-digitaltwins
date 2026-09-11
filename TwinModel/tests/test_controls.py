import json
import pytest
from twinmodel import corrections
from twinmodel.controls import feature_collection
from twinmodel.model import TwinModel


def control(kind='traffic_sign'):
    c={'op':'control.set','id':'c1','kind':kind,'type':'stop','geometry':{'type':'Point','coordinates':[2.166,41.392]},'bearing':90,'target':{'road_id':'r18','lane_id':-1}}
    if kind=='traffic_light':c['type']='pedestrian'
    if kind in ('stop_line','crosswalk','bike_crosswalk'):
        c['geometry']={'type':'LineString','coordinates':[[2.166,41.392],[2.1661,41.3921]]};c['width_m']=3 if kind=='crosswalk' else .3
        if kind=='crosswalk':c['type']='zebra'
        if kind=='bike_crosswalk':c['type']='marked';c['width_m']=2
    return c


@pytest.mark.parametrize('kind',['traffic_sign','traffic_light','stop_line','crosswalk','bike_crosswalk'])
def test_control_round_trip(kind,tmp_path):
    c=control(kind);assert corrections.validate([c])==[]
    path=tmp_path/'corrections.json';corrections.Corrections(ops=[c]).save(path)
    assert corrections.load(path).ops==[c]
    m=TwinModel(name='controls',origin_lat=41,origin_lon=2,bbox_wgs84=(40,1,42,3))
    fc=feature_collection([c]);m.metadata['control_annotations']=fc;m.save(tmp_path/'twin')
    assert json.loads((tmp_path/'twin/controls.geojson').read_text())==fc
    assert TwinModel.load(tmp_path/'twin').metadata['control_annotations']==fc
    c['disabled']=True;assert feature_collection([c])['features']==[]


@pytest.mark.parametrize('change',[{'bearing':360},{'bearing':float('nan')},{'type':'unknown'},
    {'geometry':{'type':'Point','coordinates':[181,41]}},{'type':'speed_limit','value':0},
    {'type':'custom','label':''},{'target':{'lane_id':0}}])
def test_invalid_control(change):
    c=control();c.update(change);assert corrections.validate([c])
    with pytest.raises(ValueError):feature_collection([c])


def test_crossed_or_collapsed_segment():
    for coords in ([[2,41],[2,41]],[[2,41],[3,42],[3,41],[2,42]]):
        c=control('stop_line');c['geometry']['coordinates']=coords;assert corrections.validate([c])


def test_controls_do_not_silently_edit_osm():
    raw={'elements':[]};patched,report=corrections.apply_osm(raw,[control()]);assert patched==raw and not report['unmatched']


def test_deleted_control_preserves_explicit_removal_intent():
    c=control('stop_line');c.update(deleted=True,replaces='signal:original')
    assert corrections.validate([c])==[]
    f=feature_collection([c])['features'][0]
    assert f['properties']['deleted'] is True
    assert f['properties']['replaces']=='signal:original'
    assert f['geometry']==c['geometry']


@pytest.mark.parametrize('kind', ['crosswalk', 'bike_crosswalk'])
def test_crosswalk_outline_round_trip_and_validation(kind, tmp_path):
    c=control(kind)
    c['geometry']={'type':'Polygon','coordinates':[[[2,41],[2.001,41],[2.001,41.001],[2,41.001],[2,41]]]}
    assert corrections.validate([c])==[]
    corrections.Corrections(ops=[c]).save(tmp_path/'outline.json')
    loaded=corrections.load(tmp_path/'outline.json').ops
    assert feature_collection(loaded)['features'][0]['geometry']==c['geometry']
    c['geometry']['coordinates'][0]=[[2,41],[2.001,41.001],[2.001,41],[2,41.001],[2,41]]
    assert corrections.validate([c])
