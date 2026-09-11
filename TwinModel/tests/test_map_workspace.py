"""Integration contracts: preserve saved plans; invalidate dependencies; serialize bakes."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from shapely.geometry import Point, LineString, box
from twinmodel.model import TwinModel, Surface, CurbLine, PointObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from map_workspace import MapWorkspace


@pytest.fixture
def workspace(tmp_path):
    build = tmp_path/'build'; build.mkdir()
    model = TwinModel(name='test', origin_lat=41, origin_lon=2, bbox_wgs84=(40,1,42,3))
    model.surfaces = [Surface('road','drivable',box(0,-8,120,0)),Surface('walk','sidewalk',box(0,0,120,8),z_offset=.15)]
    model.curbs = [CurbLine('curb',LineString([(0,0),(120,0)]))]
    model.objects = [PointObject('tree1','tree',Point(60,3),1)]
    twin=build/'test.twin';model.save(twin)
    poles=tmp_path/'poles.json';poles.write_text('[]')
    store=SimpleNamespace(build_dir=build,twin_dir=twin,name='test',corr=SimpleNamespace(ops=[]))
    args=SimpleNamespace(vegetation_output=str(tmp_path/'veg'),furniture_output=str(tmp_path/'furn'),
                         poles=str(poles),engine=None,project=None,level=None,region='test')
    return MapWorkspace(store,args),args,model


def test_restart_preserves_plans_and_validated_furniture(workspace):
    w,args,_=workspace
    files={name:(tool.output/'plan.json').read_bytes() for name,tool in w.tools.items()}
    again=MapWorkspace(w.store,args)
    assert files=={name:(tool.output/'plan.json').read_bytes() for name,tool in again.tools.items()}
    assert not any(s['stale'] for s in again.state()['tools'].values())


def test_vegetation_change_invalidates_furniture_across_restart(workspace):
    w,args,_=workspace
    cfg=copy.deepcopy(w.tools['vegetation'].config)
    cfg['overrides']['tree1']={'position':[25,3]}
    s=w.preview('vegetation',cfg)
    assert not s['tools']['vegetation']['stale']
    assert s['tools']['furniture']['stale']
    w=MapWorkspace(w.store,args)
    assert w.state()['tools']['furniture']['stale']
    s=w.preview('furniture',w.tools['furniture'].config)
    assert not s['tools']['furniture']['stale']
    assert w.tools['furniture'].vegetation==w.tools['vegetation'].result


def test_invalid_change_preserves_saved_configuration_and_plan(workspace):
    w,_,_=workspace;tool=w.tools['vegetation']
    before=[(tool.output/name).read_bytes() for name in ('config.json','plan.json')]
    with pytest.raises(ValueError):w.preview('vegetation',{'row_spacing_m':0})
    assert before==[(tool.output/name).read_bytes() for name in ('config.json','plan.json')]


def test_bake_blocks_all_mutations(workspace):
    w,_,_=workspace
    w.tools['vegetation'].process=SimpleNamespace(poll=lambda:None)
    for action in (lambda:w.preview('furniture',{}),lambda:w.bake('furniture'),lambda:w.set_ops([],True),w.rebuild):
        with pytest.raises(ValueError,match='bake is running'):action()


def test_layout_edits_require_model_rebuild(workspace):
    w,args,_=workspace
    w.store.corr.ops=[{'op':'test'}]
    assert w.state()['layout_pending']
    with pytest.raises(ValueError,match='Layout annotations changed'):w.preview('vegetation',{})
    with pytest.raises(ValueError,match='out of date'):w.bake('furniture')
    assert MapWorkspace(w.store,args).state()['layout_pending']


def test_external_model_change_invalidates_both_tools(workspace):
    w,_,model=workspace
    model.objects.append(PointObject('tree2','tree',Point(90,3),2));model.save(w.store.twin_dir)
    assert all(s['stale'] for s in w.state()['tools'].values())
    with pytest.raises(ValueError,match='vegetation'):w.preview('furniture',{})
    w.preview('vegetation',w.tools['vegetation'].config)
    assert any(r['id']=='tree2' for r in w.tools['vegetation'].result['review'])


def test_pole_change_invalidates_and_reloads(workspace):
    w,_,_=workspace;tool=w.tools['vegetation']
    Path(tool.args.poles).write_text(json.dumps([{'x':60,'y':-3,'layer':0}]))
    assert w.state()['tools']['vegetation']['stale']
    w.preview('vegetation',tool.config)
    assert next(r for r in tool.result['review'] if r['id']=='tree1')['status']=='rejected'


def test_http_routes_and_origin_guard(workspace):
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from twin_editor import EditorHandler
    w,_,_=workspace
    w.store.workspace=w
    handler=type('TestHandler',(EditorHandler,),{'store':w.store})
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        state=json.load(urllib.request.urlopen(base+'/api/planning'))
        assert set(state['tools'])=={'vegetation','furniture'}
        page=urllib.request.urlopen(base).read().decode()
        assert 'workspace-tabs' in page and 'planning-panel' in page
        for origin,status in [('http://evil.invalid',403),(base,400)]:
            req=urllib.request.Request(base+'/api/planning/vegetation/preview',
                data=b'{"row_spacing_m":0}',headers={'Origin':origin,'Content-Type':'application/json'})
            with pytest.raises(urllib.error.HTTPError) as e:urllib.request.urlopen(req)
            assert e.value.code==status
    finally:
        server.shutdown();server.server_close();thread.join()
