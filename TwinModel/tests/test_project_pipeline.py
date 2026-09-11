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
