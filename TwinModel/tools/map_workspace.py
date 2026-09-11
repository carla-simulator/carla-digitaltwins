"""Shared planning and mutation coordination for the canonical Twin editor.

The existing planners/bakers remain the implementation. A saved input manifest
tracks stale plans across restarts; opening the editor never regenerates a plan.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from twinmodel.model import TwinModel
from vegetation_tool import Workspace as VegetationWorkspace
from furniture_tool import Workspace as FurnitureWorkspace, atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MapWorkspace:
    def __init__(self, store, args):
        self.store = store
        self.lock = threading.RLock()
        self.tools = {}
        self.errors = {}
        self.manifest_path = getattr(store, 'planning_manifest_path', store.build_dir/'editor-planning-inputs.json')
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        self.model_stamp = self.model_signature()
        self.layout_stamp = self.manifest.get('layout', digest(store.corr.ops))
        self.inputs = self.manifest.get('inputs', {})
        common = dict(twin=str(store.twin_dir.resolve()), config=None, poles=args.poles,
                      level=args.level, project=args.project, engine=args.engine,
                      region=args.region or store.name, anchors=None, occupied_plan=[])
        veg_dir = Path(args.vegetation_output or store.build_dir.parent/f'vegetation_{store.name}')
        furniture_dir = Path(args.furniture_output or store.build_dir.parent/f'furniture_{store.name}')
        for name, cls, directory in [('vegetation', VegetationWorkspace, veg_dir),
                                      ('furniture', FurnitureWorkspace, furniture_dir)]:
            # Discover existing projects, but do not silently invent new output plans.
            if not (directory/'plan.json').exists() and not getattr(args, name+'_output'):
                self.errors[name] = f'Configure --{name}-output to create a {name} plan.'
                continue
            try:
                options = SimpleNamespace(**common, output=str(directory.resolve()),
                                          vegetation=str((veg_dir/'plan.json').resolve()))
                self.tools[name] = cls(options, restore=True)
            except (ValueError, OSError, KeyError) as exc:
                self.errors[name] = str(exc)
        for name in self.tools:
            self.inputs.setdefault(name, self.signature(name))
        self.persist()

    def persist(self):
        atomic_json(self.manifest_path, {'layout': self.layout_stamp, 'inputs': self.inputs})

    def model_signature(self):
        h = hashlib.sha256()
        for path in sorted(self.store.twin_dir.rglob('*')):
            if path.is_file():
                h.update(str(path.relative_to(self.store.twin_dir)).encode())
                h.update(path.read_bytes())
        return h.hexdigest()

    def signature(self, name):
        value = {'model': self.model_stamp}
        tool = self.tools[name]
        if tool.args.poles:
            value['poles'] = hashlib.sha256(Path(tool.args.poles).read_bytes()).hexdigest()
        if name == 'furniture' and 'vegetation' in self.tools:
            value['vegetation'] = digest(self.tools['vegetation'].result)
        return value

    def busy(self):
        return any(w.process and w.process.poll() is None for w in self.tools.values())

    def require_idle(self):
        if self.busy():
            raise ValueError('An Unreal bake is running. Wait before changing the map or another plan.')

    def reload_model(self):
        stamp = self.model_signature()
        if stamp != self.model_stamp:
            model = TwinModel.load(self.store.twin_dir)
            for w in self.tools.values():
                w.model = model
            self.model_stamp = stamp

    def state(self):
        with self.lock:
            self.reload_model()
            pending = digest(self.store.corr.ops) != self.layout_stamp
            result = {'name': self.store.name, 'layout_pending': pending,
                      'busy': self.busy(), 'tools': {}, 'errors': self.errors}
            for name, w in self.tools.items():
                result['tools'][name] = {**w.state(), 'stale': self.inputs.get(name) != self.signature(name),
                                         'output': str(w.output)}
            return result

    def set_ops(self, ops, save):
        with self.lock:
            self.require_idle()
            return self.store.set_ops(ops, save)

    def rebuild(self):
        with self.lock:
            self.require_idle()
            result = self.store.rebuild()
            if result.get('ok'):
                self.reload_model()
                self.layout_stamp = digest(self.store.corr.ops)
                self.persist()
            return result

    def preview(self, name, cfg):
        with self.lock:
            self.require_idle()
            self.reload_model()
            if digest(self.store.corr.ops) != self.layout_stamp:
                raise ValueError('Layout annotations changed. Rebuild the planning model before regenerating placements.')
            w = self.tools[name]
            if name == 'furniture':
                veg = self.tools.get('vegetation')
                if veg:
                    if self.inputs['vegetation'] != self.signature('vegetation'):
                        raise ValueError('Regenerate vegetation against the current model first.')
                    w.vegetation = veg.result
            if w.args.poles:
                raw = json.loads(Path(w.args.poles).read_text())
                poles = [s.get('placement', s) for s in raw]
                if any(p.get('status', 'ok') != 'ok' for p in poles):
                    raise ValueError('Unresolved pole input')
                w.poles = poles
            w.regenerate(cfg)
            self.inputs[name] = self.signature(name)
            self.persist()
            return self.state()

    def bake(self, name):
        with self.lock:
            self.require_idle()
            current = self.state()
            if current['layout_pending'] or current['tools'][name]['stale']:
                raise ValueError('Placements are out of date. Update the planning model and regenerate first.')
            self.tools[name].bake()
            return self.state()
