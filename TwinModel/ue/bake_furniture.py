"""Import, generate with PCG, validate count, and bake a furniture region.

UnrealEditor-Cmd <project> -run=pythonscript -script="<this> --name EixampleDemo
--plan /absolute/plan.json --region eixample --report /absolute/report.json"
Region labels are ownership keys: re-running replaces only that tool's instances.
"""
import argparse
import json
import os
import sys
import time
import tempfile
import hashlib
from pathlib import Path
from collections import defaultdict, Counter

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
    ap.add_argument('--stored-plan-path',help='Stable plan path to store after a staged bake')
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
    if plan.get('schema')!='twin-furniture-plan/1': raise ValueError('Unsupported furniture plan')
    if plan.get('region',args.region)!=args.region: raise ValueError('Plan belongs to another region')
    if points:
        lo=[min(p[k]*100 for p in points)-1000 for k in ('x','y','z')]
        hi=[max(p[k]*100 for p in points)+1000 for k in ('x','y','z')]
        descs=unreal.WorldPartitionBlueprintLibrary.get_intersecting_actor_descs(
            unreal.Box(unreal.Vector(*lo),unreal.Vector(*hi)))
        if descs:
            unreal.WorldPartitionBlueprintLibrary.load_actors([d.guid for d in descs])
    sub=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    label='FURNITURE_'+args.region
    found=[a for a in sub.get_all_level_actors() if a.get_actor_label()==label]
    if len(found)>1:
        raise RuntimeError('Duplicate furniture region actors; resolve ownership first')
    actor=found[0] if found else spawn(world,unreal.ProceduralFurnitureTool,unreal.Vector(),unreal.Rotator(),label,'Furniture')
    if not isinstance(actor,unreal.ProceduralFurnitureTool):
        raise RuntimeError('Region label is already owned by another actor')
    # Ground validation is group-atomic: unsupported pairs are visible in the
    # report, never silently split into isolated bins. All other import errors
    # abort and preserve the previous saved region.
    groups=defaultdict(list)
    for point in points: groups[point['group_id']].append(point)
    rejected={};accepted=[]
    with tempfile.TemporaryDirectory(prefix='twin-furniture-') as temp:
        probe=Path(temp)/'probe.json'
        for group,members in groups.items():
            expected={'rest':{'bench','bin'},'bus_shelter':{'bus_shelter','bus_glass'},
                      'bus_stop':{'bus_pole','bus_panel'},'banner':{'banner_pole','banner'}}
            kind=members[0].get('group_kind','rest')
            if len(members)!=2 or {m['kind'] for m in members}!=expected.get(kind) or any(m.get('group_kind','rest')!=kind for m in members):
                raise ValueError('Incomplete furniture group: '+group)
            probe.write_text(json.dumps({**plan,'points':members}))
            actor.set_editor_property('plan_file',unreal.FilePath(str(probe)))
            if actor.import_plan(): accepted.extend(members)
            elif actor.status.startswith(('Uneven furniture support:', 'Unsupported furniture footprint:')):
                rejected[group]=actor.status
            else: raise RuntimeError(actor.status)
    if points and not accepted: raise RuntimeError('No groups passed ground validation; previous region retained')
    validated={**plan,'points':accepted,'ground_rejections':rejected,
               'source_plan_sha256':hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()}
    validated['summary']={**plan['summary'],'groups':len(accepted)//2,'accepted':len(accepted),
                          'rejected':plan['summary']['rejected']+len(rejected),
                          'reasons':{**plan['summary']['reasons'],'unreal_ground_support':len(rejected)}}
    for group in validated['review']:
        if group['id'] in rejected:
            group.update(status='rejected',reason='unreal_ground_support',ground_error=rejected[group['id']])
    validated['summary']['groups_by_kind']=dict(Counter(g.get('kind','rest') for g in validated['review'] if g['status']=='accepted'))
    validated_path=Path(args.report).with_name('validated-plan.json')
    validated_path.write_text(json.dumps(validated,indent=2)+'\n')
    actor.set_editor_property('plan_file',unreal.FilePath(str(validated_path.resolve())))
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
    if args.stored_plan_path:
        actor.set_editor_property('plan_file',unreal.FilePath(args.stored_plan_path))
    if not save_all():
        raise RuntimeError('Level save failed')
    report={'map':args.name,'region':args.region,'actor':label,'instances':actor.baked_instance_count,
            'plan_points':len(actor.points),'requested_points':len(points),'ground_rejections':rejected,
            'groups_by_kind':validated['summary']['groups_by_kind'],
            'seconds':time.monotonic()-started,'status':actor.status,'saved':True}
    with open(args.report,'w') as f:json.dump(report,f,indent=2)
    print('FURNITURE_BAKED',json.dumps(report),flush=True)

if __name__=='__main__':main()
