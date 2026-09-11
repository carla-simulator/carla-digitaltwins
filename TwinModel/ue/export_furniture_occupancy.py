"""Export physical traffic support positions for furniture planning.

Run in the open editor or game Python console:
  exec(open('<this file>').read()); export_occupancy('/absolute/poles.json')
The vegetation plan is a separate required input to the planner.
"""
import json
from pathlib import Path
import unreal


def export_occupancy(output,world=None):
    if world is None:
        world=next((a.get_world() for a in unreal.ObjectIterator(unreal.ProceduralVegetationTool) if a.get_world()),None)
    if world is None:
        world=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    rows=[]
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world,unreal.Actor):
        if actor.get_class().get_name() not in ('DigitalTwinsTrafficLight','GeoTrafficSign'):
            continue
        p=actor.get_actor_location()
        rows.append(dict(x=p.x/100,y=p.y/100,z=p.z/100,layer=0,name=actor.get_name()))
    Path(output).write_text(json.dumps(rows,indent=2)+'\n')
    return len(rows)
