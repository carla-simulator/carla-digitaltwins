"""Cook a saved project target as a CARLA content pack, with distribution provenance."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import uuid

from .project import _replace_retry, checksum, locked, write
from .project_pipeline import Pipeline, PrerequisiteError, target_lock


def file_hash(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def verify_archive(archive, level, xodr, logic=None):
    """Verify hashes, required cooked containers and this map's runtime sidecars."""
    with tarfile.open(archive, 'r:*') as tar:
        manifests = [m for m in tar.getmembers() if m.isfile() and m.name.endswith('/carla-pack.json')]
        if len(manifests) != 1:
            raise RuntimeError('Expected one carla-pack.json in cooked pack')
        extracted = tar.extractfile(manifests[0])
        if extracted is None:
            raise RuntimeError('Cannot read carla-pack.json in cooked pack')
        manifest = json.load(extracted)
        top = manifests[0].name.rsplit('/', 1)[0]
        maps = [m for m in manifest['maps'] if m['package'] == level]
        if len(maps) != 1: raise RuntimeError('Pack does not contain the target map')
        entry = maps[0]
        for key, source in [('xodr', xodr), ('map_logic', logic)]:
            if source:
                if not entry.get(key): raise RuntimeError('Pack omitted '+key)
                try:
                    member = tar.extractfile(top+'/Content/'+entry[key])
                except KeyError as exc:
                    raise RuntimeError('Pack omitted '+key) from exc
                if member is None or hashlib.file_digest(member, 'sha256').hexdigest() != file_hash(source):
                    raise RuntimeError('Pack sidecar differs from saved target: '+key)
        if logic and Path(entry['xodr']).parent != Path(entry['map_logic']).parent:
            raise RuntimeError('Traffic logic must be beside its map OpenDRIVE')
        if 'files' in manifest:
            files = manifest['files']
            if not files or not any(rel.endswith('.pak') for rel in files):
                raise RuntimeError('Pack is missing cooked .pak container')
            for rel, expected_sha in files.items():
                if rel == 'carla-pack.json':
                    continue
                expected_hex = str(expected_sha).removeprefix('sha256:')
                try:
                    member = tar.extractfile(f'{top}/{rel}')
                except KeyError as exc:
                    raise RuntimeError('Pack is missing listed file: '+rel) from exc
                if member is None or hashlib.file_digest(member, 'sha256').hexdigest() != expected_hex:
                    raise RuntimeError('Pack file digest mismatch: '+rel)
    return manifest


def _has_world_partition_nav(project, target_name: str, uproject: Path, level: str) -> bool:
    """Detect CARLA 1.0 World Partition pedestrian navigation when no legacy Nav/<map>.bin exists."""
    candidates = [
        project.path(f'build/targets/{target_name}/unreal.geometry/report.json'),
        project.path('build/export/bake_report.json'),
    ]
    for report_path in candidates:
        if not report_path.is_file():
            continue
        try:
            geom = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        nav_info = geom.get('navigation') or {}
        builders = geom.get('navigation_builders')
        has_recast = int(nav_info.get('recast_navmesh', 0) or 0) > 0
        builders_ok = isinstance(builders, dict) and bool(builders) and all(bool(v) for v in builders.values())
        return has_recast and builders_ok
    ext_actors = uproject.parent / 'Content' / '__ExternalActors__' / level.removeprefix('/Game/')
    if ext_actors.is_dir():
        has_recast_asset = False
        has_chunk_asset = False
        for asset in ext_actors.rglob('*.uasset'):
            try:
                with asset.open('rb') as f:
                    head = f.read(65536)
            except OSError:
                continue
            if b'RecastNavMesh' in head:
                has_recast_asset = True
            if b'NavigationDataChunkActor' in head:
                has_chunk_asset = True
            if has_recast_asset and has_chunk_asset:
                return True
    return False


def package_project(project, target_name, base, out, name=None, version='1.0.0',
                    saved_target=False, require_nav=False, dry_run=False):
    pipe = Pipeline(project, target_name)
    if not pipe.target: raise PrerequisiteError('Configure an Unreal target first')
    target = pipe.target
    uproject = Path(target['uproject'])
    packer = uproject.parents[2]/'Util/ContentPacks/carla_pack.py'
    base = Path(base).resolve()
    if not base.exists(): raise PrerequisiteError('Missing CARLA base release metadata: '+str(base))
    if not packer.is_file(): raise PrerequisiteError('Target checkout has no carla-pack tool')
    if not Path(target['engine']).is_file(): raise PrerequisiteError('Target editor does not exist')
    if not name:
        stem = re.sub('[^A-Za-z0-9_]', '_', project.spec['name']).title().replace('_', '')
        if not stem or not stem[0].isalpha():
            stem = 'Map' + stem
        name = stem + 'Pack'
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', name): raise ValueError('Invalid content pack name')
    if not re.fullmatch(r'\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?', version): raise ValueError('Invalid pack version')
    level = target['level']
    map_file = uproject.parent/'Content'/(level.removeprefix('/Game/')+'.umap')
    if not map_file.is_file(): raise PrerequisiteError('Saved Unreal map does not exist: '+str(map_file))
    # Use the same discovery rules as carla-pack without loading a second tool module.
    map_name = map_file.stem
    map_dir = map_file.parent
    maps_root = next((p for p in (map_dir, *map_dir.parents) if p.name == 'Maps'), map_dir)
    xodr = next((p for p in [map_dir/'OpenDrive'/(map_name+'.xodr'),
                maps_root/'OpenDrive'/(map_name+'.xodr'), map_dir/(map_name+'.xodr')] if p.is_file()), None)
    if xodr is None: raise PrerequisiteError('Saved map is missing OpenDRIVE')
    import xml.etree.ElementTree as ET
    if ET.parse(xodr).getroot().tag != 'OpenDRIVE': raise ValueError('Invalid OpenDRIVE root')
    logic = xodr.with_name('map_logic.json')
    logic = logic if logic.is_file() else None
    if logic: json.loads(logic.read_text(encoding='utf-8'))
    nav = next((p for p in [maps_root/'Nav'/(map_name+'.bin'), map_dir/'Nav'/(map_name+'.bin')] if p.is_file()), None)
    has_nav = nav is not None or _has_world_partition_nav(project, target_name, uproject, level)
    if require_nav and not has_nav: raise PrerequisiteError('Saved map has no pedestrian navigation; generate it before packaging')
    status = pipe.status()
    stale = [s['stage'] for s in status['stages'] if s['state'] != 'current']
    if stale and not saved_target:
        raise PrerequisiteError('Apply the current project before packaging, or use --saved-target to explicitly package the existing saved map; stale stages: '+', '.join(stale))
    pack_manifest = uproject.parent/'Plugins/Packs'/name/'carla-pack.json'
    if pack_manifest.exists():
        existing = json.loads(pack_manifest.read_text(encoding='utf-8'))
        if existing.get('catalogs') or any(m.get('package') != level for m in existing.get('maps', [])):
            raise PrerequisiteError('Pack name already contains other content; choose a dedicated --name')
    out = Path(out).resolve()
    engine_path = Path(target['engine'])
    engine_parts_lower = {part.lower() for part in engine_path.parts}
    platform = 'Win64' if ('win64' in engine_parts_lower or engine_path.suffix.lower() == '.exe') else 'Linux'
    command = [sys.executable, str(packer), 'create', name, level, '--project', str(uproject),
               '--engine', str(engine_path.parents[3]), '--platform', platform,
               '--config', 'Shipping', '--base', str(base), '--out', str(out), '--pack-version', version]
    result = {'schema':'twin-project-package/1', 'project':project.spec['name'], 'target':target_name,
              'level':level, 'name':name, 'version':version, 'platform':platform,
              'source':'saved-target' if saved_target else 'current-project', 'pending_stages':stale,
              'pedestrian_navigation':has_nav, 'traffic_logic':bool(logic),
              'runtime_requirement':'CARLA UE 5.8 with the native DigitalTwin traffic, vegetation and furniture extensions',
              'command':command,
              'dry_run':dry_run}
    if dry_run: return result
    with locked(project.root), target_lock(target):
        status = pipe.status()
        stale = [s['stage'] for s in status['stages'] if s['state'] != 'current']
        if stale and not saved_target:
            raise PrerequisiteError('Project changed before packaging; apply it first')
        result['pending_stages'] = stale
        run = project.path('runs/package-'+uuid.uuid4().hex);run.mkdir(parents=True)
        result.update(run=str(run), state='running', revision=status['revision'],
                      target_signature=pipe.target_signature(), base_metadata=checksum(base))
        write(run/'package.json', result)
        staging = run/'distribution';staging.mkdir()
        command[command.index('--out')+1] = str(staging)
        try:
            with (run/'cook.log').open('w', encoding='utf-8') as log:
                rc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, close_fds=True).returncode
            if rc: raise RuntimeError('Content pack cook failed; see '+str(run/'cook.log'))
            archives = list(staging.glob('*.tar.gz'))
            if len(archives) != 1: raise RuntimeError('Expected exactly one cooked archive')
            archive = archives[0]
            manifest = verify_archive(archive, level, xodr, logic)
            with (run/'inspect.log').open('w', encoding='utf-8') as log:
                rc = subprocess.run([sys.executable,str(packer),'inspect',str(archive)], stdout=log,stderr=subprocess.STDOUT, close_fds=True).returncode
            if rc: raise RuntimeError('Pack integrity inspection failed; see '+str(run/'inspect.log'))
            if pipe.target_signature() != result['target_signature'] or pipe.status()['revision'] != result['revision']:
                raise RuntimeError('Project or saved target changed during cook; archive not published')
            out.mkdir(parents=True,exist_ok=True)
            destination = out/archive.name
            if destination.exists(): raise ValueError('Distribution already exists; choose a new version or output directory')
            import shutil
            tmp_dest = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                shutil.copy2(archive, tmp_dest)
                _replace_retry(tmp_dest, destination)
            finally:
                tmp_dest.unlink(missing_ok=True)
            result.update(state='success', archive=str(destination), sha256=file_hash(destination),
                          base_release=manifest.get('base_release'), dry_run=False)
            write(out/(archive.name+'.json'), result)
        except Exception as exc:
            result.update(state='failed',error=str(exc));write(run/'package.json',result);raise
        write(run/'package.json',result)
    return result
