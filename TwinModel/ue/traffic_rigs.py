"""Deterministic traffic-light layout selection, independent of Unreal.

Inputs are controlled lane centers and validated support positions in CARLA metres.
Rig transforms are local Unreal centimetres. Repeated heads share the original
signal ID; this module never invents movements, signal IDs or control phases.
"""
import copy
import math


def pick_rig(sig, rigs, style, default):
    forced = {'arrow': 'na_arrow_left', 'ped': 'na_ped_only'}.get(sig.get('kind'))
    if forced and forced in rigs:
        return rigs[forced]
    n = int(sig.get('n_driving_lanes') or 1)
    if style == 'eu':
        if n >= 2 and sig.get('lane_centers') and 'eu_mast_2head' in rigs:
            return rigs['eu_mast_2head']
        if n >= 2 and 'eu_pole_repeater' in rigs:
            return rigs['eu_pole_repeater']
        return rigs.get(default) or next(iter(rigs.values()))
    turns = set(sig.get('turns') or ())
    order = []
    if n >= 4 or (n >= 3 and 'left' in turns):order.append('na_gantry_8head')
    if n >= 2:order.append('na_mast_2head')
    if sig.get('has_crossing'):order.append('na_pole_ped')
    order.append(default)
    return next((rigs[n] for n in order if n in rigs), next(iter(rigs.values())))


def fit_mast(rig, signal, yaw_offset=90):
    """Size/mirror the cantilever from actual lane centers, not a fixed span.

    One overhead head is placed over each controlled lane; the
    pole's low repeater serves stopped drivers. Positions do not assign a head to
    an independent phase. Missing or implausible geometry fails before level edits.
    """
    result=copy.deepcopy(rig)
    lanes=signal.get('lane_centers') or []
    if len(lanes)<2:raise ValueError('Overhead rig needs at least two controlled lane centers')
    if len({l['lane_id'] for l in lanes}) != len(lanes):
        raise ValueError('Duplicate controlled lane centers')
    if len(lanes) != int(signal.get('n_driving_lanes', len(lanes))):
        raise ValueError('Incomplete controlled lane geometry')
    support=signal['placement']
    values = [support[k] for k in ("x", "y", "z")] + [signal["yaw"], yaw_offset]
    values += [lane[k] for lane in lanes for k in ("x", "y", "z")]
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Non-finite mast geometry")
    angle=math.radians(float(signal['yaw'])+yaw_offset)
    c,s=math.cos(angle),math.sin(angle)
    xs=[]
    for lane in lanes:
        dx=(lane['x']-support['x'])*100;dy=(lane['y']-support['y'])*100
        x=c*dx+s*dy;y=-s*dx+c*dy
        if not all(math.isfinite(v) for v in (x,y)) or abs(y)>1200:
            raise ValueError('Controlled lanes are too far from the support cross-section')
        xs.append(x)
    if min(xs)<-50 and max(xs)>50:
        raise ValueError('Support lies between controlled lanes; a single cantilever is unsuitable')
    # Mesh runs along local +X. Place its origin at the left end of the span,
    # irrespective of which side of the carriageway holds the support.
    start=min(0,min(xs)-50);end=max(0,max(xs)+50)
    if end-start>2000:raise ValueError('Required mast reach exceeds the supported 20 m span')
    upright,arm=result['Poles']
    upright['PoleHeight']=650
    arm['Transform']['Location'].update(X=start,Y=0,Z=650)
    arm['PoleHeight']=end-start
    heights=[float(l['z']) for l in lanes]
    # Keep the lowest lamp at least 5.5 m above the highest controlled lane.
    head_z = -75
    if (650 + head_z) / 100 + support["z"] - max(heights) < 5.5:
        raise ValueError("Road elevation is incompatible with the selected mast height")
    arm['Heads'] = [copy.deepcopy(arm['Heads'][0]) for _ in lanes]
    for head,x,lane in zip(arm['Heads'],xs,lanes):
        if lane.get('signal_id'):
            head['SignalID'] = lane['signal_id']
        head['Transform']['Location'].update(X=x-start,Y=18,Z=head_z)
    return result, {'span_m':(end-start)/100,'head_offsets_m':[x/100 for x in xs], 'lane_ids':[l['lane_id'] for l in lanes],
                    'minimum_lamp_height_m':(650+head_z)/100+support['z']-max(heights)}
