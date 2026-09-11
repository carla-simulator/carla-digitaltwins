from pathlib import Path

import pytest
import requests

from twinmodel.cli import main
from twinmodel.project import Project, read, write
from twinmodel.project_sources import acquire


def raw_map():
    nodes = [(1,41.0015,2.0002),(2,41.0015,2.002),(3,41.0015,2.0038),
             (4,41.0002,2.002),(5,41.0028,2.002)]
    elements = [dict(type='node', id=i, lat=lat, lon=lon) for i,lat,lon in nodes]
    elements += [dict(type='way',id=10,nodes=[1,2,3],tags={'highway':'residential','lanes':'2'}),
                 dict(type='way',id=11,nodes=[4,2,5],tags={'highway':'residential','lanes':'2'})]
    return {'elements': elements}


def test_coordinate_project_pins_first_fetch_and_rebuilds_offline(tmp_path, monkeypatch):
    from twinmodel.ingest import osm
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        return raw_map()
    monkeypatch.setattr(osm, 'fetch_overpass', fetch)
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a,**k: pytest.fail('Unexpected network access'))
    root = tmp_path/'map'
    assert main(['project','create',str(root),'--bbox','41','2','41.003','2.004',
                 '--profile','eu_dense','--no-dem','--no-imagery','--no-refine']) == 0
    p = Project(root)
    p.build()
    assert len(calls) == 1
    write(p.path('authoring/layout.json'), {'schema':'0.1','name':'map','ops':[
        {'id':'wider','op':'way.tags','way':10,'set':{'sidewalk:both:width':'7'}}]})
    p.build()
    assert len(calls) == 1
    assert p.status()['stages'][0]['state'] == 'current'
    write(p.path('sources/osm.json'), {'elements': []})
    with pytest.raises(ValueError, match='Pinned source'):
        p.build()


def test_explicit_refresh_keeps_previous_source_revision(tmp_path, monkeypatch):
    from twinmodel.ingest import osm
    monkeypatch.setattr(osm, 'fetch_overpass', lambda *a,**k: raw_map())
    root = tmp_path/'map'
    assert main(['project','create',str(root),'--bbox','41','2','41.003','2.004',
                 '--profile','eu_dense','--no-dem','--no-imagery','--no-refine']) == 0
    p = Project(root)
    acquire(p)
    before = p.path('sources/osm.json').read_bytes()
    acquire(p, refresh=True)
    backups = list(p.path('runs').glob('source-*/osm.json'))
    assert len(backups) == 1 and backups[0].read_bytes() == before
