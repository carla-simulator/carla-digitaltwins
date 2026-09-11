import ast
import os
from pathlib import Path

from twinmodel.project import Project, read, write
from twinmodel.project_components import execute_disabled
from twinmodel.project_pipeline import Pipeline
from .test_project_pipeline import setup


def test_disabled_furniture_produces_explicit_empty_plan_preserving_authoring(setup, tmp_path):
    project, calls, executor = setup
    config = project.path('authoring/furniture.json').read_bytes()
    project.spec['enabled']['furniture'] = False
    write(project.root/'project.json', project.spec)
    work = tmp_path/'disabled';work.mkdir()
    execute_disabled(Pipeline(Project(project.root),'test',executor), 'furniture.plan', work, work)
    result = read(work/'plan.json')
    assert result['disabled'] and result['points'] == [] and result['summary']['groups'] == 0
    assert project.path('authoring/furniture.json').read_bytes() == config
    assert project.status()['stages'][0]['state'] == 'current'


def test_empty_light_replacement_clears_bindings_only_after_successful_save(tmp_path):
    source = Path(__file__).resolve().parents[1]/'ue/place_traffic_lights.py'
    tree = ast.parse(source.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'clear_empty_logic')
    namespace = {'os':os}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    clear = namespace['clear_empty_logic']
    path = tmp_path/'map_logic.json';path.write_text('old bindings')
    assert not clear(str(path), [], False) and path.exists()
    assert not clear(str(path), [{'id':1}], True) and path.exists()
    assert clear(str(path), [], True) and not path.exists()
