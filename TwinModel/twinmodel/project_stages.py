"""Stage adapters around the canonical compiler, planners and Unreal scripts."""
from pathlib import Path, PurePath, PurePosixPath
import re
import shutil
import subprocess
import sys

from .project import ROOT, read, write
from .project_pipeline import PrerequisiteError, StageFailure

_GIT_LFS_PREFIX = b'version https://git-lfs.github.com/spec/v1'
_CLEAN_UNREAL_MARKERS = (
    'VEGETATION_GRAPH_READY ',
    'VEGETATION_BAKED ',
    'FURNITURE_GRAPH_READY ',
    'FURNITURE_BAKED ',
)


def _has_real_uasset(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open('rb') as f:
            head = f.read(64)
        return bool(head) and not head.startswith(_GIT_LFS_PREFIX)
    except OSError:
        return False


def _procedural_buildings_available(uproject: str | Path) -> bool:
    bgen = (Path(uproject).parent / 'Plugins' / 'CarlaDigitalTwinsTool'
            / 'Content' / 'Blueprints' / 'BP_BuildingGen.uasset')
    return _has_real_uasset(bgen)


def _unreal_log_completed_cleanly(log_text: str) -> bool:
    if 'Traceback (most recent call last):' in log_text:
        return False
    if re.search(r'^\[[\w_]+\]\s+FAILED\b', log_text, re.MULTILINE):
        return False
    if re.search(r'^\[[\w_]+\]\s+exit\s+[1-9]\d*\b', log_text, re.MULTILINE):
        return False
    if re.search(r'^\[[\w_]+\]\s+exit\s+0\s*$', log_text, re.MULTILINE):
        return True
    if any(marker in log_text for marker in _CLEAN_UNREAL_MARKERS):
        return True
    return 'Success - 0 error(s)' in log_text and 'Failure -' not in log_text


def _quote_ue_arg(value) -> str:
    text = value.as_posix() if isinstance(value, PurePath) else str(value)
    if not text or any(ch.isspace() or ch == '"' for ch in text):
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return text


def _parse_layer(raw) -> int:
    if raw is None or raw == '':
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0


def unreal(pipe, script, arguments, work, label):
    target = pipe.target
    script_cmd = ' '.join(_quote_ue_arg(x) for x in [Path(script), *arguments])
    command = [target['engine'], target['uproject'], '-run=pythonscript',
               '-script=' + script_cmd,
               '-nullrhi', '-unattended', '-nosound', '-nopause', '-nosplash',
               '-stdout', '-FullStdOutLogOutput']
    run_log = work.parent / f'{work.name}-{label}.log'
    stage_log = work / (label + '.log')
    with run_log.open('w', encoding='utf-8') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    if work.is_dir():
        shutil.copyfile(run_log, stage_log)
    if result.returncode:
        log_text = run_log.read_text(encoding='utf-8', errors='replace') if run_log.is_file() else ''
        if not _unreal_log_completed_cleanly(log_text):
            raise StageFailure(f'{label} failed ({result.returncode}); inspect {run_log}')


def python_script(pipe, source, work, label):
    script = work.parent / f'{work.name}-{label}.py'
    script.write_text(source, encoding='utf-8')
    if work.is_dir():
        (work / (label + '.py')).write_text(source, encoding='utf-8')
    unreal(pipe, script, [], work, label)


def require_report(path, field='saved'):
    if not path.exists():
        raise StageFailure('Stage produced no report: '+str(path))
    report = read(path)
    if not report.get(field) or report.get('failed'):
        raise StageFailure('Stage report failed validation: '+str(path))
    return report


def level_parts(pipe):
    level = PurePosixPath(pipe.target['level'])
    if level.parent.name != level.name:
        raise ValueError('Target level must use <folder>/<name>/<name> naming')
    return level.name, str(level.parent.parent)


def execute(pipe, stage, work, run_dir):
    p = pipe.project
    from .model import TwinModel
    model_path = p.path('build/model')/(p.spec['name']+'.twin')
    xodr = model_path.with_suffix('.xodr')
    report = work/'report.json'
    if stage == 'validate.model':
        from .validate import validate
        from .cli import LANE_IN_DRIVABLE_MIN
        model = TwinModel.load(model_path)
        result = validate(model, xodr.read_text(encoding='utf-8'), out_dir=work)
        if not result.get('topology', {}).get('loaded') or result.get('lane_in_drivable', {}).get('fraction', 0) < LANE_IN_DRIVABLE_MIN:
            raise StageFailure('Model validation failed; inspect '+str(work))
        write(report, result)
        return
    name, map_root = level_parts(pipe)
    shared = ['--name', name, '--map-root', map_root]
    content = Path(pipe.target['uproject']).parent/'Content'
    map_dir = content/pipe.target['level'].removeprefix('/Game/')
    map_dir = map_dir.parent
    owner = map_dir/'twin-project-owner.json'
    if stage == 'unreal.geometry':
        level_file = map_dir/(name+'.umap')
        if level_file.exists():
            if not owner.exists() or read(owner).get('project_id') != p.spec['id']:
                raise PrerequisiteError('Existing level has no ownership receipt for this project; use a new target level or adopt its ownership after review')
            if pipe.receipts.get('target_signature') != pipe.target_signature():
                raise PrerequisiteError('Target changed externally; inspect unmanaged changes before geometry replacement')
            backup = run_dir/'target-backup'
            shutil.copytree(map_dir, backup/'map')
            level_relative = pipe.target['level'].removeprefix('/Game/')
            for directory in ('__ExternalActors__', '__ExternalObjects__'):
                path = content/directory/level_relative
                if path.exists():shutil.copytree(path, backup/directory)
        buildings_mode = 'procedural' if _procedural_buildings_available(pipe.target['uproject']) else 'slabs'
        unreal(pipe, ROOT/'ue/bake_level.py', [*shared, '--manifest', p.path('build/export/manifest.json'),
               '--report', report, '--buildings', buildings_mode, '--python-bin', sys.executable], work, 'geometry')
        require_report(report, 'level_saved')
        write(owner, {'project_id': p.spec['id'], 'level': pipe.target['level']})
    elif stage == 'unreal.traffic':
        sys.path.insert(0, str(ROOT/'tools'))
        from xodr_signals import main as export_signals
        cfg_path = p.path('authoring/traffic.json')
        cfg = read(cfg_path) if cfg_path.exists() else {}
        for kind, types in [('lights', ('1000001', '1000002')), ('signs', ('205', '206', '274'))]:
            signals = work/(kind+'.json')
            export_signals(str(xodr), str(signals), kinds=types, twin_path=str(model_path))
            # Empty inputs still remove prior owned traffic actors after changes.
            script = ROOT/('ue/place_traffic_'+kind+'.py')
            options = [*shared, '--signals', signals, '--report', work/(kind+'-report.json')]
            if kind == 'lights':
                options += ['--rig', ROOT/'ue/rigs', '--style', cfg.get('style', 'eu'),
                            '--default-rig', cfg.get('default_rig', 'eu_pole')]
                for key in ('green','red','amber'):
                    if key in cfg:options += ['--'+key, cfg[key]]
            else:
                options += ['--style', 'VC' if cfg.get('style','eu') == 'eu' else 'MUTCD',
                            '--manifest', ROOT/'ue/assets/sign_catalog_manifest.json']
            unreal(pipe, script, options, work, kind)
            require_report(work/(kind+'-report.json'), 'level_saved')
        write(report, {'saved': True})
    elif stage in ('occupancy.export', 'validate.target'):
        # Load all spatial actors before reading physical supports or instance counts.
        expected = {}
        if stage == 'validate.target':
            expected = {prefix+p.spec['name']:read(pipe.output(tool+'.bake')/'report.json')['instances']
                        for tool,prefix in [('vegetation','VEGETATION_'),('furniture','FURNITURE_')]}
        source = f'''import unreal, json
from pathlib import Path
les=unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
if not les.load_level({pipe.target['level']!r}): raise RuntimeError('Cannot load target')
descs=unreal.WorldPartitionBlueprintLibrary.get_intersecting_actor_descs(unreal.Box(unreal.Vector(-1e9,-1e9,-1e9),unreal.Vector(1e9,1e9,1e9)))
if descs: unreal.WorldPartitionBlueprintLibrary.load_actors([d.guid for d in descs])
actors=unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
rows=[]; counts={{}}; placements={{}}
for a in actors:
    cls=a.get_class().get_name()
    label=a.get_actor_label()
    tags=[str(t) for t in a.tags]
    counts[cls]=counts.get(cls,0)+1
    if cls in ('ProceduralVegetationTool','ProceduralFurnitureTool'):
        if label in placements: raise RuntimeError('Duplicate placement region: '+label)
        placements[label]=a.get_editor_property('baked_instance_count')
    if cls in ('DigitalTwinsTrafficLight','GeoTrafficSign') or label.startswith(('TL_','SIGN_')) or any(t.startswith('TL_') for t in tags):
        loc=a.get_actor_location()
        rows.append(dict(x=loc.x/100,y=loc.y/100,z=loc.z/100,layer=0,name=label or a.get_name(),status='ok'))
Path({str(work/'poles.json')!r}).write_text(json.dumps(rows,indent=2),encoding='utf-8')
for label,count in {expected!r}.items():
    if placements.get(label)!=count: raise RuntimeError('Saved instance count mismatch: '+label)
Path({str(report)!r}).write_text(json.dumps(dict(saved=True,actors=counts,placements=placements,level={pipe.target['level']!r})),encoding='utf-8')
print('[twin_stage] exit 0', flush=True)
'''
        python_script(pipe, source, work, stage.replace('.', '_'))
        require_report(report)
        if stage == 'occupancy.export':
            from shapely.geometry import Point
            from shapely.strtree import STRtree
            model = TwinModel.load(model_path)
            rows = read(work/'poles.json')
            surf_geoms = [s.geometry for s in model.surfaces]
            surf_layers = [_parse_layer(s.tags.get('layer')) for s in model.surfaces]
            tree = STRtree(surf_geoms) if surf_geoms else None
            for row in rows:
                point = Point(row['x'], -row['y'])
                best_by_layer = {}
                if tree is not None:
                    for idx in tree.query(point, predicate='dwithin', distance=0.75):
                        s = model.surfaces[int(idx)]
                        lyr = surf_layers[int(idx)]
                        dz = abs(float(model.sample_z(point.x, point.y, layer=lyr)) + s.z_offset - row['z'])
                        if dz < 1.5 and (lyr not in best_by_layer or dz < best_by_layer[lyr]):
                            best_by_layer[lyr] = dz
                if not best_by_layer:
                    raise PrerequisiteError('Physical support has ambiguous surface/layer: '+row['name'])
                if len(best_by_layer) > 1:
                    ranked = sorted(best_by_layer.items(), key=lambda kv: kv[1])
                    if ranked[1][1] - ranked[0][1] < 0.25:
                        raise PrerequisiteError('Physical support has ambiguous surface/layer: '+row['name'])
                    row['layer'] = ranked[0][0]
                else:
                    row['layer'] = next(iter(best_by_layer))
            write(work/'poles.json', rows)
    elif stage.endswith('.plan'):
        tool = stage.split('.')[0]
        from importlib import import_module
        planner = import_module('twinmodel.'+tool)
        config = read(p.path(f'authoring/{tool}.json')) if p.path(f'authoring/{tool}.json').exists() else {}
        poles = read(pipe.output('occupancy.export')/'poles.json')
        args = [TwinModel.load(model_path), config, poles]
        if tool == 'furniture':
            validated = read(pipe.output('vegetation.bake')/'validated-plan.json')
            if not validated.get('ground_validated'):
                raise PrerequisiteError('Vegetation ground validation is missing')
            args.append(validated)
        plan = planner.plan(*args)
        plan['region'] = p.spec['name']
        # Flag orphan overrides instead of losing manual edits on regenerated IDs.
        ids = {g['id'] for g in plan['review']}
        missing = set(config.get('overrides', {})) - ids
        if missing:
            raise PrerequisiteError('Review orphan placement overrides: '+', '.join(sorted(missing)))
        write(work/'plan.json', plan)
    elif stage.endswith('.bake'):
        tool = stage.split('.')[0]
        unreal(pipe, ROOT/f'ue/setup_{tool}_tool.py', [], work, 'setup')
        options = ['--stored-plan-path', pipe.output(stage)/'validated-plan.json'] if tool == 'furniture' else []
        unreal(pipe, ROOT/f'ue/bake_{tool}.py', [*shared,
               '--plan', pipe.output(tool+'.plan')/'plan.json', '--region', p.spec['name'],
               '--report', report, *options], work, tool)
        require_report(report)
        if not (work/'validated-plan.json').exists():
            raise StageFailure('Missing ground-validated plan')
    else:
        raise ValueError('Unknown stage '+stage)
