"""Recoverable checkpoints for framework-owned Unreal target content."""
from pathlib import Path
import shutil
import uuid

from .project import checksum, locked, read, write


def content_paths(target):
    content = Path(target['uproject']).parent/'Content'
    level = target['level'].removeprefix('/Game/')
    paths = [str(Path(level).parent), '__ExternalActors__/'+level, '__ExternalObjects__/'+level]
    mount = Path(level).parts[0]
    paths += [mount+'/__ExternalActors__/'+level, mount+'/__ExternalObjects__/'+level]
    paths += ['Carla/Static/'+semantic+'/Twins/'+Path(level).name
              for semantic in ('Road', 'SideWalk', 'RoadLine', 'Building', 'Terrain')]
    return content, paths


def checkpoint(pipe, directory):
    content, paths = content_paths(pipe.target)
    directory.mkdir(parents=True)
    entries = {}
    for relative in paths:
        source = content/relative
        entries[relative] = checksum(source)
        if source.exists():
            shutil.copytree(source, directory/'content'/relative)
    write(directory/'checkpoint.json', {'target': pipe.target, 'entries': entries,
                                        'receipts': pipe.receipts})


def restore(project, target_name, run, stage, dry_run=False):
    from .project_pipeline import Pipeline, target_lock
    pipe = Pipeline(project, target_name)
    source = project.path('runs/'+run+'/checkpoints/'+stage)
    manifest = read(source/'checkpoint.json')
    if manifest['target'] != pipe.target:
        raise ValueError('Checkpoint belongs to a different target')
    content, paths = content_paths(pipe.target)
    if set(manifest['entries']) != set(paths):
        raise ValueError('Checkpoint content paths do not match the target')
    for relative, expected in manifest['entries'].items():
        if checksum(source/'content'/relative) != expected:
            raise ValueError('Checkpoint checksum mismatch: '+relative)
    if dry_run:
        return {'restore': paths, 'checkpoint': str(source)}
    with locked(project.root), target_lock(pipe.target):
        # Retain the displaced target too, including a partial failed apply.
        displaced = project.path('runs/'+run+'/before-restore-'+stage)
        if displaced.exists():
            raise ValueError('This checkpoint was already restored; inspect its recovery run')
        checkpoint(pipe, displaced)
        for relative, expected in manifest['entries'].items():
            destination = content/relative
            if destination.exists():shutil.rmtree(destination)
            if expected is not None:
                shutil.copytree(source/'content'/relative, destination)
        write(pipe.state, manifest['receipts'])
    return {'restored': True, 'checkpoint': str(source)}


def adopt(project, target_name, dry_run=False):
    """Inspect a legacy generated level before giving this project write ownership.

    This records ownership only; it does not certify geometry or placements as
    current. A subsequent apply rebuilds the generated level from project inputs.
    """
    from .project_pipeline import Pipeline, target_lock, PrerequisiteError
    from .project_stages import python_script, require_report
    pipe = Pipeline(project, target_name)
    if not pipe.target:
        raise ValueError('Configure the target first')
    content, _ = content_paths(pipe.target)
    map_dir = content/str(Path(pipe.target['level'].removeprefix('/Game/')).parent)
    deployed = map_dir/'OpenDrive'/(Path(pipe.target['level']).name+'.xodr')
    model_xodr = project.path('build/model')/(project.spec['name']+'.xodr')
    if not deployed.exists() or not model_xodr.exists() or deployed.read_bytes() != model_xodr.read_bytes():
        raise PrerequisiteError('Existing target OpenDRIVE differs from the project model; reconcile it before adopting ownership')
    with locked(project.root), target_lock(pipe.target):
        work = project.path('runs/inspect-'+uuid.uuid4().hex)
        work.mkdir(parents=True)
        before = pipe.target_signature()
        report = work/'report.json'
        script = f'''import unreal, json
from pathlib import Path
les=unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
if not les.load_level({pipe.target['level']!r}): raise RuntimeError('Cannot load level')
descs=unreal.WorldPartitionBlueprintLibrary.get_intersecting_actor_descs(unreal.Box(unreal.Vector(-1e9,-1e9,-1e9),unreal.Vector(1e9,1e9,1e9)))
if descs:unreal.WorldPartitionBlueprintLibrary.load_actors([d.guid for d in descs])
unknown=[]; inventory=[]
for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors():
    cls=a.get_class().get_name(); label=a.get_actor_label(); folder=str(a.get_folder_path())
    known=cls in ('WorldSettings','WorldDataLayers','LevelBounds','LevelScriptActor','PlayerStart','VehicleSpawnPoint','WorldPartitionMiniMap','PCGWorldActor')
    known=known or (cls=='DirectionalLight' and label in ('Light Source','LightSource','DirectionalLight'))
    known=known or (cls=='BP_Sky_Sphere_Movable_C' and label in ('Sky Sphere','SkySphere','BP_Sky_Sphere_Movable'))
    known=known or (cls=='DigitalTwinsTrafficLight' and label.startswith('TL_'))
    known=known or (cls=='GeoTrafficSign' and label.startswith('SIGN_'))
    known=known or (cls=='ProceduralVegetationTool' and label.startswith('VEGETATION_'))
    known=known or (cls=='ProceduralFurnitureTool' and label.startswith('FURNITURE_'))
    known=known or (cls=='BP_Carla_Sky_C' and label in ('Sky','CarlaSky','BP_Carla_Sky'))
    known=known or (cls=='BP_BuildingGen_C' and folder.startswith('Twin/'))
    known=known or (cls=='StreetMapActor' and label=='TwinStreetMap' and folder=='Twin/Buildings')
    known=known or (cls=='Actor' and label.startswith('Bldg_') and folder=='Twin/Buildings')
    if cls=='StaticMeshActor' and folder.startswith('Twin/'):
        mesh=a.static_mesh_component.static_mesh
        known=bool(mesh and ('/Twins/'+{Path(pipe.target['level']).name!r}+'/') in mesh.get_path_name())
    record=dict(label=label,actor_class=cls,folder=folder,generated=known)
    inventory.append(record)
    if not known:unknown.append(record)
Path({str(report)!r}).write_text(json.dumps(dict(saved=True,inventory=inventory,unmanaged=unknown),indent=2))
'''
        python_script(pipe, script, work, 'inspect_target')
        result = require_report(report)
        if before != pipe.target_signature():
            raise PrerequisiteError('Target changed during ownership inspection')
        if result['unmanaged'] and not dry_run:
            raise PrerequisiteError('Unmanaged actors prevent automatic replacement; inspect '+str(report))
        if not dry_run:
            owner = map_dir/'twin-project-owner.json'
            if owner.exists() and read(owner).get('project_id') != project.spec['id']:
                raise PrerequisiteError('Target belongs to another project')
            write(owner, {'project_id': project.spec['id'], 'level': pipe.target['level'], 'inspection': work.name})
            pipe.receipts = {'stages': {}, 'target_signature': pipe.target_signature()}
            write(pipe.state, pipe.receipts)
        return {'adopted': not dry_run, 'can_adopt': not result['unmanaged'], 'unmanaged': result['unmanaged'],
                'inspection': str(report), 'actors': len(result['inventory'])}
