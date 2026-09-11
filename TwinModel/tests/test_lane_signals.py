import copy
from tests.test_signal_coverage import model
from twinmodel.signal_coverage import complete_wide_crossings
from twinmodel.lane_signals import split_lane_signals


def test_lane_identities_validities_and_independent_stages():
    m=model();complete_wide_crossings(m,True)
    original=copy.deepcopy(m)
    assert split_lane_signals(m)==2
    assert len(m.signals)==len(m.controllers)==4
    assert len({s.controller_id for s in m.signals})==4
    for anchor in original.signals:
        group=[s for s in m.signals if s.tags['rig_anchor']==anchor.id]
        assert len(group)==2
        assert {tuple(s.validities[0]) for s in group}=={(-2,-2),(-1,-1)}
        assert all(s.position.equals(anchor.position) for s in group)
    assert split_lane_signals(m)==0
    assert len(m.controllers)==4


def test_default_eixample_build_exports_lane_scoped_shared_rigs():
    from pathlib import Path
    from lxml import etree
    from twinmodel import profiles
    from twinmodel.frame import LocalFrame
    from twinmodel.ingest.osm import load_fixture
    from twinmodel.lanegraph import build_lanegraph
    from twinmodel.export.xodr import export_xodr
    from twinmodel.refresh import graft_signals
    bbox=(41.3905,2.163,41.3945,2.169)
    with profiles.use('eu_dense'):
        m=build_lanegraph(load_fixture(Path(__file__).parent/'fixtures/eixample_overpass.json'),
                          LocalFrame.from_bbox(*bbox),bbox,name='eixample')
        xml=export_xodr(m)
    vehicles=[s for s in m.signals if s.kind=='traffic_light']
    assert len(vehicles)==84
    assert len({s.controller_id for s in vehicles})==84
    assert len({s.tags['rig_anchor'] for s in vehicles})==33
    assert all(len(s.validities)==1 and s.validities[0][0]==s.validities[0][1] for s in vehicles)
    # Refresh must retain the physical grouping even when grafting onto an older
    # geometry file that did not carry the metadata.
    old=etree.fromstring(xml.encode())
    for s in old.iter('signal'):
        for u in list(s.findall('userData')):s.remove(u)
    grafted,_=graft_signals(etree.tostring(old).decode(),xml)
    root=etree.fromstring(grafted.encode())
    assert len(root.findall(".//signal/userData[@code='rig_anchor']"))==84
