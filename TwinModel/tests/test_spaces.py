"""Survey boundaries: reject invalid geometry and export coordinates without approximation."""
import pytest
from twinmodel import corrections
from twinmodel.spaces import feature_collection


def space():
    return {'id':'c1','op':'space.set','kind':'parking',
            'left':[[2.0,41.0],[2.0001,41.0],[2.0002,41.00001]],
            'right':[[2.0,41.00002],[2.0002,41.00003]],
            'provenance':{'osm_way_ids':[1025549398]},'replaces':'osm:1025549398:8'}


@pytest.mark.parametrize("kind", ["parking", "bus", "taxi", "bike_crosswalk"])
def test_lossless_boundaries_and_no_osm_reclassification(tmp_path, kind):
    op=space();op["kind"]=kind
    assert corrections.validate([op])==[]
    raw={'elements':[{'type':'way','id':1025549398,'nodes':[1,2],'tags':{'highway':'cycleway'}}]}
    patched,report=corrections.apply_osm(raw,[op])
    assert patched==raw and not report['unmatched']
    c=corrections.Corrections(name='test',ops=[op]);c.save(tmp_path/'corrections.json')
    assert corrections.load(tmp_path/'corrections.json').ops==[op]
    fc=feature_collection([op])
    assert fc['features'][0]['properties']['left']==op['left']
    assert fc['features'][0]['properties']['right']==op['right']
    assert fc['features'][0]['geometry']['coordinates'][0]==op['left']+list(reversed(op['right']))+[op['left'][0]]
    op['disabled']=True
    assert feature_collection([op])['features']==[]


@pytest.mark.parametrize('change',[
    {'left':[[2,41]]}, {'right':[[2,41],[float('nan'),41]]},
    {'left':[[2,41],[2,42]],'right':[[2,42],[2,41]]},
    {'left':[[2,41],[3,42]],'right':[[3,41],[2,42]]},
    {'kind':'cycleway'}, {'right':[[200,41],[201,41]]},
])
def test_invalid_boundaries_rejected(change):
    op=space();op.update(change)
    assert corrections.validate([op])
    with pytest.raises(ValueError):feature_collection([op])


def test_model_annotation_export_and_reload(tmp_path):
    import json
    from twinmodel.model import TwinModel
    fc=feature_collection([space()])
    m=TwinModel(name='spaces',origin_lat=41,origin_lon=2,bbox_wgs84=(40,1,42,3))
    m.metadata['space_annotations']=fc
    m.save(tmp_path/'twin')
    assert json.loads((tmp_path/'twin/spaces.geojson').read_text())==fc
    assert TwinModel.load(tmp_path/'twin').metadata['space_annotations']==fc


def test_deleted_space_preserves_source_and_bounds():
    op=space();op['deleted']=True
    assert corrections.validate([op])==[]
    f=feature_collection([op])['features'][0]
    assert f['properties']['deleted'] is True
    assert f['properties']['replaces']==op['replaces']
    assert f['properties']['left']==op['left']
