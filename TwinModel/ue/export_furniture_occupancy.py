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
        cls=actor.get_class().get_name()
        label=actor.get_actor_label()
        tags=[str(t) for t in actor.tags]
        if not (cls in ('DigitalTwinsTrafficLight','GeoTrafficSign')
                or label.startswith(('TL_','SIGN_'))
                or any(t.startswith('TL_') for t in tags)):
            continue
        p=actor.get_actor_location()
        rows.append(dict(x=p.x/100,y=p.y/100,z=p.z/100,layer=0,name=label or actor.get_name()))
    Path(output).write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
    return len(rows)
