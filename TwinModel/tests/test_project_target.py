from pathlib import Path

from twinmodel.project import Project, read, write
from twinmodel.project_pipeline import Pipeline
from twinmodel.project_target import checkpoint, restore
from .test_project_pipeline import setup


def test_restore_recovers_assets_and_previous_receipts(setup):
    project, calls, executor = setup
    pipe = Pipeline(project, 'test', executor)
    content = Path(pipe.target['uproject']).parent/'Content'
    asset = content/'Maps/Test/Test.umap'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'previous saved level')
    run = 'checkpoint-test'
    checkpoint(pipe, project.path('runs/'+run+'/checkpoints/unreal.geometry'))
    asset.write_bytes(b'partially replaced level')
    assert restore(project, 'test', run, 'unreal.geometry', dry_run=True)['restore']
    assert asset.read_bytes() == b'partially replaced level'
    restore(project, 'test', run, 'unreal.geometry')
    assert asset.read_bytes() == b'previous saved level'
    displaced = project.path('runs/'+run+'/before-restore-unreal.geometry/content/Maps/Test/Test.umap')
    assert displaced.read_bytes() == b'partially replaced level'
