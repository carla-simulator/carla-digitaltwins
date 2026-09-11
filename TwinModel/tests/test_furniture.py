import copy
import pytest
from shapely.geometry import LineString, box, Point, shape, mapping
from twinmodel.model import TwinModel, Surface, CurbLine
from twinmodel.furniture import plan, configuration


def street(width=7):
    m=TwinModel(name='furniture-test',origin_lat=41,origin_lon=2,bbox_wgs84=(40,1,42,3))
    m.surfaces=[Surface('road','drivable',box(0,-8,120,0)),Surface('walk','sidewalk',box(0,0,120,width),z_offset=.15)]
    m.curbs=[CurbLine('curb',LineString([(0,0),(120,0)]))]
    return m


def test_groups_repeat_and_have_complete_supported_members():
    m=street();a=plan(m);assert a==plan(m);assert a['points']
    for g in a['review']:
        if g['status']!='accepted':continue
        assert {p['kind'] for p in g['members']}=={'bench','bin'}
        assert m.surfaces[1].geometry.covers(shape(g['occupied']))
        for p in g['members']:
            assert shape(p['footprint']).distance(m.surfaces[0].geometry)>=.45
            assert p['scale']==1 and p['z']==.15


def test_split_reversed_curbs_preserve_output():
    m=street();a=plan(m)
    m.curbs=[CurbLine('a',LineString([(120,0),(60,0)])),CurbLine('b',LineString([(60,0),(0,0)]))]
    b=plan(m)
    assert a==b


def test_narrow_walkway_and_crossing_remain_clear():
    assert not plan(street(2.5))['points']
    m=street();m.surfaces.append(Surface('cross','crossing',box(45,-8,55,7)))
    for g in plan(m)['review']:
        if g['status']=='accepted':assert shape(g['occupied']).distance(m.surfaces[-1].geometry)>=5


def test_existing_tree_base_and_pole_reserve_access():
    m=street();first=next(g for g in plan(m)['review'] if g['status']=='accepted');x,y=first['position']
    result=plan(m,poles=[dict(x=x,y=-y)],vegetation={'points':[dict(x=x+1,y=-y,pit_radius_m=1,layer=0)]})
    assert next(g for g in result['review'] if g['id']==first['id'])['reason']=='existing_obstacle_or_access'


def test_manual_moves_are_validated_and_group_disable_is_atomic():
    m=street();g=next(g for g in plan(m)['review'] if g['status']=='accepted')
    for override in [dict(position=[20,-2]),dict(disabled=True)]:
        result=plan(m,{'overrides':{g['id']:override}})
        assert not any(p['group_id']==g['id'] for p in result['points'])
        assert next(r for r in result['review'] if r['id']==g['id'])['status']=='rejected'


def test_layer_isolation_and_neighbor_region_occupancy():
    m=street();a=plan(m)
    assert not plan(m,other_plans=[a])['points'] or all(
        not shape(g['occupied']).intersects(shape(h['occupied']))
        for g in plan(m,other_plans=[a])['review'] if g['status']=='accepted'
        for h in a['review'] if h['status']=='accepted')
    m.surfaces[1].tags['layer']=1
    assert not plan(m)['points']


def test_no_split_in_pedestrian_network():
    m=street(4)
    # An existing obstruction creates a narrow passage. Groups cannot close it.
    cfg={'exclusions':[mapping(box(40,2,80,4))]}
    result=plan(m,cfg)
    for g in result['review']:
        if g['status']=='accepted':assert not shape(g['occupied']).intersects(box(40,0,80,2))


@pytest.mark.parametrize('cfg',[{'group_spacing_m':0},{'seed':1.2},{'max_support_delta_m':float('nan')},{'invented':True}])
def test_invalid_config(cfg):
    with pytest.raises(ValueError):configuration(cfg)


def test_tiny_disconnected_surface_does_not_reject_unrelated_groups():
    m=street();baseline=plan(m)['points']
    m.surfaces.append(Surface('fragment','sidewalk',box(200,200,201.81,201.81)))
    assert plan(m)['points']==baseline


def test_framework_cli_writes_loadable_plan(tmp_path):
    import json
    from twinmodel.cli import main
    m=street();m.save(tmp_path/'map.twin')
    (tmp_path/'poles.json').write_text('[]')
    (tmp_path/'vegetation.json').write_text('{"points":[]}')
    result=main(['furniture','--twin',str(tmp_path/'map.twin'),'--out',str(tmp_path/'plan.json'),
                 '--poles',str(tmp_path/'poles.json'),'--vegetation',str(tmp_path/'vegetation.json')])
    assert result==0
    assert json.loads((tmp_path/'plan.json').read_text())['points']


def test_studio_failed_edit_preserves_saved_plan(tmp_path):
    import runpy
    from types import SimpleNamespace
    from pathlib import Path
    module=runpy.run_path(str(Path(__file__).parents[1]/'tools/furniture_tool.py'))
    street().save(tmp_path/'map.twin')
    args=SimpleNamespace(twin=tmp_path/'map.twin',output=tmp_path/'studio',config=None,poles=None,
                         vegetation=None,occupied_plan=[],region='test',engine=None,project=None,level=None)
    workspace=module['Workspace'](args)
    before=(tmp_path/'studio/plan.json').read_bytes()
    with pytest.raises(ValueError):workspace.regenerate({'group_spacing_m':-1})
    assert (tmp_path/'studio/plan.json').read_bytes()==before
    assert module['Workspace'](args).result==workspace.result
