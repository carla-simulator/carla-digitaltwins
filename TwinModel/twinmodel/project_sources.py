"""Acquire source revisions once; subsequent model builds are offline."""
from pathlib import Path
import shutil
import tempfile
import uuid

from .project import ROOT, Project, checksum, locked, read, write


def acquire(project, refresh=False):
    if project.spec['source']['mode'] != 'osm':
        return
    snapshot = project.path('sources/acquisition.json')
    if snapshot.exists() and not refresh:
        pinned = read(snapshot)
        for name, expected in pinned['files'].items():
            if checksum(project.path('sources/'+name)) != expected:
                raise ValueError('Pinned source changed or missing: '+name)
        return
    from .ingest.osm import fetch_overpass, parse_osm
    from .frame import LocalFrame
    from .cli import select_profile
    from .ingest.elevation import fetch_dem
    from .ingest.imagery import fetch_ortho
    bbox = project.spec['source']['bbox']
    with tempfile.TemporaryDirectory(dir=project.root) as folder:
        staging = Path(folder)
        cache = staging/'cache'
        raw = read(project.path('sources/osm.json')) if project.path('sources/osm.json').exists() and not refresh else fetch_overpass(tuple(bbox), cache_dir=cache)
        write(staging/'osm.json', raw)
        frame = LocalFrame.from_bbox(*bbox)
        profile, _ = select_profile(project.spec['profile'], parse_osm(raw), frame, bbox, cache)
        files = ['osm.json']
        settings = project.spec['build']
        for name, enabled, fetch, save in (
            ('dem.npz', settings.get('dem', False), fetch_dem, lambda x, p: x.to_npz(p)),
            ('imagery.tif', settings.get('imagery', False), fetch_ortho, lambda x, p: x.save_geotiff(p, frame=frame))):
            if enabled:
                existing = project.path('sources/'+name)
                if existing.exists() and not refresh:
                    shutil.copyfile(existing, staging/name)
                else:
                    value = fetch(frame, bbox, cache_dir=cache,
                                  sources=profile.sources.dem if name == 'dem.npz' else profile.sources.ortho)
                    if value is None:
                        raise ValueError('No source available for '+name+'; explicitly disable it to build without it')
                    save(value, staging/name)
                files.append(name)
        # Keep an immutable prior revision for recovery from an explicit refresh.
        if snapshot.exists():
            old = project.path('runs')/('source-'+uuid.uuid4().hex)
            old.mkdir(parents=True)
            for name in read(snapshot)['files']:
                shutil.copyfile(project.path('sources/'+name), old/name)
            shutil.copyfile(snapshot, old/'acquisition.json')
        for name in files:
            shutil.copyfile(staging/name, project.path('sources/'+name))
        write(snapshot, {'profile': profile.name, 'files': {
            name: checksum(project.path('sources/'+name)) for name in files}})


def import_recipe(project, build_dir, cache_dir):
    """Attach original local compiler inputs to an adopted model without fetching."""
    with locked(project.root):
        report = read(Path(build_dir)/'report.json')
        recipe = report['build']['args']
        fixture = Path(recipe['fixture'])
        if not fixture.is_absolute():
            fixture = ROOT/fixture
        if not fixture.is_file():
            raise ValueError('Original OSM fixture is missing: '+str(fixture))
        bbox = recipe['bbox']
        from .ingest.elevation import _cache_path as dem_path, DEFAULT_RES
        from .ingest.imagery import _cache_path as imagery_path, NATIVE_RES, DEFAULT_LAYER as LAYER
        dem = dem_path(Path(cache_dir), bbox, DEFAULT_RES)
        imagery = imagery_path(Path(cache_dir), bbox, NATIVE_RES, LAYER, provider='icgc')
        inputs = {'osm.json': fixture}
        if not recipe.get('no_dem', False):
            inputs['dem.npz'] = dem
        if not recipe.get('no_imagery', False):
            # Prefer the provider and resolution recorded by the original build.
            info = report['build'].get('imagery', {})
            provider = info.get('source', 'icgc')
            imagery = imagery_path(Path(cache_dir), bbox, info.get('dx', NATIVE_RES),
                                   LAYER if provider == 'icgc' else '', provider=provider)
            inputs['imagery.tif'] = imagery
        for name, path in inputs.items():
            if not path.is_file():
                raise ValueError('Original source cache is missing: '+str(path))
        for name, path in inputs.items():
            shutil.copyfile(path, project.path('sources/'+name))
        project.spec['source'] = {'mode': 'osm', 'bbox': bbox}
        project.spec['profile'] = recipe['profile']
        project.spec['build'] = {'dem': 'dem.npz' in inputs, 'imagery': 'imagery.tif' in inputs,
                                 'refine': not recipe.get('no_refine', False),
                                 'mask_method': recipe.get('mask_method', 'classical')}
        write(project.root/'project.json', project.spec)
        write(project.path('sources/acquisition.json'), {
            'profile': report['build']['profile']['name'],
            'files': {name: checksum(project.path('sources/'+name)) for name in inputs}})
    return {'imported': True, 'source_files': list(inputs)}
