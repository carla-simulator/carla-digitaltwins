import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from project_editor import ProjectStore, ProjectWorkspace, ProjectHandler
from twinmodel.cli import main
from twinmodel.model import TwinModel
from twinmodel.project import Project, read
from twinmodel.project_io import save_authoring, revision


@pytest.fixture
def editor(tmp_path):
    source = tmp_path/'source.twin'
    TwinModel(name='sample', origin_lat=41, origin_lon=2,
              bbox_wgs84=(40, 1, 42, 3)).save(source)
    root = tmp_path/'project'
    assert main(['project', 'create', str(root), '--from-twin', str(source)]) == 0
    project = Project(root)
    project.build('model')
    store = ProjectStore(project)
    store.workspace = ProjectWorkspace(store, 'local')
    server = ThreadingHTTPServer(('127.0.0.1', 0), type('Bound', (ProjectHandler,), {'store': store}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield project, store, 'http://127.0.0.1:'+str(server.server_port)
    server.shutdown(); thread.join(); server.server_close()


def request(url, data=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:headers['If-Match'] = token
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.load(response), response.headers.get('X-Project-Revision')


def test_web_furniture_edit_is_durable_cli_input(editor):
    project, store, url = editor
    state, token = request(url+'/api/project')
    cfg = read(project.path('authoring/furniture.json')); cfg['seed'] = 99
    result, newer = request(url+'/api/planning/furniture/preview', cfg, token)
    assert newer != token
    assert read(project.path('authoring/furniture.json'))['seed'] == 99
    assert result['tools']['furniture']['plan']['config']['seed'] == 99
    assert revision(Project(project.root)) == revision(project)
    assert project.status()['stages'][0]['state'] == 'current'
    assert not project.path('build/model/editor-planning-inputs.json').exists()


def test_stale_browser_cannot_overwrite_cli_edit(editor):
    project, store, url = editor
    _, token = request(url+'/api/project')
    cfg = read(project.path('authoring/furniture.json')); cfg['seed'] = 101
    save_authoring(project, 'furniture', cfg, revision(project))
    cfg['seed'] = 102
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(url+'/api/planning/furniture/preview', cfg, token)
    assert exc.value.code == 409
    assert read(project.path('authoring/furniture.json'))['seed'] == 101


def test_invalid_configuration_does_not_change_saved_project(editor):
    project, store, url = editor
    _, token = request(url+'/api/project')
    before = revision(project)
    cfg = read(project.path('authoring/furniture.json')); cfg['group_spacing_m'] = -1
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(url+'/api/planning/furniture/preview', cfg, token)
    assert exc.value.code == 400
    assert revision(project) == before


def test_repository_app_contains_project_controls(editor):
    _, _, url = editor
    with urllib.request.urlopen(url+'/') as response:
        html = response.read().decode()
    assert 'Apply to Unreal' in html and 'Export project' in html
    assert 'If-Match' in html and 'data-tab="furniture"' in html


def test_placement_tabs_initialize_when_first_occupancy_arrives(editor):
    from twinmodel.project import write
    project, store, url = editor
    workspace = store.workspace
    workspace.tools.clear()
    workspace.errors = {'vegetation':'missing occupancy', 'furniture':'missing occupancy'}
    write(project.path('build/targets/local/occupancy.export/poles.json'), [])
    state, _ = request(url+'/api/planning')
    assert set(state['tools']) == {'vegetation', 'furniture'}
    assert state['errors'] == {}
