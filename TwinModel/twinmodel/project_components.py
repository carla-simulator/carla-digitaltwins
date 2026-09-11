"""Explicit empty outputs for disabled placement components."""
from importlib import import_module

from .project import ROOT, read, write
from .project_pipeline import PrerequisiteError


def component(stage):
    if stage == 'unreal.traffic':return 'traffic'
    if stage.startswith(('vegetation.', 'furniture.')):return stage.split('.')[0]
    return None


def execute_disabled(pipe, stage, work, run_dir):
    from .model import TwinModel
    from .project_stages import execute
    name = component(stage)
    model = TwinModel.load(pipe.project.path('build/model')/(pipe.project.spec['name']+'.twin'))
    if name == 'traffic':
        if model.signals:
            raise PrerequisiteError('Traffic cannot be disabled while the model contains traffic controls; reconcile layout controls first')
        return execute(pipe, stage, work, run_dir)
    if not stage.endswith('.plan'):
        # Baking the explicit empty plan removes only the owned placement region.
        return execute(pipe, stage, work, run_dir)
    planner = import_module('twinmodel.'+name)
    config = planner.configuration(read(pipe.project.path('authoring/'+name+'.json')))
    plan = {'schema':planner.SCHEMA, 'map':pipe.project.spec['name'], 'region':pipe.project.spec['name'],
            'origin':[model.origin_lat, model.origin_lon], 'coordinate_system':'carla-metres',
            'disabled':True, 'config':config, 'points':[], 'review':[],
            'summary':{'accepted':0,'rejected':0,'footings':0,'by_kind':{},'reasons':{},'groups':0,'groups_by_kind':{}}}
    if name == 'furniture':plan['catalog'] = planner.CATALOG
    write(work/'plan.json', plan)
