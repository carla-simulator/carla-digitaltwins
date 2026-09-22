"""Durable projects and portable build stages for the existing TwinModel CLI."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from .filelock import lock_exclusive, unlock
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

SCHEMA = 'twin-project/1'
ROOT = Path(__file__).resolve().parents[1]


class ProjectBusy(ValueError):
    pass


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def checksum(path):
    path = Path(path)
    if not path.exists():
        return None
    h = hashlib.sha256()
    files = sorted(p for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts) if path.is_dir() else [path]
    for p in files:
        h.update(str(p.relative_to(path) if path.is_dir() else p.name).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@contextmanager
def locked(root):
    (root/'state').mkdir(exist_ok=True)
    with (root/'state/project.lock').open('a') as handle:
        try:
            lock_exclusive(handle)
        except BlockingIOError:
            raise ProjectBusy('Project is busy; another writer holds its lock')
        try:
            yield
        finally:
            unlock(handle)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.spec = read(self.root/'project.json')
        if not isinstance(self.spec, dict):
            raise ValueError('Project manifest must be an object')
        if self.spec.get('schema') != SCHEMA:
            raise ValueError('Unsupported project schema')
        if not isinstance(self.spec.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', self.spec['name']):
            raise ValueError('Invalid project name')
        if not isinstance(self.spec.get('id'), str):
            raise ValueError('Project ID is missing')
        try:uuid.UUID(self.spec['id'])
        except ValueError:raise ValueError('Invalid project ID') from None
        if not isinstance(self.spec.get('source'), dict) or not isinstance(self.spec.get('build'), dict):
            raise ValueError('Project source and build recipe must be objects')
        if self.spec['source']['mode'] not in ('osm', 'snapshot'):
            raise ValueError('Unsupported source mode')
        from .profiles import PROFILES
        if self.spec.get('profile') not in (*PROFILES, 'auto'):
            raise ValueError('Unknown project profile')
        for key in ('dem', 'imagery', 'refine'):
            if not isinstance(self.spec.get('build', {}).get(key), bool):
                raise ValueError('Project build settings must contain boolean '+key)
        if not isinstance(self.spec.get('enabled', {}), dict) or any(k not in ('traffic','vegetation','furniture') or not isinstance(v, bool)
               for k,v in self.spec.get('enabled', {}).items()):
            raise ValueError('Enabled components must be traffic/vegetation/furniture booleans')
        if self.spec['build'].get('mask_method', 'classical') not in ('classical','sam','auto'):
            raise ValueError('Unknown refinement mask method')
        if self.spec['source']['mode'] == 'osm':
            bbox = self.spec['source'].get('bbox', [])
            if len(bbox) != 4 or not all(isinstance(v, (int,float)) and math.isfinite(v) for v in bbox) or not (
                -90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180):
                raise ValueError('Invalid source bounding box')
            frame = self.spec.get('frame')
            if frame and (frame['origin_lat'], frame['origin_lon']) != ((bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2):
                raise ValueError('Source bounds change the authoring coordinate origin; create a new project or reconcile coordinates')

    def path(self, relative):
        result = (self.root/relative).resolve()
        if not result.is_relative_to(self.root):
            raise ValueError('Project paths must stay inside the project')
        return result

    def fingerprint(self, stage):
        spec = read(self.root/'project.json')
        modules = sorted(p for p in Path(__file__).parent.glob('*.py')
                         if not p.name.startswith(('project', 'vegetation', 'furniture')))
        modules.append(ROOT/'twinmodel/export/xodr.py')
        inputs = {'implementation': {str(p.relative_to(ROOT)): checksum(p) for p in modules},
                  'recipe': {k: spec[k] for k in ('name', 'source', 'profile', 'build')},
                  'sources': {name: checksum(self.path('sources/'+name)) for name in
                              ('model.twin', 'model.xodr') if spec['source']['mode'] == 'snapshot'},
                  'layout': checksum(self.path('authoring/layout.json'))}
        if spec['source']['mode'] == 'osm':
            inputs['sources'] = {name: checksum(self.path('sources/'+name)) for name in
                                ('osm.json', 'dem.npz', 'imagery.tif', 'acquisition.json')}
        if stage == 'export':
            inputs['model'] = checksum(self.path('build/model'))
            inputs['exporter'] = checksum(ROOT/'twinmodel/export/ue.py')
        return digest(inputs)

    def status(self):
        stages = []
        upstream = False
        for stage, output in [('model', 'build/model'), ('export', 'build/export')]:
            receipt_path = self.path(f'state/{stage}.json')
            receipt = read(receipt_path) if receipt_path.exists() else {}
            current = (not upstream and receipt.get('input') == self.fingerprint(stage)
                       and receipt.get('output') is not None
                       and receipt.get('output') == checksum(self.path(output)))
            stages.append({'stage': stage, 'state': 'current' if current else 'stale' if receipt else 'missing',
                           'reason': 'verified inputs and outputs' if current else
                           'upstream stage requires rebuild' if upstream else 'missing or changed inputs/outputs'})
            upstream = not current
        return {'schema': 'twin-project-status/1', 'project': self.spec['name'], 'stages': stages,
                'capabilities': {'portable_build': True, 'unreal_apply': True,
                                 'layout_recompile': self.spec['source']['mode'] == 'osm'}}

    def build(self, through='export', dry_run=False, resume=None):
        if dry_run:
            return self.status()
        with locked(self.root):
            self.spec = Project(self.root).spec
            if resume and not self.path('runs/'+resume+'/inputs.json').exists():
                raise ValueError('Unknown build run to resume')
            from .project_sources import acquire
            acquire(self)
            run = self.path('runs')/uuid.uuid4().hex
            run.mkdir(parents=True)
            write(run/'inputs.json', {'project': self.spec, 'resumed_from': resume, 'authoring': {
                p.name: read(p) for p in self.path('authoring').glob('*.json')}})
            for stage in ('model', 'export'):
                state = next(s for s in self.status()['stages'] if s['stage'] == stage)
                if state['state'] != 'current':
                    before = self.fingerprint(stage)
                    work = run/stage
                    work.mkdir()
                    try:
                        with (run/f'{stage}.log').open('w') as log, redirect_stdout(log):
                            if stage == 'model':
                                self.build_model(work)
                            else:
                                from .cli import main
                                code = main(['bake-export', str(self.path('build/model')), self.spec['name'],
                                             '--out', str(work)])
                                if code:
                                    raise ValueError(f'Export failed ({code})')
                        if before != self.fingerprint(stage):
                            raise ValueError('Inputs changed during build; result was not published')
                        required = [work/f'{self.spec["name"]}.twin/model.json', work/f'{self.spec["name"]}.xodr'] if stage == 'model' else [work/'manifest.json']
                        if not all(p.is_file() for p in required):
                            raise ValueError('Stage did not produce its required artifacts')
                        output = self.path('build/'+stage)
                        output.parent.mkdir(exist_ok=True)
                        if output.exists():
                            output.rename(run/(stage+'-previous'))
                        work.rename(output)
                        write(self.path(f'state/{stage}.json'), {
                            'input': before, 'output': checksum(output), 'run': run.name})
                        write(run/f'{stage}.json', {'state': 'success'})
                    except Exception as exc:
                        write(run/f'{stage}.json', {'state': 'failed', 'error': str(exc)})
                        raise
                if stage == through:
                    break
            return self.status()

    def build_model(self, work):
        name = self.spec['name']
        if self.spec['source']['mode'] == 'snapshot':
            if checksum(self.path('authoring/layout.json')) != self.spec['source']['layout_baseline']:
                raise ValueError('Snapshot layout changed: original OSM recipe is required to recompile corrections')
            from .model import TwinModel
            from .export.xodr import export_xodr
            source = self.path('sources/model.twin')
            model = TwinModel.load(source)
            shutil.copytree(source, work/f'{name}.twin')
            xodr = self.path('sources/model.xodr')
            if xodr.exists():
                shutil.copyfile(xodr, work/f'{name}.xodr')
            else:
                export_xodr(model, work/f'{name}.xodr')
            return
        from .cli import main
        source = self.path('sources/osm.json')
        settings = self.spec['build']
        options = ['--dem-file', str(self.path('sources/dem.npz'))] if settings.get('dem') else ['--no-dem']
        options += ['--imagery-file', str(self.path('sources/imagery.tif'))] if settings.get('imagery') else ['--no-imagery']
        if not settings.get('refine'):
            options += ['--no-refine']
        profile = read(self.path('sources/acquisition.json'))['profile']
        code = main(['build', '--name', name, '--bbox', *map(str, self.spec['source']['bbox']),
                     '--fixture', str(source), '--cache', str(self.path('sources/cache')),
                     '--out', str(work), '--corrections', str(self.path('authoring/layout.json')),
                     '--profile', profile, '--mask-method', settings.get('mask_method', 'classical'), '--quick', *options])
        if code:
            raise ValueError(f'Model build failed ({code})')


def create(args):
    destination = Path(args.path).resolve()
    if destination.exists():
        raise ValueError('Destination already exists; adoption never overwrites a project')
    name = args.name or destination.name
    from .profiles import PROFILES
    if args.profile != 'auto' and args.profile not in PROFILES:
        raise ValueError(f'Unknown profile: {args.profile}')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('Name must contain letters, numbers, underscores or hyphens')
    bbox = getattr(args, 'bbox', None)
    if bbox and (not all(math.isfinite(v) for v in bbox) or
                 not (-90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180)):
        raise ValueError('Expected valid south west north east bounds')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.twin-project-', dir=destination.parent))
    try:
        for folder in ('sources', 'authoring', 'state', 'runs'):
            (temporary/folder).mkdir()
        from .corrections import SCHEMA as CORRECTIONS_SCHEMA
        write(temporary/'authoring/layout.json', {'schema': CORRECTIONS_SCHEMA, 'name': name, 'ops': []})
        from .vegetation import configuration as vegetation_config
        from .furniture import configuration as furniture_config
        write(temporary/'authoring/vegetation.json', vegetation_config())
        write(temporary/'authoring/furniture.json', furniture_config())
        write(temporary/'authoring/traffic.json', {'style': 'eu', 'default_rig': 'eu_pole'})
        source_twin = getattr(args, 'from_twin', None)
        if args.action == 'adopt':
            build = Path(args.build_dir).resolve()
            source_twin = build/f'{name}.twin'
            if args.corrections:
                shutil.copyfile(args.corrections, temporary/'authoring/layout.json')
            if (build/f'{name}.xodr').exists():
                shutil.copyfile(build/f'{name}.xodr', temporary/'sources/model.xodr')
            if (build/'report.json').exists():
                shutil.copyfile(build/'report.json', temporary/'sources/original-report.json')
            for tool in ('vegetation', 'furniture'):
                directory = getattr(args, tool, None)
                if directory:
                    shutil.copyfile(Path(directory)/'config.json', temporary/f'authoring/{tool}.json')
                    shutil.copytree(directory, temporary/f'sources/adopted-{tool}')
        if source_twin:
            from .model import TwinModel
            model = TwinModel.load(source_twin)
            frame = {'crs':'local-enu', 'origin_lat':model.origin_lat, 'origin_lon':model.origin_lon}
            shutil.copytree(source_twin, temporary/'sources/model.twin')
            source = {'mode': 'snapshot', 'layout_baseline': checksum(temporary/'authoring/layout.json')}
        else:
            source = {'mode': 'osm', 'bbox': bbox}
            frame = {'crs':'local-enu', 'origin_lat':(bbox[0]+bbox[2])/2, 'origin_lon':(bbox[1]+bbox[3])/2}
        write(temporary/'project.json', {'schema': SCHEMA, 'id': uuid.uuid4().hex, 'name': name,
              'source': source, 'frame': frame, 'profile': args.profile,
              'enabled': {'traffic':True,'vegetation':True,'furniture':True},
              'build': {'imagery': not getattr(args, 'no_imagery', False),
                        'dem': not getattr(args, 'no_dem', False),
                        'refine': not getattr(args, 'no_refine', False)}})
        (temporary/'.gitignore').write_text('build/\nstate/\nruns/\n')
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return Project(destination).status()


def command(args):
    try:
        if args.action in ('create', 'adopt'):
            result = create(args)
        elif args.action == 'import':
            from .project_io import import_project
            result = import_project(args.path, args.into)
        else:
            project = Project(args.path)
            if args.action == 'status':
                if args.target:
                    from .project_pipeline import Pipeline
                    result = Pipeline(project, args.target).status()
                else:
                    result = project.status()
            elif args.action == 'build':
                result = project.build(args.through, args.dry_run, args.resume)
            elif args.action == 'package':
                from .project_package import package_project
                result = package_project(project, args.target, args.base, args.out, args.name,
                    args.version, args.saved_target, args.require_nav, args.dry_run)
            elif args.action == 'export':
                from .project_io import export_project
                result = export_project(project, args.out)
            elif args.action == 'validate':
                from .project_io import validate_project
                result = validate_project(project)
                if project.path('build/model').exists():
                    from .project_pipeline import Pipeline
                    result['artifacts'] = Pipeline(project, args.target or 'local').run(
                        stage='validate.target' if args.target else 'validate.model')
            elif args.action in ('apply', 'run'):
                from .project_pipeline import Pipeline
                stage = getattr(args, 'stage', None)
                if stage in ('model', 'export'):
                    if stage == 'export' and project.status()['stages'][0]['state'] != 'current' and not args.dry_run:
                        from .project_pipeline import PrerequisiteError
                        raise PrerequisiteError('Run the model stage before export')
                    result = project.build(stage, args.dry_run, args.resume)
                else:
                    result = Pipeline(project, args.target).run(only=getattr(args, 'only', None),
                        dry_run=args.dry_run, resume=args.resume, stage=stage)
            elif args.action == 'edit':
                sys.path.insert(0, str(ROOT/'tools'))
                from project_editor import serve_project
                return serve_project(project, args.port, args.target, args.open)
            elif args.action == 'runtime':
                import carla
                from .project_runtime import reload_map
                target = read(project.path('state/targets.json'))[args.target]
                client = carla.Client(target.get('host', 'localhost'), target.get('port', 2000))
                client.set_timeout(120)
                result = reload_map(client, target['level'])
            elif args.action == 'model':
                from .project_io import import_model
                result = import_model(project, args.twin)
            elif args.action == 'source':
                from .project_sources import acquire, import_recipe
                if args.operation == 'import-recipe':
                    result = import_recipe(project, args.build_dir, args.cache)
                else:
                    with locked(project.root):
                        acquire(project, refresh=True)
                    result = project.status()
            elif args.action == 'target':
                if args.operation == 'adopt':
                    from .project_target import adopt
                    result = adopt(project, args.target_name, args.dry_run)
                    print(json.dumps(result, indent=2))
                    return 0
                if args.operation == 'restore':
                    if not args.run or not args.stage:
                        raise ValueError('Restore requires --run and --stage')
                    from .project_target import restore
                    result = restore(project, args.target_name, args.run, args.stage, args.dry_run)
                    print(json.dumps(result, indent=2))
                    return 0
                if not all((args.engine, args.uproject, args.level)):
                    raise ValueError('Target set requires --engine, --uproject and --level')
                for value in (args.engine, args.uproject):
                    if not Path(value).is_file():
                        raise ValueError(f'Target file does not exist: {value}')
                if not re.fullmatch(r'/Game/(?:[A-Za-z0-9_]+/)+[A-Za-z0-9_]+', args.level):
                    raise ValueError('Level must be a full /Game/ asset path')
                with locked(project.root):
                    path = project.path('state/targets.json')
                    targets = read(path) if path.exists() else {}
                    targets[args.target_name] = {'engine': str(Path(args.engine).resolve()),
                        'uproject': str(Path(args.uproject).resolve()), 'level': args.level,
                        'host': args.host, 'port': args.port}
                    write(path, targets)
                result = {'target': args.target_name, 'configured': True}
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        from .project_io import RevisionConflict
        from .project_pipeline import PrerequisiteError
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 4 if isinstance(exc, (ProjectBusy, RevisionConflict)) else 3 if isinstance(exc, PrerequisiteError) else 2
    except RuntimeError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


def add_parser(sub):
    parser = sub.add_parser('project', help='durable map projects and portable build stages')
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('create', 'adopt', 'status', 'build', 'target', 'export', 'import', 'validate', 'model', 'source', 'apply', 'run', 'edit', 'runtime', 'package'):
        p = actions.add_parser(action)
        if action == 'target':
            p.add_argument('operation', choices=['set', 'restore', 'adopt'])
        if action == 'model':
            p.add_argument('operation', choices=['import'])
        if action == 'source':
            p.add_argument('operation', choices=['refresh', 'import-recipe'])
        if action == 'runtime':
            p.add_argument('operation', choices=['reload'])
        p.add_argument('path')
        p.add_argument('--json', action='store_true', help='structured output (currently always enabled)')
        if action in ('create', 'adopt'):
            p.add_argument('--name')
            p.add_argument('--profile', default='auto', help='regional build profile')
            p.add_argument('--no-dem', action='store_true')
            p.add_argument('--no-imagery', action='store_true')
            p.add_argument('--no-refine', action='store_true')
            if action == 'create':
                source = p.add_mutually_exclusive_group(required=True)
                source.add_argument('--bbox', nargs=4, type=float, metavar=('S', 'W', 'N', 'E'))
                source.add_argument('--from-twin')
            else:
                p.add_argument('--build-dir', required=True)
                p.add_argument('--corrections', required=True)
                p.add_argument('--vegetation')
                p.add_argument('--furniture')
        if action == 'build':
            p.add_argument('--through', choices=['model', 'export'], default='export')
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--resume')
        if action == 'target':
            p.add_argument('target_name')
            p.add_argument('--engine')
            p.add_argument('--uproject')
            p.add_argument('--level')
            p.add_argument('--host', default='localhost')
            p.add_argument('--port', type=int, default=2000)
            p.add_argument('--run')
            p.add_argument('--stage')
            p.add_argument('--dry-run', action='store_true')
        if action in ('status', 'apply', 'run', 'edit', 'runtime', 'validate', 'package'):
            p.add_argument('--target', default=None if action in ('status', 'validate') else 'local')
        if action in ('apply', 'run'):
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--resume')
        if action == 'apply':
            p.add_argument('--only', choices=['traffic', 'vegetation', 'furniture'])
        if action == 'run':
            from .project_pipeline import DEPS
            p.add_argument('--stage', choices=['model','export',*DEPS], required=True)
        if action == 'edit':
            p.add_argument('--port', type=int, default=8791)
            p.add_argument('--open', action='store_true')
        if action == 'package':
            p.add_argument('--base', required=True, help='matching CARLA Linux release metadata')
            p.add_argument('--out', required=True)
            p.add_argument('--name')
            p.add_argument('--version', default='1.0.0')
            p.add_argument('--saved-target', action='store_true', help='package the saved level, explicitly excluding pending authoring changes')
            p.add_argument('--require-nav', action='store_true', help='fail if pedestrian navigation is missing')
            p.add_argument('--dry-run', action='store_true')
        if action == 'export':
            p.add_argument('--out', required=True)
        if action == 'import':
            p.add_argument('--into', required=True)
        if action == 'model':
            p.add_argument('twin')
        if action == 'source':
            p.add_argument('--build-dir')
            p.add_argument('--cache', default='data')
        p.set_defaults(func=command)
