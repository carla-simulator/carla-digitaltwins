"""Suppress repeated speed signs at internal road-segment boundaries."""
import math


def redundant_speed_signals(model):
    roads={r.id:r for r in model.roads}
    redundant=[]
    for signal in model.signals:
        if signal.kind!='speed_limit' or signal.osm_node_id is not None:
            continue
        road=roads[signal.road_id]
        forward=signal.orientation=='+'
        if not math.isclose(signal.s,0.0 if forward else road.length,abs_tol=.01):
            continue
        link=road.predecessor if forward else road.successor
        if link is None or link.element!='road' or link.id not in roads:
            continue
        upstream=roads[link.id]
        if upstream.junction_id is not None or not road.name or upstream.name!=road.name:
            continue
        if link.contact not in ('start','end'):
            continue
        upstream_forward=link.contact=='end'
        reciprocal=upstream.successor if upstream_forward else upstream.predecessor
        if reciprocal is None or reciprocal.element!='road' or reciprocal.id!=road.id:
            continue
        lanes=[l for l in upstream.lanes if l.type=='driving'
               and (l.direction=='forward')==upstream_forward]
        if not lanes or any(l.speed_limit is None for l in lanes):
            continue
        if all(math.isclose(l.speed_limit,signal.value,abs_tol=1e-6) for l in lanes):
            redundant.append(signal.id)
    # Traverse road links, including segments whose redundant sign was removed
    # on an earlier run, so closed-loop handling remains idempotent.
    candidates=set(redundant)
    by_direction={(s.road_id,s.orientation):s.id for s in model.signals if s.id in candidates}
    for key in by_direction:
        seen=[];current=key
        while current not in seen:
            seen.append(current)
            r=roads[current[0]]
            link=r.predecessor if current[1]=='+' else r.successor
            if link is None or link.element!='road' or link.id not in roads or link.contact not in ('start','end'):
                break
            current=(link.id,'+' if link.contact=='end' else '-')
        else:
            cycle=seen[seen.index(current):]
            signs=[by_direction[k] for k in cycle if k in by_direction]
            if signs:candidates.discard(min(signs))
    return sorted(candidates)
