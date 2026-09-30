"""Dependency-aware project stages, resumable receipts and one target writer."""
from contextlib import contextmanager, redirect_stdout
from .filelock import lock_exclusive, unlock
from pathlib import Path
import tempfile
import uuid
import re

from .project import ROOT, checksum, digest, locked, read, write
from .project_io import revision

DEPS = {
    'validate.model': (),
    'unreal.geometry': ('validate.model',),
    'unreal.traffic': ('unreal.geometry',),
    'occupancy.export': ('unreal.traffic',),
    'vegetation.plan': ('occupancy.export',),
    'vegetation.bake': ('vegetation.plan',),
    'furniture.plan': ('vegetation.bake',),
    'furniture.bake': ('furniture.plan',),
    'validate.target': ('furniture.bake',),
}


class PrerequisiteError(ValueError):
    pass


class StageFailure(RuntimeError):
    pass


@contextmanager
def target_lock(target):
    key = digest(str(Path(target['uproject']).resolve()))
    path = Path(tempfile.gettempdir())/('twin-target-'+key+'.lock')
    with path.open('a') as file:
        try:
            lock_exclusive(file)
        except BlockingIOError:
            raise PrerequisiteError('Another Unreal writer is using this target project')
        try:
            yield
        finally:
            unlock(file)


class Pipeline:
    def __init__(self, project, target_name='local', executor=None):
        self.project = project
        self.name = target_name
        if not target_name.replace('-', '').replace('_', '').isalnum():
            raise ValueError('Invalid target name')
        targets = project.path('state/targets.json')
        self.target = (read(targets) if targets.exists() else {}).get(target_name)
        if self.target and not re.fullmatch(r'/Game/(?:[A-Za-z0-9_]+/)+[A-Za-z0-9_]+', self.target.get('level', '')):
            raise ValueError('Invalid Unreal level asset path')
        self.base = project.path('build/targets/'+target_name)
        self.state = project.path('state/pipeline-'+target_name+'.json')
        self.receipts = read(self.state) if self.state.exists() else {'stages': {}}
        if executor is None:
            from .project_stages import execute
            executor = execute
        self.executor = executor

    def output(self, stage):
        return self.base/stage

    def fingerprint(self, stage):
        p = self.project
        common = {'target': {k:self.target[k] for k in ('engine','uproject','level')} if self.target else None, 'project_id': p.spec['id'],
                  'deps': {d: self.receipts['stages'].get(d) for d in DEPS[stage]},
                  'adapter': checksum(ROOT/'twinmodel/project_stages.py')}
        from .project_components import component
        name = component(stage)
        if name and not p.spec.get('enabled', {}).get(name, True):
            common['enabled'] = False
            common['disable_adapter'] = checksum(ROOT/'twinmodel/project_components.py')
        if stage in ('validate.model', 'unreal.geometry'):
            common['model'] = checksum(p.path('build/model'))
        if stage == 'unreal.geometry':
            common['export'] = checksum(p.path('build/export'))
            common['implementation'] = [checksum(ROOT/'ue/bake_level.py'), checksum(ROOT/'ue/twin_materials.py')]
        if stage == 'unreal.traffic':
            common['config'] = checksum(p.path('authoring/traffic.json'))
            common['implementation'] = [checksum(ROOT/'ue/place_traffic_lights.py'), checksum(ROOT/'ue/place_traffic_signs.py'), checksum(ROOT/'ue/rigs')]
        for name in ('vegetation', 'furniture'):
            if stage.startswith(name):
                common['config'] = checksum(p.path(f'authoring/{name}.json'))
                common['implementation'] = [checksum(ROOT/f'twinmodel/{name}.py'), checksum(ROOT/f'ue/bake_{name}.py')]
                common['catalogue'] = checksum(ROOT/'twinmodel/data')
                if name == 'vegetation':
                    common['defaults'] = checksum(ROOT/'vegetation/defaults.json')
                if self.target:
                    native = Path(self.target['uproject']).parent/'Plugins/Carla/Source/Carla'/name.title()
                    common['native'] = checksum(native)
        return digest(common)

    def target_signature(self):
        if not self.target:
            return None
        from .project_target import content_paths
        content, paths = content_paths(self.target)
        return digest([checksum(content/path) for path in paths])

    def status(self):
        portable = self.project.status()
        dirty = any(s['state'] != 'current' for s in portable['stages'])
        external = bool(self.receipts.get('target_signature') and
                        self.receipts['target_signature'] != self.target_signature())
        stages = list(portable['stages'])
        states = {}
        last_report = {}
        if self.receipts.get('run'):
            path = self.project.path('runs/'+self.receipts['run']+'/pipeline.json')
            if path.exists():last_report = read(path)
        for stage, deps in DEPS.items():
            r = self.receipts['stages'].get(stage)
            reason = None
            upstream_dirty = portable['stages'][0]['state'] != 'current' if stage == 'validate.model' else dirty
            if upstream_dirty or any(states[d] != 'current' for d in deps):
                reason = 'upstream inputs require rebuild'
            elif stage != 'validate.model' and not self.target:
                reason = 'target is not configured'
            elif stage.startswith('unreal.') and external:
                reason = 'saved target changed outside this pipeline'
            elif not r:
                reason = 'no successful stage receipt'
            elif r['input'] != self.fingerprint(stage):
                reason = 'stage inputs changed'
            elif r['output'] != checksum(self.output(stage)):
                reason = 'stage output changed or missing'
            states[stage] = 'current' if reason is None else 'stale' if r else 'missing'
            failed = last_report.get('stages', {}).get(stage, {})
            if states[stage] != 'current' and failed.get('state') == 'failed' and failed.get('input') == self.fingerprint(stage):
                states[stage] = 'failed'
                reason = failed.get('error', 'stage failed')
            stages.append({'stage': stage, 'state': states[stage], 'reason': reason or 'verified inputs and outputs'})
        return {'schema': 'twin-project-status/1', 'project': self.project.spec['name'],
                'revision': revision(self.project), 'target': self.name, 'stages': stages,
                'last_run': self.receipts.get('run'), 'last_run_state': last_report.get('state'),
                'last_error': last_report.get('error'), 'target_changed': external}

    def requested(self, only=None):
        end = {'traffic': 'occupancy.export', 'vegetation': 'vegetation.bake',
               'furniture': 'furniture.bake'}.get(only, 'validate.target')
        status = {s['stage']: s['state'] for s in self.status()['stages']}
        # Recreating a level removes every placement system, so restore all of them.
        if status['unreal.geometry'] != 'current':
            end = 'validate.target'
        stages = list(DEPS)
        requested = stages[:stages.index(end)+1]
        if any(status[s] != 'current' for s in requested):
            # Changes to supports/plants must also repair dependent furniture.
            requested = stages
        return requested

    def preflight(self, requested):
        blockers = []
        if not self.target:
            return ['Configure an Unreal target before applying.']
        for key in ('engine', 'uproject'):
            if not Path(self.target[key]).is_file():
                blockers.append('Missing target '+key+': '+self.target[key])
        if 'unreal.geometry' in requested:
            content = Path(self.target['uproject']).parent/'Content'
            level = content/self.target['level'].removeprefix('/Game/')
            owner = level.parent/'twin-project-owner.json'
            if level.with_suffix('.umap').exists() and (not owner.exists() or read(owner).get('project_id') != self.project.spec['id']):
                blockers.append('Existing level ownership is unverified. Inspect with project target adopt --dry-run, or select a new level.')
        return blockers

    def run(self, only=None, dry_run=False, resume=None, stage=None):
        requested = [stage] if stage else self.requested(only)
        if any(s not in DEPS for s in requested):
            raise ValueError('Unknown stage')
        initial = self.status()
        if dry_run:
            return {**initial, 'requested': requested, 'will_run': [s['stage'] for s in initial['stages']
                    if s['state'] != 'current' and (s['stage'] in requested or not stage and s['stage'] in ('model', 'export'))],
                    'blockers': self.preflight(requested)}
        if not self.target and any(s != 'validate.model' for s in requested):
            raise PrerequisiteError('Configure a target before applying the project')
        if stage:
            states = {s['stage']: s['state'] for s in initial['stages']}
            prerequisites = ('model',) if stage == 'validate.model' else ('model', 'export', *DEPS[stage])
            missing = [d for d in prerequisites if states[d] != 'current']
            if missing:
                raise PrerequisiteError('Run prerequisites first: '+', '.join(missing))
        else:
            self.project.build()
        with locked(self.project.root), target_lock(self.target or {'uproject': str(self.project.root)}):
            rev = revision(self.project)
            if resume:
                old = self.project.path('runs/'+resume+'/pipeline.json')
                if not old.exists() or read(old).get('target') != self.name:
                    raise ValueError('Resume run does not belong to this target')
            run_id = uuid.uuid4().hex
            run_dir = self.project.path('runs/'+run_id)
            run_dir.mkdir(parents=True)
            report = {'run_id': run_id, 'target': self.name, 'revision': rev,
                      'resumed_from': resume, 'state': 'running', 'stages': {}}
            write(run_dir/'inputs.json', {'project': read(self.project.root/'project.json'),
                  'authoring': {p.name: read(p) for p in self.project.path('authoring').glob('*.json')}})
            write(run_dir/'pipeline.json', report)
            self.receipts['run'] = run_id
            write(self.state, self.receipts)
            try:
                for name in requested:
                    current = next(s for s in self.status()['stages'] if s['stage'] == name)
                    if current['state'] == 'current':
                        report['stages'][name] = {'state': 'skipped'}
                        continue
                    if rev != revision(self.project):
                        raise PrerequisiteError('Authoring changed during the run')
                    work = run_dir/name
                    work.mkdir()
                    before = self.fingerprint(name)
                    report['stages'][name] = {'state': 'running'}
                    write(run_dir/'pipeline.json', report)
                    if name.startswith('unreal.') or name.endswith('.bake'):
                        from .project_target import checkpoint
                        checkpoint(self, run_dir/'checkpoints'/name)
                    with (work/'stage.log').open('w') as stage_log, redirect_stdout(stage_log):
                        from .project_components import component, execute_disabled
                        kind = component(name)
                        if kind and not self.project.spec.get('enabled', {}).get(kind, True):
                            execute_disabled(self, name, work, run_dir)
                        else:
                            self.executor(self, name, work, run_dir)
                    if rev != revision(self.project) or before != self.fingerprint(name):
                        raise PrerequisiteError('Inputs changed; stage cannot be certified')
                    output = self.output(name)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if output.exists():
                        output.rename(run_dir/(name+'-previous'))
                    work.rename(output)
                    self.receipts['stages'][name] = {'input': before, 'output': checksum(output), 'run': run_id}
                    if name != 'validate.model':
                        self.receipts['target_signature'] = self.target_signature()
                    write(self.state, self.receipts)
                    report['stages'][name] = {'state': 'success', 'output': str(output)}
                    write(run_dir/'pipeline.json', report)
                report['state'] = 'success'
            except Exception as exc:
                report['state'] = 'failed'
                report['error'] = str(exc)
                if name in report['stages']:
                    report['stages'][name] = {'state': 'failed', 'error': str(exc), 'input': before}
                write(run_dir/'pipeline.json', report)
                raise
            write(run_dir/'pipeline.json', report)
            return report
