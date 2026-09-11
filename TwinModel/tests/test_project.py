import json
from pathlib import Path

import pytest

from twinmodel.cli import main
from twinmodel.model import TwinModel
from twinmodel.project import Project, checksum, locked, write


@pytest.fixture
def project(tmp_path):
    source = tmp_path/'source.twin'
    TwinModel(name='sample', origin_lat=41, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(source)
    root = tmp_path/'project'
    assert main(['project', 'create', str(root), '--from-twin', str(source)]) == 0
    return Project(root)


def test_snapshot_build_noop_and_output_tamper(project):
    project.build('model')
    assert project.status()['stages'][0]['state'] == 'current'
    count = len(list((project.root/'runs').iterdir()))
    project.build('model')
    # A no-op records a run but performs no stage work.
    assert len(list((project.root/'runs').iterdir())) == count + 1
    assert len(list((project.root/'runs').glob('*/model.json'))) == 1
    (project.root/'build/model/project.xodr').write_text('changed')
    assert project.status()['stages'][0]['state'] == 'stale'
    project.build('model')
    assert project.status()['stages'][0]['state'] == 'current'


def test_dry_run_does_not_write_or_fetch(tmp_path, monkeypatch):
    root = tmp_path/'osm'
    assert main(['project', 'create', str(root), '--bbox', '41', '2', '42', '3']) == 0
    from twinmodel.ingest import osm
    monkeypatch.setattr(osm, 'fetch_overpass', lambda *a, **k: pytest.fail('network called'))
    before = checksum(root)
    Project(root).build(dry_run=True)
    assert checksum(root) == before


def test_layout_edit_on_snapshot_preserves_previous_output(project):
    project.build('model')
    before = checksum(project.root/'build/model')
    write(project.root/'authoring/layout.json', {'schema': '0.1', 'name': 'project', 'ops': [{'op': 'changed'}]})
    with pytest.raises(ValueError, match='original OSM recipe'):
        project.build('model')
    assert checksum(project.root/'build/model') == before
    assert project.status()['stages'][0]['state'] == 'stale'


def test_failed_stage_keeps_last_success(project, monkeypatch):
    project.build('model')
    previous = checksum(project.root/'build/model')
    (project.root/'sources/model.xodr').write_text('invalidate')
    def fail(work):
        (work/'partial').write_text('partial')
        raise ValueError('injected failure')
    monkeypatch.setattr(project, 'build_model', fail)
    with pytest.raises(ValueError, match='injected'):
        project.build('model')
    assert checksum(project.root/'build/model') == previous
    assert any(json.loads(p.read_text())['state'] == 'failed'
               for p in (project.root/'runs').glob('*/model.json'))


def test_concurrent_writer_rejected(project):
    with locked(project.root):
        with pytest.raises(ValueError, match='busy'):
            project.build('model')


def test_adoption_preserves_bytes_and_refuses_overwrite(tmp_path):
    build = tmp_path/'old'
    build.mkdir()
    TwinModel(name='sample', origin_lat=41, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(build/'sample.twin')
    corrections = tmp_path/'corrections.json'
    write(corrections, {'schema': '0.1', 'name': 'sample', 'ops': []})
    furniture = tmp_path/'furniture'
    write(furniture/'config.json', {'seed': 42})
    write(furniture/'plan.json', {'points': []})
    before = checksum(build), checksum(furniture), checksum(corrections)
    root = tmp_path/'adopted'
    cmd = ['project', 'adopt', str(root), '--name', 'sample', '--build-dir', str(build),
           '--corrections', str(corrections), '--furniture', str(furniture)]
    assert main(cmd) == 0
    assert before == (checksum(build), checksum(furniture), checksum(corrections))
    assert (root/'authoring/furniture.json').read_bytes() == (furniture/'config.json').read_bytes()
    assert main(cmd) == 2


def test_bad_bounds_and_path_escape(tmp_path, project):
    assert main(['project', 'create', str(tmp_path/'bad'), '--bbox', '42', '3', '41', '2']) == 2
    assert not (tmp_path/'bad').exists()
    with pytest.raises(ValueError, match='inside'):
        project.path('../outside')


def test_input_change_during_build_not_published(project, monkeypatch):
    original = project.build_model
    def mutate(work):
        original(work)
        (project.root/'sources/model.xodr').write_text('concurrent edit')
    monkeypatch.setattr(project, 'build_model', mutate)
    with pytest.raises(ValueError, match='Inputs changed'):
        project.build('model')
    assert not (project.root/'build/model').exists()
