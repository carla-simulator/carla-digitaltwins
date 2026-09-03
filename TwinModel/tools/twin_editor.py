#!/usr/bin/env python
"""Twin editor: review the OSM input against the imagery and record corrections, rebuild, repeat.

    python tools/twin_editor.py out/v10_eixample eixample                 # http://127.0.0.1:8791/
    python tools/twin_editor.py out/v10_eixample eixample --corrections data/corrections/eixample.json

The page is ``tools/geo_overlay.py`` (basemaps: ICGC / PNOA / Google / Esri / the build's own
ortho; twin layers incl. per-lane bands; ML detections) plus an editing side panel.  Nothing is
edited in place: every action becomes an operation in the corrections file
(:mod:`twinmodel.corrections`, default ``data/corrections/<name>.json``) that ``twinmodel build``
replays on top of the raw OSM extract, so the twin is always regenerated from OSM + corrections
and the corrections survive a re-fetch of OSM.

Tools (left panel, keys 1-8):

  1 select        click an OSM way: structured lane editor (lanes / forward / backward, one-way,
                  bus lanes, parking left/right, cycle lanes, sidewalks, width, maxspeed) + raw
                  tags -> ``way.tags``; delete way -> ``way.delete``
  2 vertices      drag / add (click a segment midpoint) / remove (right-click) the way's vertices
                  -> ``node.move`` / ``node.add`` / ``way.nodes``; a vertex dropped on another
                  way's node connects to it (shared node = junction)
  3 draw way      click vertices, double-click to finish (snaps to existing nodes) -> ``way.add``
  4 split         click an interior node of the selected way -> ``way.split``
  5 road end      click a twin road near a junction, drag the handle along the road: moves the
                  road end (stop line, signal, junction mouth) -> ``road.end``
  6 junction      click a junction polygon, edit its outline -> ``junction.polygon``
  7 pave          draw a polygon: becomes road surface -> ``drivable.add``
  8 unpave        draw a polygon: becomes sidewalk (or median / verge) -> ``drivable.cut``

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

EDITOR_CSS = r"""
  #tools { position: absolute; top: 10px; left: 10px; z-index: 1000; width: 340px; max-height: calc(100% - 70px); overflow: auto;
           background: rgba(20,20,24,.94); border: 1px solid #333; border-radius: 8px; padding: 10px 12px; }
  #tools h1 { font-size: 14px; margin: 0 0 6px; display: flex; align-items: center; gap: 8px; }
  #dirty { width: 9px; height: 9px; border-radius: 50%; background: #444; display: inline-block; }
  #dirty.on { background: #ffb000; }
  .modes { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin: 6px 0; }
  .modes button { padding: 6px 4px; font: 11px/1.2 system-ui, sans-serif; background: #262630; color: #ddd; border: 1px solid #444; border-radius: 5px; cursor: pointer; }
  .modes button.on { background: #3a6ff0; border-color: #6a8ff8; color: #fff; }
  .modes button small { display: block; color: #9aa; font-size: 10px; }
  .modes button.on small { color: #dfe6ff; }
  .actions { display: flex; gap: 6px; margin: 6px 0; flex-wrap: wrap; }
  .actions button, .field button, #ops button { padding: 4px 8px; font: 12px system-ui, sans-serif; background: #262630; color: #ddd; border: 1px solid #444; border-radius: 5px; cursor: pointer; }
  .actions button.primary { background: #2f7d4f; border-color: #4caf50; color: #fff; }
  .actions button.warn { background: #7a2f2f; border-color: #c0504d; color: #fff; }
  button:disabled { opacity: .45; cursor: default; }
  #hint { color: #cdd; font-size: 12px; min-height: 30px; margin: 4px 0; padding: 6px 8px; background: #1a1a22; border-radius: 5px; border-left: 3px solid #3a6ff0; }
  #hint.err { border-left-color: #e74c3c; }
  #hint.ok { border-left-color: #2ecc71; }
  .field { display: grid; grid-template-columns: 120px 1fr; gap: 4px 8px; align-items: center; margin: 2px 0; font-size: 12px; }
  .field label { color: #9aa; }
  .field input, .field select { background: #14141a; color: #eee; border: 1px solid #444; border-radius: 4px; padding: 3px 5px; font: 12px ui-monospace, Menlo, monospace; width: 100%; box-sizing: border-box; }
  .field input.changed, .field select.changed { border-color: #ffb000; }
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
  .leaflet-container.mode-vertices, .leaflet-container.mode-junction { cursor: crosshair; }
  .leaflet-pm-icon-vertex { background: #ffb000 !important; }
  .endhandle { background: #ffb000; border: 2px solid #000; border-radius: 50%; width: 14px !important; height: 14px !important; margin-left: -7px !important; margin-top: -7px !important; box-shadow: 0 0 0 2px #ffb000aa; }
"""

TOOLS_HTML = r"""
<div id="tools">
  <h1><span id="dirty" title="unsaved changes"></span>Twin editor · <span id="ename"></span></h1>
  <div class="muted mono" id="cpath"></div>
  <div class="modes" id="modes">
    <button data-mode="select"><b>1</b> select<small>way tags</small></button>
    <button data-mode="vertices"><b>2</b> vertices<small>move / add / del</small></button>
    <button data-mode="draw"><b>3</b> draw way<small>dbl-click ends</small></button>
    <button data-mode="split"><b>4</b> split<small>click a node</small></button>
    <button data-mode="roadend"><b>5</b> road end<small>stop line</small></button>
    <button data-mode="junction"><b>6</b> junction<small>outline</small></button>
    <button data-mode="pave"><b>7</b> pave<small>→ road</small></button>
    <button data-mode="unpave"><b>8</b> unpave<small>→ sidewalk</small></button>
  </div>
  <div id="hint">pick a tool</div>
  <div class="actions">
    <button id="b-undo" title="Ctrl+Z">↶ undo</button><button id="b-redo" title="Ctrl+Y">↷ redo</button>
    <button id="b-save" class="primary" title="Ctrl+S">save</button>
    <button id="b-rebuild" class="primary" title="save + twinmodel build --quick (~10 s)">rebuild twin</button>
  </div>
  <div id="editor"></div>
  <details open><summary>corrections <span id="nops" class="badge">0</span></summary><div id="ops"></div></details>
  <details><summary>last rebuild</summary><div id="log" class="mono">—</div></details>
</div>
"""

EDITOR_JS = r"""
// ============================================================================ editor state
const GEOMAN_OK = !!(L.PM);
let OPS = [], HIST = [], HIST_I = -1, NEXT_ID = -1, MODE = null, SEL = null, DIRTY = false;
let OSM = null, TWIN = null, WAY_BY_ID = new Map(), NODE_LL = new Map(), NODE_WAYS = new Map(), WAY_LAYER = new Map();
let osmWays = null, osmNodes = null, osmRaw = null, corrLayer = null, selLayer = null, endHandle = null, endLine = null, editing = null;
const $ = id => document.getElementById(id);
const hint = (msg, cls) => { const h = $("hint"); h.textContent = msg; h.className = cls || ""; };
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const mDist = (a, b) => { const p = FRAME.toLocal(a.lat, a.lng), q = FRAME.toLocal(b.lat, b.lng); return Math.hypot(p[0] - q[0], p[1] - q[1]); };
const lonlat = ll => [+ll.lng.toFixed(8), +ll.lat.toFixed(8)];

// ---------------------------------------------------------------------------- ops + history
function snapshot() { return JSON.stringify(OPS); }
function pushHistory() { HIST = HIST.slice(0, HIST_I + 1); HIST.push(snapshot()); HIST_I = HIST.length - 1; if (HIST.length > 200) { HIST.shift(); HIST_I--; } }
async function commit(label) {
  pushHistory();
  await pushOps(false);
  hint(label, "ok");
}
async function pushOps(save) {
  const r = await (await fetch("/api/corrections", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ops: OPS, save }) })).json();
  if (!r.ok) { hint("rejected: " + (r.problems || [r.error]).join("; "), "err"); return false; }
  setDirty(r.dirty);
  renderOps();
  await refreshOsm();
  return true;
}
function setDirty(d) { DIRTY = d; $("dirty").className = d ? "on" : ""; }
async function undo() { if (HIST_I <= 0) return; HIST_I--; OPS = JSON.parse(HIST[HIST_I]); await pushOps(false); hint("undo", ""); }
async function redo() { if (HIST_I >= HIST.length - 1) return; HIST_I++; OPS = JSON.parse(HIST[HIST_I]); await pushOps(false); hint("redo", ""); }
function newOpId() { const used = new Set(OPS.map(o => o.id)); let n = OPS.length + 1; while (used.has("c" + n)) n++; return "c" + n; }
function newOsmId() { return NEXT_ID--; }
function findOp(pred) { return OPS.find(o => pred(o)); }
function addOp(o) { o.id = o.id || newOpId(); o.ts = new Date().toISOString().slice(0, 19); OPS.push(o); return o; }
function removeOp(o) { OPS = OPS.filter(x => x !== o); }

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
  const touched = OPS.some(o => !o.disabled && (o.way === p.osm_id));
  return { color: touched ? "#ffb000" : col, weight: p.new ? 4 : 2.5, opacity: 1, dashArray: p.new ? "6 4" : null };
}
async function refreshOsm() {
  OSM = await (await fetch("/api/osm.geojson")).json();
  indexOsm(OSM);
  const prevW = removeOverlay("osm_highway"), prevN = removeOverlay("osm_node");
  WAY_LAYER.clear();
  osmWays = geoLayer(OSM, f => f.properties.layer === "osm_highway", wayStyle, null, (f, l) => {
    WAY_LAYER.set(f.properties.osm_id, l);
    l.on("click", ev => { L.DomEvent.stop(ev); onWayClick(f, l, ev); });
  });
  osmNodes = geoLayer(OSM, f => f.properties.layer === "osm_node", null, (f, ll) => {
    const p = f.properties, junction = p.degree >= 2;
    return L.circleMarker(ll, { renderer: canvas, radius: junction ? 4 : 2.5, color: p.tagged ? "#00e5ff" : (junction ? "#ff4040" : "#ffffff"),
      weight: 1, fillColor: p.osm_id < 0 ? "#2ecc71" : (junction ? "#ff4040" : (p.tagged ? "#00e5ff" : "#ffffff")), fillOpacity: 0.9, opacity: 1 });
  }, (f, l) => { l.on("click", ev => { L.DomEvent.stop(ev); onNodeClick(f, l, ev); }); });
  addOverlay("osm-layers", "osm_highway", `OSM ways, corrected (${OSM.features.filter(f => f.properties.layer === "osm_highway").length}) · orange = touched`, "#ffffff", osmWays, prevW ? prevW.opacity : 0.9, prevW ? prevW.on : true);
  addOverlay("osm-layers", "osm_node", `way nodes · red junction, cyan tagged, green new`, "#ff4040", osmNodes, prevN ? prevN.opacity : 1, prevN ? prevN.on : true);
  osmWays.bringToFront && osmWays.bringToFront();
  if (OSM.unmatched && OSM.unmatched.length) hint(`${OSM.unmatched.length} correction(s) match nothing in the extract: ${OSM.unmatched.map(u => u.id).join(", ")}`, "err");
  renderCorrLayer();
  if (SEL && SEL.kind === "way") { const f = WAY_BY_ID.get(SEL.id); if (f) selectWay(f); else clearSel(); }
}
async function refreshTwin() {
  TWIN = await (await fetch("/api/twin.geojson")).json();
  const meta = await (await fetch("/api/meta")).json();
  mountTwin(TWIN, meta.twin_counts, (f, l) => {
    l.on("click", ev => { if (onTwinClick(f, l, ev)) L.DomEvent.stop(ev); else l.openPopup && l.bindPopup(() => popupTable(f.properties), { maxWidth: 420 }).openPopup(ev.latlng); });
  });
  if (osmWays) osmWays.bringToFront && osmWays.bringToFront();
}

// ---------------------------------------------------------------------------- correction geometry layer
function renderCorrLayer() {
  if (!corrLayer) { corrLayer = L.featureGroup(); addOverlay("osm-layers", "corr", "correction polygons · orange unpave, grey pave, purple junction", "#ffb000", corrLayer, 0.9, true); }
  corrLayer.clearLayers();
  for (const o of OPS) {
    if (o.disabled || !o.polygon) continue;
    const col = o.op === "drivable.cut" ? "#ffb000" : o.op === "drivable.add" ? "#bbbbbb" : "#c860ff";
    const poly = L.polygon(o.polygon.map(c => [c[1], c[0]]), { color: col, weight: 2, fillColor: col, fillOpacity: 0.25, dashArray: "5 4" });
    poly._op = o;
    poly.bindTooltip(`${o.id} ${o.op}${o.as ? " → " + o.as : ""}${o.note ? " · " + o.note : ""}`, { sticky: true });
    poly.on("click", ev => { L.DomEvent.stop(ev); editPolygonOp(o, poly); });
    corrLayer.addLayer(poly);
  }
}

// ---------------------------------------------------------------------------- selection
function clearSel() {
  if (selLayer) { map.removeLayer(selLayer); selLayer = null; }
  SEL = null; $("editor").innerHTML = "";
}
function selectWay(f) {
  if (selLayer) map.removeLayer(selLayer);
  SEL = { kind: "way", id: f.properties.osm_id, f };
  selLayer = L.geoJSON(f, { style: { color: "#ffe14d", weight: 8, opacity: 0.55, interactive: false } }).addTo(map);
  renderWayEditor(f);
}

// ---------------------------------------------------------------------------- way editor (tags)
const LANE_FIELDS = [
  ["highway", "class", ["", "primary", "secondary", "tertiary", "residential", "unclassified", "living_street", "service", "pedestrian", "motorway", "trunk", "footway", "cycleway"]],
  ["name", "name", null],
  ["oneway", "one-way", ["", "yes", "no", "-1"]],
  ["lanes", "lanes (total)", null],
  ["lanes:forward", "lanes forward", null],
  ["lanes:backward", "lanes backward", null],
  ["lanes:psv", "bus lanes (lanes:psv)", null],
  ["busway", "busway", ["", "lane", "opposite_lane"]],
  ["busway:right", "busway right", ["", "lane", "opposite_lane"]],
  ["busway:left", "busway left", ["", "lane", "opposite_lane"]],
  ["parking:right", "parking right", ["", "lane", "street_side", "no", "separate"]],
  ["parking:right:orientation", "  orientation", ["", "parallel", "diagonal", "perpendicular"]],
  ["parking:left", "parking left", ["", "lane", "street_side", "no", "separate"]],
  ["parking:left:orientation", "  orientation", ["", "parallel", "diagonal", "perpendicular"]],
  ["parking:both", "parking both", ["", "lane", "street_side", "no"]],
  ["cycleway:right", "cycle right", ["", "lane", "track", "shared_lane", "no"]],
  ["cycleway:left", "cycle left", ["", "lane", "track", "shared_lane", "no"]],
  ["cycleway", "cycleway", ["", "lane", "track", "opposite", "no"]],
  ["sidewalk", "sidewalk", ["", "both", "left", "right", "no", "separate"]],
  ["width", "width (m)", null],
  ["maxspeed", "maxspeed", null],
  ["turn:lanes", "turn:lanes", null],
];
function wayTags(f) { const t = {}; for (const [k, v] of Object.entries(f.properties)) if (k.startsWith("tag:")) t[k.slice(4)] = v; return t; }
function renderWayEditor(f) {
  const p = f.properties, tags = wayTags(f), orig = p.orig_tags || tags;
  const twinRoads = (TWIN ? TWIN.features.filter(x => x.properties.layer === "roads" && (parseJ(x.properties.osm_way_ids) || []).includes(p.osm_id)) : []);
  const laneSummary = twinRoads.length ? twinRoads.map(r => { try { const ls = JSON.parse(r.properties.lanes); return `${r.properties.id}: ${ls.filter(l => l.type !== "sidewalk").map(l => l.type[0]).join("")}`; } catch (e) { return r.properties.id; } }).join(" · ") : "not in the twin (skipped class or outside)";
  let html = `<div class="h2">OSM way ${p.osm_id}${p.new ? '<span class="badge new">new</span>' : ""} <a target="_blank" rel="noopener" href="https://www.openstreetmap.org/way/${p.osm_id}">osm ↗</a></div>
    <div class="muted" style="font-size:11px">${p.n_nodes} nodes · twin: ${esc(laneSummary)} <span class="muted">(d driving, p parking, b biking)</span></div>
    <div class="field" id="fields">`;
  for (const [k, label, opts] of LANE_FIELDS) {
    const v = tags[k] || "", ch = (orig[k] || "") !== v ? "changed" : "";
    if (opts) html += `<label>${label}</label><select data-k="${k}" class="${ch}">${opts.map(o => `<option value="${o}" ${o === v ? "selected" : ""}>${o || "—"}</option>`).join("")}${opts.includes(v) ? "" : `<option value="${esc(v)}" selected>${esc(v)}</option>`}</select>`;
    else html += `<label>${label}</label><input data-k="${k}" value="${esc(v)}" class="${ch}">`;
  }
  html += `</div><details><summary>all tags (${Object.keys(tags).length})</summary><div id="tags"><table>`;
  const known = new Set(LANE_FIELDS.map(x => x[0]));
  for (const [k, v] of Object.entries(tags).sort()) html += `<tr><td class="mono">${esc(k)}</td><td><input data-tag="${esc(k)}" value="${esc(v)}" class="${(orig[k] || "") !== v ? "changed" : ""}"></td><td class="x" data-del="${esc(k)}" title="remove tag">×</td></tr>`;
  html += `<tr><td><input id="newk" placeholder="key"></td><td><input id="newv" placeholder="value"></td><td class="x" id="addtag" title="add">+</td></tr></table></div></details>
    <div class="actions"><button id="b-apply" class="primary">apply tags</button><button id="b-revert">revert way</button><button id="b-vert">edit vertices</button><button id="b-delway" class="warn">delete way</button></div>`;
  $("editor").innerHTML = html;
  $("b-apply").onclick = () => applyTags(f);
  $("b-revert").onclick = async () => { OPS = OPS.filter(o => !(o.op === "way.tags" && o.way === p.osm_id)); await commit(`way ${p.osm_id}: tag corrections removed`); };
  $("b-vert").onclick = () => { setMode("vertices"); startVertexEdit(p.osm_id); };
  $("b-delway").onclick = () => deleteWay(p.osm_id);
  $("addtag").onclick = () => { const k = $("newk").value.trim(), v = $("newv").value.trim(); if (!k) return; tags[k] = v; f.properties["tag:" + k] = v; renderWayEditor(f); };
  document.querySelectorAll("#tags .x[data-del]").forEach(el => el.onclick = () => { delete f.properties["tag:" + el.dataset.del]; renderWayEditor(f); });
}
function applyTags(f) {
  const p = f.properties, orig = p.orig_tags || wayTags(f);
  const now = {};
  document.querySelectorAll("#tags input[data-tag]").forEach(el => { if (el.value.trim() !== "") now[el.dataset.tag] = el.value.trim(); });
  document.querySelectorAll("#fields [data-k]").forEach(el => { const v = el.value.trim(); if (v !== "") now[el.dataset.k] = v; else delete now[el.dataset.k]; });
  const set = {}, unset = [];
  for (const [k, v] of Object.entries(now)) if (orig[k] !== v) set[k] = v;
  for (const k of Object.keys(orig)) if (!(k in now)) unset.push(k);
  OPS = OPS.filter(o => !(o.op === "way.tags" && o.way === p.osm_id));
  if (Object.keys(set).length || unset.length) {
    const o = { op: "way.tags", way: p.osm_id, set };
    if (unset.length) o.unset = unset;
    addOp(o);
    commit(`way ${p.osm_id}: ${Object.keys(set).length} tag(s) set, ${unset.length} removed`);
  } else commit(`way ${p.osm_id}: tags back to the original`);
}
async function deleteWay(wid) {
  if (!confirm(`Delete OSM way ${wid} from the twin input?`)) return;
  if (wid < 0) OPS = OPS.filter(o => !(o.op === "way.add" && o.way === wid));
  else { OPS = OPS.filter(o => o.way !== wid); addOp({ op: "way.delete", way: wid }); }
  clearSel();
  await commit(`way ${wid} deleted`);
}

// ---------------------------------------------------------------------------- vertex editing (Geoman)
function startVertexEdit(wid) {
  if (!GEOMAN_OK) { hint("Leaflet-Geoman did not load (offline?) — vertex editing unavailable", "err"); return; }
  const f = WAY_BY_ID.get(wid), lyr = WAY_LAYER.get(wid);
  if (!f || !lyr) return;
  stopEditing();
  if (selLayer) { map.removeLayer(selLayer); selLayer = null; }
  const ids = [...(f.properties.nodes || [])];
  const orig = { ids: [...ids], ll: ids.map(n => NODE_LL.get(n)) };
  editing = { kind: "way", wid, lyr, ids, moved: new Map(), added: new Map() };
  lyr.setStyle({ color: "#ffe14d", weight: 4, opacity: 1 });
  lyr.pm.enable({ allowSelfIntersection: true, snappable: true, snapDistance: 18, removeVertexOn: "contextmenu", addVertexOn: "click" });
  lyr.on("pm:vertexadded", e => { const i = e.indexPath ? e.indexPath[e.indexPath.length - 1] : e.index; const nid = newOsmId(); editing.ids.splice(i, 0, nid); editing.added.set(nid, e.latlng); });
  lyr.on("pm:vertexremoved", e => { const i = e.indexPath ? e.indexPath[e.indexPath.length - 1] : e.index; const nid = editing.ids.splice(i, 1)[0]; editing.added.delete(nid); editing.moved.delete(nid); });
  lyr.on("pm:markerdragend", e => {
    const i = e.indexPath ? e.indexPath[e.indexPath.length - 1] : e.index; const nid = editing.ids[i];
    const ll = lyr.getLatLngs()[i];
    const snapTo = nearestNode(ll, 0.15, new Set([nid]));
    if (snapTo !== null && snapTo !== nid) {          // dropped on another way's node: connect (share the node)
      editing.ids[i] = snapTo; editing.added.delete(nid); editing.moved.delete(nid);
      hint(`vertex joined to node ${snapTo} (shared with way ${(NODE_WAYS.get(snapTo) || []).join(", ")})`, "ok"); return;
    }
    if (editing.added.has(nid)) editing.added.set(nid, ll); else editing.moved.set(nid, ll);
  });
  hint(`way ${wid}: drag vertices, click a segment to add one, right-click a vertex to remove it. Enter = done, Esc = cancel`, "");
  $("editor").innerHTML = `<div class="h2">editing vertices of way ${wid}</div><div class="actions"><button id="b-done" class="primary">done (Enter)</button><button id="b-cancel">cancel (Esc)</button></div>
    <div class="muted" style="font-size:11px">A moved junction node moves for every way that shares it. Dropping a vertex onto another way's node connects the two ways.</div>`;
  $("b-done").onclick = finishVertexEdit; $("b-cancel").onclick = () => { stopEditing(); refreshOsm(); hint("vertex edit cancelled", ""); };
  editing.orig = orig;
}
async function finishVertexEdit() {
  const e = editing; if (!e || e.kind !== "way") return;
  const lls = e.lyr.getLatLngs();
  e.lyr.pm.disable();
  let n = 0;
  for (const [nid, ll] of e.added) { OPS = OPS.filter(o => !(o.op === "node.add" && o.node === nid)); addOp({ op: "node.add", node: nid, lat: +ll.lat.toFixed(8), lon: +ll.lng.toFixed(8) }); n++; }
  for (const [nid, ll] of e.moved) {
    if (mDist(ll, NODE_LL.get(nid) || ll) < 0.01) continue;
    OPS = OPS.filter(o => !(o.op === "node.move" && o.node === nid));
    addOp({ op: "node.move", node: nid, lat: +ll.lat.toFixed(8), lon: +ll.lng.toFixed(8) }); n++;
  }
  if (JSON.stringify(e.ids) !== JSON.stringify(e.orig.ids)) {
    if (e.wid < 0) { const wa = findOp(o => o.op === "way.add" && o.way === e.wid); if (wa) wa.nodes = [...e.ids]; }
    else { OPS = OPS.filter(o => !(o.op === "way.nodes" && o.way === e.wid)); addOp({ op: "way.nodes", way: e.wid, nodes: [...e.ids] }); }
    n++;
  }
  editing = null;
  if (n) await commit(`way ${e.wid}: ${e.moved.size} node(s) moved, ${e.added.size} added${e.ids.length !== e.orig.ids.length ? ", node list changed" : ""}`);
  else { await refreshOsm(); hint("no change", ""); }
  setMode("select");
}
function stopEditing() {
  if (!editing) return;
  try { if (editing.lyr && editing.lyr.pm) editing.lyr.pm.disable(); } catch (err) {}
  if (editing.kind === "junction" || editing.kind === "poly") { try { map.removeLayer(editing.lyr); } catch (err) {} }
  editing = null;
  if (endHandle) { map.removeLayer(endHandle); endHandle = null; }
  if (endLine) { map.removeLayer(endLine); endLine = null; }
}

// ---------------------------------------------------------------------------- draw a new way
function startDraw() {
  if (!GEOMAN_OK) { hint("Leaflet-Geoman did not load — drawing unavailable", "err"); return; }
  map.pm.enableDraw("Line", { snappable: true, snapDistance: 18, finishOn: "dblclick", templineStyle: { color: "#2ecc71" }, hintlineStyle: { color: "#2ecc71", dashArray: "5 5" }, pathOptions: { color: "#2ecc71", weight: 4 } });
  hint("draw way: click vertices, double-click to finish (snaps to existing nodes = connects). Esc cancels", "");
  $("editor").innerHTML = `<div class="h2">new way</div><div class="field">
    <label>highway</label><select id="nw-hw">${["residential", "tertiary", "secondary", "primary", "unclassified", "living_street", "service", "pedestrian", "footway", "cycleway"].map(o => `<option>${o}</option>`).join("")}</select>
    <label>lanes</label><input id="nw-lanes" value="2"><label>one-way</label><select id="nw-ow"><option value="">no</option><option value="yes">yes</option></select>
    <label>name</label><input id="nw-name" value=""></div><div class="muted" style="font-size:11px">Tags are applied when the line is finished; edit them afterwards in select mode.</div>`;
}
map.on("pm:create", async e => {
  const lyr = e.layer, shape = e.shape;
  map.removeLayer(lyr);
  if (shape === "Line") {
    const lls = lyr.getLatLngs(); if (lls.length < 2) return;
    const wid = newOsmId(), ids = [];
    for (const ll of lls) {
      const near = nearestNode(ll, 0.25);
      if (near !== null) ids.push(near);
      else { const nid = newOsmId(); addOp({ op: "node.add", node: nid, lat: +ll.lat.toFixed(8), lon: +ll.lng.toFixed(8) }); ids.push(nid); }
    }
    const tags = { highway: $("nw-hw") ? $("nw-hw").value : "residential" };
    if ($("nw-lanes") && $("nw-lanes").value.trim()) tags.lanes = $("nw-lanes").value.trim();
    if ($("nw-ow") && $("nw-ow").value) tags.oneway = $("nw-ow").value;
    if ($("nw-name") && $("nw-name").value.trim()) tags.name = $("nw-name").value.trim();
    addOp({ op: "way.add", way: wid, nodes: ids, tags });
    await commit(`way ${wid} added (${ids.length} nodes, ${ids.filter(i => i >= 0 || NODE_WAYS.has(i)).length} shared)`);
    setMode("select");
    const f = WAY_BY_ID.get(wid); if (f) selectWay(f);
  } else if (shape === "Polygon") {
    const ring = lyr.getLatLngs()[0].map(lonlat); ring.push(ring[0]);
    if (MODE === "pave") { addOp({ op: "drivable.add", polygon: ring }); await commit("area paved (drivable.add) — rebuild to see the surfaces"); }
    else if (MODE === "unpave") { const as = $("unpave-as") ? $("unpave-as").value : "sidewalk"; addOp({ op: "drivable.cut", polygon: ring, as }); await commit(`area un-paved → ${as} (drivable.cut) — rebuild to see the surfaces`); }
    startPolygonDraw();   // stay in the mode
  }
});
function startPolygonDraw() {
  if (!GEOMAN_OK) { hint("Leaflet-Geoman did not load — drawing unavailable", "err"); return; }
  const cut = MODE === "unpave", col = cut ? "#ffb000" : "#bbbbbb";
  map.pm.enableDraw("Polygon", { snappable: true, snapDistance: 12, finishOn: "dblclick", templineStyle: { color: col }, hintlineStyle: { color: col, dashArray: "5 5" }, pathOptions: { color: col, fillColor: col, fillOpacity: 0.3 } });
  hint(cut ? "unpave: draw the area that is NOT road (the sidewalk side of the real kerb). Double-click to close." : "pave: draw the area that IS road but the twin has as sidewalk / ground. Double-click to close.", "");
  $("editor").innerHTML = cut ? `<div class="h2">unpave → raised</div><div class="field"><label>becomes</label><select id="unpave-as"><option>sidewalk</option><option>median</option><option>verge</option><option>ground</option></select></div>
    <div class="muted" style="font-size:11px">Kerb correction: the drivable outline loses this area, the kerb line and the sidewalk are re-derived. Click an existing correction polygon to edit or delete it.</div>`
    : `<div class="h2">pave → drivable</div><div class="muted" style="font-size:11px">Adds this area to the drivable surface (e.g. a parking bay the twin left as sidewalk). Click an existing correction polygon to edit it.</div>`;
}
function editPolygonOp(o, poly) {
  if (!GEOMAN_OK) return;
  stopEditing();
  const lyr = L.polygon(poly.getLatLngs(), { color: "#ffe14d", weight: 3, fillOpacity: 0.15 }).addTo(map);
  editing = { kind: "poly", lyr, op: o };
  lyr.pm.enable({ allowSelfIntersection: false, removeVertexOn: "contextmenu" });
  hint(`${o.id} ${o.op}: drag vertices (right-click removes). Enter = done, Esc = cancel`, "");
  $("editor").innerHTML = `<div class="h2">${o.id} · ${o.op}${o.as ? " → " + o.as : ""}</div><div class="actions"><button id="b-done" class="primary">done (Enter)</button><button id="b-cancel">cancel (Esc)</button><button id="b-delop" class="warn">delete correction</button></div>`;
  $("b-done").onclick = async () => { const ring = lyr.getLatLngs()[0].map(lonlat); ring.push(ring[0]); o.polygon = ring; stopEditing(); await commit(`${o.id}: outline updated`); };
  $("b-cancel").onclick = () => { stopEditing(); renderCorrLayer(); hint("cancelled", ""); };
  $("b-delop").onclick = async () => { stopEditing(); removeOp(o); await commit(`${o.id} deleted`); };
}

// ---------------------------------------------------------------------------- split
async function onNodeClick(f, l, ev) {
  const nid = f.properties.osm_id;
  if (MODE !== "split") { l.bindPopup(() => popupTable(f.properties) + `<div class="muted">in ways ${(NODE_WAYS.get(nid) || []).join(", ")}</div>`, { maxWidth: 420 }).openPopup(); return; }
  let cands = (NODE_WAYS.get(nid) || []).filter(w => { const nodes = WAY_BY_ID.get(w).properties.nodes; const i = nodes.indexOf(nid); return i > 0 && i < nodes.length - 1; });
  if (SEL && SEL.kind === "way" && cands.includes(SEL.id)) cands = [SEL.id];
  if (!cands.length) { hint(`node ${nid} is an end node of its ways — nothing to split`, "err"); return; }
  if (cands.length > 1) { hint(`node ${nid} is interior to ways ${cands.join(", ")}: select the way to split first (mode 1), then split`, "err"); return; }
  const nw = newOsmId();
  addOp({ op: "way.split", way: cands[0], node: nid, new_way: nw });
  await commit(`way ${cands[0]} split at node ${nid} → new way ${nw} (tags copied; edit them in select mode)`);
}

// ---------------------------------------------------------------------------- twin clicks: road end, junction outline
function onWayClick(f, l, ev) {
  if (MODE === "select" || MODE === null) { selectWay(f); return; }
  if (MODE === "vertices") { selectWay(f); startVertexEdit(f.properties.osm_id); return; }
  if (MODE === "split") { selectWay(f); hint(`way ${f.properties.osm_id} selected: now click one of its interior nodes`, ""); return; }
}
function onTwinClick(f, l, ev) {
  const p = f.properties;
  if (MODE === "roadend" && p.layer === "roads") { startRoadEnd(f, ev.latlng); return true; }
  if (MODE === "junction" && p.layer === "junctions") { startJunctionEdit(f, l); return true; }
  return false;
}
function parseJ(s) { try { return typeof s === "string" ? JSON.parse(s) : s; } catch (e) { return null; } }
function startRoadEnd(f, clickLL) {
  stopEditing();
  const p = f.properties, coords = f.geometry.coordinates.map(c => L.latLng(c[1], c[0]));
  const dStart = mDist(clickLL, coords[0]), dEnd = mDist(clickLL, coords[coords.length - 1]);
  const end = dStart < dEnd ? "start" : "end";
  const link = parseJ(end === "start" ? p.predecessor : p.successor);
  if (!link || link.element !== "junction") { hint(`road ${p.id}: its ${end} does not touch a junction (${link ? link.element : "free end"})`, "err"); return; }
  const j = TWIN.features.find(x => x.properties.layer === "junctions" && x.properties.id === link.id);
  const jnodes = j ? (parseJ(j.properties.osm_node_ids) || []) : [];
  const ways = parseJ(p.osm_way_ids) || [];
  let way = ways.find(w => { const wf = WAY_BY_ID.get(w); return wf && (wf.properties.nodes || []).some(n => jnodes.includes(n)); });
  if (way === undefined) way = ways[0];
  const node = jnodes.length ? jnodes[0] : null;
  if (way === undefined || node === null) { hint(`road ${p.id}: cannot map its ${end} to an OSM (way, node) pair`, "err"); return; }
  const tags = parseJ(p.tags) || {}, base = +(tags[`end_shift_${end}`] || 0);
  const endLL = end === "start" ? coords[0] : coords[coords.length - 1], prevLL = end === "start" ? coords[1] : coords[coords.length - 2];
  const e0 = FRAME.toLocal(endLL.lat, endLL.lng), p0 = FRAME.toLocal(prevLL.lat, prevLL.lng);
  const len = Math.hypot(e0[0] - p0[0], e0[1] - p0[1]) || 1, tx = (e0[0] - p0[0]) / len, ty = (e0[1] - p0[1]) / len;   // unit vector towards the junction
  const half = 0.5 * (parseJ(p.lanes) || []).filter(l => l.type !== "sidewalk").reduce((s, l) => s + (l.width || 0), 0) || 4;
  const stopLine = d => { const cx = e0[0] + tx * d, cy = e0[1] + ty * d; const a = FRAME.toWGS(cx - ty * half, cy + tx * half), b = FRAME.toWGS(cx + ty * half, cy - tx * half); return [a, b]; };
  endLine = L.polyline(stopLine(0), { color: "#ffb000", weight: 4, opacity: 0.95, interactive: false }).addTo(map);
  endHandle = L.marker(endLL, { draggable: true, icon: L.divIcon({ className: "endhandle" }) }).addTo(map);
  let delta = 0;
  const upd = ll => { const q = FRAME.toLocal(ll.lat, ll.lng); delta = (q[0] - e0[0]) * tx + (q[1] - e0[1]) * ty; delta = Math.max(-40, Math.min(40, delta));
    endHandle.setLatLng(FRAME.toWGS(e0[0] + tx * delta, e0[1] + ty * delta)); endLine.setLatLngs(stopLine(delta));
    hint(`road ${p.id} ${end} at junction ${link.id} (way ${way}, node ${node}): ${delta >= 0 ? "+" : ""}${delta.toFixed(2)} m ${delta >= 0 ? "into" : "back from"} the junction (total ${(base + delta).toFixed(2)} m). Release, then apply.`, ""); };
  endHandle.on("drag", ev => upd(ev.target.getLatLng()));
  editing = { kind: "roadend", lyr: null };
  $("editor").innerHTML = `<div class="h2">road end · ${p.id} (${esc(p.name || p.highway)}) → junction ${link.id}</div>
    <div class="muted" style="font-size:11px">Drag the orange handle along the road. Negative pulls the stop line, the signal and the junction mouth back; positive pushes them in. Current correction: ${base.toFixed(2)} m.</div>
    <div class="actions"><button id="b-apply-end" class="primary">apply</button><button id="b-reset-end">remove correction</button><button id="b-cancel">cancel</button></div>`;
  $("b-apply-end").onclick = async () => { const total = +(base + delta).toFixed(2); OPS = OPS.filter(o => !(o.op === "road.end" && o.way === way && o.node === node)); if (Math.abs(total) > 0.01) addOp({ op: "road.end", way, node, shift_m: total, note: `${p.id} ${end} @ ${link.id}` }); stopEditing(); await commit(`road ${p.id}: end shifted ${total} m — rebuild to see it`); };
  $("b-reset-end").onclick = async () => { OPS = OPS.filter(o => !(o.op === "road.end" && o.way === way && o.node === node)); stopEditing(); await commit(`road ${p.id}: end correction removed`); };
  $("b-cancel").onclick = () => { stopEditing(); hint("cancelled", ""); };
  hint(`road ${p.id} ${end} at junction ${link.id}: drag the handle`, "");
}
function startJunctionEdit(f, l) {
  if (!GEOMAN_OK) { hint("Leaflet-Geoman did not load — polygon editing unavailable", "err"); return; }
  stopEditing();
  const p = f.properties, nodes = parseJ(p.osm_node_ids) || [];
  const lyr = L.polygon(f.geometry.coordinates[0].map(c => [c[1], c[0]]), { color: "#ffe14d", weight: 3, fillOpacity: 0.1 }).addTo(map);
  editing = { kind: "junction", lyr, nodes, jid: p.id };
  lyr.pm.enable({ allowSelfIntersection: false, removeVertexOn: "contextmenu", addVertexOn: "click" });
  const existing = findOp(o => o.op === "junction.polygon" && (o.nodes || []).some(n => nodes.includes(n)));
  hint(`junction ${p.id} (nodes ${nodes.join(", ")}): drag vertices, click a segment to add, right-click to remove. Enter = done`, "");
  $("editor").innerHTML = `<div class="h2">junction outline · ${p.id}${p.polygon_source === "correction" ? '<span class="badge">corrected</span>' : ""}</div>
    <div class="muted" style="font-size:11px">The outline is kept verbatim by the surfaces stage; sidewalks and kerbs wrap around it. ${existing ? "Replaces correction " + existing.id + "." : ""}</div>
    <div class="actions"><button id="b-done" class="primary">done (Enter)</button><button id="b-cancel">cancel (Esc)</button>${existing ? '<button id="b-delop" class="warn">remove correction</button>' : ""}</div>`;
  $("b-done").onclick = finishJunctionEdit; $("b-cancel").onclick = () => { stopEditing(); hint("cancelled", ""); };
  if (existing) $("b-delop").onclick = async () => { stopEditing(); removeOp(existing); await commit(`${existing.id} removed — rebuild to see the derived outline again`); };
}
async function finishJunctionEdit() {
  const e = editing; if (!e || e.kind !== "junction") return;
  const ring = e.lyr.getLatLngs()[0].map(lonlat); ring.push(ring[0]);
  OPS = OPS.filter(o => !(o.op === "junction.polygon" && (o.nodes || []).some(n => e.nodes.includes(n))));
  addOp({ op: "junction.polygon", nodes: e.nodes, polygon: ring, note: e.jid });
  stopEditing();
  await commit(`junction ${e.jid}: outline corrected — rebuild to see it`);
}

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
    case "road.end": return `road end way ${o.way} @ node ${o.node}: ${o.shift_m > 0 ? "+" : ""}${o.shift_m} m`;
    case "junction.polygon": return `junction outline (nodes ${(o.nodes || []).slice(0, 3).join(",")}${o.nodes.length > 3 ? "…" : ""})`;
    case "drivable.add": return `pave ${o.polygon.length - 1} pts`;
    case "drivable.cut": return `unpave → ${o.as || "sidewalk"} (${o.polygon.length - 1} pts)`;
  }
  return o.op;
}
function opTarget(o) {
  if (o.polygon) return L.latLngBounds(o.polygon.map(c => [c[1], c[0]]));
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
    row.querySelector("input[type=checkbox]").onchange = async ev => { o.disabled = !ev.target.checked; await commit(`${o.id} ${o.disabled ? "disabled" : "enabled"}`); };
    row.querySelector(".what").onclick = () => { const t = opTarget(o); if (!t) return; if (t instanceof L.LatLng) map.setView(t, Math.max(map.getZoom(), 20)); else map.fitBounds(t, { maxZoom: 20, padding: [40, 40] }); if (o.way !== undefined && WAY_BY_ID.has(o.way)) selectWay(WAY_BY_ID.get(o.way)); };
    row.querySelector(".note").onchange = ev => { o.note = ev.target.value; pushHistory(); pushOps(false); };
    row.querySelector("button").onclick = async () => { removeOp(o); await commit(`${o.id} deleted`); };
    host.appendChild(row);
  }
}

// ---------------------------------------------------------------------------- modes, save, rebuild, keys
function setMode(m) {
  if (MODE === m && m !== null) return;
  stopEditing();
  if (GEOMAN_OK) { try { map.pm.disableDraw(); } catch (e) {} }
  MODE = m;
  document.querySelectorAll("#modes button").forEach(b => b.classList.toggle("on", b.dataset.mode === m));
  map.getContainer().className = map.getContainer().className.replace(/\bmode-\w+/g, "") + (m ? ` mode-${m}` : "");
  const tips = { select: "select: click an OSM way to edit its lanes / tags", vertices: "vertices: click a way to edit its geometry", draw: "", split: "split: click an interior node (select the way first if the node is shared)",
    roadend: "road end: click a twin road (pink) near the junction whose stop line is wrong", junction: "junction: click a junction polygon (purple) to edit its outline", pave: "", unpave: "" };
  if (m === "draw") startDraw(); else if (m === "pave" || m === "unpave") startPolygonDraw(); else { hint(tips[m] || "pick a tool", ""); if (m !== "select" || !SEL) $("editor").innerHTML = ""; }
  if (m === "select" && SEL && SEL.kind === "way") { const f = WAY_BY_ID.get(SEL.id); if (f) renderWayEditor(f); }
}
async function save() { if (await pushOps(true)) hint(`saved ${OPS.length} op(s) to ${$("cpath").textContent}`, "ok"); }
async function rebuild() {
  const b = $("b-rebuild"); b.disabled = true; b.textContent = "rebuilding…"; hint("rebuilding the twin (twinmodel build --quick)…", "");
  $("log").textContent = "running…";
  try {
    const r = await (await fetch("/api/rebuild", { method: "POST" })).json();
    $("log").textContent = (r.summary || []).join("\n") + (r.error ? "\n" + r.error : "") + (r.ok ? "" : "\n\n" + (r.tail || ""));
    if (r.reports) $("log").textContent += "\n\ncorrections: " + JSON.stringify(r.reports);
    hint(r.ok ? `rebuilt in ${r.seconds} s (${r.when}); twin layers reloaded` : `rebuild FAILED (rc ${r.rc}) — see "last rebuild"`, r.ok ? "ok" : "err");
    setDirty(false);
    await refreshTwin();
    await refreshOsm();
  } catch (e) { hint("rebuild request failed: " + e, "err"); }
  b.disabled = false; b.textContent = "rebuild twin";
}
document.querySelectorAll("#modes button").forEach(b => b.onclick = () => setMode(b.dataset.mode));
$("b-undo").onclick = undo; $("b-redo").onclick = redo; $("b-save").onclick = save; $("b-rebuild").onclick = rebuild;
document.addEventListener("keydown", ev => {
  const tag = ev.target.tagName;
  if (ev.ctrlKey || ev.metaKey) {
    if (ev.key === "z") { ev.preventDefault(); undo(); } else if (ev.key === "y") { ev.preventDefault(); redo(); } else if (ev.key === "s") { ev.preventDefault(); save(); }
    return;
  }
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (ev.key === "Escape") { if (editing) { stopEditing(); refreshOsm(); hint("cancelled", ""); } else if (GEOMAN_OK && map.pm.globalDrawModeEnabled()) { setMode("select"); } else clearSel(); }
  if (ev.key === "Enter" && editing) { if (editing.kind === "way") finishVertexEdit(); else if (editing.kind === "junction") finishJunctionEdit(); else if (editing.kind === "poly") $("b-done") && $("b-done").click(); }
  if (ev.key >= "1" && ev.key <= "8") setMode(["select", "vertices", "draw", "split", "roadend", "junction", "pave", "unpave"][+ev.key - 1]);
});
window.addEventListener("beforeunload", ev => { if (DIRTY) { ev.preventDefault(); ev.returnValue = ""; } });
map.on("click", ev => { if (!FRAME) return; if (MODE === null || MODE === "select") { if (SEL) clearSel(); else placePopup(ev); } });

// ---------------------------------------------------------------------------- boot
async function bootEditor() {
  const meta = await loadMeta();
  buildBasemaps(meta);
  $("title").textContent = "layers · " + meta.name; $("ename").textContent = meta.name;
  $("sub").textContent = `${meta.profile || ""} · origin ${meta.origin[0].toFixed(5)}, ${meta.origin[1].toFixed(5)}`;
  setViewFromHash(meta);
  const c = await (await fetch("/api/corrections")).json();
  OPS = c.ops || []; NEXT_ID = c.next_osm_id; setDirty(c.dirty); pushHistory();
  $("cpath").textContent = c.path + (c.exists ? "" : " (new)");
  if (c.last_rebuild) $("log").textContent = (c.last_rebuild.summary || []).join("\n");
  document.querySelector("#help").innerHTML = "<b>F</b> flicker twin · <b>O</b> flicker OSM · <b>D</b> detections · <b>M</b> mosaic · <b>1-8</b> tools · <b>Esc</b> cancel · <b>Enter</b> done · <b>Ctrl+Z/Y/S</b>";
  const osmRawFc = await (await fetch("/api/osm_raw.geojson")).json();
  osmRaw = geoLayer(osmRawFc, f => f.properties.layer === "osm_highway", () => ({ color: "#ff00ff", weight: 1.5, opacity: 0.8, dashArray: "2 4" }));
  addOverlay("osm-layers", "osm_raw", "OSM ways, raw extract (magenta dashed)", "#ff00ff", osmRaw, 0.8, false);
  addOverlay("osm-layers", "osm_building", `buildings`, "#d9a066", geoLayer(osmRawFc, f => f.properties.layer === "osm_building", () => ({ color: "#d9a066", weight: 1, opacity: 1, fillOpacity: 0.08 })), 0.7, false);
  await refreshTwin();
  await refreshOsm();
  renderOps();
  await mountDetectAll(meta, false);   // ML layers off by default in the editor: toggle them in the panel
  watchLowfly(meta);
  if (!GEOMAN_OK) hint("Leaflet-Geoman failed to load: tag editing, road ends and split work; vertex / polygon editing needs the CDN", "err");
  else setMode("select");
}
bindFlickerKeys();
bootEditor().catch(e => { hint("failed: " + e, "err"); console.error(e); });
"""

GEOMAN_HEAD = r"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.css">
<script src="https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.18.3/dist/leaflet-geoman.min.js"></script>"""

EDITOR_PAGE = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
               "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Twin editor</title>\n"
               + go.LEAFLET_HEAD + "\n" + GEOMAN_HEAD + "\n<style>" + go.CSS + EDITOR_CSS + "</style></head>\n<body>\n<div id=\"map\"></div>\n"
               + go.PANEL_HTML + TOOLS_HTML
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
