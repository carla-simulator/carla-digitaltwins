#!/usr/bin/env python
"""Twin editor: review the OSM input against the imagery and record corrections, rebuild, repeat.

    python tools/twin_editor.py out/v10_eixample eixample                 # http://127.0.0.1:8791/
    python tools/twin_editor.py out/v10_eixample eixample --corrections data/corrections/eixample.json

The map core is shared with ``tools/geo_overlay.py`` (basemaps: ICGC / PNOA / Google / Esri / the
build's own ortho; twin layers incl. per-lane bands) but the page carries only what editing needs:
basemap, the corrected OSM extract, the twin result, the tools.  Detections and the low-fly mosaic
stay on the review page.  Nothing is
edited in place: every action becomes an operation in the corrections file
(:mod:`twinmodel.corrections`, default ``data/corrections/<name>.json``) that ``twinmodel build``
replays on top of the raw OSM extract, so the twin is always regenerated from OSM + corrections
and the corrections survive a re-fetch of OSM.

Direct manipulation, no modes: click an object and its handles appear; every drag commits on release
and is one undo step.

  street (OSM way)   vertices draggable at once; click a segment to add one, right-click to remove,
                     drop a vertex on another way's node to connect (shared node = junction),
                     Alt+click (or S then click) a vertex to split, Delete removes the way
                     -> node.move / node.add / way.nodes / way.split / way.delete
  lanes              cross-section widget of the selected street drawn from its tags: click a lane to
                     cycle driving -> parking -> bike -> bus, +/- on either side adds / removes a lane
                     (lanes, lanes:forward/backward, oneway, parking:*, cycleway:*, busway:*) -> way.tags;
                     quick fields and the raw tag table underneath
  stop lines         orange handles at the selected street's junction ends: drag along the road
                     -> road.end (positive = into the junction)
  kerb (twin)        click a kerb line, drag its vertices onto the real kerb; the corrected line
                     replaces the twin's on the map -> one curb.line op per kerb (the build re-paves
                     the strip between the two lines: sidewalk where the kerb moved into the road,
                     road where it moved out)
  junction (twin)    click the polygon, drag its outline -> junction.polygon
  correction area    click it to drag its outline, Delete removes it
  N / U / P          the only one-shot tools: draw a new way (snaps to nodes = connects), outline an
                     area to un-pave (-> sidewalk) or to pave

Ctrl+Z / Ctrl+Y undo / redo, Ctrl+S saves the file, **Rebuild** saves and runs ``twinmodel build
--quick`` with the arguments recorded in the twin's ``model.json`` (twin + xodr + validation,
~10 s for Eixample), then reloads the twin layers.  The OSM layer always shows the *corrected*
extract; the raw one can be toggled on for comparison.
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
            raw = self.path.split("?")[0]
            parts = [p for p in raw.split("/") if p]
            if parts == ["api", "corrections"]:
                b = self._body()
                self._json(self.store.set_ops(list(b.get("ops") or []), bool(b.get("save"))))
                return
            if parts == ["api", "rebuild"]:
                self._json(self.store.rebuild())
                return
            self._error(404, "unknown path")
        except BrokenPipeError:
            pass
        except Exception as exc:                               # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._json({"ok": False, "error": str(exc)}, 500)

    do_PUT = do_POST

    def _route(self) -> None:
        raw = self.path.split("?")[0]
        parts = [p for p in raw.split("/") if p]
        if not parts:
            self._send(200, EDITOR_PAGE.encode(), "text/html; charset=utf-8")
            return
        if parts == ["api", "corrections"]:
            self._json(self.store.corrections_json())
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

EDITOR_PANEL_HTML = r"""
<div id="panel">
  <h1 id="title">layers</h1>
  <div class="muted mono" id="sub"></div>
  <h2>Basemap</h2><div id="basemaps"></div>
  <h2>OpenStreetMap (input)</h2><div id="osm-layers"></div>
  <h2>Twin (result)</h2><div id="twin-layers"></div>
  <div id="help"></div>
</div>
<div id="readout" class="mono"></div>
"""

EDITOR_CSS = r"""
  #tools { position: absolute; top: 10px; left: 10px; z-index: 1000; width: 350px; max-height: calc(100% - 70px); overflow: auto;
           background: rgba(20,20,24,.94); border: 1px solid #333; border-radius: 8px; padding: 10px 12px; }
  #tools h1 { font-size: 14px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  #dirty { width: 9px; height: 9px; border-radius: 50%; background: #444; display: inline-block; }
  #dirty.on { background: #ffb000; }
  .bar { display: flex; gap: 5px; margin: 6px 0; flex-wrap: wrap; align-items: center; }
  .bar .sep { flex: 1; }
  button { padding: 4px 9px; font: 12px system-ui, sans-serif; background: #262630; color: #ddd; border: 1px solid #444; border-radius: 5px; cursor: pointer; }
  button.on { background: #3a6ff0; border-color: #6a8ff8; color: #fff; }
  button.primary { background: #2f7d4f; border-color: #4caf50; color: #fff; }
  button.warn { background: #7a2f2f; border-color: #c0504d; color: #fff; }
  button:disabled { opacity: .45; cursor: default; }
  kbd { font: 10px ui-monospace, Menlo, monospace; background: #111; border: 1px solid #555; border-radius: 3px; padding: 0 3px; color: #bbb; margin-left: 3px; }
  #hint { color: #cdd; font-size: 12px; min-height: 18px; margin: 4px 0; padding: 5px 8px; background: #1a1a22; border-radius: 5px; border-left: 3px solid #3a6ff0; }
  #hint.err { border-left-color: #e74c3c; }
  #hint.ok { border-left-color: #2ecc71; }
  #sel { margin-top: 6px; }
  .h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #9aa; margin: 8px 0 4px; }
  .h2 b { color: #eee; text-transform: none; letter-spacing: 0; font-size: 12px; }
  .field { display: grid; grid-template-columns: 96px 1fr; gap: 3px 8px; align-items: center; margin: 2px 0; font-size: 12px; }
  .field label { color: #9aa; }
  .field input, .field select { background: #14141a; color: #eee; border: 1px solid #444; border-radius: 4px; padding: 3px 5px; font: 12px ui-monospace, Menlo, monospace; width: 100%; box-sizing: border-box; }
  .field input.changed, .field select.changed { border-color: #ffb000; }
  /* cross-section widget */
  #xs { margin: 6px 0 2px; user-select: none; }
  #xs .strip { display: flex; align-items: stretch; height: 58px; gap: 1px; }
  #xs .lane { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; font: 11px system-ui, sans-serif; color: #fff; cursor: pointer; border-radius: 3px; min-width: 22px; box-shadow: inset 0 0 0 1px #0008; }
  #xs .lane:hover { filter: brightness(1.25); outline: 1px solid #fff; }
  #xs .lane small { font-size: 10px; opacity: .8; }
  #xs .lane .arrow { font-size: 13px; line-height: 1; }
  #xs .lane.sidewalk { cursor: default; color: #333; }
  #xs .lane.sidewalk:hover { filter: none; outline: none; }
  #xs .pm { display: flex; flex-direction: column; justify-content: space-between; }
  #xs .pm button { padding: 0 6px; font-size: 14px; line-height: 22px; height: 26px; }
  #xs .legend { display: flex; gap: 8px; font-size: 10px; color: #9aa; margin-top: 3px; flex-wrap: wrap; }
  #xs .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; vertical-align: -1px; margin-right: 3px; }
  #tags table { border-collapse: collapse; width: 100%; }
  #tags td { padding: 1px 2px; }
  #tags input { width: 100%; box-sizing: border-box; background: #14141a; color: #eee; border: 1px solid #333; border-radius: 3px; padding: 2px 4px; font: 11px ui-monospace, Menlo, monospace; }
  #tags input.changed { border-color: #ffb000; }
  #tags .x { color: #e74c3c; cursor: pointer; padding: 0 4px; }
  #ops .op { display: grid; grid-template-columns: 16px 1fr auto; gap: 6px; align-items: center; padding: 3px 0; border-bottom: 1px solid #222; font-size: 11px; }
  #ops .op.off { opacity: .5; }
  #ops .op .what { cursor: pointer; }
  #ops .op .what:hover { color: #8cf; }
  #ops .op input.note { width: 100%; background: transparent; color: #9aa; border: 0; border-bottom: 1px dashed #333; font: 11px system-ui, sans-serif; }
  #ops .op button { padding: 1px 5px; font-size: 11px; }
  details summary { cursor: pointer; color: #9aa; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; margin: 8px 0 4px; }
  #log { font: 11px ui-monospace, Menlo, monospace; white-space: pre-wrap; color: #bbb; max-height: 160px; overflow: auto; background: #0e0e12; padding: 6px; border-radius: 4px; }
  .badge { display: inline-block; padding: 0 5px; border-radius: 3px; background: #333; color: #ddd; font-size: 10px; margin-left: 4px; }
  .badge.new { background: #2f7d4f; }
  .muted.small { font-size: 11px; }
  .leaflet-pm-icon-vertex, .marker-icon { background: #ffe14d !important; border: 1px solid #000 !important; width: 11px !important; height: 11px !important; margin-left: -6px !important; margin-top: -6px !important; }
  .marker-icon-middle { background: #ffe14d88 !important; border: 1px solid #0006 !important; width: 8px !important; height: 8px !important; margin-left: -4px !important; margin-top: -4px !important; }
  .endhandle { background: #ffb000; border: 2px solid #000; border-radius: 50%; width: 16px !important; height: 16px !important; margin-left: -8px !important; margin-top: -8px !important; box-shadow: 0 0 0 3px #ffb00066; cursor: grab; }
  .endhandle:active { cursor: grabbing; }
  .leaflet-interactive.hover { filter: brightness(1.4); }
  .leaflet-container.tool-draw, .leaflet-container.tool-pave, .leaflet-container.tool-unpave { cursor: crosshair; }
  .leaflet-container.split-armed { cursor: cell; }
"""

TOOLS_HTML = r"""
<div id="tools">
  <h1><span id="dirty" title="unsaved changes"></span>Twin editor · <span id="ename"></span></h1>
  <div class="muted mono small" id="cpath"></div>
  <div class="bar">
    <button id="t-draw" title="N">new way<kbd>N</kbd></button>
    <button id="t-unpave" title="U: draw an area that is sidewalk, not road">unpave<kbd>U</kbd></button>
    <button id="t-pave" title="P: draw an area that is road">pave<kbd>P</kbd></button>
    <span class="sep"></span>
    <button id="b-undo" title="Ctrl+Z">↶</button><button id="b-redo" title="Ctrl+Y">↷</button>
  </div>
  <div class="bar">
    <button id="b-save" class="primary" title="Ctrl+S">save</button>
    <button id="b-rebuild" class="primary" title="save + twinmodel build --quick (~10 s), then reload the twin layers">rebuild twin<kbd>R</kbd></button>
    <span class="sep"></span><span id="stale" class="muted small"></span>
  </div>
  <div id="hint">click a street, a kerb line or a junction</div>
  <div id="sel"></div>
  <details open><summary>corrections <span id="nops" class="badge">0</span></summary><div id="ops"></div></details>
  <details><summary>last rebuild</summary><div id="log" class="mono">—</div></details>
</div>
"""

EDITOR_JS = r"""
// ============================================================================ editor state
// Direct manipulation: select an object, its handles appear, every drag commits on release (one
// undo step each). Only drawing (new way, pave / unpave area) is a one-shot tool.
const GEOMAN_OK = !!(L.PM);
let OPS = [], HIST = [], HIST_I = -1, NEXT_ID = -1, SEL = null, TOOL = null, DIRTY = false, STALE = false, SPLIT_ARMED = false;
let OSM = null, TWIN = null, WAY_BY_ID = new Map(), NODE_LL = new Map(), NODE_WAYS = new Map(), WAY_LAYER = new Map();
let osmWays = null, osmNodes = null, corrLayer = null, DRIVABLE = [];
let handles = [];        // extra map layers of the current selection (end handles, editable copies, highlights)
const $ = id => document.getElementById(id);
const hint = (msg, cls) => { const h = $("hint"); h.textContent = msg; h.className = cls || ""; };
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const mDist = (a, b) => { const p = FRAME.toLocal(a.lat, a.lng), q = FRAME.toLocal(b.lat, b.lng); return Math.hypot(p[0] - q[0], p[1] - q[1]); };
const lonlat = ll => [+ll.lng.toFixed(8), +ll.lat.toFixed(8)];
const parseJ = s => { try { return typeof s === "string" ? JSON.parse(s) : s; } catch (e) { return null; } };
const isEditable = el => ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);

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
function setDirty(d) { DIRTY = d; $("dirty").className = d ? "on" : ""; }
function setStale(s) { STALE = s; $("stale").textContent = s ? "twin out of date → rebuild" : ""; $("b-rebuild").classList.toggle("on", s); }
async function undo() { if (HIST_I <= 0) return; HIST_I--; OPS = JSON.parse(HIST[HIST_I]); deselect(true); await pushOps(false); hint("undo", ""); }
async function redo() { if (HIST_I >= HIST.length - 1) return; HIST_I++; OPS = JSON.parse(HIST[HIST_I]); deselect(true); await pushOps(false); hint("redo", ""); }
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
function nearestNode(ll, maxM, exclude) {
  let best = null, bd = maxM;
  for (const [id, nll] of NODE_LL) { if (exclude && exclude.has(id)) continue; const d = mDist(ll, nll); if (d < bd) { bd = d; best = id; } }
  return best;
}
function wayStyle(f) {
  const p = f.properties, col = HW[p.class] || HW.other;
  const touched = OPS.some(o => !o.disabled && o.way === p.osm_id);
  return { color: touched ? "#ffb000" : col, weight: p.new ? 4 : 3, opacity: 1, dashArray: p.new ? "6 4" : null };
}
async function refreshOsm() {
  const keep = SEL && SEL.kind === "way" ? SEL.id : null;
  OSM = await (await fetch("/api/osm.geojson")).json();
  indexOsm(OSM);
  const prevW = removeOverlay("osm_highway"), prevN = removeOverlay("osm_node");
  WAY_LAYER.clear();
  osmWays = geoLayer(OSM, f => f.properties.layer === "osm_highway", wayStyle, null, (f, l) => {
    WAY_LAYER.set(f.properties.osm_id, l);
    l.on("click", ev => { L.DomEvent.stop(ev); if (!TOOL) selectWay(f.properties.osm_id); });
    l.on("mouseover", () => { if (!SEL || SEL.id !== f.properties.osm_id) l.setStyle({ weight: 6 }); });
    l.on("mouseout", () => { if (!SEL || SEL.id !== f.properties.osm_id) l.setStyle({ weight: wayStyle(f).weight }); });
  });
  osmNodes = geoLayer(OSM, f => f.properties.layer === "osm_node", null, (f, ll) => {
    const p = f.properties, junction = p.degree >= 2;
    return L.circleMarker(ll, { renderer: canvas, radius: junction ? 4 : 2.5, color: p.tagged ? "#00e5ff" : (junction ? "#ff4040" : "#ffffff"),
      weight: 1, fillColor: p.osm_id < 0 ? "#2ecc71" : (junction ? "#ff4040" : (p.tagged ? "#00e5ff" : "#ffffff")), fillOpacity: 0.9, opacity: 1, interactive: false });
  }, () => null);
  addOverlay("osm-layers", "osm_highway", `ways (${WAY_BY_ID.size}) · orange = corrected`, "#ffffff", osmWays, prevW ? prevW.opacity : 0.9, prevW ? prevW.on : true);
  addOverlay("osm-layers", "osm_node", "nodes · red junction, cyan tagged, green new", "#ff4040", osmNodes, prevN ? prevN.opacity : 1, prevN ? prevN.on : true);
  osmWays.bringToFront && osmWays.bringToFront();
  if (OSM.unmatched && OSM.unmatched.length) hint(`${OSM.unmatched.length} correction(s) match nothing in the extract: ${OSM.unmatched.map(u => u.id).join(", ")}`, "err");
  renderCorrLayer();
  if (keep !== null) { if (WAY_BY_ID.has(keep)) selectWay(keep, true); else deselect(); }
}
async function refreshTwin() {
  TWIN = await (await fetch("/api/twin.geojson")).json();
  const meta = await (await fetch("/api/meta")).json();
  DRIVABLE = TWIN.features.filter(f => f.properties.layer === "surfaces" && ["drivable", "crossing", "parking"].includes(f.properties.kind)).map(f => f.geometry);
  mountTwin(TWIN, meta.twin_counts, (f, l) => {
    const p = f.properties;
    if (p.layer === "curbs") { l.on("click", ev => { L.DomEvent.stop(ev); if (!TOOL) selectKerb(f, l); }); l.on("mouseover", () => l.setStyle({ weight: 5 })); l.on("mouseout", () => l.setStyle({ weight: 1.5 })); }
    else if (p.layer === "junctions") { l.on("click", ev => { L.DomEvent.stop(ev); if (!TOOL) selectJunction(f); }); }
    else if (p.layer === "roads" && !p.junction_id) { l.on("click", ev => { L.DomEvent.stop(ev); const ws = parseJ(p.osm_way_ids) || []; if (!TOOL && ws.length && WAY_BY_ID.has(ws[0])) selectWay(ws[0]); }); }
    else l.bindPopup(() => popupTable(p), { maxWidth: 420 });
  }, ["surfaces", "lanes", "curbs", "roads", "junctions", "markings", "signals"]);
  if (osmWays) osmWays.bringToFront && osmWays.bringToFront();
  if (corrLayer) renderCorrLayer();
  setStale(false);
}
function renderCorrLayer() {
  if (!corrLayer) { corrLayer = L.featureGroup(); addOverlay("osm-layers", "corr", "correction areas · orange unpave, grey pave, purple junction", "#ffb000", corrLayer, 0.9, true); }
  corrLayer.clearLayers();
  if (groups.curbs) groups.curbs.layer.eachLayer(l => { if (l.feature && !(SEL && SEL.kind === "kerb" && SEL.f === l.feature)) l.setStyle({ weight: 1.5, opacity: 1, dashArray: null }); });
  for (const o of OPS) {
    if (o.disabled || o.op !== "curb.line") continue;
    const k = kerbLayerFor(o);
    if (k) k.l.setStyle({ weight: 1.5, opacity: 0.25, dashArray: "4 4" });      // the twin's kerb, superseded
    if (SEL && SEL.kind === "kerb" && SEL.op === o) continue;                       // being edited: the yellow edit line shows it
    const line = L.polyline(o.line.map(llOf), { color: "#ffb000", weight: 3, opacity: 1 });
    line.bindTooltip(`${o.id} corrected kerb${o.note ? " · " + o.note : ""} — click to edit`, { sticky: true });
    line.on("click", ev => { L.DomEvent.stop(ev); if (TOOL) return; const k2 = kerbLayerFor(o); if (k2) selectKerb(k2.f, k2.l); else hint(`${o.id}: its kerb is not in the current twin any more — rebuild, or delete the correction from the list`, "err"); });
    corrLayer.addLayer(line);
  }
  for (const o of OPS) {
    if (o.disabled || !o.polygon || (SEL && SEL.kind === "corr" && SEL.op === o)) continue;
    const col = o.op === "drivable.cut" ? "#ffb000" : o.op === "drivable.add" ? "#bbbbbb" : "#c860ff";
    const poly = L.polygon(o.polygon.map(c => [c[1], c[0]]), { color: col, weight: 2, fillColor: col, fillOpacity: 0.25, dashArray: "5 4" });
    poly.bindTooltip(`${o.id} ${o.op}${o.as ? " → " + o.as : ""}${o.note ? " · " + o.note : ""}`, { sticky: true });
    poly.on("click", ev => { L.DomEvent.stop(ev); if (!TOOL) selectCorr(o); });
    corrLayer.addLayer(poly);
  }
}

// ---------------------------------------------------------------------------- selection plumbing
function clearHandles() { for (const h of handles) { try { if (h.pm) h.pm.disable(); } catch (e) {} map.removeLayer(h); } handles = []; }
function deselect(quiet) {
  if (SEL && SEL.kind === "way") { const l = WAY_LAYER.get(SEL.id); if (l) { try { l.pm.disable(); } catch (e) {} l.setStyle(wayStyle(WAY_BY_ID.get(SEL.id))); } }
  if (SEL && SEL.kind === "kerb" && SEL.l) SEL.l.setStyle({ weight: 1.5, opacity: 1, dashArray: null });
  clearHandles(); SPLIT_ARMED = false; map.getContainer().classList.remove("split-armed");
  SEL = null; $("sel").innerHTML = ""; renderCorrLayer();
  if (!TOOL && !quiet) hint("click a street, a kerb line or a junction", "");
}
const track = l => { handles.push(l); return l; };

// ---------------------------------------------------------------------------- way: vertices, ends, lanes, tags
function selectWay(wid, silent) {
  const f = WAY_BY_ID.get(wid), lyr = WAY_LAYER.get(wid);
  if (!f || !lyr) return;
  if (SEL && !(SEL.kind === "way" && SEL.id === wid)) deselect(); else if (SEL) { clearHandles(); try { lyr.pm.disable(); } catch (e) {} }
  SEL = { kind: "way", id: wid, f, ids: [...(f.properties.nodes || [])] };
  lyr.setStyle({ color: "#ffe14d", weight: 5, opacity: 1 });
  lyr.bringToFront && lyr.bringToFront();
  if (GEOMAN_OK) {
    lyr.off("pm:markerdragend pm:vertexadded pm:vertexremoved pm:vertexclick pm:markerdragstart");
    lyr.pm.enable({ allowSelfIntersection: true, snappable: true, snapDistance: 18, removeVertexOn: "contextmenu", addVertexOn: "click", preventMarkerRemoval: false });
    lyr.on("pm:markerdragend", onVertexDragEnd);
    lyr.on("pm:vertexadded", onVertexAdded);
    lyr.on("pm:vertexremoved", onVertexRemoved);
    lyr.on("pm:vertexclick", onVertexClick);
  }
  mountEndHandles(wid);
  renderWayPanel(f);
  if (!silent) hint(`way ${wid}: drag vertices · click a segment to add one · right-click a vertex to remove · Alt+click a vertex to split · orange handles move the stop lines · Delete removes the way`, "");
}
const vIndex = e => e.indexPath ? e.indexPath[e.indexPath.length - 1] : e.index;
function moveNodeLocal(nid, ll) {
  NODE_LL.set(nid, ll);
  for (const w of NODE_WAYS.get(nid) || []) {
    const l = WAY_LAYER.get(w), f = WAY_BY_ID.get(w);
    if (!l || !f || (SEL && SEL.kind === "way" && SEL.id === w)) continue;
    const lls = l.getLatLngs(); f.properties.nodes.forEach((n, i) => { if (n === nid && lls[i]) lls[i] = ll; }); l.setLatLngs(lls);
  }
}
async function onVertexDragEnd(e) {
  if (!SEL || SEL.kind !== "way") return;
  const i = vIndex(e), lyr = e.layer || e.target, ll = lyr.getLatLngs()[i], nid = SEL.ids[i];
  const snapTo = nearestNode(ll, 0.2, new Set([nid]));
  if (snapTo !== null) {                       // dropped on another way's node: share it (connect)
    lyr.getLatLngs()[i] = NODE_LL.get(snapTo); lyr.setLatLngs(lyr.getLatLngs());
    SEL.ids[i] = snapTo; setWayNodes(SEL.id, SEL.ids);
    await commit(`way ${SEL.id}: vertex joined to node ${snapTo} (shared with way ${(NODE_WAYS.get(snapTo) || []).filter(w => w !== SEL.id).join(", ")})`, { stale: true });
    return;
  }
  const shared = (NODE_WAYS.get(nid) || []).length > 1;
  if (nid < 0) { const a = findOp(o => o.op === "node.add" && o.node === nid); if (a) { a.lat = +ll.lat.toFixed(8); a.lon = +ll.lng.toFixed(8); } }
  else { removeOps(o => o.op === "node.move" && o.node === nid); addOp({ op: "node.move", node: nid, lat: +ll.lat.toFixed(8), lon: +ll.lng.toFixed(8) }); }
  moveNodeLocal(nid, ll);
  await commit(`node ${nid} moved${shared ? " (shared: every way through it follows)" : ""}`, { local: true, stale: true });
  mountEndHandles(SEL.id);
}
function setWayNodes(wid, ids) {
  if (wid < 0) { const wa = findOp(o => o.op === "way.add" && o.way === wid); if (wa) wa.nodes = [...ids]; }
  else { removeOps(o => o.op === "way.nodes" && o.way === wid); addOp({ op: "way.nodes", way: wid, nodes: [...ids] }); }
}
async function onVertexAdded(e) {
  if (!SEL || SEL.kind !== "way") return;
  const i = vIndex(e), nid = newOsmId();
  SEL.ids.splice(i, 0, nid);
  addOp({ op: "node.add", node: nid, lat: +e.latlng.lat.toFixed(8), lon: +e.latlng.lng.toFixed(8) });
  setWayNodes(SEL.id, SEL.ids);
  await commit(`way ${SEL.id}: vertex added (node ${nid})`, { stale: true });
}
async function onVertexRemoved(e) {
  if (!SEL || SEL.kind !== "way") return;
  const i = vIndex(e), nid = SEL.ids.splice(i, 1)[0];
  if (nid < 0 && !(NODE_WAYS.get(nid) || []).some(w => w !== SEL.id)) removeOps(o => o.op === "node.add" && o.node === nid);
  setWayNodes(SEL.id, SEL.ids);
  await commit(`way ${SEL.id}: vertex removed`, { stale: true });
}
async function onVertexClick(e) {
  if (!SEL || SEL.kind !== "way") return;
  const oe = e.originalEvent || (e.markerEvent && e.markerEvent.originalEvent) || {};
  const alt = oe.altKey || oe.shiftKey;
  if (!SPLIT_ARMED && !alt) return;
  const i = vIndex(e), nid = SEL.ids[i], wid = SEL.id;
  SPLIT_ARMED = false; map.getContainer().classList.remove("split-armed");
  if (i === 0 || i === SEL.ids.length - 1) { hint("cannot split at an end vertex", "err"); return; }
  if (nid < 0) { hint("split at an original node (new vertices become real nodes only after a rebuild)", "err"); return; }
  const nw = newOsmId();
  addOp({ op: "way.split", way: wid, node: nid, new_way: nw });
  await commit(`way ${wid} split at node ${nid} → new way ${nw} (tags copied)`, { stale: true });
  selectWay(nw);
}
async function deleteWay(wid) {
  if (!confirm(`Delete OSM way ${wid} from the twin input?`)) return;
  if (wid < 0) removeOps(o => o.op === "way.add" && o.way === wid); else { removeOps(o => o.way === wid); addOp({ op: "way.delete", way: wid }); }
  deselect(); await commit(`way ${wid} deleted`, { stale: true });
}

// road ends of the twin roads built from this way: draggable stop-line handles
function twinRoadsOf(wid) { return TWIN ? TWIN.features.filter(x => x.properties.layer === "roads" && !x.properties.junction_id && (parseJ(x.properties.osm_way_ids) || []).includes(wid)) : []; }
function mountEndHandles(wid) {
  handles.filter(h => h._isEnd).forEach(h => map.removeLayer(h)); handles = handles.filter(h => !h._isEnd);
  for (const r of twinRoadsOf(wid)) {
    const p = r.properties, coords = r.geometry.coordinates.map(c => L.latLng(c[1], c[0]));
    for (const end of ["start", "end"]) {
      const link = parseJ(end === "start" ? p.predecessor : p.successor);
      if (!link || link.element !== "junction") continue;
      const j = TWIN.features.find(x => x.properties.layer === "junctions" && x.properties.id === link.id);
      const jnodes = j ? (parseJ(j.properties.osm_node_ids) || []) : [];
      const ways = parseJ(p.osm_way_ids) || [];
      let way = ways.find(w => { const wf = WAY_BY_ID.get(w); return wf && (wf.properties.nodes || []).some(n => jnodes.includes(n)); });
      if (way === undefined) way = ways[0];
      if (way === undefined || !jnodes.length) continue;
      const node = jnodes[0], base = +((parseJ(p.tags) || {})[`end_shift_${end}`] || 0);
      const endLL = end === "start" ? coords[0] : coords[coords.length - 1], prevLL = end === "start" ? coords[1] : coords[coords.length - 2];
      const e0 = FRAME.toLocal(endLL.lat, endLL.lng), p0 = FRAME.toLocal(prevLL.lat, prevLL.lng);
      const len = Math.hypot(e0[0] - p0[0], e0[1] - p0[1]) || 1, tx = (e0[0] - p0[0]) / len, ty = (e0[1] - p0[1]) / len;
      const half = 0.5 * (parseJ(p.lanes) || []).filter(l => l.type !== "sidewalk").reduce((s, l) => s + (l.width || 0), 0) || 4;
      const pending = findOp(o => o.op === "road.end" && o.way === way && o.node === node);
      const shown = pending ? pending.shift_m - base : 0;      // a correction not yet rebuilt: show the handle where it will be
      const at = d => FRAME.toWGS(e0[0] + tx * d, e0[1] + ty * d);
      const stopLine = d => { const cx = e0[0] + tx * d, cy = e0[1] + ty * d; return [FRAME.toWGS(cx - ty * half, cy + tx * half), FRAME.toWGS(cx + ty * half, cy - tx * half)]; };
      const line = track(L.polyline(stopLine(shown), { color: "#ffb000", weight: 4, opacity: 0.95, interactive: false }).addTo(map)); line._isEnd = true;
      const h = track(L.marker(at(shown), { draggable: true, icon: L.divIcon({ className: "endhandle" }), title: `stop line of ${p.id} at ${link.id}: drag along the road` }).addTo(map)); h._isEnd = true;
      let delta = shown;
      h.on("drag", ev => { const q = FRAME.toLocal(ev.target.getLatLng().lat, ev.target.getLatLng().lng); delta = Math.max(-40, Math.min(40, (q[0] - e0[0]) * tx + (q[1] - e0[1]) * ty));
        h.setLatLng(at(delta)); line.setLatLngs(stopLine(delta));
        hint(`${p.id} ${end} at ${link.id}: ${delta >= 0 ? "+" : ""}${delta.toFixed(2)} m ${delta >= 0 ? "into" : "back from"} the junction`, ""); });
      h.on("dragend", async () => { const total = +(base + delta).toFixed(2); removeOps(o => o.op === "road.end" && o.way === way && o.node === node);
        if (Math.abs(total) > 0.01) addOp({ op: "road.end", way, node, shift_m: total, note: `${p.id} ${end} @ ${link.id}` });
        await commit(`${p.id}: stop line ${total >= 0 ? "+" : ""}${total} m at ${link.id}`, { local: true, stale: true }); });
    }
  }
}

// cross-section widget: the street as its tags describe it; click a lane to change its type, +/− adds / removes lanes
const XS_COL = { driving: "#6f7f9a", parking: "#3b7dd8", bike: "#2ecc71", bus: "#e74c3c", sidewalk: "#c9b99a" };
function wayTags(f) { const t = {}; for (const [k, v] of Object.entries(f.properties)) if (k.startsWith("tag:")) t[k.slice(4)] = v; return t; }
function crossSection(tags, twinLanes) {
  const oneway = tags.oneway === "yes" || tags.oneway === "-1" || tags.oneway === "1";
  const n = parseInt(tags.lanes) || null;
  let fwd = parseInt(tags["lanes:forward"]), bwd = parseInt(tags["lanes:backward"]);
  const twinDriving = twinLanes ? twinLanes.filter(l => l.type === "driving") : [];
  let source = "tags";
  if (oneway) { fwd = n || (isNaN(fwd) ? null : fwd); bwd = 0; if (fwd === null) { fwd = twinDriving.length || 1; source = "twin default"; } }
  else {
    if (isNaN(fwd) && isNaN(bwd)) { if (n) { fwd = Math.ceil(n / 2); bwd = n - fwd; } else { fwd = twinDriving.filter(l => l.id < 0).length || 1; bwd = twinDriving.filter(l => l.id > 0).length || 1; source = "twin default"; } }
    else if (isNaN(fwd)) fwd = Math.max(0, (n || bwd + 1) - bwd); else if (isNaN(bwd)) bwd = Math.max(0, (n || fwd + 1) - fwd);
  }
  const side = s => {
    const out = [];
    const park = tags[`parking:${s}`] || tags["parking:both"] || tags[`parking:lane:${s}`] || tags["parking:lane:both"];
    const bus = tags[`busway:${s}`] || (tags.busway === "lane" && s === "right" ? "lane" : null);
    const cyc = tags[`cycleway:${s}`] || tags["cycleway:both"] || (s === "right" ? tags.cycleway : null);
    if (park && !["no", "separate", "none", "street_side"].includes(park)) out.push({ type: "parking", tag: park });
    if (cyc && !["no", "none", "shared_lane", "share_busway"].includes(cyc)) out.push({ type: "bike", tag: cyc });
    if (bus && !["no"].includes(bus)) out.push({ type: "bus", tag: bus });
    return out;    // outermost first
  };
  const sw = tags.sidewalk || tags["sidewalk:both"] || "";
  const swL = ["both", "left"].includes(sw) || tags["sidewalk:left"] === "yes" || (!sw && !tags["sidewalk:left"]), swR = ["both", "right"].includes(sw) || tags["sidewalk:right"] === "yes" || (!sw && !tags["sidewalk:right"]);
  // left to right in driving direction of the way: left sidewalk, left extras (outer→inner), backward lanes, forward lanes, right extras (inner→outer), right sidewalk
  const lanes = [];
  if (swL) lanes.push({ type: "sidewalk", side: "left" });
  for (const x of side("left")) lanes.push({ ...x, side: "left", dir: "back" });
  for (let i = 0; i < bwd; i++) lanes.push({ type: "driving", side: "left", dir: "back" });
  for (let i = 0; i < fwd; i++) lanes.push({ type: "driving", side: "right", dir: "fwd" });
  for (const x of side("right").reverse()) lanes.push({ ...x, side: "right", dir: "fwd" });
  if (swR) lanes.push({ type: "sidewalk", side: "right" });
  return { lanes, fwd, bwd, oneway, source, n };
}
function renderCrossSection(f) {
  const tags = wayTags(f), roads = twinRoadsOf(f.properties.osm_id);
  const longest = roads.sort((a, b) => b.geometry.coordinates.length - a.geometry.coordinates.length)[0];
  const xs = crossSection(tags, longest ? parseJ(longest.properties.lanes) : null);
  const host = $("xs"); if (!host) return;
  const box = l => {
    const col = XS_COL[l.type] || "#555", w = l.type === "sidewalk" ? 1.2 : l.type === "parking" ? 1.4 : l.type === "bike" ? 1.1 : 2.2;
    const arrow = l.type === "driving" || l.type === "bus" ? `<span class="arrow">${l.dir === "back" ? "▼" : "▲"}</span>` : "";
    const label = l.type === "sidewalk" ? "walk" : l.type;
    return `<div class="lane ${l.type}" style="flex:${w};background:${col}" data-side="${l.side}" data-type="${l.type}" title="${l.type === "sidewalk" ? "sidewalk (from the buildings / profile)" : "click: change this lane"}">${arrow}<small>${label}</small></div>`;
  };
  host.innerHTML = `<div class="strip">
      <div class="pm"><button data-add="left" title="add a lane on the left side">+</button><button data-rem="left" title="remove a lane on the left side">−</button></div>
      ${xs.lanes.map(box).join("")}
      <div class="pm"><button data-add="right" title="add a lane on the right side">+</button><button data-rem="right" title="remove a lane on the right side">−</button></div>
    </div>
    <div class="legend"><span>${xs.oneway ? "one-way" : "two-way"} · ${xs.fwd} fwd${xs.oneway ? "" : " / " + xs.bwd + " back"} (${xs.source})</span>
      <span><i style="background:${XS_COL.driving}"></i>driving</span><span><i style="background:${XS_COL.parking}"></i>parking</span><span><i style="background:${XS_COL.bus}"></i>bus</span><span><i style="background:${XS_COL.bike}"></i>bike</span>
      <span title="bus lanes are recorded in OSM tags but the lane graph does not build them yet">bus: tags only</span></div>`;
  host.querySelectorAll(".lane:not(.sidewalk)").forEach(el => el.onclick = () => cycleLane(f, el.dataset.side, el.dataset.type, xs));
  host.querySelectorAll("[data-add]").forEach(el => el.onclick = () => changeLaneCount(f, el.dataset.add, +1, xs));
  host.querySelectorAll("[data-rem]").forEach(el => el.onclick = () => changeLaneCount(f, el.dataset.rem, -1, xs));
}
function applyTagChange(f, set, unset, label) {
  const p = f.properties, orig = p.orig_tags || wayTags(f), now = wayTags(f);
  for (const k of unset) delete now[k];
  Object.assign(now, set);
  const dset = {}, dunset = [];
  for (const [k, v] of Object.entries(now)) if (orig[k] !== v) dset[k] = v;
  for (const k of Object.keys(orig)) if (!(k in now)) dunset.push(k);
  removeOps(o => o.op === "way.tags" && o.way === p.osm_id);
  if (Object.keys(dset).length || dunset.length) { const o = { op: "way.tags", way: p.osm_id, set: dset }; if (dunset.length) o.unset = dunset; addOp(o); }
  return commit(label, { stale: true });
}
function changeLaneCount(f, side, d, xs) {
  const t = wayTags(f), set = {}, unset = [];
  if (xs.oneway) { const n = Math.max(1, xs.fwd + d); set.lanes = String(n); unset.push("lanes:forward", "lanes:backward"); }
  else {
    let fwd = xs.fwd, bwd = xs.bwd;
    if (side === "right") fwd = Math.max(1, fwd + d); else bwd = Math.max(0, bwd + d);
    set["lanes:forward"] = String(fwd); set["lanes:backward"] = String(bwd); set.lanes = String(fwd + bwd);
    if (bwd === 0) { set.oneway = "yes"; unset.push("lanes:forward", "lanes:backward"); }
  }
  return applyTagChange(f, set, unset, `way ${f.properties.osm_id}: ${d > 0 ? "+" : "−"}1 lane on the ${side} → lanes=${set.lanes}`);
}
function cycleLane(f, side, type, xs) {
  // driving → parking → bike → bus → driving ; on the outer lane of that side
  const S = side, set = {}, unset = [];
  const clearSide = () => unset.push(`parking:${S}`, `parking:${S}:orientation`, `parking:lane:${S}`, `cycleway:${S}`, `busway:${S}`);
  const next = { driving: "parking", parking: "bike", bike: "bus", bus: "driving" }[type];
  if (type === "driving") {           // the outer driving lane becomes a parking lane: one vehicle lane less
    if (xs.oneway ? xs.fwd <= 1 : (S === "right" ? xs.fwd <= 1 : xs.bwd <= 1)) { hint("keep at least one driving lane", "err"); return; }
    if (xs.oneway) set.lanes = String(xs.fwd - 1);
    else { const fwd = S === "right" ? xs.fwd - 1 : xs.fwd, bwd = S === "left" ? xs.bwd - 1 : xs.bwd; set["lanes:forward"] = String(fwd); set["lanes:backward"] = String(bwd); set.lanes = String(fwd + bwd); }
  }
  clearSide();
  if (next === "parking") { set[`parking:${S}`] = "lane"; set[`parking:${S}:orientation`] = "parallel"; }
  else if (next === "bike") set[`cycleway:${S}`] = "lane";
  else if (next === "bus") set[`busway:${S}`] = "lane";
  else if (next === "driving") {      // the bus lane becomes a driving lane again
    if (xs.oneway) set.lanes = String(xs.fwd + 1);
    else { const fwd = S === "right" ? xs.fwd + 1 : xs.fwd, bwd = S === "left" ? xs.bwd + 1 : xs.bwd; set["lanes:forward"] = String(fwd); set["lanes:backward"] = String(bwd); set.lanes = String(fwd + bwd); }
  }
  // both-sided tags would leak to the other side: split them
  const t = wayTags(f);
  for (const k of ["parking:both", "parking:lane:both", "cycleway:both"]) if (t[k]) { unset.push(k); const other = S === "right" ? "left" : "right"; set[k.replace("both", other)] = t[k]; }
  if (t.cycleway && S === "right") unset.push("cycleway");
  return applyTagChange(f, set, unset, `way ${f.properties.osm_id}: outer ${S} lane → ${next}`);
}
function renderWayPanel(f) {
  const p = f.properties, tags = wayTags(f), orig = p.orig_tags || tags;
  const roads = twinRoadsOf(p.osm_id);
  const twinLanes = roads.length ? roads.map(r => { const ls = parseJ(r.properties.lanes) || []; return `${r.properties.id}: ${ls.filter(l => l.type !== "sidewalk").map(l => l.type[0]).join("")}`; }).join(" · ") : "not in the twin";
  const quick = [["highway", "class", ["primary", "secondary", "tertiary", "residential", "unclassified", "living_street", "service", "pedestrian", "motorway", "trunk", "footway", "cycleway"]], ["name", "name", null], ["oneway", "one-way", ["", "yes", "no", "-1"]], ["maxspeed", "maxspeed", null], ["width", "width (m)", null], ["sidewalk", "sidewalk", ["", "both", "left", "right", "no", "separate"]], ["turn:lanes", "turn:lanes", null]];
  let html = `<div class="h2"><b>way ${p.osm_id}</b> ${esc(tags.name || "")}${p.new ? '<span class="badge new">new</span>' : ""} <a target="_blank" rel="noopener" href="https://www.openstreetmap.org/way/${p.osm_id}">osm ↗</a>
      <span class="muted">· ${p.n_nodes} nodes · twin ${esc(twinLanes)}</span></div>
    <div id="xs"></div>
    <div class="field">`;
  for (const [k, label, opts] of quick) {
    const v = tags[k] || "", ch = (orig[k] || "") !== v ? "changed" : "";
    if (opts) html += `<label>${label}</label><select data-k="${k}" class="${ch}">${(opts.includes("") ? opts : ["", ...opts]).map(o => `<option value="${o}" ${o === v ? "selected" : ""}>${o || "—"}</option>`).join("")}${opts.includes(v) || v === "" ? "" : `<option value="${esc(v)}" selected>${esc(v)}</option>`}</select>`;
    else html += `<label>${label}</label><input data-k="${k}" value="${esc(v)}" class="${ch}">`;
  }
  html += `</div><details><summary>all tags (${Object.keys(tags).length})</summary><div id="tags"><table>`;
  for (const [k, v] of Object.entries(tags).sort()) html += `<tr><td class="mono">${esc(k)}</td><td><input data-tag="${esc(k)}" value="${esc(v)}" class="${(orig[k] || "") !== v ? "changed" : ""}"></td><td class="x" data-del="${esc(k)}" title="remove tag">×</td></tr>`;
  html += `<tr><td><input id="newk" placeholder="key"></td><td><input id="newv" placeholder="value"></td><td class="x" id="addtag" title="add">+</td></tr></table></div></details>
    <div class="bar"><button id="b-split" title="then click a vertex (or Alt+click a vertex any time)">split<kbd>S</kbd></button><button id="b-revert" title="drop every correction on this way">revert way</button><span class="sep"></span><button id="b-delway" class="warn">delete<kbd>Del</kbd></button></div>`;
  $("sel").innerHTML = html;
  renderCrossSection(f);
  const onField = el => { const k = el.dataset.k, v = el.value.trim(); applyTagChange(f, v ? { [k]: v } : {}, v ? [] : [k], `way ${p.osm_id}: ${k}=${v || "(removed)"}`); };
  document.querySelectorAll("#sel .field [data-k]").forEach(el => el.onchange = () => onField(el));
  document.querySelectorAll("#tags input[data-tag]").forEach(el => el.onchange = () => { const k = el.dataset.tag, v = el.value.trim(); applyTagChange(f, v ? { [k]: v } : {}, v ? [] : [k], `way ${p.osm_id}: ${k}=${v}`); });
  document.querySelectorAll("#tags .x[data-del]").forEach(el => el.onclick = () => applyTagChange(f, {}, [el.dataset.del], `way ${p.osm_id}: ${el.dataset.del} removed`));
  $("addtag").onclick = () => { const k = $("newk").value.trim(), v = $("newv").value.trim(); if (k) applyTagChange(f, { [k]: v }, [], `way ${p.osm_id}: ${k}=${v}`); };
  $("b-split").onclick = armSplit;
  $("b-revert").onclick = () => { removeOps(o => o.way === p.osm_id && o.op !== "way.add"); commit(`way ${p.osm_id}: corrections dropped`, { stale: true }); };
  $("b-delway").onclick = () => deleteWay(p.osm_id);
}
function armSplit() { if (!SEL || SEL.kind !== "way") return; SPLIT_ARMED = true; map.getContainer().classList.add("split-armed"); hint("split: click one of the way's vertices", ""); }

// ---------------------------------------------------------------------------- kerb: drag the line itself; one curb.line op per kerb
// The corrected kerb replaces the twin's kerb line on the map. The pipeline polygonises the strip
// between the two lines and un-paves / paves it (corrections.kerb_strips), so nothing else to draw.
function pointInRing(pt, ring) { let inside = false; for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) { const a = ring[i], b = ring[j]; if (((a[1] > pt[1]) !== (b[1] > pt[1])) && (pt[0] < (b[0] - a[0]) * (pt[1] - a[1]) / (b[1] - a[1]) + a[0])) inside = !inside; } return inside; }
function inDrivable(ll) {
  const pt = [ll.lng, ll.lat];
  for (const g of DRIVABLE) { const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates; for (const rings of polys) { if (pointInRing(pt, rings[0]) && !rings.slice(1).some(h => pointInRing(pt, h))) return true; } }
  return false;
}
const llOf = c => L.latLng(c[1], c[0]);
function endsDist(a, b) {   // how well two [lon,lat] polylines match at their ends (m), either orientation
  const d = (p, q) => mDist(llOf(p), llOf(q));
  return Math.min(d(a[0], b[0]) + d(a[a.length - 1], b[b.length - 1]), d(a[0], b[b.length - 1]) + d(a[a.length - 1], b[0]));
}
function kerbOpFor(f) {     // the curb.line op that belongs to this twin kerb (same id + place, or same place)
  const c = f.geometry.coordinates;
  let best = null, bd = 3.0;
  for (const o of OPS) { if (o.op !== "curb.line") continue; const d = Math.min(endsDist(o.line, c), endsDist(o.base, c)); if (d < bd) { bd = d; best = o; } }
  return best;
}
function kerbLayerFor(o) {  // the twin kerb (feature, layer) a curb.line op belongs to
  let best = null, bd = 3.0;
  if (!groups.curbs) return null;
  groups.curbs.layer.eachLayer(l => { const f = l.feature; if (!f) return; const d = Math.min(endsDist(o.line, f.geometry.coordinates), endsDist(o.base, f.geometry.coordinates)); if (d < bd) { bd = d; best = { f, l }; } });
  return best;
}
function offsetFrom(line, base) {   // largest distance (m) of a line vertex from the base polyline
  const segs = []; for (let i = 1; i < base.length; i++) segs.push([FRAME.toLocal(base[i - 1][1], base[i - 1][0]), FRAME.toLocal(base[i][1], base[i][0])]);
  let worst = 0;
  for (const c of line) { const q = FRAME.toLocal(c[1], c[0]); let d = Infinity;
    for (const [a, b] of segs) { const vx = b[0] - a[0], vy = b[1] - a[1], L2 = vx * vx + vy * vy || 1e-9; const t = Math.max(0, Math.min(1, ((q[0] - a[0]) * vx + (q[1] - a[1]) * vy) / L2)); d = Math.min(d, Math.hypot(q[0] - a[0] - t * vx, q[1] - a[1] - t * vy)); }
    worst = Math.max(worst, d); }
  return worst;
}
function selectKerb(f, l) {
  deselect(true);
  const p = f.properties, existing = kerbOpFor(f);
  const start = existing ? existing.line.map(llOf) : f.geometry.coordinates.map(llOf);
  SEL = { kind: "kerb", f, l, op: existing, base: existing ? existing.base : f.geometry.coordinates.map(c => [c[0], c[1]]) };
  l.setStyle({ weight: 1.5, opacity: 0.25, dashArray: "4 4" });
  renderCorrLayer();          // hides this kerb's corrected line while it is being edited
  const edit = track(L.polyline(start, { color: "#ffe14d", weight: 4, opacity: 1 }).addTo(map));
  SEL.edit = edit;
  if (GEOMAN_OK) {
    edit.pm.enable({ allowSelfIntersection: true, snappable: false, removeVertexOn: "contextmenu", addVertexOn: "click" });
    edit.on("pm:markerdragend pm:vertexremoved", () => saveKerb("dragged"));
    edit.on("pm:vertexadded", () => {});   // a new vertex on the segment changes nothing until it is dragged
  }
  $("sel").innerHTML = `<div class="h2"><b>kerb ${p.id}</b>${existing ? '<span class="badge">corrected</span>' : ""} <span class="muted">· ${p.low_side_kind} | ${p.high_side_kind}</span></div>
    <div class="muted small">Drag the vertices onto the real kerb in the imagery; click a segment to add a vertex where the kerb bends, right-click one to remove it. The strip between the old and the new line is re-paved by the rebuild: sidewalk where the kerb moved into the road, road where it moved out.</div>
    <div class="bar">${existing ? '<button id="b-delop" class="warn">reset kerb<kbd>Del</kbd></button>' : ""}</div>`;
  if (existing) $("b-delop").onclick = deleteSelected;
  hint(`kerb ${p.id}: drag its vertices onto the real kerb`, "");
}
async function saveKerb(what) {
  if (!SEL || SEL.kind !== "kerb") return;
  const line = SEL.edit.getLatLngs().map(lonlat);
  if (line.length < 2) return;
  const p = SEL.f.properties, off = offsetFrom(line, SEL.base);
  if (SEL.op) removeOps(o => o === SEL.op);
  if (off < 0.02 && line.length === SEL.base.length) { SEL.op = null; await commit(`kerb ${p.id}: back on the twin's line`, { local: true, stale: true }); renderKerbPanelBadge(); return; }
  SEL.op = addOp({ op: "curb.line", curb: p.id, base: SEL.base, line, as: p.high_side_kind === "verge" ? "verge" : "sidewalk", note: `kerb ${p.id}: up to ${off.toFixed(2)} m` });
  await commit(`kerb ${p.id}: moved up to ${off.toFixed(2)} m (rebuild re-paves the strip)`, { local: true, stale: true });
  renderKerbPanelBadge();
}
function renderKerbPanelBadge() { if (!SEL || SEL.kind !== "kerb") return; const h = $("sel").querySelector(".h2"); if (h) h.innerHTML = `<b>kerb ${SEL.f.properties.id}</b>${SEL.op ? '<span class="badge">corrected</span>' : ""} <span class="muted">· ${SEL.f.properties.low_side_kind} | ${SEL.f.properties.high_side_kind}</span>`; }

// ---------------------------------------------------------------------------- junction outline
function selectJunction(f) {
  deselect();
  const p = f.properties, nodes = parseJ(p.osm_node_ids) || [];
  const existing = findOp(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n)));
  const coords = (existing ? existing.polygon.slice(0, -1).map(c => [c[1], c[0]]) : f.geometry.coordinates[0].slice(0, -1).map(c => [c[1], c[0]]));
  const edit = track(L.polygon(coords, { color: "#ffe14d", weight: 3, fillOpacity: 0.08 }).addTo(map));
  SEL = { kind: "junction", f, nodes, edit, jid: p.id };
  const save = async label => { const ring = edit.getLatLngs()[0].map(lonlat); ring.push(ring[0]);
    removeOps(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n)));
    addOp({ op: "junction.polygon", nodes, polygon: ring, note: p.id });
    await commit(label, { local: true, stale: true }); };
  if (GEOMAN_OK) {
    edit.pm.enable({ allowSelfIntersection: false, removeVertexOn: "contextmenu", addVertexOn: "click" });
    edit.on("pm:markerdragend", () => save(`junction ${p.id}: outline vertex moved`));
    edit.on("pm:vertexadded", () => save(`junction ${p.id}: outline vertex added`));
    edit.on("pm:vertexremoved", () => save(`junction ${p.id}: outline vertex removed`));
  }
  $("sel").innerHTML = `<div class="h2"><b>junction ${p.id}</b>${p.polygon_source === "correction" || existing ? '<span class="badge">corrected</span>' : ""} <span class="muted">· nodes ${nodes.join(", ")}</span></div>
    <div class="muted small">Drag the outline onto the real pavement edge; click a segment to add a vertex, right-click one to remove it. The surfaces stage keeps this outline verbatim; sidewalks and kerbs wrap around it.</div>
    <div class="bar">${existing ? '<button id="b-delop" class="warn">drop outline correction</button>' : ""}</div>`;
  if (existing) $("b-delop").onclick = async () => { removeOps(o => o === existing); deselect(); await commit(`junction ${p.id}: outline correction dropped`, { stale: true }); };
  hint(`junction ${p.id}: drag the outline`, "");
}

// ---------------------------------------------------------------------------- correction areas
function selectCorr(o) {
  deselect();
  SEL = { kind: "corr", op: o };
  renderCorrLayer();
  const col = o.op === "drivable.cut" ? "#ffb000" : o.op === "drivable.add" ? "#bbbbbb" : "#c860ff";
  const edit = track(L.polygon(o.polygon.slice(0, -1).map(c => [c[1], c[0]]), { color: "#ffe14d", weight: 3, fillColor: col, fillOpacity: 0.25 }).addTo(map));
  SEL.edit = edit;
  const save = async () => { const ring = edit.getLatLngs()[0].map(lonlat); ring.push(ring[0]); o.polygon = ring; await commit(`${o.id}: outline updated`, { local: true, stale: true }); };
  if (GEOMAN_OK) { edit.pm.enable({ allowSelfIntersection: false, removeVertexOn: "contextmenu", addVertexOn: "click" }); edit.on("pm:markerdragend pm:vertexadded pm:vertexremoved", save); }
  $("sel").innerHTML = `<div class="h2"><b>${o.id}</b> ${o.op}${o.as ? " → " + o.as : ""} <span class="muted">${esc(o.note || "")}</span></div>
    <div class="field">${o.op === "drivable.cut" ? `<label>becomes</label><select id="corr-as">${["sidewalk", "median", "verge", "ground"].map(a => `<option ${a === (o.as || "sidewalk") ? "selected" : ""}>${a}</option>`).join("")}</select>` : ""}
    <label>note</label><input id="corr-note" value="${esc(o.note || "")}"></div>
    <div class="bar"><span class="sep"></span><button id="b-delop" class="warn">delete<kbd>Del</kbd></button></div>`;
  if ($("corr-as")) $("corr-as").onchange = () => { o.as = $("corr-as").value; commit(`${o.id} → ${o.as}`, { local: true, stale: true }); };
  $("corr-note").onchange = () => { o.note = $("corr-note").value; pushHistory(); pushOps(false, { local: true }); };
  $("b-delop").onclick = () => deleteSelected();
  hint(`${o.id}: drag the outline · Delete removes the correction`, "");
}
async function deleteSelected() {
  if (!SEL) return;
  if (SEL.kind === "way") return deleteWay(SEL.id);
  if (SEL.kind === "corr") { const o = SEL.op; deselect(); removeOps(x => x === o); await commit(`${o.id} deleted`, { stale: true }); }
  else if (SEL.kind === "kerb") { const o = SEL.op, id = SEL.f.properties.id; deselect(); if (o) { removeOps(x => x === o); await commit(`kerb ${id}: correction dropped`, { stale: true }); } }
  else if (SEL.kind === "junction") { const nodes = SEL.nodes, jid = SEL.jid; deselect(); removeOps(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n))); await commit(`junction ${jid}: outline correction dropped`, { stale: true }); }
}

// ---------------------------------------------------------------------------- one-shot drawing tools
function setTool(t) {
  if (TOOL === t) t = null;
  if (GEOMAN_OK) { try { map.pm.disableDraw(); } catch (e) {} }
  if (t) deselect();
  TOOL = t;
  ["draw", "pave", "unpave"].forEach(k => { $("t-" + k).classList.toggle("on", TOOL === k); map.getContainer().classList.toggle("tool-" + k, TOOL === k); });
  if (!TOOL) { hint("click a street, a kerb line or a junction", ""); if (!SEL) $("sel").innerHTML = ""; return; }
  if (!GEOMAN_OK) { hint("Leaflet-Geoman did not load — drawing unavailable", "err"); TOOL = null; return; }
  if (TOOL === "draw") {
    map.pm.enableDraw("Line", { snappable: true, snapDistance: 18, finishOn: "dblclick", templineStyle: { color: "#2ecc71" }, hintlineStyle: { color: "#2ecc71", dashArray: "5 5" }, pathOptions: { color: "#2ecc71", weight: 4 } });
    hint("new way: click the vertices (existing nodes snap = connect), double-click to finish, Esc to cancel", "");
    $("sel").innerHTML = `<div class="h2"><b>new way</b></div><div class="field">
      <label>class</label><select id="nw-hw">${["residential", "tertiary", "secondary", "primary", "unclassified", "living_street", "service", "pedestrian", "footway", "cycleway"].map(o => `<option>${o}</option>`).join("")}</select>
      <label>lanes</label><input id="nw-lanes" value="2"><label>one-way</label><select id="nw-ow"><option value="">no</option><option value="yes">yes</option></select>
      <label>name</label><input id="nw-name" value=""></div>`;
  } else {
    const cut = TOOL === "unpave", col = cut ? "#ffb000" : "#bbbbbb";
    map.pm.enableDraw("Polygon", { snappable: true, snapDistance: 12, finishOn: "dblclick", templineStyle: { color: col }, hintlineStyle: { color: col, dashArray: "5 5" }, pathOptions: { color: col, fillColor: col, fillOpacity: 0.3 } });
    hint(cut ? "unpave: outline the area that is NOT road (it becomes sidewalk); double-click to close" : "pave: outline the area that IS road; double-click to close", "");
    $("sel").innerHTML = cut ? `<div class="h2"><b>unpave area</b></div><div class="field"><label>becomes</label><select id="unpave-as"><option>sidewalk</option><option>median</option><option>verge</option><option>ground</option></select></div><div class="muted small">Tip: for a kerb that is a little off, drag the kerb line instead (click it).</div>`
                             : `<div class="h2"><b>pave area</b></div><div class="muted small">Adds the area to the drivable surface, e.g. a parking bay the twin left as sidewalk.</div>`;
  }
}
map.on("pm:create", async e => {
  const lyr = e.layer, shape = e.shape;
  map.removeLayer(lyr);
  if (shape === "Line") {
    const lls = lyr.getLatLngs(); if (lls.length < 2) return;
    const wid = newOsmId(), ids = [];
    for (const ll of lls) {
      const near = nearestNode(ll, 0.3);
      if (near !== null) ids.push(near); else { const nid = newOsmId(); addOp({ op: "node.add", node: nid, lat: +ll.lat.toFixed(8), lon: +ll.lng.toFixed(8) }); ids.push(nid); }
    }
    const tags = { highway: $("nw-hw") ? $("nw-hw").value : "residential" };
    if ($("nw-lanes") && $("nw-lanes").value.trim()) tags.lanes = $("nw-lanes").value.trim();
    if ($("nw-ow") && $("nw-ow").value) tags.oneway = $("nw-ow").value;
    if ($("nw-name") && $("nw-name").value.trim()) tags.name = $("nw-name").value.trim();
    addOp({ op: "way.add", way: wid, nodes: ids, tags });
    TOOL = null; ["draw", "pave", "unpave"].forEach(k => { $("t-" + k).classList.remove("on"); map.getContainer().classList.remove("tool-" + k); });
    await commit(`way ${wid} added (${ids.length} nodes, ${ids.filter(i => (NODE_WAYS.get(i) || []).length).length} shared)`, { stale: true });
    selectWay(wid);
  } else if (shape === "Polygon") {
    const ring = lyr.getLatLngs()[0].map(lonlat); ring.push(ring[0]);
    if (TOOL === "pave") { addOp({ op: "drivable.add", polygon: ring }); await commit("area paved (rebuild to see the surfaces)", { local: true, stale: true }); }
    else { const as = $("unpave-as") ? $("unpave-as").value : "sidewalk"; addOp({ op: "drivable.cut", polygon: ring, as }); await commit(`area un-paved → ${as} (rebuild to see the surfaces)`, { local: true, stale: true }); }
    const t = TOOL; TOOL = null; setTool(t);       // stay in the tool for the next area
  }
});

// ---------------------------------------------------------------------------- ops panel
function opWhat(o) {
  switch (o.op) {
    case "way.tags": return `way ${o.way}: ${Object.entries(o.set || {}).map(([k, v]) => `${k}=${v}`).join(" ")}${o.unset && o.unset.length ? " −" + o.unset.join(",") : ""}`;
    case "node.move": return `node ${o.node} moved`;
    case "node.add": return `node ${o.node} added`;
    case "node.delete": return `node ${o.node} deleted`;
    case "way.nodes": return `way ${o.way}: ${o.nodes.length} nodes`;
    case "way.add": return `way ${o.way} added (${(o.nodes || []).length} nodes, ${Object.entries(o.tags || {}).map(([k, v]) => `${k}=${v}`).join(" ")})`;
    case "way.delete": return `way ${o.way} deleted`;
    case "way.split": return `way ${o.way} split at ${o.node} → ${o.new_way}`;
    case "road.end": return `stop line way ${o.way} @ node ${o.node}: ${o.shift_m > 0 ? "+" : ""}${o.shift_m} m`;
    case "junction.polygon": return `junction outline ${o.note || ""} (nodes ${(o.nodes || []).slice(0, 3).join(",")}${o.nodes.length > 3 ? "…" : ""})`;
    case "curb.line": return `${o.note || "kerb " + o.curb + " moved"} → ${o.as || "sidewalk"}`;
    case "drivable.add": return `pave ${o.note || (o.polygon.length - 1) + " pts"}`;
    case "drivable.cut": return `unpave → ${o.as || "sidewalk"} ${o.note || ""}`;
  }
  return o.op;
}
function opTarget(o) {
  if (o.polygon) return L.latLngBounds(o.polygon.map(c => [c[1], c[0]]));
  if (o.line) return L.latLngBounds(o.line.map(c => [c[1], c[0]]));
  if (o.node !== undefined && NODE_LL.has(o.node)) return NODE_LL.get(o.node);
  if (o.way !== undefined && WAY_BY_ID.has(o.way)) return L.geoJSON(WAY_BY_ID.get(o.way)).getBounds();
  if (o.nodes && o.nodes.length) { const ll = o.nodes.map(n => NODE_LL.get(n)).filter(Boolean); if (ll.length) return L.latLngBounds(ll); }
  return null;
}
function renderOps() {
  const host = $("ops"); host.innerHTML = "";
  $("nops").textContent = OPS.length;
  for (const o of [...OPS].reverse()) {
    const row = document.createElement("div"); row.className = "op" + (o.disabled ? " off" : "");
    row.innerHTML = `<input type="checkbox" ${o.disabled ? "" : "checked"} title="enabled"><div><span class="what mono" title="zoom to"><b>${o.id}</b> ${esc(opWhat(o))}</span><br><input class="note" placeholder="note…" value="${esc(o.note || "")}"></div><button title="delete">×</button>`;
    row.querySelector("input[type=checkbox]").onchange = async ev => { o.disabled = !ev.target.checked; await commit(`${o.id} ${o.disabled ? "disabled" : "enabled"}`, { stale: true }); };
    row.querySelector(".what").onclick = () => { const t = opTarget(o); if (!t) return; if (t instanceof L.LatLng) map.setView(t, Math.max(map.getZoom(), 20)); else map.fitBounds(t, { maxZoom: 20, padding: [40, 40] });
      if (o.op === "curb.line") { const k = kerbLayerFor(o); if (k) selectKerb(k.f, k.l); }
      else if (o.polygon && o.op !== "junction.polygon") selectCorr(o); else if (o.way !== undefined && WAY_BY_ID.has(o.way)) selectWay(o.way); };
    row.querySelector(".note").onchange = ev => { o.note = ev.target.value; pushHistory(); pushOps(false, { local: true }); };
    row.querySelector("button").onclick = async () => { if (SEL && (SEL.kind === "corr" || SEL.kind === "kerb") && SEL.op === o) deselect(true); removeOps(x => x === o); await commit(`${o.id} deleted`, { stale: true }); };
    host.appendChild(row);
  }
}

// ---------------------------------------------------------------------------- save, rebuild, keys
async function save() { if (await pushOps(true, { local: true })) hint(`saved ${OPS.length} correction(s) to ${$("cpath").textContent}`, "ok"); }
async function rebuild() {
  const b = $("b-rebuild"); b.disabled = true; b.textContent = "rebuilding…"; hint("rebuilding the twin (twinmodel build --quick)…", "");
  $("log").textContent = "running…";
  try {
    const r = await (await fetch("/api/rebuild", { method: "POST" })).json();
    $("log").textContent = (r.summary || []).join("\n") + (r.error ? "\n" + r.error : "") + (r.ok ? "" : "\n\n" + (r.tail || ""));
    if (r.reports) $("log").textContent += "\n\ncorrections: " + JSON.stringify(r.reports);
    setDirty(false);
    const keep = SEL && SEL.kind === "way" ? SEL.id : null;
    deselect(true);
    await refreshTwin();
    await refreshOsm();
    if (keep !== null && WAY_BY_ID.has(keep)) selectWay(keep, true);
    hint(r.ok ? `rebuilt in ${r.seconds} s (${r.when}); twin layers reloaded` : `rebuild FAILED (rc ${r.rc}) — see "last rebuild"`, r.ok ? "ok" : "err");
  } catch (e) { hint("rebuild request failed: " + e, "err"); }
  b.disabled = false; b.innerHTML = "rebuild twin<kbd>R</kbd>";
}
$("t-draw").onclick = () => setTool("draw"); $("t-pave").onclick = () => setTool("pave"); $("t-unpave").onclick = () => setTool("unpave");
$("b-undo").onclick = undo; $("b-redo").onclick = redo; $("b-save").onclick = save; $("b-rebuild").onclick = rebuild;
document.addEventListener("keydown", ev => {
  if (ev.ctrlKey || ev.metaKey) {
    if (ev.key === "z") { ev.preventDefault(); undo(); } else if (ev.key === "y") { ev.preventDefault(); redo(); } else if (ev.key === "s") { ev.preventDefault(); save(); }
    return;
  }
  if (isEditable(ev.target)) return;
  const k = ev.key.toLowerCase();
  if (ev.key === "Escape") { if (TOOL) setTool(TOOL); else if (SPLIT_ARMED) { SPLIT_ARMED = false; map.getContainer().classList.remove("split-armed"); hint("split cancelled", ""); } else deselect(); }
  else if (ev.key === "Delete" || ev.key === "Backspace") { if (SEL) { ev.preventDefault(); deleteSelected(); } }
  else if (k === "n") setTool("draw"); else if (k === "u") setTool("unpave"); else if (k === "p") setTool("pave");
  else if (k === "s") armSplit(); else if (k === "r") rebuild();
  else if (k === "f") flicker(TWIN_KEYS.filter(x => groups[x])); else if (k === "o") flicker(["osm_highway", "osm_node", "osm_raw", "osm_building", "corr"].filter(x => groups[x]));
});
window.addEventListener("beforeunload", ev => { if (DIRTY) { ev.preventDefault(); ev.returnValue = ""; } });
map.on("click", ev => { if (!FRAME || TOOL) return; if (SEL) deselect(); else placePopup(ev); });

// ---------------------------------------------------------------------------- boot
async function bootEditor() {
  const meta = await loadMeta();
  buildBasemaps(meta);
  $("title").textContent = "layers · " + meta.name; $("ename").textContent = meta.name;
  $("sub").textContent = `${meta.profile || ""} · origin ${meta.origin[0].toFixed(5)}, ${meta.origin[1].toFixed(5)}`;
  setViewFromHash(meta);
  const c = await (await fetch("/api/corrections")).json();
  OPS = c.ops || []; NEXT_ID = c.next_osm_id; setDirty(c.dirty); pushHistory();
  $("cpath").textContent = c.path + (c.exists ? "" : " (created on save)");
  if (c.last_rebuild) $("log").textContent = (c.last_rebuild.summary || []).join("\n");
  $("help").innerHTML = "<b>F</b> flicker twin · <b>O</b> flicker OSM · <b>Esc</b> deselect · <b>Del</b> delete · <b>S</b> split · <b>N</b>/<b>U</b>/<b>P</b> draw · <b>R</b> rebuild · <b>Ctrl+Z/Y/S</b> · click empty map: coordinates + Street View";
  const osmRawFc = await (await fetch("/api/osm_raw.geojson")).json();
  addOverlay("osm-layers", "osm_raw", "ways before corrections (magenta dashed)", "#ff00ff", geoLayer(osmRawFc, f => f.properties.layer === "osm_highway", () => ({ color: "#ff00ff", weight: 1.5, opacity: 0.8, dashArray: "2 4", interactive: false }), null, () => null), 0.8, false);
  addOverlay("osm-layers", "osm_building", "buildings", "#d9a066", geoLayer(osmRawFc, f => f.properties.layer === "osm_building", () => ({ color: "#d9a066", weight: 1, opacity: 1, fillOpacity: 0.08, interactive: false }), null, () => null), 0.7, false);
  await refreshTwin();
  await refreshOsm();
  renderOps();
  if (!GEOMAN_OK) hint("Leaflet-Geoman failed to load: tags and stop lines work; vertex / outline dragging needs the CDN", "err");
}
bootEditor().catch(e => { hint("failed: " + e, "err"); console.error(e); });
"""

GEOMAN_HEAD = r"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.css">
<script src="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.min.js"></script>"""

EDITOR_PAGE = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
               "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Twin editor</title>\n"
               + go.LEAFLET_HEAD + "\n" + GEOMAN_HEAD + "\n<style>" + go.CSS + EDITOR_CSS + "</style></head>\n<body>\n<div id=\"map\"></div>\n"
               + EDITOR_PANEL_HTML + TOOLS_HTML
               + "<script>" + go.JS_CORE + EDITOR_JS + "</script>\n</body></html>\n")


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
