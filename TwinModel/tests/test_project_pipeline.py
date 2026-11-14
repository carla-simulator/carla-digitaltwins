import pytest

from twinmodel.cli import main
from twinmodel.model import TwinModel
from twinmodel.project import Project, read, write
from twinmodel.project_pipeline import Pipeline, DEPS, StageFailure, PrerequisiteError


@pytest.fixture
def setup(tmp_path):
    source = tmp_path/'source.twin'
    TwinModel(name='sample', origin_lat=41, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(source)
    root = tmp_path/'project'
    assert main(['project', 'create', str(root), '--from-twin', str(source)]) == 0
    project = Project(root)
    project.build()
    write(project.path('state/targets.json'), {'test': {'uproject': str(tmp_path/'Carla.uproject'),
          'engine': '/fake/engine', 'level': '/Game/Maps/Test/Test'}})
    calls = []
    def executor(pipe, stage, work, run):
        calls.append(stage)
        write(work/'report.json', {'saved': True})
    return project, calls, executor


def test_complete_apply_then_noop(setup):
    project, calls, executor = setup
    pipe = Pipeline(project, 'test', executor)
    assert pipe.run()['state'] == 'success'
    assert calls == list(DEPS)
    calls.clear()
    pipe = Pipeline(project, 'test', executor)
    pipe.run()
    assert calls == []


def test_furniture_only_does_not_rebuild_geometry(setup):
    project, calls, executor = setup
    Pipeline(project, 'test', executor).run()
    cfg = read(project.path('authoring/furniture.json')); cfg['seed'] += 1
    write(project.path('authoring/furniture.json'), cfg)
    calls.clear()
    Pipeline(project, 'test', executor).run(only='furniture')
    assert calls == ['furniture.plan', 'furniture.bake', 'validate.target']


def test_vegetation_updates_downstream_furniture(setup):
    project, calls, executor = setup
    Pipeline(project, 'test', executor).run()
    cfg = read(project.path('authoring/vegetation.json')); cfg['seed'] += 1
    write(project.path('authoring/vegetation.json'), cfg)
    calls.clear()
    Pipeline(project, 'test', executor).run(only='vegetation')
    assert calls == ['vegetation.plan', 'vegetation.bake', 'furniture.plan', 'furniture.bake', 'validate.target']


def test_failure_resume_reuses_verified_success(setup):
    project, calls, executor = setup
    def fail(pipe, stage, work, run):
        if stage == 'vegetation.bake':raise StageFailure('injected failure')
        executor(pipe, stage, work, run)
    with pytest.raises(StageFailure):
        Pipeline(project, 'test', fail).run()
    pipe = Pipeline(project, 'test', executor)
    prior = pipe.receipts['run']
    assert read(project.path('runs/'+prior+'/pipeline.json'))['state'] == 'failed'
    calls.clear()
    pipe.run(resume=prior)
    assert calls == ['vegetation.bake', 'furniture.plan', 'furniture.bake', 'validate.target']


def test_strict_stage_refuses_missing_dependencies_and_dry_run_does_not_execute(setup):
    project, calls, executor = setup
    pipe = Pipeline(project, 'test', executor)
    with pytest.raises(PrerequisiteError):pipe.run(stage='furniture.bake')
    assert 'unreal.geometry' in pipe.run(only='furniture', dry_run=True)['will_run']
    assert calls == []


def test_ue58_teardown_exit_and_procedural_buildings_detection(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from twinmodel import project_stages as ps

    uproject = tmp_path / 'Unreal/CarlaUnreal/CarlaUnreal.uproject'
    uproject.parent.mkdir(parents=True)
    uproject.write_text('{}', encoding='utf-8')
    assert ps._procedural_buildings_available(uproject) is False

    bgen = uproject.parent / 'Plugins/CarlaDigitalTwinsTool/Content/Blueprints/BP_BuildingGen.uasset'
    bgen.parent.mkdir(parents=True)
    bgen.write_bytes(b'version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 123\n')
    assert ps._procedural_buildings_available(uproject) is False

    bgen.write_bytes(b'\xc1\x83\x2a\x9e\x00\x00\x00\x00real-uasset-binary')
    assert ps._procedural_buildings_available(uproject) is True

    work = tmp_path / 'run/unreal.geometry'
    work.mkdir(parents=True)
    pipe = SimpleNamespace(target={'engine': 'fake-ue', 'uproject': str(uproject), 'level': '/Game/Maps/T/T'})

    def fake_run_clean(cmd, stdout, stderr, close_fds):
        assert '-FullStdOutLogOutput' in cmd
        stdout.write('[bake_level] exit 0\nShutdown handler: cleanup.\n')
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ps.subprocess, 'run', fake_run_clean)
    ps.unreal(pipe, tmp_path / 'bake_level.py', [], work, 'geometry')

    def fake_run_failed(cmd, stdout, stderr, close_fds):
        stdout.write('Traceback (most recent call last):\n[bake_level] FAILED\n[bake_level] exit 1\n')
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ps.subprocess, 'run', fake_run_failed)
    with pytest.raises(StageFailure):
        ps.unreal(pipe, tmp_path / 'bake_level.py', [], work, 'geometry')

