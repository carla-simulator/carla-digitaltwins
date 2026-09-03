#!/usr/bin/env python
"""Geo overlay review page: satellite imagery | raw OSM | twin model | CARLA low-fly mosaic.

    python tools/geo_overlay.py out/v10_eixample eixample            # http://127.0.0.1:8790/
    python tools/geo_overlay.py out/v8_soma sf_soma --lowfly out/lowfly/Sf_Soma --port 8791

Why: the OSM input is hand-digitised.  Way centrelines are coarse (a 4-lane avenue as one
polyline with three nodes, kerbs implied by ``lanes=`` / ``width=`` tags, junction nodes placed
by eye) and the twin inherits every kink.  This page puts the three things a reviewer needs on
ONE zoomable web-mercator map so the discrepancy is visible at the metre level:

  * a basemap: ICGC 10 cm (2020) and 25 cm (2025) orthos for Catalonia and IGN PNOA for Spain
    (WMS in EPSG:3857, the same servers ``ingest.imagery`` uses), Google satellite / hybrid,
    Esri World Imagery, OSM carto, and "pipeline input ortho": the cached GeoTIFF the build
    actually consumed (``data/ortho_<bbox>_*.tif``) tiled on the fly.  The default follows the
    bbox: Catalonia -> ICGC 10 cm, Spain -> PNOA, elsewhere -> Google;
  * the RAW OpenStreetMap elements the build consumed (from the Overpass cache in ``data/``):
    highway ways coloured by class, every way node as a dot (junction nodes and tagged nodes
    highlighted), tags in the popup;
  * the twin model (``<build_dir>/<name>.twin``) reprojected from local ENU to WGS84: surfaces
    by kind, road reference lines, junction polygons, kerbs, markings, signals, buildings;
  * ML detections from ``tools/ortho_detect.py`` (``<build_dir>/detect/detections.geojson``:
    Grounding DINO + SAM cars, crosswalks, trees, ...) as one layer per label, re-read on change;
  * optionally the CARLA 1 cm/px low-fly orthomosaic of the baked level (``out/lowfly/<Map>``)
    reprojected tile-by-tile onto web mercator, so the rendered map can be checked against the
    real world too.

Every overlay has its own opacity slider; ``F`` flickers the twin layers, ``O`` the OSM layers,
``M`` the mosaic.  ``?on=lowfly,markings&off=osm_node&base=2`` presets layers / basemap in the URL and
``#zoom/lat/lon`` (kept up to date as you pan) makes a view shareable.
Clicking the map shows lat/lon, model x/y, CARLA x/y and links that open Google Street View /
Google Earth at that exact spot.

Tiles from Google / Esri / ICGC / PNOA / OSM are fetched by the browser directly.  The Google ``mt``
tile endpoint has no API key and is a review convenience, not a redistribution channel: nothing
is cached or written to disk.  Everything else is served from this process: the page,
``/api/meta``, ``/api/twin.geojson``, ``/api/osm.geojson``, ``/lowfly/z<z>/<i>_<j>.webp`` and
``/ortho/<z>/<x>/<y>.png`` (web-mercator tiles cut from the cached input ortho with rasterio).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twinmodel.frame import LocalFrame            # noqa: E402
from twinmodel.ingest.osm import fetch_overpass   # noqa: E402

log = logging.getLogger("geo_overlay")

TWIN_LAYERS = ("surfaces", "roads", "junctions", "curbs", "markings", "signals", "buildings", "objects")
HIGHWAY_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
                   "residential", "living_street", "service", "pedestrian", "footway", "cycleway",
                   "path", "steps", "track")


# ------------------------------------------------------------------------------------ data

def _flatten_props(props: dict[str, Any]) -> dict[str, Any]:
    """GeoJSON-friendly, small: lists/dicts become compact JSON strings."""
    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, (list, dict)):
            s = json.dumps(v, separators=(",", ":"))
            out[k] = s if len(s) <= 400 else s[:397] + "..."
        else:
            out[k] = v
    return out


def _reproject_coords(tf, coords):
    """Recursively map [x, y, (z)] model-metre coordinates to [lon, lat] with one pyproj Transformer."""
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        lon, lat = tf.transform(coords[0], coords[1])
        return [round(float(lon), 8), round(float(lat), 8)]
    if isinstance(coords[0][0], (int, float)):            # a ring / line: one vectorised call
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        lons, lats = tf.transform(xs, ys)
        return [[round(float(a), 8), round(float(b), 8)] for a, b in zip(lons, lats)]
    return [_reproject_coords(tf, c) for c in coords]


def twin_geojson(twin_dir: Path, frame: LocalFrame) -> dict[str, Any]:
    """All twin layers in one FeatureCollection; ``layer`` property names the source file."""
    feats: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    tf = frame._to_wgs()                                   # one Transformer for the whole model
    for layer in TWIN_LAYERS:
        p = twin_dir / f"{layer}.geojson"
        if not p.exists():
            continue
        fc = json.loads(p.read_text())
        n = 0
        for f in fc.get("features", []):
            geom = f.get("geometry")
            if not geom:
                continue
            props = _flatten_props(f.get("properties") or {})
            props["layer"] = layer
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": geom["type"],
                                       "coordinates": _reproject_coords(tf, geom["coordinates"])}})
            n += 1
        counts[layer] = n
    log.info("twin: %s", " ".join(f"{k}={v}" for k, v in counts.items()))
    return {"type": "FeatureCollection", "features": feats, "counts": counts}


def osm_geojson(raw: dict[str, Any]) -> dict[str, Any]:
    """Raw Overpass JSON -> ways (highway / building / other), way nodes with degree, tagged nodes."""
    nodes: dict[int, dict[str, Any]] = {}
    ways: list[dict[str, Any]] = []
    for el in raw.get("elements", []):
        if el.get("type") == "node":
            nodes[el["id"]] = el
        elif el.get("type") == "way":
            ways.append(el)

    degree: dict[int, int] = {}
    hw_nodes: set[int] = set()
    for w in ways:
        if "highway" not in (w.get("tags") or {}):
            continue
        for nid in w.get("nodes", []):
            degree[nid] = degree.get(nid, 0) + 1
            hw_nodes.add(nid)

    feats: list[dict[str, Any]] = []
    n_ways = n_bld = n_nodes = 0
    for w in ways:
        tags = w.get("tags") or {}
        pts = [[nodes[n]["lon"], nodes[n]["lat"]] for n in w.get("nodes", []) if n in nodes]
        if len(pts) < 2:
            continue
        props = {"osm_id": w["id"], "n_nodes": len(pts)}
        props.update({f"tag:{k}": v for k, v in tags.items()})
        if "highway" in tags:
            props["layer"] = "osm_highway"
            props["class"] = tags["highway"] if tags["highway"] in HIGHWAY_CLASSES else "other"
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "LineString", "coordinates": pts}})
            n_ways += 1
        elif "building" in tags and pts[0] == pts[-1]:
            props["layer"] = "osm_building"
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Polygon", "coordinates": [pts]}})
            n_bld += 1
        else:
            props["layer"] = "osm_other"
            gtype = "Polygon" if (pts[0] == pts[-1] and len(pts) >= 4) else "LineString"
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": gtype,
                                       "coordinates": [pts] if gtype == "Polygon" else pts}})

    for nid in sorted(hw_nodes):
        n = nodes.get(nid)
        if n is None:
            continue
        tags = n.get("tags") or {}
        props = {"osm_id": nid, "degree": degree.get(nid, 0), "layer": "osm_node",
                 "tagged": bool(tags)}
        props.update({f"tag:{k}": v for k, v in tags.items()})
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]}})
        n_nodes += 1
    log.info("osm: %d highway ways, %d buildings, %d highway nodes", n_ways, n_bld, n_nodes)
    return {"type": "FeatureCollection", "features": feats,
            "counts": {"highway": n_ways, "building": n_bld, "node": n_nodes}}


def region_for_bbox(bbox_swne: Sequence[float]) -> str:
    """Coarse region tag that picks the default basemap: icgc (Catalonia) | pnoa (Spain) | google."""
    s_, w_, n_, e_ = bbox_swne
    lat, lon = (s_ + n_) / 2, (w_ + e_) / 2
    if 40.5 <= lat <= 42.9 and 0.15 <= lon <= 3.35:
        return "icgc"
    if 35.9 <= lat <= 43.8 and -9.4 <= lon <= 4.4:
        return "pnoa"
    return "google"


def find_input_ortho(data_dir: Path, bbox_swne: Sequence[float]) -> Path | None:
    """The cached ortho GeoTIFF ``ingest.imagery`` wrote for this bbox; prefer ICGC, then PNOA, then NAIP."""
    key = "_".join(f"{float(v):.5f}" for v in bbox_swne)
    cands = sorted(data_dir.glob(f"ortho_{key}_*.tif"))
    if not cands:
        return None
    try:
        import rasterio
    except ImportError:
        return cands[0]

    def rank(p: Path) -> int:
        try:
            with rasterio.open(p) as d:
                src = (d.tags().get("source") or d.tags().get("detail") or "").lower()
        except Exception:                                      # noqa: BLE001
            return 9
        for i, k in enumerate(("icgc", "ign", "pnoa", "naip")):
            if k in src:
                return i
        return 8
    return sorted(cands, key=rank)[0]


class OrthoTiler:
    """Web-mercator XYZ tiles cut from one GeoTIFF (any CRS) with a per-tile ``warp.reproject``."""
    TILE = 256
    R = 6378137.0

    def __init__(self, path: Path):
        import numpy as np
        import rasterio
        from rasterio.warp import transform_bounds
        self.path = path
        self.src = rasterio.open(path)
        self.rgb = self.src.read(indexes=[1, 2, 3]).astype("uint8")          # ~11 MB for 2024x1794
        self.alpha = np.full(self.rgb.shape[1:], 255, dtype="uint8")
        self.bounds = transform_bounds(self.src.crs, "EPSG:3857", *self.src.bounds)   # xmin ymin xmax ymax
        self.detail = self.src.tags().get("detail") or self.src.tags().get("source") or path.name
        self.res_m = float(self.src.res[0])

    @classmethod
    def tile_bounds(cls, z: int, x: int, y: int) -> tuple[float, float, float, float]:
        n = 2 ** z
        size = 2 * math.pi * cls.R / n
        xmin = -math.pi * cls.R + x * size
        ymax = math.pi * cls.R - y * size
        return xmin, ymax - size, xmin + size, ymax

    def tile_png(self, z: int, x: int, y: int) -> bytes | None:
        import numpy as np
        from PIL import Image
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        from rasterio.warp import reproject
        xmin, ymin, xmax, ymax = self.tile_bounds(z, x, y)
        bx0, by0, bx1, by1 = self.bounds
        if xmax <= bx0 or xmin >= bx1 or ymax <= by0 or ymin >= by1:
            return None
        dst_tf = from_bounds(xmin, ymin, xmax, ymax, self.TILE, self.TILE)
        rgb = np.zeros((3, self.TILE, self.TILE), dtype="uint8")
        alpha = np.zeros((self.TILE, self.TILE), dtype="uint8")
        common = dict(src_transform=self.src.transform, src_crs=self.src.crs, dst_transform=dst_tf, dst_crs="EPSG:3857")
        reproject(self.rgb, rgb, resampling=Resampling.bilinear, **common)
        reproject(self.alpha, alpha, resampling=Resampling.nearest, **common)
        rgba = np.concatenate([np.transpose(rgb, (1, 2, 0)), alpha[..., None]], axis=2)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, format="PNG", compress_level=3)
        return buf.getvalue()


def discover_lowfly(twin_dir: Path, lowfly_root: Path) -> Path | None:
    """The ``out/lowfly/<Map>`` whose manifest points at this twin, if any."""
    if not lowfly_root.exists():
        return None
    want = twin_dir.resolve()
    for d in sorted(p for p in lowfly_root.iterdir() if p.is_dir()):
        m = d / "manifest.json"
        if not m.exists():
            continue
        try:
            raw = json.loads(m.read_text()).get("twin_dir") or ""
        except (OSError, json.JSONDecodeError):
            continue
        td = Path(raw)
        if not td.is_absolute():
            td = ROOT / raw
        if td.resolve() == want:
            return d
    return None


class Store:
    def __init__(self, build_dir: Path, name: str, data_dir: Path, lowfly: Path | None):
        self.build_dir = build_dir
        self.name = name
        self.twin_dir = build_dir / f"{name}.twin"
        self.model = json.loads((self.twin_dir / "model.json").read_text())
        self.frame = LocalFrame(self.model["origin_lat"], self.model["origin_lon"])
        self.bbox = [float(v) for v in self.model["bbox_wgs84"]]        # S W N E
        self.lowfly = lowfly
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self._twin: bytes | None = None
        self._osm: bytes | None = None
        self.twin_counts: dict[str, int] = {}
        self.osm_counts: dict[str, int] = {}
        self.region = region_for_bbox(self.bbox)
        self._detect: bytes | None = None
        self.detect_counts: dict[str, int] = {}
        self.detect_meta: dict[str, Any] = {}
        self.ortho: OrthoTiler | None = None
        op = find_input_ortho(data_dir, self.bbox)
        if op is not None:
            try:
                self.ortho = OrthoTiler(op)
                log.info("input ortho: %s (%s, %.2f m/px)", op.name, self.ortho.detail, self.ortho.res_m)
            except Exception as exc:                           # noqa: BLE001 - rasterio missing / unreadable
                log.warning("input ortho %s unusable: %s", op, exc)
        else:
            log.info("no cached input ortho for this bbox in %s", data_dir)

    def twin_bytes(self) -> bytes:
        with self.lock:
            if self._twin is None:
                fc = twin_geojson(self.twin_dir, self.frame)
                self.twin_counts = fc.pop("counts")
                self._twin = json.dumps(fc, separators=(",", ":")).encode()
            return self._twin

    def osm_bytes(self) -> bytes:
        with self.lock:
            if self._osm is None:
                s_, w_, n_, e_ = self.bbox
                raw = fetch_overpass((s_, w_, n_, e_), cache_dir=self.data_dir)
                fc = osm_geojson(raw)
                self.osm_counts = fc.pop("counts")
                self._osm = json.dumps(fc, separators=(",", ":")).encode()
            return self._osm

    def detect_bytes(self) -> bytes | None:
        """``<build_dir>/detect/detections.geojson`` (tools/ortho_detect.py, model metres) -> WGS84; re-read when it changes."""
        p = self.build_dir / "detect" / "detections.geojson"
        if not p.exists():
            return None
        mtime = p.stat().st_mtime
        with self.lock:
            if getattr(self, "_detect_mtime", None) == mtime:
                return self._detect
            fc = json.loads(p.read_text())
            tf = self.frame._to_wgs()
            feats = []
            for f in fc.get("features", []):
                g = f.get("geometry")
                if not g:
                    continue
                feats.append({"type": "Feature", "properties": f.get("properties") or {},
                              "geometry": {"type": g["type"], "coordinates": _reproject_coords(tf, g["coordinates"])}})
            self.detect_counts = {}
            for f in feats:
                lb = f["properties"].get("label", "?")
                self.detect_counts[lb] = self.detect_counts.get(lb, 0) + 1
            self.detect_meta = {"ortho": fc.get("ortho"), "params": fc.get("params"), "n": len(feats)}
            self._detect = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")).encode()
            self._detect_mtime = mtime
            return self._detect

    def lowfly_manifest(self) -> dict[str, Any] | None:
        """Re-read every time: a flight in progress writes the manifest when it lands."""
        if self.lowfly is None:
            return None
        m = self.lowfly / "manifest.json"
        if not m.exists():
            return None
        try:
            return json.loads(m.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def meta(self) -> dict[str, Any]:
        self.twin_bytes()
        self.osm_bytes()
        return {
            "name": self.name,
            "build_dir": str(self.build_dir),
            "twin_dir": str(self.twin_dir),
            "origin": [self.model["origin_lat"], self.model["origin_lon"]],
            "bbox_swne": self.bbox,
            "profile": (self.model.get("metadata") or {}).get("profile", {}).get("name"),
            "twin_counts": self.twin_counts,
            "osm_counts": self.osm_counts,
            "lowfly": self.lowfly_manifest(),
            "lowfly_dir": str(self.lowfly) if self.lowfly else None,
            "detect": ({"counts": self.detect_counts, **self.detect_meta} if self.detect_bytes() else None),
            "region": self.region,
            "ortho": ({"detail": self.ortho.detail, "res_m": self.ortho.res_m, "file": self.ortho.path.name}
                      if self.ortho else None),
        }


# ------------------------------------------------------------------------------------ http

class Handler(BaseHTTPRequestHandler):
    server_version = "TwinGeoOverlay/1.0"
    protocol_version = "HTTP/1.1"
    store: Store

    def log_message(self, format: str, *args: Any) -> None:   # noqa: A002 - stdlib hook
        log.debug(format, *args)

    def _send(self, code: int, body: bytes, ctype: str, cache: int = 0) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", f"max-age={cache}")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str) -> None:
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    def do_GET(self) -> None:                                  # noqa: N802
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as exc:                               # noqa: BLE001
            log.exception("GET %s failed", self.path)
            self._error(500, str(exc))

    def _route(self) -> None:
        raw = self.path.split("?")[0]
        parts = [p for p in raw.split("/") if p]
        if not parts:
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if parts == ["api", "meta"]:
            self._send(200, json.dumps(self.store.meta()).encode(), "application/json")
            return
        if parts == ["api", "twin.geojson"]:
            self._send(200, self.store.twin_bytes(), "application/geo+json", cache=60)
            return
        if parts == ["api", "osm.geojson"]:
            self._send(200, self.store.osm_bytes(), "application/geo+json", cache=60)
            return
        if parts == ["api", "detect.geojson"]:
            b = self.store.detect_bytes()
            if b is None:
                self._error(404, "no detect/detections.geojson in the build dir")
                return
            self._send(200, b, "application/geo+json")
            return
        if parts[0] == "lowfly" and len(parts) == 3 and self.store.lowfly is not None:
            z, fname = parts[1], parts[2]
            if not (z.startswith("z") and z[1:].isdigit() and fname.endswith(".webp")
                    and fname[:-5].replace("_", "").isdigit()):
                self._error(404, "tile path is /lowfly/z<z>/<i>_<j>.webp")
                return
            p = self.store.lowfly / z / fname
            if not p.exists():
                self._error(404, "no tile")
                return
            self._send(200, p.read_bytes(), "image/webp", cache=3600)
            return
        if parts[0] == "ortho" and len(parts) == 4 and self.store.ortho is not None:
            z, x, y = parts[1], parts[2], parts[3][:-4] if parts[3].endswith(".png") else ""
            if not (z.isdigit() and x.isdigit() and y.isdigit()):
                self._error(404, "tile path is /ortho/<z>/<x>/<y>.png")
                return
            png = self.store.ortho.tile_png(int(z), int(x), int(y))
            if png is None:
                self._send(204, b"", "image/png", cache=3600)
                return
            self._send(200, png, "image/png", cache=3600)
            return
        self._error(404, "unknown path")


# ------------------------------------------------------------------------------------ page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Twin geo overlay</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; font: 13px/1.35 system-ui, sans-serif; color: #eee; background: #111; }
  #map { position: absolute; inset: 0; }
  #panel { position: absolute; top: 10px; right: 10px; z-index: 1000; width: 300px; max-height: calc(100% - 20px);
           overflow: auto; background: rgba(20,20,24,.92); border: 1px solid #333; border-radius: 8px; padding: 10px 12px; }
  #panel h1 { font-size: 14px; margin: 0 0 6px; }
  #panel h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #9aa; margin: 10px 0 4px; }
  .row { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  .row label { flex: 1; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row input[type=range] { width: 90px; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  .mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .muted { color: #9aa; }
  #readout { position: absolute; left: 10px; bottom: 10px; z-index: 1000; background: rgba(20,20,24,.92);
             border: 1px solid #333; border-radius: 8px; padding: 6px 10px; }
  #help { margin-top: 8px; color: #9aa; font-size: 11px; }
  .leaflet-popup-content { font: 12px/1.3 ui-monospace, Menlo, monospace; max-height: 260px; overflow: auto; }
  .leaflet-popup-content table { border-collapse: collapse; }
  .leaflet-popup-content td { padding: 0 6px 0 0; vertical-align: top; }
  .leaflet-popup-content td:first-child { color: #557; }
  .leaflet-container { background: #000; }
  a { color: #8cf; }
</style></head>
<body>
<div id="map"></div>
<div id="panel">
  <h1 id="title">Twin geo overlay</h1>
  <div class="muted mono" id="sub"></div>
  <h2>Basemap</h2><div id="basemaps"></div>
  <h2>OpenStreetMap (input)</h2><div id="osm-layers"></div>
  <h2>Twin model (ours)</h2><div id="twin-layers"></div>
  <h2>ML detections (ortho_detect)</h2><div id="detect-layers"><span class="muted">no detect/detections.geojson</span></div>
  <h2>CARLA low-fly mosaic</h2><div id="lowfly-layers"><span class="muted">no manifest yet</span></div>
  <div id="help"><b>F</b> flicker twin · <b>O</b> flicker OSM · <b>D</b> flicker detections · <b>M</b> flicker mosaic · click: coordinates + Street View</div>
</div>
<div id="readout" class="mono">move the mouse over the map</div>
<script>
window.addEventListener("error", ev => { const el = document.getElementById("sub"); if (el) el.textContent = "JS error: " + ev.message + " (line " + ev.lineno + ")"; });
window.addEventListener("unhandledrejection", ev => { const el = document.getElementById("sub"); if (el) el.textContent = "failed: " + (ev.reason && ev.reason.message || ev.reason); });
// ------------------------------------------------------------------ WGS84 <-> local ENU (tmerc, k=1)
// Same projection as twinmodel.frame.LocalFrame: +proj=tmerc +lat_0=ORIGIN_LAT +lon_0=ORIGIN_LON +k=1.
// Snyder series; mm-level within the few km a twin spans.
const A = 6378137.0, F = 1 / 298.257223563, E2 = F * (2 - F), EP2 = E2 / (1 - E2);
function mArc(phi) {
  const e4 = E2 * E2, e6 = e4 * E2;
  return A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
    - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * Math.sin(2 * phi)
    + (15 * e4 / 256 + 45 * e6 / 1024) * Math.sin(4 * phi)
    - (35 * e6 / 3072) * Math.sin(6 * phi));
}
function makeFrame(lat0, lon0) {
  const p0 = lat0 * Math.PI / 180, l0 = lon0 * Math.PI / 180, M0 = mArc(p0);
  return {
    toLocal(lat, lon) {
      const p = lat * Math.PI / 180, l = lon * Math.PI / 180;
      const sp = Math.sin(p), cp = Math.cos(p), tp = Math.tan(p);
      const N = A / Math.sqrt(1 - E2 * sp * sp), T = tp * tp, C = EP2 * cp * cp, Aa = (l - l0) * cp;
      const A2 = Aa * Aa, A3 = A2 * Aa, A4 = A3 * Aa, A5 = A4 * Aa, A6 = A5 * Aa;
      const x = N * (Aa + (1 - T + C) * A3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * EP2) * A5 / 120);
      const y = (mArc(p) - M0) + N * tp * (A2 / 2 + (5 - T + 9 * C + 4 * C * C) * A4 / 24
        + (61 - 58 * T + T * T + 600 * C - 330 * EP2) * A6 / 720);
      return [x, y];
    }
  };
}

// ------------------------------------------------------------------ map + basemaps
// URL presets: ?on=lowfly,markings&off=osm_node&base=2   (comma lists of layer keys; base = index in BASEMAPS)
const Q = new URLSearchParams(location.search);
const Q_ON = new Set((Q.get("on") || "").split(",").filter(Boolean)), Q_OFF = new Set((Q.get("off") || "").split(",").filter(Boolean));
const map = L.map("map", { zoomControl: true, maxZoom: 24, zoomSnap: 0.25, zoomDelta: 0.5, wheelPxPerZoomLevel: 90 });
L.control.scale({ imperial: false, maxWidth: 200 }).addTo(map);
const canvas = L.canvas({ padding: 0.5 });

const WMS = (url, layers, attribution) => L.tileLayer.wms(url, { layers, styles: "", format: "image/jpeg", version: "1.3.0",
  transparent: false, tileSize: 512, maxZoom: 24, attribution });
const ICGC = "https://geoserveis.icgc.cat/servei/catalunya/orto-territorial/wms";
const BASEMAPS = [   // [label, layer, region-default key]
  ["ICGC ortho 10 cm 2020 (Catalonia)", WMS(ICGC, "ortofoto_10cm_color_2020", "ICGC"), "icgc"],
  ["ICGC ortho 25 cm 2025 (Catalonia)", WMS(ICGC, "ortofoto_25cm_color_2025", "ICGC"), null],
  ["IGN PNOA ~25 cm (Spain)", WMS("https://www.ign.es/wms-inspire/pnoa-ma", "OI.OrthoimageCoverage", "IGN PNOA"), "pnoa"],
  ["Google satellite", L.tileLayer("https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
      { subdomains: "0123", maxNativeZoom: 21, maxZoom: 24, attribution: "Imagery © Google" }), "google"],
  ["Google hybrid", L.tileLayer("https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
      { subdomains: "0123", maxNativeZoom: 21, maxZoom: 24, attribution: "Imagery © Google" }), null],
  ["Esri World Imagery", L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxNativeZoom: 19, maxZoom: 24, attribution: "Esri, Maxar, Earthstar Geographics" }), null],
  ["OSM carto", L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxNativeZoom: 19, maxZoom: 24, attribution: "© OpenStreetMap contributors" }), null],
  ["none", L.layerGroup(), null],
];
let base = null;
function setBase(i) {
  if (base) map.removeLayer(base);
  base = BASEMAPS[i][1]; base.addTo(map); base.bringToBack && base.bringToBack();
  document.querySelectorAll("#basemaps input").forEach((el, k) => { el.checked = (k === i); });
  try { localStorage.setItem("geo-overlay:base", i); } catch (e) {}
}
function buildBasemaps(meta) {
  if (meta.ortho) {   // the GeoTIFF the build consumed, tiled by the server; insert first
    BASEMAPS.unshift([`pipeline input ortho · ${meta.ortho.res_m.toFixed(2)} m/px (${meta.ortho.detail})`,
      L.tileLayer("/ortho/{z}/{x}/{y}.png", { maxNativeZoom: 21, maxZoom: 24, attribution: meta.ortho.detail }), null]);
  }
  const host = document.getElementById("basemaps");
  host.innerHTML = "";
  let pick = BASEMAPS.findIndex(b => b[2] === meta.region); if (pick < 0) pick = BASEMAPS.findIndex(b => b[2] === "google");
  try { const v = localStorage.getItem("geo-overlay:base"); if (v !== null && +v < BASEMAPS.length) pick = +v; } catch (e) {}
  if (Q.has("base")) pick = Math.max(0, Math.min(BASEMAPS.length - 1, +Q.get("base") || 0));
  BASEMAPS.forEach(([label], i) => {
    const row = document.createElement("div"); row.className = "row";
    row.innerHTML = `<input type="radio" name="base" id="b${i}"><label for="b${i}" title="${label}">${label}</label>`;
    row.querySelector("input").addEventListener("change", () => setBase(i));
    host.appendChild(row);
  });
  setBase(pick);
}

// ------------------------------------------------------------------ overlays with opacity
const groups = {};   // key -> { layer, opacity, on, host }
function addOverlay(hostId, key, label, colour, layer, opacity, on) {
  if (Q_ON.has(key)) on = true;
  if (Q_OFF.has(key)) on = false;
  const host = document.getElementById(hostId);
  const row = document.createElement("div"); row.className = "row";
  row.innerHTML = `<input type="checkbox" id="c-${key}" ${on ? "checked" : ""}>
    <label for="c-${key}"><span class="sw" style="background:${colour}"></span>${label}</label>
    <input type="range" min="0" max="1" step="0.05" value="${opacity}" title="opacity">`;
  host.appendChild(row);
  const g = { layer, opacity, on, key };
  groups[key] = g;
  const cb = row.querySelector("input[type=checkbox]"), sl = row.querySelector("input[type=range]");
  cb.addEventListener("change", () => { g.on = cb.checked; g.on ? layer.addTo(map) : map.removeLayer(layer); });
  sl.addEventListener("input", () => { g.opacity = +sl.value; applyOpacity(g); });
  if (on) layer.addTo(map);
  applyOpacity(g);
  return g;
}
function applyOpacity(g) {
  const L_ = g.layer;
  if (L_.setOpacity) { L_.setOpacity(g.opacity); return; }
  if (L_.eachLayer) L_.eachLayer(l => {
    if (l.setStyle && l._baseStyle) {
      const s = Object.assign({}, l._baseStyle);
      if (s.opacity !== undefined) s.opacity = s.opacity * g.opacity;
      if (s.fillOpacity !== undefined) s.fillOpacity = s.fillOpacity * g.opacity;
      l.setStyle(s);
    }
  });
}
function flicker(keys) {
  keys.forEach(k => { const g = groups[k]; if (!g) return; g.on = !g.on;
    document.getElementById("c-" + k).checked = g.on; g.on ? g.layer.addTo(map) : map.removeLayer(g.layer); });
}

function popupTable(props) {
  const rows = Object.entries(props).filter(([k]) => k !== "layer")
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `<tr><td>${k}</td><td>${String(v).replace(/</g, "&lt;")}</td></tr>`).join("");
  return `<b>${props.layer}</b><table>${rows}</table>`;
}
function geoLayer(fc, filter, style, pointToLayer) {
  const lyr = L.geoJSON(fc, {
    renderer: canvas, filter, style, pointToLayer,
    onEachFeature: (f, l) => { l._baseStyle = style ? style(f) : (pointToLayer ? l.options : null); l.bindPopup(() => popupTable(f.properties), { maxWidth: 420 }); }
  });
  return lyr;
}

// ------------------------------------------------------------------ styles
const HW = { motorway: "#e892a2", trunk: "#f9b29c", primary: "#fcd6a4", secondary: "#f7fabf", tertiary: "#ffffff",
  residential: "#ffffff", unclassified: "#ffffff", living_street: "#ededed", service: "#cfcfcf", pedestrian: "#dddde8",
  footway: "#fa8072", cycleway: "#3c78d8", path: "#c8a27a", steps: "#fa8072", track: "#a97b3d", other: "#bbb" };
const SURF = { drivable: "#3c3c3f", sidewalk: "#a7a59c", crossing: "#ecece6", median: "#7e9a70", island: "#8c9e80",
  verge: "#5c8545", parking: "#4d4d4f", ground: "#5a6a4c" };

// ------------------------------------------------------------------ low-fly mosaic as a reprojected GridLayer
const LowFly = L.GridLayer.extend({
  initialize(man, frame, opts) { L.GridLayer.prototype.initialize.call(this, opts); this.man = man; this.frame = frame; this.cache = new Map(); },
  _img(z, i, j) {
    const id = z + "/" + i + "/" + j;
    let e = this.cache.get(id);
    if (!e) {
      e = { p: new Promise(res => { const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = `/lowfly/z${z}/${i}_${j}.webp`; }) };
      this.cache.set(id, e);
      if (this.cache.size > 900) { const k = this.cache.keys().next().value; this.cache.delete(k); }
    }
    return e.p;
  },
  createTile(coords, done) {
    const m = this.man, tile = document.createElement("canvas"), size = this.getTileSize();
    tile.width = size.x; tile.height = size.y;
    const nw = this._map.unproject(coords.scaleBy(size), coords.z);
    const se = this._map.unproject(coords.add([1, 1]).scaleBy(size), coords.z);
    const ne = L.latLng(nw.lat, se.lng), sw = L.latLng(se.lat, nw.lng);
    const [x0, y0] = this.frame.toLocal(nw.lat, nw.lng), [x1, y1] = this.frame.toLocal(ne.lat, ne.lng), [x2, y2] = this.frame.toLocal(sw.lat, sw.lng);
    // affine ENU -> tile px from three corners: px = a x + b y + c ; py = d x + e y + f
    const ux = x1 - x0, uy = y1 - y0, vx = x2 - x0, vy = y2 - y0, det = ux * vy - uy * vx;
    const a = size.x * vy / det, b = -size.x * vx / det, d = -size.y * uy / det, e = size.y * ux / det;
    const c = -(a * x0 + b * y0), f = -(d * x0 + e * y0);
    const mPerPx = Math.hypot(ux, uy) / size.x;
    const lvl = Math.max(0, Math.min(m.levels - 1, Math.round(Math.log2(mPerPx / (m.cm_per_px / 100)))));
    const t = m.leaf_m * Math.pow(2, lvl), s = t / m.leaf_px;
    const [x3, y3] = this.frame.toLocal(se.lat, se.lng);
    const xs = [x0, x1, x2, x3], ys = [y0, y1, y2, y3];
    const i0 = Math.floor((Math.min(...xs) - m.x0) / t), i1 = Math.floor((Math.max(...xs) - m.x0) / t);
    const j0 = Math.floor((Math.min(...ys) - m.y0) / t), j1 = Math.floor((Math.max(...ys) - m.y0) / t);
    const nI = Math.ceil(m.leaves[0] / Math.pow(2, lvl)), nJ = Math.ceil(m.leaves[1] / Math.pow(2, lvl));
    const jobs = [];
    for (let i = Math.max(0, i0); i <= Math.min(nI - 1, i1); i++)
      for (let j = Math.max(0, j0); j <= Math.min(nJ - 1, j1); j++)
        jobs.push(this._img(lvl, i, j).then(im => [im, i, j]));
    if (!jobs.length) { setTimeout(() => done(null, tile), 0); return tile; }
    Promise.all(jobs).then(list => {
      const g = tile.getContext("2d");
      g.imageSmoothingEnabled = true;
      for (const [im, i, j] of list) {
        if (!im) continue;
        const X0 = m.x0 + i * t, Ytop = m.y0 + (j + 1) * t;    // image row 0 is the north edge
        g.setTransform(a * s, d * s, -b * s, -e * s, a * X0 + b * Ytop + c, d * X0 + e * Ytop + f);
        g.drawImage(im, 0, 0, im.naturalWidth || m.leaf_px, im.naturalHeight || m.leaf_px, -0.5, -0.5, m.leaf_px + 1, m.leaf_px + 1);
      }
      done(null, tile);
    });
    return tile;
  }
});

// ------------------------------------------------------------------ boot
let META = null, FRAME = null, lowflyGroup = null;
async function boot() {
  META = await (await fetch("/api/meta")).json();
  FRAME = makeFrame(META.origin[0], META.origin[1]);
  buildBasemaps(META);
  const [S, W, N, E] = META.bbox_swne;
  document.getElementById("title").textContent = "Twin geo overlay · " + META.name;
  document.getElementById("sub").textContent = `${META.profile || ""} · origin ${META.origin[0].toFixed(5)}, ${META.origin[1].toFixed(5)}`;
  const h = location.hash.match(/^#(\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)$/);   // #zoom/lat/lon, shareable
  if (h) map.setView([+h[2], +h[3]], +h[1]); else map.fitBounds([[S, W], [N, E]]);
  map.on("moveend", () => { const c = map.getCenter(); history.replaceState(null, "", `#${map.getZoom().toFixed(2)}/${c.lat.toFixed(7)}/${c.lng.toFixed(7)}`); });
  L.rectangle([[S, W], [N, E]], { color: "#ff0", weight: 1, fill: false, dashArray: "4 4", interactive: false }).addTo(map);

  const [osm, twin] = await Promise.all([fetch("/api/osm.geojson").then(r => r.json()), fetch("/api/twin.geojson").then(r => r.json())]);

  // OSM
  addOverlay("osm-layers", "osm_highway", `highway ways (${META.osm_counts.highway})`, "#ffffff",
    geoLayer(osm, f => f.properties.layer === "osm_highway", f => ({ color: HW[f.properties.class] || HW.other, weight: 2.5, opacity: 1 })), 0.9, true);
  addOverlay("osm-layers", "osm_node", `way nodes (${META.osm_counts.node}) · red = junction, cyan = tagged`, "#ff4040",
    geoLayer(osm, f => f.properties.layer === "osm_node", null, (f, ll) => {
      const p = f.properties, junction = p.degree >= 2;
      return L.circleMarker(ll, { renderer: canvas, radius: junction ? 4 : 2.5, color: p.tagged ? "#00e5ff" : (junction ? "#ff4040" : "#ffffff"),
        weight: 1, fillColor: junction ? "#ff4040" : (p.tagged ? "#00e5ff" : "#ffffff"), fillOpacity: 0.9, opacity: 1 });
    }), 1, true);
  addOverlay("osm-layers", "osm_building", `buildings (${META.osm_counts.building})`, "#d9a066",
    geoLayer(osm, f => f.properties.layer === "osm_building", () => ({ color: "#d9a066", weight: 1, opacity: 1, fillOpacity: 0.08 })), 0.7, false);
  addOverlay("osm-layers", "osm_other", "other ways / areas", "#9aa",
    geoLayer(osm, f => f.properties.layer === "osm_other", () => ({ color: "#9aa", weight: 1, opacity: 1, fillOpacity: 0.05, dashArray: "3 3" })), 0.7, false);

  // Twin
  const tc = META.twin_counts;
  addOverlay("twin-layers", "surfaces", `surfaces (${tc.surfaces || 0})`, SURF.drivable,
    geoLayer(twin, f => f.properties.layer === "surfaces", f => ({ color: SURF[f.properties.kind] || "#888", weight: 1, opacity: 1, fillColor: SURF[f.properties.kind] || "#888", fillOpacity: 0.45 })), 0.8, true);
  addOverlay("twin-layers", "curbs", `kerb lines (${tc.curbs || 0})`, "#ffd400",
    geoLayer(twin, f => f.properties.layer === "curbs", () => ({ color: "#ffd400", weight: 1.5, opacity: 1 })), 1, true);
  addOverlay("twin-layers", "roads", `road reference lines (${tc.roads || 0})`, "#ff2d95",
    geoLayer(twin, f => f.properties.layer === "roads", () => ({ color: "#ff2d95", weight: 2, opacity: 1 })), 1, true);
  addOverlay("twin-layers", "junctions", `junction polygons (${tc.junctions || 0})`, "#c860ff",
    geoLayer(twin, f => f.properties.layer === "junctions", () => ({ color: "#c860ff", weight: 2, opacity: 1, fillOpacity: 0.12 })), 1, true);
  addOverlay("twin-layers", "markings", `markings (${tc.markings || 0})`, "#f2f2f2",
    geoLayer(twin, f => f.properties.layer === "markings", f => ({ color: f.properties.color === "yellow" ? "#f2cc26" : "#f2f2f2", weight: 1, opacity: 1, dashArray: f.properties.kind === "dashed" ? "4 4" : null })), 1, false);
  addOverlay("twin-layers", "signals", `signals (${tc.signals || 0})`, "#39ff14",
    geoLayer(twin, f => f.properties.layer === "signals", null, (f, ll) => L.circleMarker(ll, { renderer: canvas, radius: 4, color: "#39ff14", weight: 1.5, fillColor: "#000", fillOpacity: 0.7, opacity: 1 })), 1, false);
  addOverlay("twin-layers", "buildings", `buildings (${tc.buildings || 0})`, "#66aaff",
    geoLayer(twin, f => f.properties.layer === "buildings", () => ({ color: "#66aaff", weight: 1, opacity: 1, fillOpacity: 0.1 })), 0.8, false);
  addOverlay("twin-layers", "objects", `objects / trees (${tc.objects || 0})`, "#2ecc40",
    geoLayer(twin, f => f.properties.layer === "objects", null, (f, ll) => L.circleMarker(ll, { renderer: canvas, radius: 3, color: "#2ecc40", weight: 1, fillColor: "#2ecc40", fillOpacity: 0.6, opacity: 1 })), 1, false);

  if (META.detect) {
    const det = await (await fetch("/api/detect.geojson")).json();
    const host = document.getElementById("detect-layers"); host.innerHTML = "";
    const DC = { car: "#ff3b3b", van: "#ff8c3b", truck: "#ffb03b", bus: "#ffd23b", motorcycle: "#ff3bd2", crosswalk: "#ffffff",
      tree: "#3bff6e", "street lamp": "#3bd2ff" };
    const labels = Object.keys(META.detect.counts).sort((a, b) => META.detect.counts[b] - META.detect.counts[a]);
    labels.forEach((lb, i) => {
      const col = DC[lb] || ["#e0e0e0", "#a0a0ff", "#ffa0ff", "#a0ffff"][i % 4];
      addOverlay("detect-layers", "det_" + lb.replace(/\W+/g, "_"), `${lb} (${META.detect.counts[lb]})`, col,
        geoLayer(det, f => f.properties.label === lb, () => ({ color: col, weight: 1.5, opacity: 1, fillColor: col, fillOpacity: 0.25 })), 1, true);
    });
    const note = document.createElement("div"); note.className = "muted";
    note.textContent = `${META.detect.n} detections · ${(META.detect.ortho || {}).detail || ""}`.trim();
    host.appendChild(note);
  }
  mountLowfly(META.lowfly);
  if (!META.lowfly && META.lowfly_dir) setInterval(async () => {   // flight in progress: pick the manifest up when it lands
    const m = await (await fetch("/api/meta")).json();
    if (m.lowfly) { mountLowfly(m.lowfly); }
  }, 30000);
}
function mountLowfly(man) {
  if (!man || lowflyGroup) return;
  document.getElementById("lowfly-layers").innerHTML = "";
  const lyr = new LowFly(man, FRAME, { tileSize: 256, maxZoom: 24, opacity: 1, updateWhenZooming: false, keepBuffer: 2 });
  lowflyGroup = addOverlay("lowfly-layers", "lowfly", `${man.map} · ${man.cm_per_px} cm/px · ${man.levels} levels`, "#c8c8c8", lyr, 1, false);
  const note = document.createElement("div"); note.className = "muted";
  note.textContent = `${man.leaf_tiles} leaf tiles, flown ${(man.seconds / 60).toFixed(0)} min`;
  document.getElementById("lowfly-layers").appendChild(note);
}

// ------------------------------------------------------------------ readout + click
const ro = document.getElementById("readout");
map.on("mousemove", ev => {
  if (!FRAME) return;
  const [x, y] = FRAME.toLocal(ev.latlng.lat, ev.latlng.lng);
  ro.textContent = `lat ${ev.latlng.lat.toFixed(7)}  lon ${ev.latlng.lng.toFixed(7)}   model x ${x.toFixed(2)}  y ${y.toFixed(2)}   CARLA x ${x.toFixed(2)}  y ${(-y).toFixed(2)}   z${map.getZoom().toFixed(2)}`;
});
map.on("click", ev => {
  if (!FRAME) return;
  const la = ev.latlng.lat, lo = ev.latlng.lng, [x, y] = FRAME.toLocal(la, lo);
  const sv = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${la.toFixed(7)},${lo.toFixed(7)}`;
  const gm = `https://www.google.com/maps/@${la.toFixed(7)},${lo.toFixed(7)},21z/data=!3m1!1e3`;
  const ge = `https://earth.google.com/web/@${la.toFixed(7)},${lo.toFixed(7)},0a,120d,35y,0h,0t,0r`;
  const osm = `https://www.openstreetmap.org/edit#map=21/${la.toFixed(7)}/${lo.toFixed(7)}`;
  L.popup({ maxWidth: 360 }).setLatLng(ev.latlng).setContent(
    `<table><tr><td>lat, lon</td><td>${la.toFixed(7)}, ${lo.toFixed(7)}</td></tr>
     <tr><td>model x, y</td><td>${x.toFixed(2)}, ${y.toFixed(2)}</td></tr>
     <tr><td>CARLA x, y</td><td>${x.toFixed(2)}, ${(-y).toFixed(2)}</td></tr></table>
     <div style="margin-top:6px"><a target="_blank" rel="noopener" href="${sv}">Street View here</a> ·
     <a target="_blank" rel="noopener" href="${gm}">Google Maps satellite</a> ·
     <a target="_blank" rel="noopener" href="${ge}">Google Earth</a> ·
     <a target="_blank" rel="noopener" href="${osm}">edit in OSM iD</a></div>`).openOn(map);
});
document.addEventListener("keydown", ev => {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "f" || ev.key === "F") flicker(["surfaces", "curbs", "roads", "junctions", "markings", "signals", "buildings", "objects"].filter(k => groups[k]));
  if (ev.key === "o" || ev.key === "O") flicker(["osm_highway", "osm_node", "osm_building", "osm_other"].filter(k => groups[k]));
  if (ev.key === "m" || ev.key === "M") flicker(["lowfly"].filter(k => groups[k]));
  if (ev.key === "d" || ev.key === "D") flicker(Object.keys(groups).filter(k => k.startsWith("det_")));
});
boot().catch(e => { document.getElementById("sub").textContent = "failed: " + e; console.error(e); });
</script>
</body></html>
"""


# ------------------------------------------------------------------------------------ main

def make_server(store: Store, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": store})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir", help="twin build directory, e.g. out/v10_eixample")
    ap.add_argument("name", help="twin name, e.g. eixample (-> <build_dir>/<name>.twin)")
    ap.add_argument("--lowfly", help="out/lowfly/<Map> pyramid (default: the one whose manifest names this twin)")
    ap.add_argument("--lowfly-root", default="out/lowfly")
    ap.add_argument("--data", default="data", help="Overpass cache dir (twinmodel default: data/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(name)s %(message)s")

    build_dir = Path(args.build_dir)
    twin_dir = build_dir / f"{args.name}.twin"
    if not (twin_dir / "model.json").exists():
        ap.error(f"{twin_dir}/model.json not found")
    lowfly = Path(args.lowfly) if args.lowfly else discover_lowfly(twin_dir, Path(args.lowfly_root))
    if lowfly is None:
        # A flight in progress has no manifest yet: mount the pyramid whose folder name carries the twin
        # name (out/lowfly/EixampleDemo for "eixample") so the page picks the manifest up when it lands.
        root = Path(args.lowfly_root)
        cands = [d for d in (root.iterdir() if root.exists() else [])
                 if d.is_dir() and args.name.lower().split("_")[0] in d.name.lower() and (d / "z0").exists()]
        lowfly = sorted(cands)[0] if cands else None
    if lowfly is not None:
        state = "manifest present" if (lowfly / "manifest.json").exists() else "NO manifest yet (flight in progress?)"
        log.info("low-fly mosaic: %s (%s)", lowfly, state)
    else:
        log.info("no low-fly pyramid found for %s; mosaic layer off", args.name)

    store = Store(build_dir, args.name, Path(args.data), lowfly)
    store.meta()                                  # build the GeoJSON up front so the first page load is instant
    srv = make_server(store, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    log.info("serving %s (%s) at %s", args.name, twin_dir, url)
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
