"""Portable project archives and revision-checked authoring inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from .project import Project, checksum, digest, locked, read, write

ARCHIVE_SCHEMA = 'twin-project-archive/1'


class RevisionConflict(ValueError):
    pass


def revision(project):
    return digest({'recipe': read(project.root/'project.json'),
                   'authoring': checksum(project.path('authoring'))})


def validate_authoring(name, value):
    if name == 'layout':
        from .corrections import SCHEMA, validate
        if value.get('schema') != SCHEMA:
            raise ValueError('Unsupported corrections schema')
        problems = validate(value['ops'])
        if problems:
            raise ValueError(str(problems))
    elif name in ('vegetation', 'furniture'):
        from importlib import import_module
        value = import_module('twinmodel.' + name).configuration(value)
    elif name == 'traffic':
        allowed = {'style', 'default_rig', 'green', 'red', 'amber', 'buildings'}
        if set(value) - allowed:
            raise ValueError('Unsupported traffic settings')
        if value.get('style', 'eu') not in ('eu', 'na'):
            raise ValueError('Invalid traffic style')
        for key in ('green', 'red', 'amber'):
            if key in value and (not isinstance(value[key], (int, float)) or not 0 < value[key] < 3600):
                raise ValueError('Invalid traffic duration')
    else:
        raise ValueError('Unknown authoring document')
    # Reject NaNs even in nested free-form metadata.
    json.dumps(value, allow_nan=False)
    return value


def save_authoring(project, name, value, expected_revision):
    with locked(project.root):
        if expected_revision != revision(project):
            raise RevisionConflict('Project changed; reload before saving your edits')
        value = validate_authoring(name, value)
        write(project.path(f'authoring/{name}.json'), value)
        return {'revision': revision(project), 'saved': True}


def validate_project(project):
    for file in project.path('authoring').glob('*.json'):
        validate_authoring(file.stem, read(file))
    mode = project.spec['source']['mode']
    if mode == 'snapshot':
        from .model import TwinModel
        model = TwinModel.load(project.path('sources/model.twin'))
        frame = {'origin_lat': model.origin_lat, 'origin_lon': model.origin_lon,
                 'bbox_wgs84': list(model.bbox_wgs84)}
    else:
        bbox = project.spec['source'].get('bbox', [])
        if len(bbox) != 4 or not (-90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180):
            raise ValueError('Invalid source bounding box')
        if project.path('sources/osm.json').exists() and 'elements' not in read(project.path('sources/osm.json')):
            raise ValueError('Invalid OSM snapshot')
        frame = {'bbox_wgs84': bbox}
    expected = project.spec.get('frame')
    if expected and mode == 'snapshot':
        if expected.get('crs') != 'local-enu' or any(abs(expected[k]-frame[k]) > 1e-9 for k in ('origin_lat','origin_lon')):
            raise ValueError('Project frame does not match its model snapshot')
    return {'valid': True, 'frame': frame, 'revision': revision(project)}


def export_project(project, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Archive already exists')
    with locked(project.root):
        validate_project(project)
        files = [project.root/'project.json']
        files += sorted(p for p in project.path('authoring').rglob('*') if p.is_file())
        # Keep actual source inputs; omit adoption reports, plans and machine logs.
        files += sorted(p for p in project.path('sources').rglob('*') if p.is_file()
                        and not any(part.startswith('adopted-') for part in p.relative_to(project.root).parts)
                        and p.name != 'original-report.json')
        payload = {}
        for file in files:
            if file.is_symlink() or not file.resolve().is_relative_to(project.root):
                raise ValueError('Portable projects cannot contain external links')
            data = file.read_bytes()
            if file.name == 'model.json' and file.parent.suffix == '.twin':
                model = json.loads(data)
                # Build diagnostics contain host-specific paths, not model geometry.
                model.get('metadata', {}).pop('build', None)
                model.get('metadata', {}).pop('corrections', None)
                data = (json.dumps(model, indent=2) + '\n').encode()
            payload[str(file.relative_to(project.root))] = data
        reference = project.path('build/model')/(project.spec['name']+'.twin')
        if reference.exists():
            for file in sorted(p for p in reference.rglob('*') if p.is_file()):
                if not file.resolve().is_relative_to(project.root):
                    raise ValueError('External link in editor reference model')
                data = file.read_bytes()
                if file.name == 'model.json':
                    value = json.loads(data)
                    value.get('metadata', {}).pop('build', None)
                    value.get('metadata', {}).pop('corrections', None)
                    data = (json.dumps(value, indent=2)+'\n').encode()
                payload['sources/editor-reference.twin/'+str(file.relative_to(reference))] = data
        manifest = {'schema': ARCHIVE_SCHEMA, 'files': {
            name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in payload.items():
                    archive.writestr(name, data)
                archive.writestr('archive.json', json.dumps(manifest))
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {'archive': str(destination), 'files': len(payload)}


def import_project(source, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Import destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.twin-import-', dir=destination.parent))
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or sum(i.file_size for i in infos) > 8 * 1024**3:
                raise ValueError('Duplicate archive paths or oversized project')
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or path.as_posix() != info.filename or '..' in path.parts or '\\' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('Unsafe archive path')
            manifest = json.loads(archive.read('archive.json'))
            if manifest.get('schema') != ARCHIVE_SCHEMA or set(names) != set(manifest['files']) | {'archive.json'}:
                raise ValueError('Invalid archive manifest')
            for name, expected in manifest['files'].items():
                if name != 'project.json' and not name.startswith(('sources/', 'authoring/')):
                    raise ValueError('Archive includes nonportable project state')
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('Archive checksum mismatch: ' + name)
                path = temporary/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        imported = Project(temporary)
        validate_project(imported)
        reference = temporary/'sources/editor-reference.twin'
        if not reference.exists():reference = temporary/'sources/model.twin'
        if reference.exists():
            from .model import TwinModel
            TwinModel.load(reference)
            # A review baseline is not a successful build receipt. This lets users
            # open and repair pending annotations after importing a project.
            shutil.copytree(reference, temporary/'build/model'/(imported.spec['name']+'.twin'))
        (temporary/'.gitignore').write_text('build/\nstate/\nruns/\n')
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {'project': str(destination), **validate_project(Project(destination))}


def import_model(project, source):
    from .model import TwinModel
    with locked(project.root):
        old_path = project.path('sources/model.twin')
        if not old_path.exists():
            raise ValueError('Model replacement requires a snapshot project')
        old, new = TwinModel.load(old_path), TwinModel.load(source)
        if (old.origin_lat, old.origin_lon) != (new.origin_lat, new.origin_lon):
            raise ValueError('Coordinate origin changed; authoring coordinates must be reviewed')
        # IDs of generated groups depend on geometry; require explicit review if
        # positional overrides exist rather than silently dropping them.
        for name in ('vegetation', 'furniture'):
            path = project.path(f'authoring/{name}.json')
            if path.exists() and read(path).get('overrides'):
                raise ValueError('Review and clear placement overrides before replacing the model')
        if read(project.path('authoring/layout.json')).get('ops'):
            raise ValueError('Snapshot has layout corrections; reconcile them before replacing its base model')
        with tempfile.TemporaryDirectory(dir=project.root) as folder:
            staged = Path(folder)/'model.twin'
            shutil.copytree(source, staged)
            previous = project.path('sources/previous-model.twin')
            if previous.exists():
                shutil.rmtree(previous)
            old_path.rename(previous)
            staged.rename(old_path)
        project.path('sources/model.xodr').unlink(missing_ok=True)
        project.spec['source']['layout_baseline'] = checksum(project.path('authoring/layout.json'))
        write(project.root/'project.json', project.spec)
    return validate_project(project)
