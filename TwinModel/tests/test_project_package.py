import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import pytest

from twinmodel.project import write
from twinmodel.project_package import package_project, verify_archive
from twinmodel.project_pipeline import PrerequisiteError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from twinmodel import project_package as module
    root=tmp_path/'project';root.mkdir()
    up=tmp_path/'carla/Unreal/CarlaUnreal/CarlaUnreal.uproject';up.parent.mkdir(parents=True);up.touch()
    engine=tmp_path/'engine/Engine/Binaries/Linux/UnrealEditor-Cmd';engine.parent.mkdir(parents=True);engine.touch()
    packer=up.parents[2]/'Util/ContentPacks/carla_pack.py';packer.parent.mkdir(parents=True);packer.touch()
    level='/Game/Carla/Maps/Test/Test'
    mapfile=up.parent/'Content/Carla/Maps/Test/Test.umap';mapfile.parent.mkdir(parents=True);mapfile.touch()
    od=mapfile.parent/'OpenDrive/Test.xodr';od.parent.mkdir();od.write_text('<OpenDRIVE/>')
    base=tmp_path/'base.tar.gz';base.touch()
    project=SimpleNamespace(root=root,spec={'name':'test'},path=lambda p:root/p)
    pipe=SimpleNamespace(target={'engine':str(engine),'uproject':str(up),'level':level},
                         status=lambda:{'stages':[{'stage':'model','state':'stale'}],'revision':'r'})
    monkeypatch.setattr(module,'Pipeline',lambda *args:pipe)
    return project,base,tmp_path/'dist',pipe,od


def test_pending_authoring_requires_explicit_saved_target(setup):
    project,base,out,pipe,od=setup
    with pytest.raises(PrerequisiteError,match='saved-target'):
        package_project(project,'local',base,out,dry_run=True)
    result=package_project(project,'local',base,out,saved_target=True,dry_run=True)
    assert result['source']=='saved-target'
    assert not out.exists() and not (project.root/'runs').exists()
    assert result['pending_stages']==['model']


def test_require_nav_and_malformed_logic_fail_before_cook(setup):
    project,base,out,pipe,od=setup
    with pytest.raises(PrerequisiteError,match='navigation'):
        package_project(project,'local',base,out,saved_target=True,require_nav=True,dry_run=True)
    od.with_name('map_logic.json').write_text('{')
    with pytest.raises(ValueError):
        package_project(project,'local',base,out,saved_target=True,dry_run=True)


def test_archive_cannot_silently_omit_or_change_traffic_phases(tmp_path):
    od=tmp_path/'Test.xodr';od.write_text('<OpenDRIVE/>')
    logic=tmp_path/'map_logic.json';logic.write_text('{"phases": [1,2]}')
    archive=tmp_path/'pack.tar.gz'
    level='/Game/Test'
    def make(include=True, payload=None):
        entry={'package':level,'xodr':'Maps/OpenDrive/Test/Test.xodr'}
        if include:entry['map_logic']='Maps/OpenDrive/Test/map_logic.json'
        files={'Pack/carla-pack.json':json.dumps({'maps':[entry]}).encode(),
               'Pack/Content/'+entry['xodr']:od.read_bytes()}
        if include:files['Pack/Content/'+entry['map_logic']]=payload or logic.read_bytes()
        with tarfile.open(archive,'w:gz') as tar:
            for name,blob in files.items():
                info=tarfile.TarInfo(name);info.size=len(blob);tar.addfile(info,io.BytesIO(blob))
    make();assert verify_archive(archive,level,od,logic)['maps']
    make(False)
    with pytest.raises(RuntimeError,match='omitted'):verify_archive(archive,level,od,logic)
    make(payload=b'{}')
    with pytest.raises(RuntimeError,match='differs'):verify_archive(archive,level,od,logic)


def test_dedicated_pack_name_cannot_pull_in_another_map(setup):
    project,base,out,pipe,od=setup
    manifest=Path(pipe.target['uproject']).parent/'Plugins/Packs/TestPack/carla-pack.json'
    write(manifest,{'maps':[{'package':'/Game/Other'}],'catalogs':[]})
    with pytest.raises(PrerequisiteError,match='other content'):
        package_project(project,'local',base,out,name='TestPack',saved_target=True,dry_run=True)
