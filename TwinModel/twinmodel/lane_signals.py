"""Independent lane signals on shared physical rigs, using CARLA's head bindings."""
import copy
from .model import Controller


def split_lane_signals(model):
    vehicle = [s for s in model.signals if s.kind in ('traffic_light','traffic_light_arrow')]
    if any(s.tags.get('rig_anchor') for s in vehicle):
        if not all(s.tags.get('rig_anchor') for s in vehicle):
            raise ValueError('Partially converted lane-signal model')
        return 0
    by_ctl = {c.id:c for c in model.controllers}
    existing = {s.id for s in model.signals}
    expanded = {}
    stages = []
    # Validate and prepare everything before changing the model.
    for ctl in model.controllers:
        members = [s for s in vehicle if s.controller_id == ctl.id]
        for s in members:
            lanes = sorted({i for a,b in s.validities for i in range(min(a,b),max(a,b)+1) if i})
            if not lanes:raise ValueError('Missing lane validity: '+s.id)
            children=[]
            for index,lane in enumerate(lanes):
                child=copy.deepcopy(s)
                child.id=s.id if index==0 else s.id+'_lane_'+('m' if lane<0 else 'p')+str(abs(lane))
                if index and child.id in existing:raise ValueError('Lane signal ID collision: '+child.id)
                existing.add(child.id)
                child.validities=[(lane,lane)]
                child.tags.update(rig_anchor=s.id,controlled_lane=lane)
                child.controller_id='lane_'+child.id
                children.append(child)
                stages.append(Controller(id=child.controller_id,junction_id=ctl.junction_id,
                                         sequence=len(stages),signal_ids=[child.id]))
            expanded[s.id]=children
    if len(expanded)!=len(vehicle):raise ValueError('Vehicle signal without a controller')
    other=[s for s in model.signals if s.kind not in ('traffic_light','traffic_light_arrow')]
    # Retain the junction reference until connect_pedestrian_signals assigns walk stages.
    first_stage={s.controller_id:expanded[s.id][0].controller_id for s in reversed(vehicle)}
    used_ids={c.id for c in stages}
    if len(used_ids)!=len(stages) or used_ids & (set(by_ctl)-set(first_stage)):
        raise ValueError('Lane controller ID collision')
    for s in other:
        if s.controller_id in first_stage:
            s.controller_id=first_stage[s.controller_id]
            next(c for c in stages if c.id==s.controller_id).signal_ids.append(s.id)
    model.signals=[child for s in model.signals for child in expanded.get(s.id,[s])]
    model.controllers=stages+[c for c in model.controllers if c.id not in first_stage]
    return sum(len(v)-1 for v in expanded.values())
