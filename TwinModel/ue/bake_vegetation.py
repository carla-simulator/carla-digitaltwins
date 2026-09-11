"""Import, generate with PCG, validate count, and bake a vegetation region.

UnrealEditor-Cmd <project> -run=pythonscript -script="<this> --name EixampleDemo
--plan /absolute/plan.json --region eixample --report /absolute/report.json"
Region labels are ownership keys: re-running replaces only that tool's instances.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import hashlib

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_level import MAP_ROOT, save_all, spawn


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--name',required=True)
    ap.add_argument('--map-root',default=MAP_ROOT)
    ap.add_argument('--plan',required=True)
    ap.add_argument('--region',default='main')
    ap.add_argument('--report',required=True)
    ap.add_argument('--timeout',type=float,default=120)
    args=ap.parse_args()
    les=unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(f'{args.map_root}/{args.name}/{args.name}'):
        raise RuntimeError('Cannot load map')
    world=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    # Commandlets initially load only always-loaded actors. Ground meshes are
    # spatial actors; explicitly load the plan region before collision projection.
    with open(args.plan) as f:
        plan=json.load(f)
        points=plan['points']
    if points:
        lo=[min(p[k]*100 for p in points)-1000 for k in ('x','y','z')]
        hi=[max(p[k]*100 for p in points)+1000 for k in ('x','y','z')]
        descs=unreal.WorldPartitionBlueprintLibrary.get_intersecting_actor_descs(
            unreal.Box(unreal.Vector(*lo),unreal.Vector(*hi)))
        if descs:
            unreal.WorldPartitionBlueprintLibrary.load_actors([d.guid for d in descs])
    sub=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    label='VEGETATION_'+args.region
    found=[a for a in sub.get_all_level_actors() if a.get_actor_label()==label]
    if len(found)>1:
        raise RuntimeError('Duplicate vegetation region actors; resolve ownership first')
    actor=found[0] if found else spawn(world,unreal.ProceduralVegetationTool,unreal.Vector(),unreal.Rotator(),label,'Vegetation')
    if not isinstance(actor,unreal.ProceduralVegetationTool):
        raise RuntimeError('Region label is already owned by another actor')
    actor.set_editor_property('plan_file',unreal.FilePath(os.path.abspath(args.plan)))
    if not actor.import_plan():
        raise RuntimeError(actor.status)
    started=time.monotonic()
    actor.preview()
    while actor.is_generating():
        if time.monotonic()-started>args.timeout:
            raise RuntimeError('PCG generation timeout: '+actor.status)
        actor.tick_commandlet_generation()
        time.sleep(.005)
    if not actor.bake():
        raise RuntimeError(actor.status)
    validated={**plan,'source_plan_sha256':hashlib.sha256(Path(args.plan).read_bytes()).hexdigest(),
               'ground_validated':True}
    poses={p.id:p.transform.translation for p in actor.points}
    for point in validated['points']:
        pose=poses.get(point['id'])
        if pose is None:raise RuntimeError('Missing validated plant: '+point['id'])
        point.update(x=pose.x/100,y=pose.y/100,z=pose.z/100)
    Path(args.report).with_name('validated-plan.json').write_text(json.dumps(validated,indent=2)+'\n')
    if not save_all():
        raise RuntimeError('Level save failed')
    report={'map':args.name,'region':args.region,'actor':label,'instances':actor.baked_instance_count,
            'plan_points':len(actor.points),'vegetation_points':len(points),
            'footings':len(actor.points)-len(points),'seconds':time.monotonic()-started,'status':actor.status,'saved':True}
    with open(args.report,'w') as f:json.dump(report,f,indent=2)
    print('VEGETATION_BAKED',json.dumps(report),flush=True)

if __name__=='__main__':main()
