import json
import zipfile

import pytest

from twinmodel.cli import main
from twinmodel.model import TwinModel
from twinmodel.project import Project, checksum, read, write
from twinmodel.project_io import (export_project, import_project, revision, save_authoring,
                                 RevisionConflict, import_model)


@pytest.fixture
def project(tmp_path):
    source = tmp_path/'source.twin'
    TwinModel(name='sample', origin_lat=41, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(source)
    root = tmp_path/'project'
    assert main(['project', 'create', str(root), '--from-twin', str(source)]) == 0
    return Project(root)


def test_archive_roundtrip_keeps_authoring_excludes_machine_state(project, tmp_path):
    write(project.path('state/targets.json'), {'secret': '/home/somebody/engine'})
    cfg = read(project.path('authoring/furniture.json'))
    cfg['seed'] = 83
    save_authoring(project, 'furniture', cfg, revision(project))
    archive = tmp_path/'map.twinproject'
    export_project(project, archive)
    result = tmp_path/'imported'
    import_project(archive, result)
    assert checksum(project.path('authoring')) == checksum(result/'authoring')
    assert not (result/'state/targets.json').exists()
    assert Project(result).spec['id'] == project.spec['id']
    Project(result).build('model')
    assert Project(result).status()['stages'][0]['state'] == 'current'


def test_revision_conflict_does_not_lose_newer_edit(project):
    rev = revision(project)
    cfg = read(project.path('authoring/furniture.json'))
    cfg['seed'] = 83
    save_authoring(project, 'furniture', cfg, rev)
    cfg['seed'] = 84
    with pytest.raises(RevisionConflict):
        save_authoring(project, 'furniture', cfg, rev)
    assert read(project.path('authoring/furniture.json'))['seed'] == 83


def test_furniture_save_does_not_invalidate_geometry(project):
    project.build('model')
    cfg = read(project.path('authoring/furniture.json'))
    cfg['seed'] += 1
    save_authoring(project, 'furniture', cfg, revision(project))
    assert project.status()['stages'][0]['state'] == 'current'


@pytest.mark.parametrize('bad_name', ['../escape', '/absolute', 'sources/../../escape', 'sources\\escape'])
def test_archive_rejects_unsafe_paths(tmp_path, bad_name):
    archive = tmp_path/'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(bad_name, 'bad')
    with pytest.raises(ValueError, match='Unsafe'):
        import_project(archive, tmp_path/'result')
    assert not (tmp_path/'result').exists()


def test_archive_checksum_rejection(project, tmp_path):
    archive = tmp_path/'good.zip'
    export_project(project, archive)
    tampered = tmp_path/'bad.zip'
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, 'w') as out:
        for name in source.namelist():
            out.writestr(name, b'{}' if name == 'project.json' else source.read(name))
    with pytest.raises(ValueError, match='checksum'):
        import_project(tampered, tmp_path/'result')


def test_model_origin_change_rejected(project, tmp_path):
    new = tmp_path/'new.twin'
    TwinModel(name='sample', origin_lat=42, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(new)
    before = checksum(project.path('sources/model.twin'))
    with pytest.raises(ValueError, match='origin'):
        import_model(project, new)
    assert checksum(project.path('sources/model.twin')) == before


def test_pending_layout_can_be_opened_after_archive_import(project, tmp_path):
    project.build('model')
    layout = read(project.path('authoring/layout.json'))
    layout['ops'] = [{'id':'pending','op':'way.tags','way':10,'set':{'lanes':'3'}}]
    save_authoring(project, 'layout', layout, revision(project))
    archive = tmp_path/'pending.twinproject'
    export_project(project, archive)
    destination = tmp_path/'imported'
    import_project(archive, destination)
    imported = Project(destination)
    assert imported.path('build/model/project.twin/model.json').exists()
    assert not imported.path('state/model.json').exists()
    assert read(imported.path('authoring/layout.json'))['ops'] == layout['ops']
