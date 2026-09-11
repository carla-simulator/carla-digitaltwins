#!/usr/bin/env python
"""Digital twin workspace: layout, vegetation and furniture on one map.

    python tools/twin_editor.py out/v10_eixample eixample
    python tools/twin_editor.py out/v10_eixample eixample --corrections data/corrections/eixample.json

Browse (default) pans and inspects without editing. Edit enables space/line/point manipulation,
middle-click insertion and Delete on a selected point or segment. Ctrl+Z/Y undo/redo in Edit;
Ctrl+S or Save persists annotations. The panel contains type colors/visibility, global overlay
opacity, selected name/type and an object-level rendering stack. Display choices are browser
preferences, never correction operations. Raw OSM layers and correction/rebuild controls are
absent from this page; the separate geo_overlay viewer remains available for source review.

Existing corrections are preserved. Space and control annotations retain exact WGS84 geometry;
the reviewed-map compiler checks their geometry and explicit simulation bindings separately. This
server also supplies the planning tabs and a coordinated planning-model rebuild.
See tools/MAP_WORKSPACE.md for the shared workflow and Unreal bake configuration.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from twinmodel import corrections as corr_mod                       # noqa: E402
from twinmodel.ingest.osm import fetch_overpass                     # noqa: E402
import geo_overlay as go                                            # noqa: E402

log = logging.getLogger("twin_editor")


# ------------------------------------------------------------------------------------ data

def osm_geojson_edit(raw: dict[str, Any], original: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """``geo_overlay.osm_geojson`` of the corrected extract, plus what the editor needs: every
    highway way carries its ``nodes`` id list and, when a ``way.tags`` op touches it, the
    ``orig_tags`` of the raw extract (so the panel can diff against the original)."""
    fc = go.osm_geojson(raw)
    ways = {int(el["id"]): el for el in raw.get("elements", []) if el.get("type") == "way"}
    orig_ways = {int(el["id"]): el for el in original.get("elements", []) if el.get("type") == "way"}
    touched = {int(o["way"]) for o in ops if o.get("op") == "way.tags" and not o.get("disabled")}
    for f in fc["features"]:
        p = f["properties"]
        if p.get("layer") in ("osm_highway", "osm_other", "osm_building"):
            w = ways.get(int(p["osm_id"]))
            if w is not None:
                p["nodes"] = list(w.get("nodes", []))
                p["new"] = int(p["osm_id"]) < 0
            if int(p["osm_id"]) in touched:
                ow = orig_ways.get(int(p["osm_id"]))
                p["orig_tags"] = dict((ow or {}).get("tags") or {})
    return fc


class EditorStore(go.Store):
    def __init__(self, build_dir: Path, name: str, data_dir: Path, lowfly: Path | None,
                 corrections_path: Path | None):
        super().__init__(build_dir, name, data_dir, lowfly)
        self.build_args: dict[str, Any] = ((self.model.get("metadata") or {}).get("build") or {}).get("args") or {}
        self.corr_path = Path(corrections_path) if corrections_path else corr_mod.default_path(data_dir, name)
        self.corr = corr_mod.load_or_empty(self.corr_path, name)
        self.corr.path = self.corr_path
        self.dirty = False
        self._raw_original: dict[str, Any] | None = None
        self._osm_raw_bytes: bytes | None = None
        self.rebuild_lock = threading.Lock()
        self.last_rebuild: dict[str, Any] | None = None
        log.info("corrections: %s (%d ops%s)", self.corr_path, len(self.corr.ops),
                 "" if self.corr_path.exists() else ", new file")

    # -- OSM: the extract the build consumed (fixture or Overpass cache), raw and corrected
    def osm_original(self) -> dict[str, Any]:
        if self._raw_original is None:
            fx = self.build_args.get("fixture")
            fp = (ROOT / fx) if fx and not Path(fx).is_absolute() else (Path(fx) if fx else None)
            if fp is not None and fp.exists():
                self._raw_original = json.loads(fp.read_text())
                log.info("osm extract: fixture %s", fp)
            else:
                s_, w_, n_, e_ = self.bbox
                self._raw_original = fetch_overpass((s_, w_, n_, e_), cache_dir=self.data_dir)
                log.info("osm extract: overpass cache for bbox")
        return self._raw_original

    def osm_bytes(self) -> bytes:
        with self.lock:
            if self._osm is None:
                orig = self.osm_original()
                ops = self.corr.active()
                patched, rep = corr_mod.apply_osm(orig, ops) if ops else (orig, {"unmatched": []})
                fc = osm_geojson_edit(patched, orig, ops)
                fc["unmatched"] = rep.get("unmatched", [])
                self.osm_counts = fc.pop("counts")
                self._osm = json.dumps(fc, separators=(",", ":")).encode()
            return self._osm

    def osm_raw_bytes(self) -> bytes:
        with self.lock:
            if self._osm_raw_bytes is None:
                fc = go.osm_geojson(self.osm_original())
                fc.pop("counts", None)
                self._osm_raw_bytes = json.dumps(fc, separators=(",", ":")).encode()
            return self._osm_raw_bytes

    # -- corrections
    def corrections_json(self) -> dict[str, Any]:
        return {"schema": corr_mod.SCHEMA, "name": self.name, "path": str(self.corr_path), "ops": self.corr.ops,
                "dirty": self.dirty, "exists": self.corr_path.exists(),
                "next_osm_id": min(corr_mod.min_new_osm_id(self.corr.ops), corr_mod.min_new_osm_id(self.osm_original())) - 1,
                "last_rebuild": self.last_rebuild, "build_args": self.build_args}

    def set_ops(self, ops: list[dict[str, Any]], save: bool) -> dict[str, Any]:
        problems = corr_mod.validate(ops)
        if problems:
            return {"ok": False, "problems": problems}
        with self.lock:
            self.corr.ops = ops
            self._osm = None
            self.dirty = True
        if save:
            self.corr.save(self.corr_path)
            self.dirty = False
            log.info("saved %d ops -> %s", len(ops), self.corr_path)
        return {"ok": True, "n": len(ops), "saved": save, "dirty": self.dirty}

    # -- rebuild
    def rebuild_cmd(self) -> list[str]:
        a = self.build_args
        cmd = [sys.executable, "-m", "twinmodel", "build", "--name", self.name, "--out", str(self.build_dir),
               "--cache", str(a.get("cache") or self.data_dir)]
        if a.get("fixture"):
            cmd += ["--fixture", str(a["fixture"])]
        if a.get("bbox"):
            cmd += ["--bbox"] + [str(v) for v in a["bbox"]]
        elif not a.get("fixture"):
            cmd += ["--bbox"] + [str(v) for v in self.bbox]
        for flag in ("no_imagery", "no_dem", "no_refine"):
            if a.get(flag):
                cmd.append("--" + flag.replace("_", "-"))
        if a.get("mask_method"):
            cmd += ["--mask-method", str(a["mask_method"])]
        if a.get("profile"):
            cmd += ["--profile", str(a["profile"])]
        if a.get("step"):
            cmd += ["--step", str(a["step"])]
        cmd += ["--corrections", str(self.corr_path), "--quick"]
        return cmd

    def rebuild(self) -> dict[str, Any]:
        if not self.rebuild_lock.acquire(blocking=False):
            return {"ok": False, "error": "a rebuild is already running"}
        try:
            self.corr.save(self.corr_path)
            self.dirty = False
            cmd = self.rebuild_cmd()
            log.info("rebuild: %s", " ".join(cmd))
            t0 = time.time()
            res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
            secs = time.time() - t0
            out = (res.stdout or "") + (res.stderr or "")
            (self.build_dir / "editor_rebuild.log").write_text(out)
            lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
            summary = [ln for ln in lines if any(k in ln for k in ("PASS", "FAIL", "corrections:", "BUILD FAILED", "timings"))]
            with self.lock:
                self._twin = None
                try:
                    self.model = json.loads((self.twin_dir / "model.json").read_text())
                except (OSError, json.JSONDecodeError):
                    pass
            corr_meta = (self.model.get("metadata") or {}).get("corrections") or {}
            self.last_rebuild = {"ok": res.returncode == 0, "rc": res.returncode, "seconds": round(secs, 1),
                                 "summary": summary[-16:], "tail": out[-2500:], "when": time.strftime("%H:%M:%S"),
                                 "reports": corr_meta.get("reports")}
            log.info("rebuild rc %d in %.0fs", res.returncode, secs)
            return self.last_rebuild
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "rebuild timed out (900 s)"}
        finally:
            self.rebuild_lock.release()

    def meta(self) -> dict[str, Any]:
        m = super().meta()
        m["editor"] = {"corrections": str(self.corr_path), "n_ops": len(self.corr.ops), "dirty": self.dirty,
                       "build_args": self.build_args, "rebuild_cmd": " ".join(self.rebuild_cmd())}
        return m


# ------------------------------------------------------------------------------------ http

class EditorHandler(go.Handler):
    server_version = "TwinEditor/1.0"
    store: EditorStore

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_POST(self) -> None:                                 # noqa: N802
        try:
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://localhost:{self.server.server_port}', f'http://127.0.0.1:{self.server.server_port}'):
                self._json({'error': 'Origin not allowed'}, 403)
                return
            if int(self.headers.get('Content-Length') or 0) > 2_000_000:
                self._json({'error': 'Request too large'}, 413)
                return
            raw = self.path.split("?")[0]
            parts = [p for p in raw.split("/") if p]
            workspace = getattr(self.store, 'workspace', None)
            if len(parts) == 4 and parts[:2] == ['api', 'planning'] and workspace:
                name, action = parts[2:]
                if name not in workspace.tools or action not in ('preview', 'bake'):
                    self._json({'error': 'Unknown planning tool or action'}, 404)
                    return
                body = self._body()
                result = workspace.preview(name, body) if action == 'preview' else workspace.bake(name)
                self._json(result)
                return
            if parts == ["api", "corrections"]:
                b = self._body()
                self._json((workspace or self.store).set_ops(list(b.get("ops") or []), bool(b.get("save"))))
                return
            if parts == ["api", "rebuild"]:
                self._json((workspace or self.store).rebuild())
                return
            self._error(404, "unknown path")
        except BrokenPipeError:
            pass
        except (ValueError, KeyError, TypeError) as exc:
            self._json({'ok': False, 'error': str(exc)}, 400)
        except Exception as exc:                               # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._json({"ok": False, "error": str(exc)}, 500)

    do_PUT = do_POST

    def _route(self) -> None:
        raw = self.path.split("?")[0]
        parts = [p for p in raw.split("/") if p]
        if parts == ['api', 'planning']:
            workspace = getattr(self.store, 'workspace', None)
            self._json(workspace.state() if workspace else {'tools': {}, 'errors': {}})
            return
        if not parts:
            self._send(200, EDITOR_PAGE.encode(), "text/html; charset=utf-8")
            return
        if parts == ["api", "corrections"]:
            self._json(self.store.corrections_json())
            return
        if parts == ["api", "annotations", "review"]:
            from twinmodel.model import TwinModel
            from twinmodel.reviewed_map import resolve_reviewed_map
            # Snapshot current in-memory edits, including unsaved work, without changing them.
            with self.store.lock:
                ops = json.loads(json.dumps(self.store.corr.ops))
            self._json(resolve_reviewed_map(TwinModel.load(self.store.twin_dir), ops))
            return
        if parts == ["api", "osm_raw.geojson"]:
            self._send(200, self.store.osm_raw_bytes(), "application/geo+json", cache=60)
            return
        if parts == ["api", "osm.geojson"]:
            self._send(200, self.store.osm_bytes(), "application/geo+json")   # no cache: changes with every edit
            return
        if parts == ["api", "twin.geojson"]:
            self._send(200, self.store.twin_bytes(), "application/geo+json")
            return
        super()._route()


# ------------------------------------------------------------------------------------ page

EDITOR_PANEL_HTML = ""

EDITOR_CSS = r"""
.creation-vertex{box-sizing:border-box;background:#172c46;border:3px solid white;border-radius:50%;box-shadow:0 0 0 2px #172c46;cursor:grab}.creation-vertex:active{cursor:grabbing}#create-error{color:#ffc3b6;margin-top:8px}

.creation-card{position:fixed;z-index:1200;width:246px;padding:14px;background:#20252c;color:#e8ecf1;border:1px solid #526073;border-radius:10px;box-shadow:0 6px 28px #0006;font:13px/1.4 system-ui;box-sizing:border-box}.creation-card label{display:flex;flex-direction:column;gap:4px;margin-top:10px}.creation-card input,.creation-card select{width:100%;box-sizing:border-box;background:#151b22;color:inherit;border:1px solid #526073;border-radius:5px;padding:6px;font:inherit}.creation-card button{padding:7px 12px;border:1px solid #526073;border-radius:6px;background:#34445a;color:#fff;cursor:pointer}.creation-card button:disabled{opacity:.4;cursor:default}.creation-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}.creation-card [hidden]{display:none}#create-progress{left:50%;bottom:20px;transform:translateX(-50%);width:360px}#create-progress>span{display:block;margin-top:5px;color:#bdc7d5}

#tools{position:absolute;left:16px;top:16px;bottom:16px;width:308px;z-index:1000;background:#20252c;color:#e8ecf1;border:1px solid #454c56;border-radius:12px;box-shadow:0 8px 30px #0005;display:flex;flex-direction:column;overflow:hidden;font:13px/1.4 system-ui,sans-serif}
#tools *{box-sizing:border-box}#tools button,#tools input{font:inherit}#tools button{cursor:pointer;color:inherit;background:transparent;border:1px solid transparent;border-radius:6px;padding:7px 9px}#tools button:hover{background:#ffffff12}#tools button:focus-visible,#tools input:focus-visible{outline:2px solid #a8c8ff;outline-offset:1px}#tools button:disabled{opacity:.25;cursor:default}
#tools header{display:flex;align-items:center;gap:8px;padding:12px;border-bottom:1px solid #ffffff16}#tools header strong{font-size:17px;flex:1}#tools #b-save{background:#cee0ff;color:#172c46;font-weight:600;padding:7px 14px}
#scene-panel-body{padding:14px;overflow:auto;min-height:0;display:flex;flex-direction:column;gap:18px;flex:1}#scene-panel-body[hidden]{display:none}
.mode-switch{display:flex;gap:4px;background:#14191f;border-radius:8px;padding:4px}.mode-switch button{flex:1;display:flex;justify-content:center;align-items:center;gap:8px}.mode-switch svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7}.mode-switch [aria-pressed=true]{background:#40516a!important;color:#fff!important}
#scene-active{min-height:86px;display:flex;flex-direction:column;gap:5px}#scene-active small{color:#9da9b8;font-size:11px;text-transform:uppercase;letter-spacing:.08em}#scene-active strong{font-size:18px;font-weight:600;line-height:1.25;overflow-wrap:anywhere}#scene-active select{font:inherit;color:inherit;background:#293441;border:1px solid #526073;border-radius:5px;padding:3px;min-width:0;max-width:100%}#scene-active span{display:flex;align-items:center;gap:7px;color:#bdc7d5}#tools i{display:inline-block;width:9px;height:9px;border-radius:3px;flex-shrink:0}
.opacity-control label{display:flex;justify-content:space-between}.opacity-control output{color:#afc9ef;font-variant-numeric:tabular-nums}#space-alpha{width:100%;margin:10px 0 0;accent-color:#b2cdf8}.section-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px;font-weight:600}.section-title label,.section-title small{font-size:11px;font-weight:400;color:#aebacb}.section-title label{display:flex;align-items:center;gap:5px}#tools input[type=checkbox]{accent-color:#afcafa;width:14px;height:14px;cursor:pointer}
#scene-stack{display:grid;grid-template-columns:1fr 1fr;gap:2px 12px}.type-row{display:flex;align-items:center;gap:6px;min-width:0;height:29px;font-size:11px}.type-row span{flex:1}.type-row input[type=color]{width:17px;height:19px;border:0;background:none;padding:0;cursor:pointer}.type-row input[type=color]::-webkit-color-swatch-wrapper{padding:1px}.type-row input[type=color]::-webkit-color-swatch{border:0;border-radius:3px}
.object-section{display:flex;flex-direction:column;min-height:140px;flex:1}#scene-search{width:100%;background:#151b22;border:1px solid #455160;border-radius:6px;padding:7px 9px;color:inherit;margin:0 0 8px}#scene-components{overflow:auto;min-height:90px;flex:1}.object-row{display:flex;align-items:center;border-bottom:1px solid #ffffff09}.object-row .object-pick{display:flex;align-items:center;gap:8px;flex:1;min-width:0;text-align:left;padding:7px 2px!important}.object-pick span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.object-pick small{display:block;font-size:10px;color:#a6b1c0}.object-row>button[data-shift]{padding:4px!important}.object-row.type-hidden .object-pick{opacity:.4}
#tools.scene-collapsed{width:46px;bottom:auto}#tools.scene-collapsed header{padding:7px}#tools.scene-collapsed header strong,#tools.scene-collapsed #b-save{display:none}#tools.scene-collapsed #scene-collapse{padding:6px}#geometry-part{font-size:11px;color:#ffb29b}#geometry-part[hidden],#hint[hidden]{display:none}#hint{padding:10px;background:#5c3030;color:#fff;font-size:12px}
.leaflet-left .leaflet-control-zoom{display:none}.leaflet-interactive{cursor:inherit}.editing-spaces .leaflet-interactive{cursor:crosshair}.scene-control-icon{background:none;border:0}.control-face{width:28px;height:28px;border:2px solid white;border-radius:5px;display:flex;align-items:center;justify-content:center;color:#171b22;font:bold 9px system-ui;box-shadow:0 1px 4px #0008}.control-bearing{width:28px;height:18px;text-align:center;color:#fff;transform-origin:14px 0;font:bold 19px system-ui}.leaflet-tooltip{font:12px system-ui}.editing-spaces .marker-icon:not(.marker-icon-middle){width:14px!important;height:14px!important;margin-left:-7px!important;margin-top:-7px!important;border:3px solid #fff!important;background:#182538!important;box-shadow:0 0 0 2px #101820!important}
@media(max-height:780px){#scene-panel-body{gap:12px}.type-row{height:25px}#scene-active{min-height:68px}}
"""

TOOLS_HTML = r"""
<aside id="tools" aria-label="Spaces editor">
  <header><button id="scene-collapse" aria-label="Collapse panel" aria-expanded="true">‹</button><strong>Spaces</strong><button id="b-save">Save</button></header>
  <div id="scene-panel-body">
    <div class="mode-switch" role="group" aria-label="Interaction mode">
      <button id="mode-browse" aria-pressed="true" title="Pan and inspect. Geometry is locked."><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3l14 10-7 1-4 7z"/></svg>Browse</button>
      <button id="mode-edit" aria-pressed="false" title="Move spaces and edit their points"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 16L16 4l4 4L8 20H4z"/></svg>Edit</button>
    </div>
    <section id="scene-active" aria-live="polite"></section>
    <div id="geometry-part" hidden></div>
    <section class="opacity-control"><label for="space-alpha">Overlay opacity <output id="space-alpha-value">30%</output></label><input id="space-alpha" type="range" min="0" max="100" value="30" aria-label="Overlay opacity"></section>
    <section><div class="section-title"><span>Types</span><label><input id="scene-all" type="checkbox" checked> Show all</label></div><div id="scene-stack"></div></section>
    <section class="object-section"><div class="section-title"><span>Objects</span><small>Front → back · <span id="scene-count"></span></small></div><input id="scene-search" type="search" placeholder="Find an object…" aria-label="Find an object"><div id="scene-components"></div></section>
  </div>
  <div id="hint" role="status" hidden></div>
</aside>
"""

EDITOR_JS = r"""
// ============================================================================ editor state
// Browse is read-only. Edit exposes direct geometry gestures; each drag is one undo step.
const GEOMAN_OK = !!(L.PM);
if(GEOMAN_OK)map.pm.setGlobalOptions({snappable:false,snapDistance:0});
let EDIT_MODE = false;
let OPS = [], HIST = [], HIST_I = -1, NEXT_ID = -1, SEL = null, TOOL = null, DIRTY = false, STALE = false, SPLIT_ARMED = false;
let OSM = null, TWIN = null, WAY_BY_ID = new Map(), NODE_LL = new Map(), NODE_WAYS = new Map(), WAY_LAYER = new Map();

let handles = [];        // extra map layers of the current selection (end handles, editable copies, highlights)
const $ = id => document.getElementById(id);
const hint = (msg, cls) => { const h = $("hint"); h.textContent = msg; h.className = cls || ""; h.hidden = cls !== "err"; };
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mDist = (a, b) => { const p = FRAME.toLocal(a.lat, a.lng), q = FRAME.toLocal(b.lat, b.lng); return Math.hypot(p[0] - q[0], p[1] - q[1]); };
const llOf = c => L.latLng(c[1],c[0]);
const lonlat = ll => [+ll.lng.toFixed(8), +ll.lat.toFixed(8)];
const parseJ = s => { try { return typeof s === "string" ? JSON.parse(s) : s; } catch (e) { return null; } };
const isEditable = el => el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
let draggingGeometry = false, ignoreSelectionUntil = 0;
const canSelect = () => !TOOL && !draggingGeometry && performance.now() > ignoreSelectionUntil;
function geometryDragStart() { clearGeometryPart();draggingGeometry = true; map.closePopup(); }
function geometryDragEnd() { draggingGeometry = false; ignoreSelectionUntil = performance.now() + 300; }
map.on("pm:markerdragstart", geometryDragStart);
map.on("pm:markerdragend", geometryDragEnd);
function selectionChanged() { clearGeometryPart();sceneActive();map.closePopup();if(SEL)map.getContainer().focus({preventScroll:true}); }


// ---------------------------------------------------------------------------- ops, history, server
function pushHistory() { HIST = HIST.slice(0, HIST_I + 1); HIST.push(JSON.stringify(OPS)); HIST_I = HIST.length - 1; if (HIST.length > 300) { HIST.shift(); HIST_I--; } }
async function commit(label, opts = {}) {
  pushHistory();
  const ok = await pushOps(false, opts);
  if (ok) { hint(label, "ok"); if (opts.stale) setStale(true); }
  return ok;
}
async function pushOps(save, opts = {}) {
  const r = await (await fetch("/api/corrections", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ops: OPS, save }) })).json();
  if (!r.ok) { hint("rejected: " + (r.problems || [r.error]).join("; "), "err"); return false; }
  setDirty(r.dirty);
  renderOps();
  if (!opts.local) await refreshOsm(); else renderCorrLayer();
  return true;
}
function setDirty(d) { DIRTY = d; $("b-save").textContent = d ? "Save •" : "Save"; }
function setStale(s) { STALE = s; }
function selectionKey() {
  if (!SEL) return null;
  return { kind: SEL.kind, id: SEL.id, feature: SEL.f, layer: SEL.l, opId: SEL.op && SEL.op.id, space: SEL.space };
}
function restoreSelection(key) {
  if (!key) return;
  if (key.kind === "space") { const replacement=key.space.replaces||key.space.key;const active=OPS.filter(o=>o.op==="space.set"&&replacement&&o.replaces===replacement&&!o.disabled);const s=OPS.find(o=>o.id===key.opId&&!o.disabled&&!o.deleted)||active.find(o=>!o.deleted)||(!active.length&&spaceCandidates.find(s=>s.key===replacement));if(s)selectSpace(s); }
  else if (key.kind === "control") {const c=OPS.find(o=>o.id===key.opId&&!o.disabled&&!o.deleted);if(c)selectControl(c);}
  else if (key.kind === "junction") selectJunction(key.feature);
  else if (key.kind === "feature") inspectSceneFeature(key.feature);
}
async function travelHistory(delta) {
  const next = HIST_I + delta;
  if (next < 0 || next >= HIST.length) return;
  const key = selectionKey(); HIST_I = next; OPS = JSON.parse(HIST[HIST_I]);
  deselect(true); await pushOps(false); restoreSelection(key); setStale(true);
  hint(delta < 0 ? "Change undone. Continue editing." : "Change restored. Continue editing.", "");
}
const undo = () => travelHistory(-1), redo = () => travelHistory(1);
function newOpId() { const used = new Set(OPS.map(o => o.id)); let n = OPS.length + 1; while (used.has("c" + n)) n++; return "c" + n; }
function newOsmId() { return NEXT_ID--; }
function findOp(pred) { return OPS.find(pred); }
function addOp(o) { o.id = o.id || newOpId(); o.ts = new Date().toISOString().slice(0, 19); OPS.push(o); return o; }
function removeOps(pred) { OPS = OPS.filter(o => !pred(o)); }

// ---------------------------------------------------------------------------- OSM layers (corrected extract)
function indexOsm(fc) {
  WAY_BY_ID.clear(); NODE_LL.clear(); NODE_WAYS.clear();
  for (const f of fc.features) {
    const p = f.properties;
    if (p.layer === "osm_node") NODE_LL.set(p.osm_id, L.latLng(f.geometry.coordinates[1], f.geometry.coordinates[0]));
    if (p.layer === "osm_highway") { WAY_BY_ID.set(p.osm_id, f); for (const n of p.nodes || []) { if (!NODE_WAYS.has(n)) NODE_WAYS.set(n, []); NODE_WAYS.get(n).push(p.osm_id); } }
  }
}
async function refreshOsm() {
  OSM = await (await fetch('/api/osm.geojson')).json(); indexOsm(OSM);
  buildSpaceCandidates(); buildControlCandidates(); renderCorrLayer();
}
async function refreshTwin() {
  TWIN = await (await fetch('/api/twin.geojson', {cache:'no-store'})).json();
}
function renderCorrLayer() {
  renderSpaces(); renderControls(); renderSceneContext(); sceneActive(); renderSceneList();
}

// ---------------------------------------------------------------------------- selection plumbing
function clearHandles() { if (cancelLineDrag) cancelLineDrag(); for (const h of handles) { try { if (h.pm) h.pm.disable(); } catch (e) {} map.removeLayer(h); } handles = []; }
function deselect(quiet) {
  if (cancelLineDrag) cancelLineDrag();
  clearHandles(); spaceSplit = false; SPLIT_ARMED = false; map.getContainer().classList.remove("split-armed");
  SEL = null; selectionChanged(); renderCorrLayer();

}
const track = l => { handles.push(l); return l; };

// Direct geometry gestures: vertices stay draggable; dragging between them moves the shape.
// Keep Geoman's edit handles, but do not require separate edit/drag modes or midpoint targets.
let cancelLineDrag = null;
const selectedLayer = () => SEL?.edit;
function enableDirectLine(layer) {
  if (!GEOMAN_OK) return;
  map.doubleClickZoom.disable();
  layer.off("mousedown", beginLineDrag).on("mousedown", beginLineDrag);

}
function insertLineVertex(ev) {
  const layer = ev.target;
  if (!EDIT_MODE || TOOL || layer !== selectedLayer() || draggingGeometry) return;
  L.DomEvent.stop(ev);
  const polygon = layer instanceof L.Polygon, ring = polygon ? layer.getLatLngs()[0] : layer.getLatLngs();
  const click = map.latLngToContainerPoint(ev.latlng);
  if (ring.some(ll => map.latLngToContainerPoint(ll).distanceTo(click)<7)) return;
  let best = null;
  for(let i=0;i<(polygon?ring.length:ring.length-1);i++) {
    const a=map.latLngToContainerPoint(ring[i]),b=map.latLngToContainerPoint(ring[(i+1)%ring.length]);
    const dx=b.x-a.x,dy=b.y-a.y,n=dx*dx+dy*dy;if(!n)continue;
    const t=Math.max(0,Math.min(1,((click.x-a.x)*dx+(click.y-a.y)*dy)/n));
    const pt=L.point(a.x+t*dx,a.y+t*dy),d=pt.distanceTo(click);
    if(!best||d<best.d)best={i:i+1,pt,d};
  }
  if(!best || best.d>15) return;
  clearGeometryPart();
  const ll=map.containerPointToLatLng(best.pt),coords=ring.slice();coords.splice(best.i,0,ll);
  const options={...layer.pm.getOptions()};layer.pm.disable();layer.setLatLngs(polygon?[coords]:coords);layer.pm.enable(options);
  layer.fire("pm:vertexadded",{layer,index:best.i,indexPath:polygon?[0,best.i]:[best.i],latlng:ll});
}
// Capture the middle button before Leaflet/Geoman or browser autoscroll consumes it.
map.getContainer().addEventListener("mousedown", originalEvent => {
  if(originalEvent.button!==1)return;
  originalEvent.preventDefault();originalEvent.stopPropagation();
  if(!EDIT_MODE||!GEOMAN_OK||!SEL||TOOL||draggingGeometry)return;
  const point=map.mouseEventToContainerPoint(originalEvent);
  const layers=SEL.kind==="space"?Object.values(SEL.edges):[selectedLayer()];
  let best=null;
  for(const layer of layers){
    if(!layer||!layer.getLatLngs)continue;
    const polygon=layer instanceof L.Polygon,ring=polygon?layer.getLatLngs()[0]:layer.getLatLngs();
    for(let i=0;i<(polygon?ring.length:ring.length-1);i++){
      const a=map.latLngToContainerPoint(ring[i]),b=map.latLngToContainerPoint(ring[(i+1)%ring.length]);
      const distance=L.LineUtil.pointToSegmentDistance(point,a,b);
      if(!best||distance<best.distance)best={layer,distance};
    }
  }
  if(!best||best.distance>15)return;
  if(SEL.kind==="space")SEL.edit=best.layer;
  insertLineVertex({target:best.layer,latlng:map.mouseEventToLatLng(originalEvent),originalEvent});
},true);
map.getContainer().addEventListener("auxclick",ev=>{if(ev.button===1){ev.preventDefault();ev.stopPropagation();}},true);

function beginLineDrag(ev) {
  const layer=ev.target,oe=ev.originalEvent;
  if(!EDIT_MODE || !oe || oe.button!==0 || TOOL || layer!==selectedLayer() || draggingGeometry) return;
  const polygon=layer instanceof L.Polygon,ring=(polygon?layer.getLatLngs()[0]:layer.getLatLngs()).map(ll=>L.latLng(ll.lat,ll.lng));
  const origin=map.mouseEventToContainerPoint(oe),points=ring.map(ll=>map.latLngToContainerPoint(ll));
  if(points.some(pt=>pt.distanceTo(origin)<9)) return; // vertex handles own their drag
  const pan=map.dragging.enabled(),options={...layer.pm.getOptions()};let moved=false;
  map.dragging.disable();L.DomEvent.stop(oe);
  const cleanup=()=>{document.removeEventListener("mousemove",move);document.removeEventListener("mouseup",end);cancelLineDrag=null;if(pan)map.dragging.enable();};
  const restoreHandles=()=>{if(layer===selectedLayer())layer.pm.enable(options);};
  const move=e=>{
    const at=map.mouseEventToContainerPoint(e),delta=at.subtract(origin);
    if(!moved && delta.distanceTo(L.point(0,0))<3)return;
    if(!moved){moved=true;geometryDragStart();layer.pm.disable();}
    const coords=points.map(pt=>map.containerPointToLatLng(pt.add(delta)));layer.setLatLngs(polygon?[coords]:coords);
  };
  const end=()=>{cleanup();if(moved){restoreHandles();geometryDragEnd();layer.fire("pm:dragend",{layer});}};
  cancelLineDrag=()=>{cleanup();if(moved){layer.setLatLngs(polygon?[ring]:ring);restoreHandles();geometryDragEnd();}};
  document.addEventListener("mousemove",move);document.addEventListener("mouseup",end);
}
function pointInRing(pt, ring) { let inside = false; for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) { const a = ring[i], b = ring[j]; if (((a[1] > pt[1]) !== (b[1] > pt[1])) && (pt[0] < (b[0] - a[0]) * (pt[1] - a[1]) / (b[1] - a[1]) + a[0])) inside = !inside; } return inside; }
// ---------------------------------------------------------------------------- junction outline
function selectJunction(f) {
  if (SEL && SEL.kind === "junction" && SEL.jid === f.properties.id) return;
  deselect();
  const p = f.properties, nodes = parseJ(p.osm_node_ids) || [];
  const existing = findOp(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n)));
  const coords = (existing ? existing.polygon.slice(0, -1).map(c => [c[1], c[0]]) : f.geometry.coordinates[0].slice(0, -1).map(c => [c[1], c[0]]));
  const edit = track(L.polygon(coords, { ...(EDIT_MODE?objectOptions("intersections",p.id):sceneOptions("selection")), interactive:EDIT_MODE, color: "#ffe14d", weight: 3, fillColor:sceneColour("intersections"), fillOpacity: EDIT_MODE?1:0 }).addTo(map));
  SEL = { kind: "junction", f, nodes, edit, jid: p.id };
  selectionChanged();
  edit.on("pm:markerdragstart", geometryDragStart);
  edit.on("pm:markerdragend", geometryDragEnd);
  const save = async label => { const ring = edit.getLatLngs()[0].map(lonlat); ring.push(ring[0]);
    removeOps(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n)));
    addOp({ op: "junction.polygon", nodes, polygon: ring, note: p.id });
    await commit(label, { local: true, stale: true }); };
  if (GEOMAN_OK && EDIT_MODE) {
    edit.pm.enable({ allowSelfIntersection: false, snappable: false, removeVertexOn: "disabled", hideMiddleMarkers: true });
    enableDirectLine(edit);
    edit.on("pm:dragend",()=>save(`junction ${p.id}: outline moved`));
    edit.on("pm:markerdragend", () => save(`junction ${p.id}: outline vertex moved`));
    edit.on("pm:vertexadded", () => save(`junction ${p.id}: outline vertex added`));
    edit.on("pm:vertexremoved", () => save(`junction ${p.id}: outline vertex removed`));
  }
  renderSceneContext(); sceneActive(); renderSceneList();
}

function renderOps() { renderSceneList(); }
async function save() {
  try { if(await pushOps(true,{local:true})) { $('b-save').textContent='Saved'; } }
  catch(e) { hint('Save failed: '+e,'err'); }
}
$('b-save').onclick=save;
document.addEventListener('keydown',ev=>{
  if(handleCreationKey(ev))return;
  if(isEditable(ev.target))return;
  const k=ev.key.toLowerCase();
  if((ev.ctrlKey||ev.metaKey)&&k==='s'){ev.preventDefault();save();return;}
  if(ev.key==='Escape'){if(GEOM_PART)clearGeometryPart();else deselect();return;}
  if(!EDIT_MODE)return;
  if((ev.ctrlKey||ev.metaKey)&&['z','y'].includes(k)){ev.preventDefault();k==='y'||ev.shiftKey?redo():undo();}
  if(['Delete','Backspace'].includes(ev.key)&&SEL){ev.preventDefault();if(ev.repeat||draggingGeometry)return;GEOM_PART?deleteGeometryPart():deleteSelectedObject();}
});
window.addEventListener('beforeunload',ev=>{if(DIRTY){ev.preventDefault();ev.returnValue='';}});
map.on('click',()=>{if(canSelect())deselect();});

// ---------------------------------------------------------------------------- boot
async function bootEditor() {
  const meta=await loadMeta();
  if(meta.ortho)BASEMAPS.unshift(['Ortho',L.tileLayer('/ortho/{z}/{x}/{y}.png',{maxNativeZoom:21,maxZoom:24,attribution:meta.ortho.detail}),null]);
  let pick=BASEMAPS.findIndex(b=>b[2]===meta.region);if(pick<0)pick=BASEMAPS.findIndex(b=>b[2]==='google');
  try{const v=localStorage.getItem('geo-overlay:base');if(v!==null&&Number(v)>=0&&Number(v)<BASEMAPS.length)pick=Number(v);}catch(e){}
  setBase(Math.max(0,pick));setViewFromHash(meta);
  const c=await(await fetch('/api/corrections')).json();OPS=c.ops||[];NEXT_ID=c.next_osm_id;setDirty(c.dirty);pushHistory();
  await refreshTwin();await refreshOsm();
  if(!GEOMAN_OK)hint('Editing handles could not load. Reload to try again.','err');
}
bootEditor().catch(e => { hint("failed: " + e, "err"); console.error(e); });
"""

GEOMAN_HEAD = r"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.css">
<script src="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.min.js"></script>"""

EDITOR_PAGE = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
               "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Twin editor</title>\n"
               + go.LEAFLET_HEAD + "\n" + GEOMAN_HEAD + "\n<style>" + go.CSS + EDITOR_CSS + "</style></head>\n<body>\n<div id=\"map\"></div>\n"
               + EDITOR_PANEL_HTML + TOOLS_HTML
               + "<script>" + go.JS_CORE[:go.JS_CORE.index("const ro =")] + EDITOR_JS + (ROOT / "tools" / "spaces_editor.js").read_text() + (ROOT / "tools" / "scene_editor.js").read_text() + (ROOT / "tools" / "geometry_parts.js").read_text() + (ROOT / "tools" / "create_objects.js").read_text() + (ROOT / "tools" / "map_workspace.js").read_text() + "</script>\n</body></html>\n")


# ------------------------------------------------------------------------------------ main

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir", help="twin build directory, e.g. out/v10_eixample")
    ap.add_argument("name", help="twin name, e.g. eixample (-> <build_dir>/<name>.twin)")
    ap.add_argument("--corrections", help="corrections JSON (default <data>/corrections/<name>.json; created on save)")
    ap.add_argument("--lowfly", help="out/lowfly/<Map> pyramid (default: the one whose manifest names this twin)")
    ap.add_argument("--lowfly-root", default="out/lowfly")
    ap.add_argument("--data", default="data", help="Overpass cache dir (twinmodel default: data/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument('--vegetation-output', help='Existing vegetation workspace, or directory for a new plan')
    ap.add_argument('--furniture-output', help='Existing furniture workspace, or directory for a new plan')
    ap.add_argument('--poles', help='Validated physical traffic-pole placements shared by both planners')
    ap.add_argument('--level', help='Unreal map name for placement baking')
    ap.add_argument('--project', help='CarlaUnreal.uproject path')
    ap.add_argument('--engine', help='UnrealEditor-Cmd path')
    ap.add_argument('--region', help='Placement region (default: twin name)')
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(name)s %(message)s")

    build_dir = Path(args.build_dir)
    twin_dir = build_dir / f"{args.name}.twin"
    if not (twin_dir / "model.json").exists():
        ap.error(f"{twin_dir}/model.json not found")
    lowfly = Path(args.lowfly) if args.lowfly else go.discover_lowfly(twin_dir, Path(args.lowfly_root))
    store = EditorStore(build_dir, args.name, Path(args.data), lowfly,
                        Path(args.corrections) if args.corrections else None)
    # Reuse the physical support export produced by the furniture workflow.
    if not args.poles:
        pole_path = Path(args.furniture_output or build_dir.parent/f'furniture_{args.name}')/'poles.json'
        if pole_path.exists():
            args.poles = str(pole_path.resolve())
    from map_workspace import MapWorkspace
    store.workspace = MapWorkspace(store, args)
    store.meta()
    handler = type("BoundEditorHandler", (EditorHandler,), {"store": store})
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    log.info("editing %s (%s) at %s", args.name, twin_dir, url)
    log.info("rebuild command: %s", " ".join(store.rebuild_cmd()))
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
