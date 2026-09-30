"""Canonical web editor bound to durable DigitalTwin project inputs."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from twinmodel.filelock import lock_exclusive, unlock
import threading
from types import SimpleNamespace
import webbrowser
from http.server import ThreadingHTTPServer

from twinmodel.project import Project, ROOT, checksum, digest, locked, read
from twinmodel.project_io import revision, RevisionConflict, export_project
from twinmodel import corrections as corr_mod
from twinmodel.project_pipeline import Pipeline
from twin_editor import EditorStore, EditorHandler, EDITOR_PAGE
from geo_overlay import OrthoTiler
from map_workspace import MapWorkspace


class ProjectStore(EditorStore):
    def __init__(self, project):
        self.project = project
        self.planning_manifest_path = project.path('state/editor-planning-inputs.json')
        super().__init__(project.path('build/model'), project.spec['name'], project.path('sources'),
                         None, project.path('authoring/layout.json'))
        # Never fetch OSM just because a snapshot was opened in the editor.
        source = project.path('sources/osm.json')
        self._raw_original = read(source) if source.exists() else {'elements': []}
        imagery = project.path('sources/imagery.tif')
        if imagery.exists():
            self.ortho = OrthoTiler(imagery)

    def osm_original(self):
        source = self.project.path('sources/osm.json')
        signature = checksum(source)
        if signature != getattr(self, '_source_signature', None):
            self._raw_original = read(source) if source.exists() else {'elements': []}
            self._source_signature = signature
            self._osm = self._osm_raw_bytes = None
        return self._raw_original

    def corrections_json(self):
        if not self.dirty:
            self.corr = corr_mod.load_or_empty(self.corr_path, self.name)
        return super().corrections_json()

    def rebuild(self):
        result = self.project.build('model')
        self.model = read(self.twin_dir/'model.json')
        self._twin = self._osm = None
        self.last_rebuild = {'ok': True, 'stages': result['stages']}
        return self.last_rebuild


class ProjectWorkspace(MapWorkspace):
    def __init__(self, store, target):
        self.project = store.project
        self.target_name = target
        self.job = None
        self.job_log = None
        self.writing = threading.local()
        p = self.project
        # Preserve adopted placements for the initial preview. Their configs live
        # under authoring; generated copies may be discarded and regenerated.
        for name in ('vegetation', 'furniture'):
            directory = p.path('build/editor/'+name)
            original = p.path('sources/adopted-'+name)
            if not directory.exists() and original.exists():
                shutil.copytree(original, directory)
        poles = p.path('build/targets/'+target+'/occupancy.export/poles.json')
        if not poles.exists():
            poles = p.path('sources/adopted-furniture/poles.json')
        options = SimpleNamespace(vegetation_output=str(p.path('build/editor/vegetation')),
             furniture_output=str(p.path('build/editor/furniture')), poles=str(poles) if poles.exists() else None,
             level=None, project=None, engine=None, region=p.spec['name'])
        self.options = options
        super().__init__(store, options)
        for name, tool in self.tools.items():
            tool.config_path = p.path('authoring/'+name+'.json')
            if tool.config_path.exists():
                tool.config = read(tool.config_path)

    def busy(self):
        if bool(self.job and self.job.poll() is None) or super().busy():
            return True
        path = self.project.path('state/project.lock')
        if path.exists() and not getattr(self.writing, 'active', False):
            with path.open('r') as file:
                try:
                    lock_exclusive(file)
                except BlockingIOError:
                    return True
                unlock(file)
        return False

    def token(self):
        if not self.store.dirty:
            current = corr_mod.load_or_empty(self.store.corr_path, self.store.name)
            if current.ops != self.store.corr.ops:
                self.store.corr = current
                self.store._osm = None
        return digest({'revision': revision(self.project), 'ops': self.store.corr.ops})

    def state(self):
        self.ensure_tools()
        for name, tool in self.tools.items():
            if tool.config_path.exists():
                tool.config = read(tool.config_path)
        value = super().state()
        pipe = Pipeline(Project(self.project.root), self.target_name)
        stages = {s['stage']:s['state'] for s in pipe.status()['stages']}
        changed = False
        for name, entry in value['tools'].items():
            entry['stale'] |= entry['config'] != entry['plan']['config']
            entry['bake']['enabled'] = pipe.target is not None
            entry['bake']['running'] = self.busy()
            if stages.get(name+'.bake') == 'current':
                candidate = read(pipe.output(name+'.plan')/'plan.json')
                validated = read(pipe.output(name+'.bake')/'validated-plan.json')
                self.tools[name].result = candidate
                signature = self.signature(name)
                if self.inputs.get(name) != signature:
                    self.inputs[name] = signature
                    changed = True
                entry['plan'] = validated
                entry['stale'] = False
                entry['bake']['report'] = read(pipe.output(name+'.bake')/'report.json')
            if self.job and self.job.poll() not in (None, 0):
                entry['bake']['error'] = 'Project run failed. Open the project panel for diagnostics.'
        if changed:self.persist()
        return value

    def ensure_tools(self):
        """Bring placement tabs online after the first apply exports occupancy."""
        if not self.errors or self.busy():
            return
        poles = self.project.path('build/targets/'+self.target_name+'/occupancy.export/poles.json')
        if not poles.exists():
            return
        from vegetation_tool import Workspace as VegetationWorkspace
        from furniture_tool import Workspace as FurnitureWorkspace
        for name, cls in [('vegetation', VegetationWorkspace), ('furniture', FurnitureWorkspace)]:
            if name in self.tools:
                continue
            config = self.project.path('authoring/'+name+'.json')
            options = SimpleNamespace(twin=str(self.store.twin_dir), output=getattr(self.options, name+'_output'),
                config=str(config) if config.exists() else None, poles=str(poles), anchors=None,
                vegetation=str(self.project.path('build/editor/vegetation/plan.json')), occupied_plan=[],
                level=None, project=None, engine=None, region=self.project.spec['name'])
            try:
                tool = cls(options, restore=True)
                tool.config_path = config
                self.tools[name] = tool
                self.inputs[name] = self.signature(name)
                self.errors.pop(name, None)
                self.persist()
            except (ValueError, OSError, KeyError) as exc:
                self.errors[name] = str(exc)

    def reload_model(self):
        before = self.model_stamp
        super().reload_model()
        if before != self.model_stamp:
            self.store.model = read(self.store.twin_dir/'model.json')
            self.store._twin = self.store._osm = None
            if self.project.status()['stages'][0]['state'] == 'current':
                self.layout_stamp = digest(self.store.corr.ops)
                self.persist()

    def set_ops(self, ops, save):
        with locked(self.project.root):
            self.writing.active = True
            try:
                self.check_expected()
                return super().set_ops(ops, save)
            finally:
                self.writing.active = False

    def preview(self, name, cfg):
        with locked(self.project.root):
            self.writing.active = True
            try:
                self.check_expected()
                return super().preview(name, cfg)
            finally:
                self.writing.active = False

    def check_expected(self):
        expected = getattr(self, 'expected_token', None)
        if expected is not None and expected != self.token():
            raise RevisionConflict('Project changed while acquiring its write lock; reload before saving')

    def rebuild(self):
        self.require_idle()
        if self.store.dirty:
            raise ValueError('Save layout before rebuilding the project')
        return super().rebuild()

    def start(self, action='apply', only=None):
        self.require_idle()
        if self.store.dirty:
            raise ValueError('Save layout edits before starting a project run')
        command = [sys.executable, '-m', 'twinmodel', 'project', action]
        if action == 'runtime':command += ['reload']
        command += [str(self.project.root)]
        if action == 'apply':
            command += ['--target', self.target_name]
            if only:command += ['--only', only]
        elif action == 'runtime':
            command += ['--target', self.target_name]
        path = self.project.path('state/editor-job.log')
        if self.job_log:self.job_log.close()
        self.job_log = path.open('w')
        self.job = subprocess.Popen(command, cwd=ROOT, stdout=self.job_log, stderr=subprocess.STDOUT)
        return self.project_state()

    def bake(self, name):
        self.start(only=name)
        return self.state()

    def project_state(self):
        pipe = Pipeline(Project(self.project.root), self.target_name)
        value = pipe.status()
        value['target_level'] = pipe.target.get('level') if pipe.target else None
        value['token'] = self.token()
        value['dirty'] = self.store.dirty
        value['job'] = {'running': self.busy(), 'exit_code': self.job.poll() if self.job else None}
        log = self.project.path('state/editor-job.log')
        if log.exists():value['job']['log'] = log.read_text(errors='replace')[-5000:]
        diagnostics = sorted(self.project.path('runs').glob('*/model/*.reviewed-map.json'),
                             key=lambda p: p.stat().st_mtime)
        value['diagnostics'] = read(diagnostics[-1]).get('diagnostics', []) if diagnostics else []
        return value


class ProjectHandler(EditorHandler):
    def end_headers(self):
        self.send_header('X-Project-Revision', self.store.workspace.token())
        super().end_headers()

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://localhost:{self.server.server_port}', f'http://127.0.0.1:{self.server.server_port}'):
            self._json({'error': 'Origin not allowed'}, 403)
            return
        workspace = self.store.workspace
        with workspace.lock:
            if self.headers.get('If-Match') != workspace.token():
                self._json({'error': 'Project changed; reload the page before saving'}, 409)
                return
            workspace.expected_token = self.headers.get('If-Match')
            if self.path.split('?')[0] == '/api/project/run':
                try:
                    if int(self.headers.get('Content-Length') or 0) > 2000000:
                        raise ValueError('Request too large')
                    body = self._body()
                    if body.get('action', 'apply') not in ('apply', 'build', 'runtime'):
                        raise ValueError('Unknown project action')
                    self._json(workspace.start(body.get('action', 'apply'), body.get('only')))
                except ValueError as exc:
                    self._json({'error': str(exc)}, 400)
                return
            return super().do_POST()

    do_PUT = do_POST

    def _route(self):
        if self.path.split('?')[0] == '/api/project':
            self._json(self.store.workspace.project_state())
            return
        if self.path.split('?')[0] == '/api/project/export':
            # Archive generation is explicit and serialized with project writers.
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                archive = Path(folder)/'map.twinproject'
                export_project(self.store.project, archive)
                self._send(200, archive.read_bytes(), 'application/zip')
            return
        if self.path == '/':
            script = (ROOT/'tools/project_workspace.js').read_text()
            # Revision-aware transport must install before the canonical scripts.
            page = EDITOR_PAGE.replace('<script>', '<script>'+script+'</script><script>', 1)
            self._send(200, page.encode(), 'text/html; charset=utf-8')
            return
        super()._route()


def serve_project(project, port=8791, target='local', open_browser=False):
    if not project.path('build/model/'+project.spec['name']+'.twin/model.json').exists():
        project.build('model')
    store = ProjectStore(project)
    store.workspace = ProjectWorkspace(store, target)
    handler = type('BoundProjectHandler', (ProjectHandler,), {'store': store})
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    print(f'DigitalTwin project editor: http://localhost:{server.server_port}', flush=True)
    if open_browser:webbrowser.open(f'http://localhost:{server.server_port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
