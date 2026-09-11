"""Protected pedestrian stages shared by the paired heads of a junction."""
from .model import Controller


def connect_pedestrian_signals(model):
    """Give each signalized junction an exclusive walk stage, after vehicle stages.

    Keep sidewalk validities: pedestrian heads must never control driving lanes.
    The runtime uses green/red only; the red tail is crossing clearance.
    """
    controllers = {c.id: c for c in model.controllers}
    groups = {}
    for signal in model.signals:
        if signal.kind != 'traffic_light_ped':
            continue
        controller = controllers.get(signal.controller_id)
        junction = controller.junction_id if controller else signal.tags.get('junction_id')
        if (signal.controller_id and controller is None) or not junction or not any(
                c.junction_id == junction for c in model.controllers):
            raise ValueError('Pedestrian signal without junction controller: ' + signal.id)
        groups.setdefault(junction, []).append(signal)
    planned = []
    for junction, signals in sorted(groups.items()):
        cid = 'ped_' + junction
        ids = {s.id for s in signals}
        if cid in controllers and set(controllers[cid].signal_ids) - ids:
            raise ValueError('Pedestrian controller ID collision: ' + cid)
        planned.append((cid, junction, signals))
    ped_ids = {s.id for _, _, signals in planned for s in signals}
    kept = [Controller(c.id, c.junction_id,
                       [sid for sid in c.signal_ids if sid not in ped_ids], c.sequence)
            for c in model.controllers]
    kept = [c for c in kept if c.signal_ids]
    for cid, junction, signals in planned:
        sequence = max((c.sequence for c in kept if c.junction_id == junction), default=-1) + 1
        for signal in signals:
            signal.controller_id = cid
        kept.append(Controller(id=cid, junction_id=junction,
                               signal_ids=[s.id for s in signals], sequence=sequence))
    model.controllers = kept
    return len(planned)
