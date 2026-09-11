"""Explicitly inferred simulation controls for wide, untagged urban crossings.

Never replaces surveyed/OSM or reviewed controls. Each incoming approach has its
own stage: inferred turning movements cannot receive conflicting greens.
"""
import math
from .model import Controller


def complete_wide_crossings(model, enabled=False):
    report = []
    if not enabled:
        return report
    controlled = {c.junction_id for c in model.controllers}
    for junction in model.junctions:
        if junction.id in controlled or junction.tags.get('kind') != 'intersection':
            continue
        incoming = {c.incoming_road for c in junction.connections}
        candidates = [s for s in model.signals if s.tags.get('junction_id') == junction.id
                      and s.kind in ('stop', 'yield', 'priority_road', 'traffic_light')]
        # Only upgrade a complete set of synthetic fallback controls. Explicit
        # source controls or a missing approach require review rather than guessing.
        if len(incoming) < 2 or len(candidates) != len(incoming):
            continue
        if {s.road_id for s in candidates} != incoming:
            continue
        if any(s.tags.get('source') != 'unsignalised_control' for s in candidates):
            continue
        lane_counts = []
        for s in candidates:
            valid = {i for a,b in s.validities for i in range(min(a,b),max(a,b)+1)}
            lane_counts.append(sum(l.type == 'driving' and l.id in valid
                                   for l in model.road(s.road_id).lanes))
        if min(lane_counts) < 2:
            continue
        # A multi-lane continuation, merge or opposite approaches alone is not
        # enough: require crossing approach directions (30..150 degrees apart).
        if not any(abs(math.sin(a.heading-b.heading)) >= 0.5
                   for a in candidates for b in candidates):
            continue
        proposed = [f'inferred_{junction.id}_p{k}' for k in range(len(candidates))]
        if set(proposed) & {c.id for c in model.controllers}:
            raise ValueError('Inferred controller ID collision at ' + junction.id)
        for k,s in enumerate(sorted(candidates, key=lambda x:x.id)):
            s.kind = 'traffic_light'
            s.controller_id = proposed[k]
            s.tags = {**s.tags, 'source':'inferred_wide_crossing',
                      'control':'one_approach_per_stage', 'inferred':True}
            model.controllers.append(Controller(id=proposed[k],junction_id=junction.id,
                                                 sequence=k,signal_ids=[s.id]))
        junction.tags['signal_control_source'] = 'inferred_wide_crossing'
        report.append({'junction_id':junction.id, 'signals':[s.id for s in candidates],
                       'roads':[model.road(s.road_id).name for s in candidates],
                       'lane_counts':lane_counts, 'source':'inferred_wide_crossing'})
    return report
