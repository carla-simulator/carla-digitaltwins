"""Build the *low-fly review* page: the 1 cm/px orthomosaic tile pyramid from
``tools/carla_lowfly.py`` under the same region-drawing / commenting workflow the artifact page
(``tools/region_review_page.py``) has.

    python tools/review_server.py --root out/lowfly            # then open http://localhost:8765/

Why a local page instead of an artifact: a whole map at 1 cm/px is ~1 GB of WebP, so nothing can be
embedded.  The base layer here is a *pyramid of URLs* streamed by ``tools/review_server.py``; the
page keeps a small LRU of decoded tiles, draws whatever coarser tiles are already cached while the
finer ones arrive, and never asks for a tile outside the manifest's grid.

Everything else is lifted from the artifact page: the same vector overlays out of
``extract_twin``, the same polygon / polyline / point draw tools, the same comment form and region
list, and the same document schema (model *and* CARLA coordinates on every region).  Storage is a
JSON file behind the server's ``/api/<Map>/regions`` instead of the artifact database.

The URL hash is the view -- ``#<x>,<y>,<px per metre>`` -- so a spot on the mosaic can be pasted
into a message the way a region's coordinates can.

Coordinates.  Model metres are ENU (x east, y north); CARLA/UE is ``(x, -y)``.  Tile ``(z, i, j)``
covers ``[x0 + i*leaf_m*2^z, x0 + (i+1)*leaf_m*2^z) x [y0 + j*..., ...)`` with j increasing north
and image row 0 the north edge, so a tile is drawn at ``(sx(x0 + i*t), sy(y0 + (j+1)*t))``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("lowfly_viewer")


def _load_region_review():
    """Import the sibling artifact-page generator (extract_twin, the shared CSS, the y flip)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("region_review_page", here / "region_review_page.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load_region_review()
extract_twin = rr.extract_twin
model_to_carla_xy = rr.model_to_carla_xy

CATEGORIES = ("materials", "signals", "signs", "geometry", "buildings", "vegetation", "other")
PRIORITIES = ("low", "normal", "high")


def region_defaults(doc: dict, *, map_name: str, carla_map: str) -> dict:
    """Fill a posted region out into the stored schema, deriving the CARLA coordinates.

    The server is authoritative about the flip so a client cannot store an inconsistent pair.
    """
    pts = [[round(float(p[0]), 2), round(float(p[1]), 2)] for p in (doc.get("coords_model") or [])]
    cx = round(sum(p[0] for p in pts) / len(pts), 2) if pts else 0.0
    cy = round(sum(p[1] for p in pts) / len(pts), 2) if pts else 0.0
    cat = doc.get("category") or "other"
    prio = doc.get("priority") or "normal"
    out = {
        "map": doc.get("map") or map_name,
        "carla_map": doc.get("carla_map") or carla_map,
        "kind": doc.get("kind") or "polygon",
        "coords_model": pts,
        "coords_carla": [[p[0], round(-p[1], 2)] for p in pts],
        "centroid_model": [cx, cy],
        "centroid_carla": [cx, round(-cy, 2)],
        "title": (doc.get("title") or "").strip() or "Untitled region",
        "category": cat if cat in CATEGORIES else "other",
        "priority": prio if prio in PRIORITIES else "normal",
        "comment": doc.get("comment") or "",
        "status": "done" if doc.get("status") == "done" else "open",
        "replies": list(doc.get("replies") or []),
    }
    return out


# ---------------------------------------------------------------------------------------- page

EXTRA_CSS = r"""
html{color-scheme:light dark}
img{max-width:100%}
[hidden]{display:none!important}
#idx{display:block;height:auto;overflow:auto;padding:28px 32px;max-width:820px}
#idx h1{margin:0 0 4px}
#idx ul{list-style:none;padding:0;margin:16px 0 0;display:grid;gap:8px}
#idx a{color:var(--acc);text-decoration:none;font-weight:600}
#idx li{border:1px solid var(--line);border-radius:7px;background:var(--panel);padding:10px 12px}
#idx .tag{display:block;margin-top:3px}
kbd{font:500 11px "JetBrains Mono",ui-monospace,monospace;border:1px solid var(--line);
  border-radius:4px;padding:0 4px;background:var(--panel2)}
a.link{color:var(--acc);text-decoration:none;font-weight:400;font-size:11px;letter-spacing:0;
  text-transform:none}
"""

JS = r"""
(() => {
"use strict";
const MAN = JSON.parse(document.getElementById("manifest").textContent);
const TWINEL = document.getElementById("twin");
const TWIN = TWINEL ? JSON.parse(TWINEL.textContent) : null;
const MAP = MAN.map;
const TILE_BASE = MAN.tile_base, API = MAN.api_base, L0 = MAN.l0 || null;
const LS = "twin-lowfly:" + MAP + ":";
const CATEGORIES = ["materials","signals","signs","geometry","buildings","vegetation","other"];
const PRIORITIES = ["low","normal","high"];
const TILE = MAN.tile_m || 250;              // baker material tile, model metres
const LEAF = MAN.leaf_m || 5;                // level-0 tile footprint, model metres
const CMPX = MAN.cm_per_px || 1;             // level-0 ground resolution
const LEVELS = Math.max(1, MAN.levels || 1);
const MAX_TILES = 600;                       // decoded-tile LRU; 500x500 RGBA is ~1 MB each
const COARSE = 3;                            // levels above the target drawn from cache while loading
const POLL_MS = 15000;

const POLY_LAYERS = [
  ["ground","Ground","--l-ground",null],
  ["verge","Verge","--l-verge",null],
  ["median","Median","--l-median",null],
  ["island","Island","--l-island",null],
  ["parking","Parking","--l-parking",null],
  ["drivable","Drivable","--l-drivable",null],
  ["sidewalk","Sidewalk","--l-sidewalk",null],
  ["crossing","Crossing","--l-crossing",null],
  ["buildings","Buildings","--l-building","--l-building-line"],
];
const EXTRA_LAYERS = [
  ["curbs","Curbs","--l-curb"], ["markings","Lane markings","--l-mark-white"],
  ["roads","Road centrelines","--l-road"], ["trees","Trees","--tree"],
  ["signals","Signals & signs","--sig-tl"], ["junctions","Junction labels","--jn"],
  ["grid","250 m tile grid","--grid"],
];
const DEFAULT_ON = {image:1,ground:0,verge:0,median:0,island:0,parking:0,drivable:0,sidewalk:0,
  crossing:0,buildings:0,curbs:0,markings:0,roads:0,trees:0,signals:0,junctions:0,grid:0};
const DEFAULT_VEC_OPACITY = 70;
const SIG_COLOR = {traffic_light:"--sig-tl",traffic_light_arrow:"--sig-tl",traffic_light_ped:"--sig-ped",
  stop:"--sig-stop",yield:"--sig-yield",speed_limit:"--sig-speed",priority_road:"--sig-yield",
  crosswalk:"--sig-other",traffic_sign:"--sig-other"};
const SIG_LABEL = {traffic_light:"Traffic light",traffic_light_arrow:"Arrow head",
  traffic_light_ped:"Pedestrian head",stop:"Stop",yield:"Yield",speed_limit:"Speed limit",
  priority_road:"Priority road",crosswalk:"Crosswalk",traffic_sign:"OSM traffic sign"};
const TOKENS = ["--canvas","--grid","--grid-ink","--jn","--tree","--l-mark-white","--l-mark-yellow",
  "--l-curb","--l-road","--rg","--rg-fill","--rg-done","--rg-done-fill","--rg-hi","--draft",
  "--draft-fill","--ink","--panel","--muted","--acc"]
  .concat(POLY_LAYERS.map(l => l[2])).concat(POLY_LAYERS.map(l => l[3]).filter(Boolean))
  .concat(Object.values(SIG_COLOR));

const $ = (s, r=document) => r.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const round2 = v => Math.round(v*100)/100;
const store = {
  get(k, d){ try { const v = localStorage.getItem(LS+k); return v === null ? d : JSON.parse(v); } catch(e){ return d; } },
  set(k, v){ try { localStorage.setItem(LS+k, JSON.stringify(v)); } catch(e){} },
};

// tiles per axis at each level: the baker halves the grid with ceil() until one tile spans the map
const GRID = [];
{ let gx = MAN.leaves[0], gy = MAN.leaves[1];
  for (let z=0; z<LEVELS; z++){ GRID.push([gx, gy]); gx = Math.ceil(gx/2); gy = Math.ceil(gy/2); } }

const S = {
  cx: 0, cy: 0, k: 1, z: 0, on: Object.assign({}, DEFAULT_ON, store.get("layers", {})),
  op: store.get("vecopacity", DEFAULT_VEC_OPACITY),
  mode: null, draft: [], cursor: null, regions: new Map(), hover: null, sel: null,
  pending: null, drawn: 0, want: 0,
};
let PAL = {};

/* ------------------------------------------------------------------ payload preparation */
function prepare(m){
  if (!m || m._ready) return m;
  const bbF = f => { let a=Infinity,b=Infinity,c=-Infinity,d=-Infinity;
    for (const r of f) for (let i=0;i<r.length;i+=2){ const x=r[i],y=r[i+1];
      if(x<a)a=x; if(x>c)c=x; if(y<b)b=y; if(y>d)d=y; }
    f._bb=[a,b,c,d]; };
  const bbL = r => { let a=Infinity,b=Infinity,c=-Infinity,d=-Infinity;
    for (let i=0;i<r.length;i+=2){ const x=r[i],y=r[i+1];
      if(x<a)a=x; if(x>c)c=x; if(y<b)b=y; if(y>d)d=y; }
    r._bb=[a,b,c,d]; };
  for (const [id] of POLY_LAYERS) (m.layers[id]||[]).forEach(bbF);
  (m.layers.curbs||[]).forEach(bbL); (m.layers.roads||[]).forEach(bbL);
  Object.values(m.layers.markings||{}).forEach(a => a.forEach(bbL));
  m._ready = true; return m;
}
prepare(TWIN);

/* ------------------------------------------------------------------------- canvas & view */
const cv = $("#cv"), g = cv.getContext("2d");
let DPR = 1, W = 0, H = 0, RAF = 0;

function readPalette(){
  const cs = getComputedStyle(document.documentElement);
  PAL = {}; for (const t of TOKENS) PAL[t] = cs.getPropertyValue(t).trim() || "#888";
}
function resize(){
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  const r = cv.getBoundingClientRect();
  W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
  cv.width = Math.round(W*DPR); cv.height = Math.round(H*DPR);
  draw();
}
const sx = x => x*S.k + (W/2 - S.cx*S.k);
const sy = y => (H/2 + S.cy*S.k) - y*S.k;
const wx = px => (px - W/2)/S.k + S.cx;
const wy = py => (H/2 - py)/S.k + S.cy;
function schedule(){ if (RAF) return; RAF = requestAnimationFrame(() => { RAF = 0; draw(); }); }

function dataBounds(){
  // the twin's own extent, not the mosaic's: the pyramid is padded out to whole 250 m tiles and
  // its corners are the empty void outside the map
  if (TWIN && TWIN.bounds && (TWIN.bounds[2] > TWIN.bounds[0])) return TWIN.bounds;
  if (MAN.bounds_model_data) return MAN.bounds_model_data;
  return MAN.bounds;
}
function fit(){
  const b = dataBounds(), pad = 40;
  const w = Math.max(1, b[2]-b[0]), h = Math.max(1, b[3]-b[1]);
  S.cx = (b[0]+b[2])/2; S.cy = (b[1]+b[3])/2;
  S.k = Math.min((W-2*pad)/w, (H-2*pad)/h) || 1;
}
function zoomTo(minx, miny, maxx, maxy){
  const pad = 90;
  S.cx = (minx+maxx)/2; S.cy = (miny+maxy)/2;
  const w = Math.max(6, maxx-minx), h = Math.max(6, maxy-miny);
  S.k = clampK(Math.min((W-2*pad)/w, (H-2*pad)/h));
  draw();
}
const clampK = k => Math.max(0.01, Math.min(400, k));

// #x,y,k deep link: the URL is the view, so a spot on the mosaic can be pasted to someone else
function viewFromHash(){
  const m = /^#(-?[\d.]+),(-?[\d.]+),([\d.]+)$/.exec(location.hash || "");
  if (!m) return false;
  S.cx = parseFloat(m[1]); S.cy = parseFloat(m[2]); S.k = clampK(parseFloat(m[3]));
  return isFinite(S.cx) && isFinite(S.cy) && isFinite(S.k);
}
let hashT = 0;
function pushHash(){
  clearTimeout(hashT);
  hashT = setTimeout(() => {
    const h = "#" + S.cx.toFixed(1) + "," + S.cy.toFixed(1) + "," + S.k.toFixed(2);
    if (h !== location.hash) history.replaceState(null, "", h);
  }, 400);
}
window.addEventListener("hashchange", () => { if (viewFromHash()) draw(); });

/* ------------------------------------------------------------------------ tile pyramid */
const TC = new Map();                  // "z/i/j" -> {img, bad, used}
let FRAME = 0, L0IMG = null;

// the level whose tile pixels are closest to screen pixels: px/m at level z is (100/cm_per_px)/2^z
function levelFor(k){
  const z = Math.round(Math.log2((100/CMPX) / Math.max(k, 1e-6)));
  return Math.max(0, Math.min(LEVELS-1, z));
}
const tileM = z => LEAF * Math.pow(2, z);

function tileRange(z){
  const t = tileM(z), gr = GRID[z];
  return [Math.max(0, Math.floor((wx(0) - MAN.x0)/t)),
          Math.min(gr[0]-1, Math.floor((wx(W) - MAN.x0)/t)),
          Math.max(0, Math.floor((wy(H) - MAN.y0)/t)),
          Math.min(gr[1]-1, Math.floor((wy(0) - MAN.y0)/t)), t];
}
function tileImage(z, i, j, fetchIt){
  const id = z + "/" + i + "/" + j;
  let e = TC.get(id);
  if (!e){
    if (!fetchIt) return null;
    const img = new Image();
    e = {img: img, bad: 0};
    TC.set(id, e);
    img.decoding = "async";
    img.addEventListener("load", schedule);
    img.addEventListener("error", () => { e.bad = 1; });   // outside the map: nothing to draw
    img.src = TILE_BASE + "z" + z + "/" + i + "_" + j + ".webp";
  }
  e.used = FRAME;
  return e.bad ? null : e.img;
}
function evictTiles(){
  if (TC.size <= MAX_TILES) return;
  const rows = [...TC.entries()].sort((a,b) => (a[1].used||0) - (b[1].used||0));
  for (const [id, e] of rows){
    if (TC.size <= MAX_TILES) break;
    if (e.used === FRAME) continue;
    e.img.removeAttribute("src");      // drop the decoded bitmap (src="" would refetch the page)
    TC.delete(id);
  }
}
function drawL0(){
  if (!L0) return 0;
  if (!L0IMG){ L0IMG = new Image(); L0IMG.addEventListener("load", schedule); L0IMG.src = L0.url; }
  if (!L0IMG.complete || !L0IMG.naturalWidth) return 0;
  const b = L0.bounds;
  g.drawImage(L0IMG, sx(b[0]), sy(b[3]), (b[2]-b[0])*S.k, (b[3]-b[1])*S.k);
  return 1;
}
function drawTiles(){
  FRAME++;
  const z = S.z = levelFor(S.k);
  g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
  drawL0();
  let drawn = 0, want = 0;
  // coarse first (cache only, no fetch), then the target level, so a finer tile always wins
  for (let zz = Math.min(LEVELS-1, z+COARSE); zz >= z; zz--){
    const [i0,i1,j0,j1,t] = tileRange(zz);
    if (i1 < i0 || j1 < j0) continue;
    if ((i1-i0+1)*(j1-j0+1) > 4000) continue;
    const side = t*S.k + 0.6;          // the 0.6 px overlap hides the seams between neighbours
    for (let j=j0;j<=j1;j++) for (let i=i0;i<=i1;i++){
      if (zz === z) want++;
      const im = tileImage(zz, i, j, zz === z);
      if (!im || !im.complete || !im.naturalWidth) continue;
      g.drawImage(im, sx(MAN.x0 + i*t), sy(MAN.y0 + (j+1)*t), side, side);
      if (zz === z) drawn++;
    }
  }
  S.drawn = drawn; S.want = want;
  evictTiles();
}

/* ----------------------------------------------------------------------------- rendering */
function polyPath(feats, a, bx, by, vb){
  g.beginPath();
  for (const f of feats){
    const bb = f._bb;
    if (bb[2] < vb[0] || bb[0] > vb[2] || bb[3] < vb[1] || bb[1] > vb[3]) continue;
    for (const r of f){
      if (r.length < 6) continue;
      g.moveTo(r[0]*a+bx, by-r[1]*a);
      for (let i=2;i<r.length;i+=2) g.lineTo(r[i]*a+bx, by-r[i+1]*a);
      g.closePath();
    }
  }
}
function linePath(lines, a, bx, by, vb){
  g.beginPath();
  for (const r of lines){
    const bb = r._bb;
    if (bb && (bb[2] < vb[0] || bb[0] > vb[2] || bb[3] < vb[1] || bb[1] > vb[3])) continue;
    g.moveTo(r[0]*a+bx, by-r[1]*a);
    for (let i=2;i<r.length;i+=2) g.lineTo(r[i]*a+bx, by-r[i+1]*a);
  }
}
function glyph(x, y, kind, r){
  g.beginPath();
  if (kind === "stop"){
    for (let i=0;i<8;i++){ const t = Math.PI/8 + i*Math.PI/4;
      const px = x + r*Math.cos(t), py = y + r*Math.sin(t);
      i ? g.lineTo(px,py) : g.moveTo(px,py); }
    g.closePath();
  } else if (kind === "yield" || kind === "priority_road"){
    g.moveTo(x, y+r); g.lineTo(x-r, y-r*0.75); g.lineTo(x+r, y-r*0.75); g.closePath();
  } else if (kind === "traffic_light" || kind === "traffic_light_arrow"){
    g.rect(x-r*0.62, y-r, r*1.24, r*2);
  } else if (kind === "traffic_light_ped"){
    g.rect(x-r*0.6, y-r*0.85, r*1.2, r*1.7);
  } else if (kind === "crosswalk"){
    g.moveTo(x-r*0.8,y-r*0.8); g.lineTo(x+r*0.8,y+r*0.8);
    g.moveTo(x+r*0.8,y-r*0.8); g.lineTo(x-r*0.8,y+r*0.8);
  } else {
    g.arc(x, y, r, 0, 6.2832);
  }
}
function draw(){
  if (!W) return;
  g.setTransform(DPR,0,0,DPR,0,0);
  g.globalAlpha = 1;
  g.fillStyle = PAL["--canvas"]; g.fillRect(0,0,W,H);
  if (S.on.image) drawTiles(); else { S.drawn = 0; S.want = 0; S.z = levelFor(S.k); }
  if (TWIN) drawVectors();
  g.globalAlpha = 1;
  drawRegions();
  drawDraft();
  updateHud();
}
function drawVectors(){
  const m = TWIN;
  const a = m.scale*S.k, bx = W/2 - S.cx*S.k, by = H/2 + S.cy*S.k;
  const vb = [(0-bx)/a, (by-H)/a, (W-bx)/a, by/a];
  g.lineJoin = "round"; g.lineCap = "round";
  g.globalAlpha = Math.max(0.05, Math.min(1, S.op/100));
  for (const [id, , fillTok, strokeTok] of POLY_LAYERS){
    if (!S.on[id]) continue;
    const feats = m.layers[id]; if (!feats || !feats.length) continue;
    polyPath(feats, a, bx, by, vb);
    g.fillStyle = PAL[fillTok]; g.fill("evenodd");
    if (strokeTok && S.k > 0.35){ g.strokeStyle = PAL[strokeTok]; g.lineWidth = 1; g.stroke(); }
  }
  if (S.on.roads && m.layers.roads){
    linePath(m.layers.roads, a, bx, by, vb);
    g.strokeStyle = PAL["--l-road"]; g.lineWidth = 1; g.setLineDash([6,4]); g.stroke(); g.setLineDash([]);
  }
  if (S.on.curbs && m.layers.curbs){
    linePath(m.layers.curbs, a, bx, by, vb);
    g.strokeStyle = PAL["--l-curb"]; g.lineWidth = Math.max(0.7, Math.min(2.4, 0.16*S.k)); g.stroke();
  }
  if (S.on.markings && m.layers.markings){
    const mk = m.layers.markings, lw = Math.max(0.7, Math.min(3, 0.14*S.k));
    for (const [key, tok, dash] of [["sw","--l-mark-white",0],["sy","--l-mark-yellow",0],
                                   ["bw","--l-mark-white",1],["by","--l-mark-yellow",1]]){
      const arr = mk[key]; if (!arr || !arr.length) continue;
      linePath(arr, a, bx, by, vb);
      g.strokeStyle = PAL[tok]; g.lineWidth = lw;
      g.setLineDash(dash ? [Math.max(4,3*S.k), Math.max(3,2*S.k)] : []);
      g.stroke();
    }
    g.setLineDash([]);
  }
  if (S.on.grid) drawGrid(a, bx, by);
  if (S.on.trees && m.layers.trees){
    const t = m.layers.trees, r = Math.max(1.6, Math.min(4, 0.5*S.k));
    g.fillStyle = PAL["--tree"]; g.beginPath();
    for (let i=0;i<t.length;i+=2){
      const px = t[i]*a+bx, py = by - t[i+1]*a;
      if (px < -8 || px > W+8 || py < -8 || py > H+8) continue;
      g.moveTo(px+r, py); g.arc(px, py, r, 0, 6.2832);
    }
    g.fill();
  }
  if (S.on.signals && m.layers.signals){
    const r = Math.max(2.6, Math.min(6.5, 0.9*S.k));
    for (const s of m.layers.signals){
      const px = s.x*a+bx, py = by - s.y*a;
      if (px < -12 || px > W+12 || py < -12 || py > H+12) continue;
      g.fillStyle = PAL[SIG_COLOR[s.k] || "--sig-other"];
      g.strokeStyle = PAL[SIG_COLOR[s.k] || "--sig-other"];
      g.lineWidth = 1.4;
      glyph(px, py, s.k, r);
      if (s.k === "crosswalk") g.stroke(); else g.fill();
    }
  }
  if (S.on.junctions && S.k > 0.25){
    g.fillStyle = PAL["--jn"]; g.font = '500 11px ui-monospace,monospace';
    g.textAlign = "center"; g.textBaseline = "middle";
    for (const j of m.junctions){
      const px = j.x*a+bx, py = by - j.y*a;
      if (px < 0 || px > W || py < 0 || py > H) continue;
      g.fillText(j.id, px, py);
    }
  }
  g.globalAlpha = 1;
}
function drawGrid(a, bx, by){
  const t = TILE / (TWIN ? TWIN.scale : 1);
  const x0 = Math.floor((0-bx)/a/t), x1 = Math.ceil((W-bx)/a/t);
  const y0 = Math.floor((by-H)/a/t), y1 = Math.ceil(by/a/t);
  if ((x1-x0)*(y1-y0) > 4000) return;
  g.strokeStyle = PAL["--grid"]; g.lineWidth = 1; g.setLineDash([2,4]); g.beginPath();
  for (let i=x0;i<=x1;i++){ const px = i*t*a+bx; g.moveTo(px,0); g.lineTo(px,H); }
  for (let j=y0;j<=y1;j++){ const py = by - j*t*a; g.moveTo(0,py); g.lineTo(W,py); }
  g.stroke(); g.setLineDash([]);
  if (t*a > 70){
    g.fillStyle = PAL["--grid-ink"]; g.font = '400 10px ui-monospace,monospace';
    g.textAlign = "left"; g.textBaseline = "top";
    for (let i=x0;i<x1;i++) for (let j=y0;j<y1;j++){
      const px = i*t*a+bx, py = by - (j+1)*t*a;
      if (px > W || py > H || px < -80 || py < -30) continue;
      g.fillText(i + "," + j, px+4, py+4);
    }
  }
}
function drawRegions(){
  for (const r of S.regions.values()){
    const pts = r.coords_model || []; if (!pts.length) continue;
    const hi = (S.hover === r.id || S.sel === r.id), done = r.status === "done";
    g.lineWidth = hi ? 3 : 2;
    g.strokeStyle = hi ? PAL["--rg-hi"] : PAL[done ? "--rg-done" : "--rg"];
    g.fillStyle = PAL[done ? "--rg-done-fill" : "--rg-fill"];
    if (r.kind === "point"){
      const px = sx(pts[0][0]), py = sy(pts[0][1]);
      g.beginPath(); g.arc(px, py, hi ? 9 : 6.5, 0, 6.2832); g.fill(); g.stroke();
      g.beginPath(); g.arc(px, py, 2, 0, 6.2832); g.fill();
      continue;
    }
    g.beginPath();
    g.moveTo(sx(pts[0][0]), sy(pts[0][1]));
    for (let i=1;i<pts.length;i++) g.lineTo(sx(pts[i][0]), sy(pts[i][1]));
    if (r.kind === "polygon"){ g.closePath(); g.fill(); }
    g.stroke();
  }
}
function drawDraft(){
  if (!S.draft.length) return;
  const pts = S.draft.slice();
  if (S.cursor && S.mode !== "point") pts.push(S.cursor);
  g.strokeStyle = PAL["--draft"]; g.fillStyle = PAL["--draft-fill"]; g.lineWidth = 2;
  g.setLineDash([6,4]);
  g.beginPath(); g.moveTo(sx(pts[0][0]), sy(pts[0][1]));
  for (let i=1;i<pts.length;i++) g.lineTo(sx(pts[i][0]), sy(pts[i][1]));
  if (S.mode === "polygon" && pts.length > 2){ g.closePath(); g.fill(); }
  g.stroke(); g.setLineDash([]);
  g.fillStyle = PAL["--draft"];
  for (const p of S.draft){ g.beginPath(); g.rect(sx(p[0])-3, sy(p[1])-3, 6, 6); g.fill(); }
}
function updateHud(){
  const want = 130;
  let best = 0.1;
  for (const p of [0.1,0.2,0.5,1,2,5,10,20,50,100,200,500,1000,2000,5000]) if (p*S.k <= want) best = p;
  $("#scalebar .bar").style.width = (best*S.k).toFixed(1) + "px";
  $("#scalebar .lab").textContent = best >= 1000 ? (best/1000) + " km"
    : (best < 1 ? (best*100) + " cm" : best + " m");
  $("#ro-level").textContent = !S.on.image ? "off"
    : "z" + S.z + " · " + fmtCm(CMPX*Math.pow(2, S.z)) + "/px · " + S.drawn + "/" + S.want;
  $("#ro-zoom").textContent = S.k.toFixed(2) + " px/m";
  pushHash();
}
function fmtCm(cm){ return cm >= 100 ? (cm/100).toFixed(cm % 100 ? 1 : 0) + " m" : cm.toFixed(cm < 10 ? 0 : 0) + " cm"; }

/* ----------------------------------------------------------------------------- interaction */
let dragging = false, dragMoved = 0, lastX = 0, lastY = 0;
cv.addEventListener("pointerdown", e => {
  cv.setPointerCapture(e.pointerId);
  dragging = true; dragMoved = 0; lastX = e.clientX; lastY = e.clientY;
});
cv.addEventListener("pointermove", e => {
  const r = cv.getBoundingClientRect(), px = e.clientX-r.left, py = e.clientY-r.top;
  if (dragging){
    dragMoved += Math.abs(e.clientX-lastX) + Math.abs(e.clientY-lastY);
    S.cx -= (e.clientX-lastX)/S.k; S.cy += (e.clientY-lastY)/S.k;
    lastX = e.clientX; lastY = e.clientY; schedule();
  } else if (S.mode && S.draft.length){
    S.cursor = [wx(px), wy(py)]; schedule();
  }
  readout(px, py); tooltip(px, py);
});
cv.addEventListener("pointerup", e => {
  dragging = false;
  const r = cv.getBoundingClientRect();
  if (dragMoved < 5) onClick(e.clientX-r.left, e.clientY-r.top);
});
cv.addEventListener("pointerleave", () => { $("#tip").hidden = true; });
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), px = e.clientX-r.left, py = e.clientY-r.top;
  const bx = wx(px), by = wy(py);
  const f = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0016));
  S.k = clampK(S.k*f);
  S.cx = bx - (px - W/2)/S.k; S.cy = by + (py - H/2)/S.k;   // zoom about the cursor
  draw(); readout(px, py);
}, {passive:false});
cv.addEventListener("dblclick", e => { e.preventDefault(); if (S.mode) finish(); });

function onClick(px, py){
  if (S.mode){
    const p = [round2(wx(px)), round2(wy(py))];
    const n = S.draft.length;
    if (n && Math.hypot(sx(S.draft[n-1][0])-px, sy(S.draft[n-1][1])-py) < 6) return; // dblclick echo
    S.draft.push(p);
    if (S.mode === "point"){ finish(); return; }
    draw();
    return;
  }
  const hit = hitRegion(px, py);
  select(hit ? hit.id : null);
}
function hitRegion(px, py){
  const x = wx(px), y = wy(py), tol = 8/S.k;
  let best = null, bestD = Infinity;
  for (const r of S.regions.values()){
    const pts = r.coords_model || []; if (!pts.length) continue;
    if (r.kind === "point"){
      const d = Math.hypot(pts[0][0]-x, pts[0][1]-y);
      if (d < Math.max(tol, 0.5) && d < bestD){ best = r; bestD = d; }
      continue;
    }
    let d = Infinity;
    for (let i=0;i+1<pts.length;i++) d = Math.min(d, segDist(x, y, pts[i], pts[i+1]));
    if (r.kind === "polygon"){
      d = Math.min(d, segDist(x, y, pts[pts.length-1], pts[0]));
      if (inside(x, y, pts)) d = Math.min(d, tol*0.5);
    }
    if (d < tol && d < bestD){ best = r; bestD = d; }
  }
  return best;
}
function segDist(x, y, a, b){
  const dx = b[0]-a[0], dy = b[1]-a[1], L = dx*dx+dy*dy;
  let t = L ? ((x-a[0])*dx + (y-a[1])*dy)/L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(x-(a[0]+t*dx), y-(a[1]+t*dy));
}
function inside(x, y, pts){
  let c = false;
  for (let i=0, j=pts.length-1; i<pts.length; j=i++){
    const xi=pts[i][0], yi=pts[i][1], xj=pts[j][0], yj=pts[j][1];
    if (((yi>y) !== (yj>y)) && (x < (xj-xi)*(y-yi)/(yj-yi)+xi)) c = !c;
  }
  return c;
}
function readout(px, py){
  const x = wx(px), y = wy(py);
  $("#ro-model").textContent = x.toFixed(2) + ", " + y.toFixed(2);
  $("#ro-carla").textContent = x.toFixed(2) + ", " + (-y).toFixed(2);
  $("#ro-tile").textContent = Math.floor((x-MAN.x0)/TILE) + ", " + Math.floor((y-MAN.y0)/TILE);
}
function tooltip(px, py){
  const tip = $("#tip");
  if (!S.on.signals || !TWIN){ tip.hidden = true; return; }
  const a = TWIN.scale*S.k, bx = W/2 - S.cx*S.k, by = H/2 + S.cy*S.k;
  let best = null, bd = 11;
  for (const s of TWIN.layers.signals){
    const d = Math.hypot(s.x*a+bx-px, by-s.y*a-py);
    if (d < bd){ bd = d; best = s; }
  }
  if (!best){ tip.hidden = true; return; }
  const bits = [esc(SIG_LABEL[best.k] || best.k)];
  if (best.id) bits.push('<span class="mono">' + esc(best.id) + "</span>");
  if (best.t) bits.push('<span class="mono">type ' + esc(best.t) + (best.s ? "/" + esc(best.s) : "") + "</span>");
  if (best.r) bits.push('<span class="mono">road ' + esc(best.r) + "</span>");
  if (best.c) bits.push('<span class="mono">ctl ' + esc(best.c) + "</span>");
  tip.innerHTML = bits.join(" &middot; ");
  tip.hidden = false;
  tip.style.left = Math.min(W-260, px+14) + "px";
  tip.style.top = Math.max(4, py-34) + "px";
}

/* --------------------------------------------------------------------------- draw tools */
function setMode(mode){
  S.mode = mode; S.draft = []; S.cursor = null;
  document.querySelectorAll("#tools button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  cv.classList.toggle("draw", !!mode);
  $("#drawhint").hidden = !mode;
  $("#drawhint .t").textContent = mode === "point"
    ? "Click the spot to drop a point."
    : "Click to add vertices · double-click or Enter to finish · Backspace undo · Esc cancel";
  draw();
}
function finish(){
  const need = S.mode === "polygon" ? 3 : (S.mode === "polyline" ? 2 : 1);
  if (S.draft.length < need) return;
  S.pending = {kind: S.mode, coords: S.draft.slice()};
  setMode(null);
  openForm();
}
document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target||{}).tagName || "");
  if (e.key === "Escape"){
    if (!$("#form").hidden){ closeForm(); return; }
    if (S.mode){ setMode(null); return; }
  }
  if (typing) return;
  if (e.key === "Enter" && S.mode){ e.preventDefault(); finish(); }
  if (e.key === "Backspace" && S.mode){ e.preventDefault(); S.draft.pop(); draw(); }
  if (!S.mode && (e.key === "g" || e.key === "l" || e.key === "p")){
    setMode(e.key === "g" ? "polygon" : e.key === "l" ? "polyline" : "point");
  }
});

/* ---------------------------------------------------------------------------- storage */
async function api(method, path, body){
  const opt = {method: method, headers: {}};
  if (body !== undefined){ opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(API + path, opt);
  if (!res.ok) throw new Error(method + " " + path + " -> " + res.status);
  return res.status === 204 ? null : res.json();
}
async function loadRegions(quiet){
  try {
    const list = await api("GET", "");
    S.regions = new Map((list || []).map(r => [r.id, r]));
    setStatus(S.regions.size + " region" + (S.regions.size === 1 ? "" : "s"), "ok");
    renderList(); draw();
  } catch(err){
    if (!quiet) setStatus("store unreachable", "bad");
  }
}
async function patch(r, fields){
  try {
    const next = await api("PUT", "/" + encodeURIComponent(r.id), fields);
    S.regions.set(next.id, next); renderList(); draw();
  } catch(err){ setStatus("update failed: " + err.message, "bad"); }
}

/* -------------------------------------------------------------------------------- form */
function openForm(){
  const f = $("#form");
  f.hidden = false;
  $("#f-title").value = ""; $("#f-comment").value = "";
  $("#f-cat").value = "geometry"; $("#f-prio").value = "normal";
  $("#f-kind").textContent = S.pending.kind + " · " + S.pending.coords.length + " point" +
    (S.pending.coords.length === 1 ? "" : "s");
  $("#f-title").focus();
}
function closeForm(){ $("#form").hidden = true; S.pending = null; draw(); }
$("#f-save").addEventListener("click", async () => {
  if (!S.pending) return;
  $("#f-save").disabled = true;
  try {
    const doc = await api("POST", "", {
      map: MAP, carla_map: MAN.carla_map || MAP, kind: S.pending.kind,
      coords_model: S.pending.coords,
      title: $("#f-title").value.trim(), category: $("#f-cat").value,
      priority: $("#f-prio").value, comment: $("#f-comment").value.trim(),
    });
    S.regions.set(doc.id, doc); S.sel = doc.id;
    closeForm(); renderList(); draw();
    setStatus("saved " + doc.id, "ok");
  } catch(err){ setStatus("save failed: " + err.message, "bad"); }
  $("#f-save").disabled = false;
});
$("#f-cancel").addEventListener("click", closeForm);

/* ------------------------------------------------------------------------- regions list */
function visible(){
  return Array.from(S.regions.values())
    .sort((a, b) => (a.status === b.status
      ? String(b.created_at||"").localeCompare(String(a.created_at||""))
      : (a.status === "done" ? 1 : -1)));
}
function renderList(){
  const list = $("#regions"), rows = visible();
  $("#rcount").textContent = rows.length
    ? rows.filter(r => r.status !== "done").length + " open / " + rows.length : "0";
  if (!rows.length){
    list.innerHTML = '<p class="empty">Nothing yet. Pick a draw tool above, outline something on ' +
      "the mosaic and describe it.</p>";
    return;
  }
  list.innerHTML = rows.map(r => {
    const c = r.centroid_model || [0,0];
    const reps = (r.replies || []).map(p =>
      '<div class="rep ' + (p.author === "claude" ? "claude" : "") + '"><b>' +
      esc(p.author === "claude" ? "Claude" : "you") + "</b>" +
      '<time>' + esc(String(p.at || "").slice(0,16).replace("T"," ")) + "</time><br>" +
      esc(p.text) + "</div>").join("");
    return '<article class="r' + (r.status === "done" ? " done" : "") + (S.sel === r.id ? " sel" : "") +
      '" data-id="' + esc(r.id) + '" tabindex="0">' +
      '<div class="top"><span class="ttl">' + esc(r.title) + '</span>' +
      '<span class="kd">' + esc(r.kind) + "</span></div>" +
      '<div class="meta"><span class="chip">' + esc(r.category) + "</span>" +
      '<span class="chip p-' + esc(r.priority) + '">' + esc(r.priority) + "</span>" +
      (r.status === "done" ? '<span class="chip st">done</span>' : "") + "</div>" +
      (r.comment ? '<div class="cm">' + esc(r.comment) + "</div>" : "") +
      '<div class="xy mono">model ' + c[0].toFixed(1) + ", " + c[1].toFixed(1) +
      " · carla " + c[0].toFixed(1) + ", " + (-c[1]).toFixed(1) + "</div>" +
      (reps ? '<div class="replies">' + reps + "</div>" : "") +
      '<div class="acts">' +
      '<button class="link" data-act="zoom">fly to</button>' +
      '<button class="link" data-act="status">' + (r.status === "done" ? "reopen" : "mark done") + "</button>" +
      '<button class="link" data-act="reply">reply</button>' +
      '<button class="link" data-act="del">delete</button>' +
      "</div></article>";
  }).join("");
}
$("#regions").addEventListener("click", async e => {
  const art = e.target.closest(".r"); if (!art) return;
  const r = S.regions.get(art.dataset.id); if (!r) return;
  const act = (e.target.dataset || {}).act;
  if (act === "zoom" || !act){ select(r.id); zoomRegion(r); return; }
  if (act === "status") await patch(r, {status: r.status === "done" ? "open" : "done"});
  if (act === "del"){
    if (!confirm("Delete “" + r.title + "”? This cannot be undone.")) return;
    try { await api("DELETE", "/" + encodeURIComponent(r.id)); S.regions.delete(r.id); renderList(); draw(); }
    catch(err){ setStatus("delete failed: " + err.message, "bad"); }
  }
  if (act === "reply"){
    const text = prompt("Add a follow-up comment");
    if (text && text.trim()) await patch(r, {replies: (r.replies||[]).concat(
      [{author:"user", text: text.trim(), at: new Date().toISOString()}])});
  }
});
$("#regions").addEventListener("mouseover", e => {
  const art = e.target.closest(".r"); const id = art ? art.dataset.id : null;
  if (id !== S.hover){ S.hover = id; draw(); }
});
$("#regions").addEventListener("mouseleave", () => { if (S.hover){ S.hover = null; draw(); } });
function select(id){ S.sel = id; renderList(); draw(); }
function zoomRegion(r){
  const pts = r.coords_model || []; if (!pts.length) return;
  let a=Infinity,b=Infinity,c=-Infinity,d=-Infinity;
  for (const p of pts){ a=Math.min(a,p[0]); b=Math.min(b,p[1]); c=Math.max(c,p[0]); d=Math.max(d,p[1]); }
  zoomTo(a-8, b-8, c+8, d+8);
}

/* ----------------------------------------------------------------------------- chrome */
function setStatus(text, cls){ const el = $("#status"); el.textContent = text; el.className = cls || ""; }
function buildLayerUI(){
  const base = $("#baselayer");
  base.innerHTML =
    '<label><input type="checkbox" data-layer="image"' + (S.on.image ? " checked" : "") + '>' +
    '<span class="sw" style="background:var(--acc)"></span><span>Low-fly orthomosaic</span>' +
    '<span class="n">' + fmtCm(CMPX) + "/px</span></label>";
  const box = $("#layers");
  const rows = POLY_LAYERS.map(l => [l[0], l[1], l[2]]).concat(EXTRA_LAYERS);
  box.innerHTML = rows.map(([id, label, tok]) =>
    '<label><input type="checkbox" data-layer="' + id + '"' + (S.on[id] ? " checked" : "") +
    (TWIN ? "" : " disabled") + '>' +
    '<span class="sw" style="background:var(' + tok + ')"></span>' +
    "<span>" + esc(label) + '</span><span class="n" data-n="' + id + '"></span></label>').join("");
  const flip = e => {
    const id = e.target.dataset.layer; if (!id) return;
    S.on[id] = e.target.checked ? 1 : 0; store.set("layers", S.on); draw();
  };
  box.addEventListener("change", flip);
  base.addEventListener("change", flip);
  const sl = $("#vecop");
  sl.value = S.op; $("#vecopv").textContent = S.op + "%";
  sl.addEventListener("input", () => {
    S.op = Number(sl.value); $("#vecopv").textContent = S.op + "%";
    store.set("vecopacity", S.op); draw();
  });
  $("#alloff").addEventListener("click", () => {
    for (const [id] of POLY_LAYERS.concat(EXTRA_LAYERS)) S.on[id] = 0;
    document.querySelectorAll("#layers input[data-layer]").forEach(i => { i.checked = false; });
    store.set("layers", S.on); draw();
  });
  if (TWIN) for (const el of document.querySelectorAll("[data-n]")){
    const id = el.dataset.n;
    const n = id === "junctions" ? TWIN.junctions.length
      : (TWIN.counts[id] != null ? TWIN.counts[id] : "");
    el.textContent = n === "" ? "" : n;
  }
  $("#novec").hidden = !!TWIN;
}
$("#tools").addEventListener("click", e => {
  const b = e.target.closest("button[data-mode]"); if (!b) return;
  setMode(S.mode === b.dataset.mode ? null : b.dataset.mode);
});
$("#fitbtn").addEventListener("click", () => { fit(); draw(); });
$("#zin").addEventListener("click", () => { S.k = clampK(S.k*1.4); draw(); });
$("#zout").addEventListener("click", () => { S.k = clampK(S.k/1.4); draw(); });
$("#reload").addEventListener("click", () => loadRegions(false));
$("#theme").addEventListener("click", e => {
  const b = e.target.closest("button[data-theme]"); if (!b) return;
  const v = b.dataset.theme;
  if (v === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", v);
  store.set("theme", v);
  document.querySelectorAll("#theme button").forEach(x =>
    x.setAttribute("aria-pressed", String(x.dataset.theme === v)));
  readPalette(); draw();
});

/* -------------------------------------------------------------------------------- boot */
(function applyStoredTheme(){
  const v = store.get("theme", "auto");
  if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
  document.querySelectorAll("#theme button").forEach(x =>
    x.setAttribute("aria-pressed", String(x.dataset.theme === v)));
})();
readPalette();
buildLayerUI();
$("#f-cat").innerHTML = CATEGORIES.map(c => '<option value="' + c + '">' + c + "</option>").join("");
$("#f-prio").innerHTML = PRIORITIES.map(c => '<option value="' + c + '"' +
  (c === "normal" ? " selected" : "") + ">" + c + "</option>").join("");
$("#mapmeta").innerHTML =
  Math.round(MAN.bounds[2]-MAN.bounds[0]) + " × " + Math.round(MAN.bounds[3]-MAN.bounds[1]) +
  " m padded · " + MAN.leaves[0] + "×" + MAN.leaves[1] + " leaves · " + LEVELS + " levels · " +
  '<span class="mono">' + fmtCm(CMPX) + "/px</span>" +
  (MAN.bytes ? " · " + (MAN.bytes/1e6).toFixed(0) + " MB" : "");
// read-only view of the pyramid's state, for the headless check
window.lowflyState = () => ({map: MAP, z: S.z, k: S.k, drawn: S.drawn, want: S.want,
  cached: TC.size, levels: LEVELS, regions: S.regions.size, layers: Object.assign({}, S.on)});
new ResizeObserver(resize).observe(cv);
window.addEventListener("resize", resize);
if (window.matchMedia) try {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readPalette(); draw(); });
} catch(e){}
resize();
if (!viewFromHash()) fit();
setMode(null);
draw();
loadRegions(false);
setInterval(() => loadRegions(true), POLL_MS);
})();
"""

BODY = """
<aside>
  <header>
    <h1>{{NAME}} low-fly review</h1>
    <div class="tag">Draw over the 1 cm/px orthomosaic, leave notes for Claude.</div>
    <div class="rowline"><span id="status">loading…</span>
      <span class="seg small" id="theme">
        <button type="button" data-theme="auto" aria-pressed="true">Auto</button>
        <button type="button" data-theme="light" aria-pressed="false">Light</button>
        <button type="button" data-theme="dark" aria-pressed="false">Dark</button>
      </span></div>
  </header>
  <div class="scroll">
    <section>
      <h2>Mosaic <a class="link" href="/">all maps</a></h2>
      <p class="hint" id="mapmeta"></p>
      <div class="rowline"><button type="button" class="wide" id="fitbtn">Fit map to view</button></div>
    </section>
    <section>
      <h2>Draw a region</h2>
      <div class="seg" id="tools">
        <button type="button" data-mode="polygon" aria-pressed="false">Polygon</button>
        <button type="button" data-mode="polyline" aria-pressed="false">Polyline</button>
        <button type="button" data-mode="point" aria-pressed="false">Point</button>
      </div>
      <p class="hint">Click to place vertices, double-click or <kbd>Enter</kbd> to finish,
        <kbd>Backspace</kbd> to undo one, <kbd>Esc</kbd> to cancel. Shortcuts: <kbd>g</kbd> polygon,
        <kbd>l</kbd> polyline, <kbd>p</kbd> point.</p>
    </section>
    <section>
      <h2>Base</h2>
      <div class="layers" id="baselayer"></div>
      <p class="hint">The pyramid streams from this server: the level whose tile pixels match your
        screen pixels is fetched, coarser cached tiles hold the picture until it lands. The URL
        hash is the view (<span class="mono">#x,y,px-per-m</span>) — copy it to send someone a
        spot.</p>
    </section>
    <section>
      <h2>Vector overlays <button type="button" class="link" id="alloff">all off</button></h2>
      <div class="layers" id="layers"></div>
      <p class="hint" id="novec" hidden>No twin directory for this map, so the model's own geometry
        cannot be overlaid.</p>
      <div class="rowline"><label for="vecop" class="hint" style="margin:0">Overlay opacity</label>
        <span class="mono tag" id="vecopv">70%</span></div>
      <input type="range" id="vecop" min="5" max="100" step="5" value="70"
             aria-label="Vector overlay opacity">
      <p class="hint">The overlays are the twin model's own geometry. Fade them over the mosaic to
        check what CARLA actually drew.</p>
    </section>
    <section>
      <h2>Regions <span id="rcount" class="tag"></span></h2>
      <div class="rlist" id="regions"></div>
      <div class="rowline"><button type="button" class="wide" id="reload">Reload regions</button></div>
    </section>
  </div>
  <div id="form" hidden>
    <h2>Describe this region <span class="tag mono" id="f-kind"></span></h2>
    <div><div class="fl">Title</div><input type="text" id="f-title" placeholder="e.g. kerb texture wrong on this block"></div>
    <div class="two">
      <div><div class="fl">Category</div><select id="f-cat"></select></div>
      <div><div class="fl">Priority</div><select id="f-prio"></select></div>
    </div>
    <div><div class="fl">Comment</div><textarea id="f-comment" placeholder="What is wrong, and what should it look like?"></textarea></div>
    <div class="seg">
      <button type="button" class="wide primary" id="f-save">Save region</button>
      <button type="button" class="wide" id="f-cancel">Cancel</button>
    </div>
  </div>
</aside>
<main>
  <canvas id="cv"></canvas>
  <div id="tip" hidden></div>
  <div class="overlay card" id="drawhint" hidden><span class="t"></span></div>
  <div class="overlay" id="hud">
    <div class="card" id="scalebar"><span class="lab mono">100 m</span><div class="bar"></div></div>
    <div class="card mono" id="readout">
      <div><span>model x,y</span><span id="ro-model">–</span></div>
      <div><span>carla x,y</span><span id="ro-carla">–</span></div>
      <div><span>tile 250 m</span><span id="ro-tile">–</span></div>
      <div><span>pyramid</span><span id="ro-level">–</span></div>
      <div><span>zoom</span><span id="ro-zoom">–</span></div>
    </div>
  </div>
  <div id="zoombox">
    <button type="button" id="zin" title="Zoom in" aria-label="Zoom in">+</button>
    <button type="button" id="zout" title="Zoom out" aria-label="Zoom out">−</button>
  </div>
</main>
"""


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# a 4-tile pyramid glyph, inline so the browser never asks for /favicon.ico
FAVICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
               "<rect width='16' height='16' fill='#0f5fd0'/>"
               "<path d='M2 2h5v5H2zM9 2h5v5H9zM2 9h5v5H2zM9 9h5v5H9z' fill='#fff'/></svg>")
FAVICON = ('<link rel="icon" href="data:image/svg+xml,'
           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
           "%3Crect width='16' height='16' fill='%230f5fd0'/%3E"
           "%3Cpath d='M2 2h5v5H2zM9 2h5v5H9zM2 9h5v5H2zM9 9h5v5H9z' fill='%23fff'/%3E"
           '%3C/svg%3E">')


def _json_block(el_id: str, payload: Any) -> str:
    # `</script>` cannot appear inside a script element, whatever the type
    body = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return '<script type="application/json" id="%s">%s</script>' % (el_id, body)


def build_page(name: str, manifest: dict, twin: dict | None = None, *,
               tile_base: str | None = None, api_base: str | None = None,
               l0: dict | None = None) -> str:
    """Self-contained HTML page for one map's tile pyramid.  No external requests but the tiles.

    ``manifest`` is ``out/lowfly/<Map>/manifest.json``; ``twin`` is an
    :func:`region_review_page.extract_twin` payload (or ``None`` for the mosaic alone); ``l0`` is
    an optional ``{"url", "bounds"}`` coarse mosaic drawn underneath while tiles load.
    """
    man = dict(manifest)
    man["map"] = man.get("map") or name
    man["carla_map"] = (twin or {}).get("carla_map") or man.get("carla_map") or name
    man["tile_base"] = tile_base or ("/tiles/%s/" % name)
    man["api_base"] = api_base or ("/api/%s/regions" % name)
    man.pop("l0", None)
    if l0:
        man["l0"] = l0
    blocks = [_json_block("manifest", man)]
    if twin is not None:
        blocks.append(_json_block("twin", twin))
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s low-fly review</title>" % _esc(name),
        FAVICON,
        "<style>%s%s</style>" % (rr.CSS, EXTRA_CSS),
        "</head><body>",
        BODY.replace("{{NAME}}", _esc(name)),
        *blocks,
        "<script>%s</script>" % JS,
        "</body></html>",
    ])


def build_index(maps: Sequence[dict]) -> str:
    """The root listing: one row per map that has a manifest."""
    rows = []
    for m in maps:
        man = m.get("manifest") or {}
        bits = []
        if man.get("levels"):
            bits.append("%d levels" % man["levels"])
        if man.get("leaf_tiles"):
            bits.append("%d leaves" % man["leaf_tiles"])
        if man.get("bytes"):
            bits.append("%.0f MB" % (man["bytes"] / 1e6))
        if man.get("cm_per_px"):
            bits.append("%g cm/px" % man["cm_per_px"])
        if m.get("regions") is not None:
            bits.append("%d regions" % m["regions"])
        rows.append('<li><a href="/%s/">%s</a><span class="tag mono">%s</span></li>'
                    % (_esc(m["name"]), _esc(m["name"]), _esc(" · ".join(bits) or "no tiles yet")))
    body = ("<div id=\"idx\"><h1>Twin low-fly review</h1>"
            "<p class=\"tag\">1 cm/px orthomosaics streamed from disk. Pick a map.</p>"
            "<ul>%s</ul></div>" % ("".join(rows) or '<li class="tag">No manifests under the root.</li>'))
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Twin low-fly review</title>",
        FAVICON,
        "<style>%s%s</style>" % (rr.CSS, EXTRA_CSS),
        "</head><body>", body, "</body></html>",
    ])


def main(argv: Sequence[str] | None = None) -> int:
    """Write one map's page to a file (the server builds the same page in memory)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pyramid", required=True, help="out/lowfly/<Map> (holds manifest.json)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--twin", default=None, help="twin directory; default: the manifest's own")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    pyr = Path(args.pyramid)
    man = json.loads((pyr / "manifest.json").read_text())
    name = man.get("map") or pyr.name
    tdir = Path(args.twin) if args.twin else Path(man.get("twin_dir") or "")
    twin = extract_twin(tdir, name, name) if tdir and tdir.exists() else None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_page(name, man, twin))
    log.info("wrote %s (%.2f MB)", out, out.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
