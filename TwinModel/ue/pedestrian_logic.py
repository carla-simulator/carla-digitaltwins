"""Bind pedestrian rigs to exclusive stages and size their crossing clearance."""
import math


def connect_logic(logic, signals, controller_of, junction_of):
    pedestrians = [s for s in signals if s.get('kind') == 'ped']
    ped_ids = {str(s['id']) for s in pedestrians}
    by_controller = {}
    for signal in pedestrians:
        sid = str(signal['id'])
        cid = controller_of[sid]
        if not cid.startswith('ped_') or any(
                other not in ped_ids and controller == cid
                for other, controller in controller_of.items()):
            raise ValueError('Pedestrian stage is not exclusive: ' + cid)
        by_controller.setdefault(cid, []).append(signal)
    clearance = {}
    for cid, members in by_controller.items():
        crossings = {}
        for signal in members:
            crossings.setdefault((signal['road_id'], round(signal['s'], 4)), []).append(signal)
        longest = 0.0
        for pair in crossings.values():
            if len(pair) != 2:
                raise ValueError('Expected two pedestrian heads per crossing: ' + str(pair))
            a, b = pair
            longest = max(longest, math.hypot(a['x']-b['x'], a['y']-b['y']))
        # After WALK ends, allow the last entrant to finish at 1.2 m/s, plus 2 s.
        clearance[cid] = math.ceil(longest / 1.2) + 2.0
    seen = set()
    for entry in logic['TrafficLights']:
        sid = str(entry['SignalID'])
        if sid not in ped_ids:
            continue
        cid = controller_of[sid]
        entry['TrafficLightGroupID'] = cid
        entry['JunctionID'] = int(junction_of[cid])
        entry['Timing'].update(GreenDuration=10.0, AmberDuration=0.0,
                               RedDuration=clearance[cid])
        seen.add(sid)
    if seen != ped_ids:
        raise ValueError('Missing pedestrian rigs in map logic: ' + str(ped_ids-seen))
    return clearance
