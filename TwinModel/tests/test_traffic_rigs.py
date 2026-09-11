import copy
import json
from pathlib import Path

import pytest
from ue.traffic_rigs import pick_rig, fit_mast

RIGS=Path(__file__).parents[1]/'ue/rigs'


def signal(side=10):
    return {'id':'sig','kind':'through','n_driving_lanes':3,'yaw':0,
            'placement':{'x':0,'y':side,'z':0},
            'lane_centers':[{'x':0,'y':y,'z':0,'lane_id':i} for i,y in enumerate((1,4,7))]}


def test_european_layout_selection_and_missing_geometry_fallback():
    rigs={n:n for n in ('eu_pole','eu_pole_repeater','eu_mast_2head')}
    assert pick_rig({'n_driving_lanes':1},rigs,'eu','eu_pole')=='eu_pole'
    assert pick_rig({'n_driving_lanes':2},rigs,'eu','eu_pole')=='eu_pole_repeater'
    assert pick_rig(signal(),rigs,'eu','eu_pole')=='eu_mast_2head'
    assert pick_rig({'n_driving_lanes':5},rigs,'eu','eu_pole')=='eu_pole_repeater'


@pytest.mark.parametrize('side,expected',[(10,[-9,-6,-3]),(-2,[3,6,9])])
def test_mast_heads_cover_lanes_from_either_support_side(side,expected):
    template=json.loads((RIGS/'eu_mast_2head.json').read_text());before=copy.deepcopy(template)
    result,info=fit_mast(template,signal(side))
    arm=result['Poles'][1]
    xs=[(arm['Transform']['Location']['X']+h['Transform']['Location']['X'])/100 for h in arm['Heads']]
    assert xs==pytest.approx(expected)
    assert len(arm['Heads']) == len(signal(side)['lane_centers'])
    assert info['minimum_lamp_height_m']>=5.5
    assert template==before
    assert all(h['SignalID']=='@through' and h['Style']=='European' for p in result['Poles'] for h in p['Heads'])


def test_invalid_mast_geometry_is_rejected():
    template=json.loads((RIGS/'eu_mast_2head.json').read_text())
    with pytest.raises(ValueError,match='between'):fit_mast(template,signal(4))
    s=signal();s['lane_centers'][0]['x']=20
    with pytest.raises(ValueError,match='too far'):fit_mast(template,s)
    s=signal();s['placement']['y']=30
    with pytest.raises(ValueError,match='20 m'):fit_mast(template,s)


def test_mast_rejects_nonfinite_and_insufficient_road_clearance():
    template=json.loads((RIGS/'eu_mast_2head.json').read_text())
    s=signal();s['lane_centers'][0]['z']=float('nan')
    with pytest.raises(ValueError,match='Non-finite'):fit_mast(template,s)
    s=signal();s['lane_centers'][0]['z']=1
    with pytest.raises(ValueError,match='elevation'):fit_mast(template,s)


@pytest.mark.parametrize('count',[2,5])
def test_every_controlled_lane_gets_its_own_overhead_head(count):
    template=json.loads((RIGS/'eu_mast_2head.json').read_text())
    s=signal(-2)
    s['lane_centers']=[{'lane_id':-i-1,'x':0,'y':i*3,'z':0,'signal_id':'lane_'+str(i)} for i in range(count)]
    s['n_driving_lanes']=count
    result,info=fit_mast(template,s)
    assert len(result['Poles'][1]['Heads'])==count
    assert [h['SignalID'] for h in result['Poles'][1]['Heads']]==['lane_'+str(i) for i in range(count)]
    assert info['lane_ids']==[-i-1 for i in range(count)]
    assert info['head_offsets_m']==pytest.approx([2+i*3 for i in range(count)])
