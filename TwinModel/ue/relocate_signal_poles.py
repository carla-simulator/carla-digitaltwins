"""Apply validated xodr_signals --twin placements to an existing baked level.

Moves supports only; preserves meshes, IDs, head bindings, timing and OpenDRIVE.
Run in UnrealEditor-Cmd -run=pythonscript with --name, --signals and --report.
"""
import argparse
import json
import math
import os
import sys

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_level import MAP_ROOT, save_all


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--report', required=True)
    args = ap.parse_args(argv)
    records = json.load(open(args.signals))
    targets = {}
    for s in records:
        p = s.get('placement', {})
        if p.get('status') != 'ok' or not all(math.isfinite(p[k]) for k in ('x', 'y', 'z')):
            raise ValueError('Missing/unsafe placement for ' + s['id'])
        prefix = 'TL_' if s['type'] in ('1000001', '1000002') else 'SIGN_'
        targets[prefix + s['id']] = p
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(f'{MAP_ROOT}/{args.name}/{args.name}'):
        raise RuntimeError('Cannot load level ' + args.name)
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
    jobs = []
    for a in actors:
        label = a.get_actor_label()
        if label not in targets:
            continue
        jobs.append((a, label, targets[label]))
    missing = sorted(set(targets) - {label for _, label, _ in jobs})
    # Priority-road plates were not included in earlier catalog placement. Report
    # absent actors; never invent replacement actors or mutate their control logic.
    report = {'moved': [], 'missing': missing}
    for a, label, p in jobs:
        before = a.get_actor_location()
        dest = unreal.Vector(p['x'] * 100, p['y'] * 100, p['z'] * 100)
        if (before - dest).length() > .1:
            a.modify()
            a.set_actor_location(dest, False, False)
        after = a.get_actor_location()
        if (after - dest).length() > .1:
            raise RuntimeError('Failed to move ' + label)
        report['moved'].append({'label': label, 'before': [before.x/100, before.y/100, before.z/100],
                                'after': [after.x/100, after.y/100, after.z/100]})
    report['saved'] = bool(save_all())
    with open(args.report, 'w') as f:
        json.dump(report, f, indent=2)
    if not report['saved']:
        raise RuntimeError('Level save failed')
    print('Relocated', len(jobs), 'poles; absent:', missing, flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
