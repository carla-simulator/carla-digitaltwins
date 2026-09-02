"""Build the interactive *region review* page: a top-down view of the baked twin maps on which
regions (polygons / polylines / points) can be drawn and commented on.  Everything the reviewer
draws is stored in the artifact's shared database (collection ``regions``, one document per region)
so Claude can read the feedback back with model *and* CARLA coordinates.

    python tools/carla_topview.py --port 3000 --out out/review            # level 0, 20 cm/px
    python tools/carla_topview.py --port 3000 --out out/review --detail   # level 1, 6 cm/px
    python tools/region_review_page.py --out out/review/region_review.html

The base layer is the real CARLA render as a two-level image pyramid, captured by
``tools/carla_topview.py`` from a running server: level 0 is one 20 cm/px mosaic of the whole map,
level 1 is one 6 cm/px image per 250 m material tile.  ``--images`` points at that directory (the
page's own output directory by default); level 0 is embedded as a JPEG data URI and each level-1
tile as a WebP one.  Level 1 draws only for tiles that intersect the viewport once the view is
zoomed past ``DETAIL_K`` screen pixels per model metre, and its images are decoded lazily and
evicted, because a whole map at 6 cm/px would be a 17000 px image and over a gigabyte decoded.

The vector layers are still there, now as optional overlays -- off by default apart from signals
and junction labels -- with an opacity slider, so a reviewer can flick them on to check what the
geometry claims against what CARLA actually drew.  Without the images the page falls back to the
vector-only view it had before.

Pages.  ``region_review.html`` carries every map at level 0 (the overview, and the map picker).
Each map also gets ``region_review_<Level>.html`` with its own level-1 tiles, because three maps'
detail tiles in one page would blow past the artifact size cap; a per-map page has a static map
label instead of the picker.  Every page uses the same ``regions`` collection and document schema,
so each published artifact keeps its own feedback.

The page is written as artifact page *content* (no ``<html>``/``<head>``/``<body>`` wrapper): the
host wraps it and adds the charset/viewport meta plus a small reset.

Coordinates.  Twin models are local ENU metres (x east, y north).  CARLA/UE runtime coordinates
are ``(x, -y)`` metres -- see ``twinmodel/export/ue.py:model_to_ue``.  Every region document
carries both, and the cursor readout shows both, so feedback can be pasted straight into a CARLA
client script.

Payload encoding.  Coordinates are quantised to ``QUANT`` metres and emitted as integers in
``QUANT`` units (``x_metres = x_int * scale``), which keeps the embedded JSON small and makes the
rounding explicit.  Rings are flat ``[x0,y0,x1,y1,...]`` arrays; a polygon feature is a list of
rings, exterior first (holes are rendered with the even-odd rule).
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("region_review")

# --------------------------------------------------------------------------------- data model

QUANT = 0.05  # metres per integer unit in the embedded payload
COLLINEAR_TOL = 0.02  # metres: a vertex closer than this to the chord a->b is redundant

# surfaces.geojson `properties.kind` -> payload layer.  Anything not listed is dropped.
SURFACE_KINDS = ("ground", "verge", "median", "island", "parking", "drivable", "sidewalk", "crossing")

# markings.geojson -> one bucket per (kind, colour); keys are what the page draws.
MARK_BUCKETS = {("solid", "white"): "sw", ("solid", "yellow"): "sy",
                ("broken", "white"): "bw", ("broken", "yellow"): "by"}

# signals.geojson `kind` -> (OpenDRIVE type, subtype) -- mirrors twinmodel.export.xodr.SIGNAL_TYPES.
SIGNAL_XODR = {
    "traffic_light": ("1000001", "-1"), "traffic_light_arrow": ("1000001", "-1"),
    "traffic_light_ped": ("1000002", "-1"), "stop": ("206", "-1"), "yield": ("205", "-1"),
    "priority_road": ("306", "-1"), "speed_limit": ("274", ""),
}

# name -> (CARLA level name, twin directory relative to the TwinModel root)
DEFAULT_MAPS: list[tuple[str, str, str]] = [
    ("Sf_Soma", "Sf_Soma", "out/v8_soma/sf_soma.twin"),
    ("Sunnyvale", "Sunnyvale", "out/v8_sunnyvale/sunnyvale.twin"),
    # v10 is the build whose .xodr matches the baked EixampleDemo level byte-for-byte outside
    # <signals>/<controller> (v8 and v9 do not).
    ("EixampleDemo", "EixampleDemo", "out/v10_eixample/eixample.twin"),
]


def model_to_carla_xy(x: float, y: float) -> tuple[float, float]:
    """Model metres (x east, y north) -> CARLA/UE metres ``(x, -y)`` (``export.ue.model_to_ue``)."""
    return (x, -y)


def quantize(v: float) -> int:
    """Metres -> integer payload units (``QUANT`` m each)."""
    return int(round(float(v) / QUANT))


# ------------------------------------------------------------------------------- base imagery

def model_to_image_pixel(image: dict, x: float, y: float) -> tuple[float, float]:
    """Model metres -> pixel in the top-down mosaic.  The mosaic is north-up over the model-frame
    rectangle ``image["bounds"]`` = ``[xmin, ymin, xmax, ymax]``, so pixel row 0 is ``ymax``."""
    x0, y0, x1, y1 = image["bounds"]
    w, h = image["size"]
    return ((x - x0) * w / (x1 - x0), (y1 - y) * h / (y1 - y0))


def image_pixel_to_model(image: dict, px: float, py: float) -> tuple[float, float]:
    """Inverse of :func:`model_to_image_pixel` (the JS canvas uses the same rectangle)."""
    x0, y0, x1, y1 = image["bounds"]
    w, h = image["size"]
    return (x0 + px * (x1 - x0) / w, y1 - py * (y1 - y0) / h)


def load_image(image_dir: Path | str, carla_map: str) -> tuple[dict, str] | tuple[None, None]:
    """``topview_<carla_map>.json`` + ``.jpg`` from ``carla_topview.py`` -> (payload meta, data URI).

    Returns ``(None, None)`` when either file is missing, so the page still builds vector-only.
    """
    d = Path(image_dir)
    meta_p, jpg = d / f"topview_{carla_map}.json", d / f"topview_{carla_map}.jpg"
    if not (meta_p.exists() and jpg.exists()):
        return (None, None)
    raw = json.loads(meta_p.read_text())
    blob = jpg.read_bytes()
    meta = {
        "bounds": [float(v) for v in raw["bounds"]],
        "size": [int(v) for v in raw["size"]],
        "px_per_m": float(raw.get("px_per_m") or raw["size"][0] / (raw["bounds"][2] - raw["bounds"][0])),
        "cm_per_px": float(raw.get("cm_per_px") or 0.0) or None,
        "bytes": len(blob),
        "camera": raw.get("camera") or {},
    }
    return (meta, "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii"))


def load_detail(image_dir: Path | str, carla_map: str) -> tuple[dict, dict[str, str]] | tuple[None, dict]:
    """``topview_<carla_map>_detail.json`` + its per-tile WebPs -> (payload meta, {"i,j": data URI}).

    The tiles are kept apart rather than stitched: the page decodes only the ones on screen.
    """
    d = Path(image_dir)
    man = d / f"topview_{carla_map}_detail.json"
    if not man.exists():
        return (None, {})
    raw = json.loads(man.read_text())
    tiles: list[dict] = []
    srcs: dict[str, str] = {}
    total = 0
    for e in raw.get("tiles") or []:
        p = d / e["file"]
        if not p.exists():
            log.warning("%s: detail tile %s missing", carla_map, e["file"])
            continue
        blob = p.read_bytes()
        key = "%d,%d" % (e["i"], e["j"])
        tiles.append({"i": int(e["i"]), "j": int(e["j"]), "b": [float(v) for v in e["bounds"]]})
        srcs[key] = "data:image/webp;base64," + base64.b64encode(blob).decode("ascii")
        total += len(blob)
    if not tiles:
        return (None, {})
    meta = {
        "px_per_m": float(raw["px_per_m"]), "cm_per_px": float(raw["cm_per_px"]),
        "tile_m": float((raw.get("grid") or {}).get("tile_m") or 250.0),
        "tile_px": int(raw["tile_px"]), "tiles": tiles, "bytes": total,
    }
    return (meta, srcs)


def detail_tiles_for_viewport(detail: dict, view: Sequence[float]) -> list[dict]:
    """The level-1 tiles a ``[xmin, ymin, xmax, ymax]`` model-frame viewport touches.

    The page runs exactly this test before it decodes anything; it lives here so it can be tested
    without a browser.
    """
    x0, y0, x1, y1 = view
    return [t for t in detail["tiles"]
            if not (t["b"][2] < x0 or t["b"][0] > x1 or t["b"][3] < y0 or t["b"][1] > y1)]


def simplify(coords: Sequence[Sequence[float]], closed: bool) -> list[int]:
    """Quantise, drop repeated vertices and drop vertices that lie on the chord of their
    neighbours.  Returns a flat ``[x0,y0,x1,y1,...]`` integer array (empty when degenerate)."""
    pts: list[tuple[int, int]] = []
    for c in coords:
        p = (quantize(c[0]), quantize(c[1]))
        if pts and pts[-1] == p:
            continue
        pts.append(p)
    if closed:
        while len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        if len(pts) < 3:
            return []
    else:
        if len(pts) < 2:
            return []
    tol = COLLINEAR_TOL / QUANT
    keep: list[tuple[int, int]] = []
    n = len(pts)
    for i, p in enumerate(pts):
        if closed:
            a = keep[-1] if keep else pts[-1]
            b = pts[(i + 1) % n]
        else:
            if i == 0 or i == n - 1:
                keep.append(p)
                continue
            a, b = keep[-1], pts[i + 1]
        ax, ay = b[0] - a[0], b[1] - a[1]
        chord = (ax * ax + ay * ay) ** 0.5
        if chord > 0:
            dist = abs((p[0] - a[0]) * ay - (p[1] - a[1]) * ax) / chord
            if dist < tol:
                continue
        keep.append(p)
    if closed and len(keep) < 3:
        return []
    if not closed and len(keep) < 2:
        return []
    return [v for p in keep for v in p]


def _rings(geom: dict) -> list[list[list[float]]]:
    t = geom.get("type")
    if t == "Polygon":
        return [geom["coordinates"]]
    if t == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def _lines(geom: dict) -> list[list[list[float]]]:
    t = geom.get("type")
    if t == "LineString":
        return [geom["coordinates"]]
    if t == "MultiLineString":
        return list(geom["coordinates"])
    return []


def polygon_features(features: Iterable[dict]) -> list[list[list[int]]]:
    """GeoJSON polygon features -> ``[[exterior, hole, ...], ...]`` of flat integer rings."""
    out = []
    for f in features:
        for poly in _rings(f.get("geometry") or {}):
            rings = [r for r in (simplify(ring, True) for ring in poly) if r]
            if rings:
                out.append(rings)
    return out


def line_features(features: Iterable[dict]) -> list[list[int]]:
    out = []
    for f in features:
        for line in _lines(f.get("geometry") or {}):
            s = simplify(line, False)
            if s:
                out.append(s)
    return out


def _load(twin_dir: Path, stem: str) -> list[dict]:
    p = twin_dir / f"{stem}.geojson"
    if not p.exists():
        log.warning("%s missing in %s", p.name, twin_dir)
        return []
    return json.loads(p.read_text()).get("features") or []


def _bounds(layers: dict) -> list[float]:
    lo_x = lo_y = float("inf")
    hi_x = hi_y = float("-inf")

    def take(xs: Sequence[int]) -> None:
        nonlocal lo_x, lo_y, hi_x, hi_y
        for i in range(0, len(xs), 2):
            x, y = xs[i] * QUANT, xs[i + 1] * QUANT
            lo_x, hi_x = min(lo_x, x), max(hi_x, x)
            lo_y, hi_y = min(lo_y, y), max(hi_y, y)

    for kind in SURFACE_KINDS + ("buildings",):
        for feat in layers.get(kind, []):
            for ring in feat:
                take(ring)
    for line in layers.get("curbs", []) + layers.get("roads", []):
        take(line)
    for arr in (layers.get("markings") or {}).values():
        for line in arr:
            take(line)
    for s in layers.get("signals", []):
        take([s["x"], s["y"]])
    take(layers.get("trees") or [])
    if lo_x > hi_x:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(lo_x, 2), round(lo_y, 2), round(hi_x, 2), round(hi_y, 2)]


def extract_twin(twin_dir: Path | str, name: str, carla_map: str) -> dict[str, Any]:
    """Read a ``*.twin`` directory into the compact payload the page embeds."""
    twin_dir = Path(twin_dir)
    model = json.loads((twin_dir / "model.json").read_text())

    surfaces = _load(twin_dir, "surfaces")
    layers: dict[str, Any] = {}
    for kind in SURFACE_KINDS:
        layers[kind] = polygon_features(f for f in surfaces if (f.get("properties") or {}).get("kind") == kind)
    layers["buildings"] = polygon_features(_load(twin_dir, "buildings"))
    layers["curbs"] = line_features(_load(twin_dir, "curbs"))
    layers["roads"] = line_features(_load(twin_dir, "roads"))

    marks: dict[str, list[list[int]]] = {v: [] for v in MARK_BUCKETS.values()}
    for f in _load(twin_dir, "markings"):
        p = f.get("properties") or {}
        bucket = MARK_BUCKETS.get((p.get("kind") or "solid", p.get("color") or "white"))
        if bucket is None:
            continue
        marks[bucket].extend(line_features([f]))
    layers["markings"] = marks

    signals = []
    for f in _load(twin_dir, "signals"):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        kind = p.get("kind") or "other"
        xtype, xsub = SIGNAL_XODR.get(kind, ("", ""))
        if kind == "speed_limit" and p.get("value") is not None:
            xsub = str(int(round(float(p["value"]))))
        signals.append({"x": quantize(g["coordinates"][0]), "y": quantize(g["coordinates"][1]),
                        "k": kind, "id": p.get("id") or "", "t": xtype, "s": xsub,
                        "r": p.get("road_id") or "", "c": p.get("controller_id") or ""})
    # OSM traffic-sign nodes live in objects.geojson, not signals.geojson; they are signage too.
    trees: list[int] = []
    for f in _load(twin_dir, "objects"):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        x, y = quantize(g["coordinates"][0]), quantize(g["coordinates"][1])
        if p.get("kind") == "tree":
            trees += [x, y]
        elif p.get("kind") == "traffic_sign":
            signals.append({"x": x, "y": y, "k": "traffic_sign", "id": p.get("id") or "",
                            "t": "", "s": "", "r": "", "c": ""})
    layers["signals"] = signals
    layers["trees"] = trees

    junctions = []
    for f in _load(twin_dir, "junctions"):
        p = f.get("properties") or {}
        centre = (p.get("tags") or {}).get("centre")
        if centre is None:
            rings = _rings(f.get("geometry") or {})
            pts = [pt for poly in rings for pt in poly[0]] if rings else []
            if not pts:
                continue
            centre = [sum(pt[0] for pt in pts) / len(pts), sum(pt[1] for pt in pts) / len(pts)]
        junctions.append({"id": p.get("id") or "", "x": quantize(centre[0]), "y": quantize(centre[1])})

    bounds = _bounds(layers)
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    counts = {k: (len(v) if not isinstance(v, dict) else sum(len(x) for x in v.values()))
              for k, v in layers.items()}
    counts["trees"] = len(trees) // 2
    return {
        "name": name, "carla_map": carla_map, "twin_dir": str(twin_dir),
        "origin": {"lat": model.get("origin_lat"), "lon": model.get("origin_lon")},
        "geo_reference": model.get("geo_reference", ""),
        "scale": QUANT, "bounds": bounds,
        "bounds_carla": [*model_to_carla_xy(bounds[0], bounds[3]), *model_to_carla_xy(bounds[2], bounds[1])],
        "centre": [round(cx, 2), round(cy, 2)],
        "centre_carla": [round(v, 2) for v in model_to_carla_xy(cx, cy)],
        "layers": layers, "junctions": junctions, "counts": counts,
    }


# ---------------------------------------------------------------------------------------- page

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Archivo:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">')

CSS = r"""
:root{
  --bg:#e9ebee; --panel:#fbfcfd; --panel2:#f0f2f5; --ink:#14181d; --ink2:#3d4650; --muted:#6b7480;
  --line:#d3d8de; --line2:#e3e7ec; --acc:#0f5fd0; --acc-ink:#ffffff; --acc-soft:#dbe8fb;
  --ok:#1c7a4b; --warn:#9a6108; --bad:#b3261e;
  --canvas:#dfe3e8;
  --l-ground:#c2cfb4; --l-verge:#9dc07c; --l-median:#9ab887; --l-island:#adc498; --l-parking:#aeb3bb;
  --l-drivable:#8a929c; --l-sidewalk:#e2ddd1; --l-crossing:#fbfbf7;
  --l-building:#b3b9c4; --l-building-line:#868f9c;
  --l-curb:#6f757e; --l-road:#69737f; --l-mark-white:#ffffff; --l-mark-yellow:#e0ae12;
  --grid:#aeb6bf; --grid-ink:#8b939d; --tree:#3d7a45; --jn:#5a6470;
  --sig-tl:#d62d20; --sig-ped:#7b3ff2; --sig-stop:#a3190f; --sig-yield:#c07000;
  --sig-speed:#0f5fd0; --sig-other:#57606b;
  --rg:#e2570b; --rg-fill:rgba(226,87,11,.20); --rg-done:#1c7a4b; --rg-done-fill:rgba(28,122,75,.16);
  --rg-hi:#111418; --draft:#0f5fd0; --draft-fill:rgba(15,95,208,.14);
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#101317; --panel:#171b21; --panel2:#1d222a; --ink:#e8ecf1; --ink2:#c2cad4; --muted:#8b94a0;
  --line:#2b323b; --line2:#232a33; --acc:#5b9bff; --acc-ink:#08111f; --acc-soft:#1b2a41;
  --ok:#4dbb84; --warn:#e0a340; --bad:#f0705f;
  --canvas:#0c0f13;
  --l-ground:#222c22; --l-verge:#2f4a2a; --l-median:#2c4429; --l-island:#334c30; --l-parking:#2a2f36;
  --l-drivable:#343a42; --l-sidewalk:#3b3a35; --l-crossing:#585a56;
  --l-building:#252a32; --l-building-line:#3d4550;
  --l-curb:#5a626c; --l-road:#5b6674; --l-mark-white:#cfd3cd; --l-mark-yellow:#b08c1b;
  --grid:#333b45; --grid-ink:#69727d; --tree:#4e9a58; --jn:#8b94a0;
  --sig-tl:#ff6257; --sig-ped:#a97bff; --sig-stop:#ff5a4a; --sig-yield:#e2a344;
  --sig-speed:#5b9bff; --sig-other:#8b94a0;
  --rg:#ff8a3d; --rg-fill:rgba(255,138,61,.22); --rg-done:#4dbb84; --rg-done-fill:rgba(77,187,132,.18);
  --rg-hi:#ffffff; --draft:#5b9bff; --draft-fill:rgba(91,155,255,.18);
}}
:root[data-theme="dark"]{
  --bg:#101317; --panel:#171b21; --panel2:#1d222a; --ink:#e8ecf1; --ink2:#c2cad4; --muted:#8b94a0;
  --line:#2b323b; --line2:#232a33; --acc:#5b9bff; --acc-ink:#08111f; --acc-soft:#1b2a41;
  --ok:#4dbb84; --warn:#e0a340; --bad:#f0705f;
  --canvas:#0c0f13;
  --l-ground:#222c22; --l-verge:#2f4a2a; --l-median:#2c4429; --l-island:#334c30; --l-parking:#2a2f36;
  --l-drivable:#343a42; --l-sidewalk:#3b3a35; --l-crossing:#585a56;
  --l-building:#252a32; --l-building-line:#3d4550;
  --l-curb:#5a626c; --l-road:#5b6674; --l-mark-white:#cfd3cd; --l-mark-yellow:#b08c1b;
  --grid:#333b45; --grid-ink:#69727d; --tree:#4e9a58; --jn:#8b94a0;
  --sig-tl:#ff6257; --sig-ped:#a97bff; --sig-stop:#ff5a4a; --sig-yield:#e2a344;
  --sig-speed:#5b9bff; --sig-other:#8b94a0;
  --rg:#ff8a3d; --rg-fill:rgba(255,138,61,.22); --rg-done:#4dbb84; --rg-done-fill:rgba(77,187,132,.18);
  --rg-hi:#ffffff; --draft:#5b9bff; --draft-fill:rgba(91,155,255,.18);
}
*{box-sizing:border-box}
body{margin:0;height:100vh;overflow:hidden;background:var(--bg);color:var(--ink);
  font:14px/1.45 Archivo,"Helvetica Neue",Arial,system-ui,sans-serif;
  display:grid;grid-template-columns:344px minmax(0,1fr)}
.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:3px}

aside{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;
  min-height:0;overflow:hidden}
aside > header{padding:14px 16px 12px;border-bottom:1px solid var(--line)}
h1{margin:0 0 2px;font:600 17px/1.15 Archivo,system-ui,sans-serif;letter-spacing:-.01em}
.tag{color:var(--muted);font-size:12px}
.scroll{overflow-y:auto;flex:1;min-height:0}
section{border-bottom:1px solid var(--line2);padding:12px 16px}
section > h2{margin:0 0 8px;font:600 11px/1 Archivo,system-ui,sans-serif;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
label{display:block}
select,input[type=text],textarea{width:100%;background:var(--panel2);color:var(--ink);
  border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit;font-size:13px}
textarea{resize:vertical;min-height:60px}
.seg{display:flex;gap:4px;flex-wrap:wrap}
.seg button{flex:1 1 auto;border:1px solid var(--line);background:var(--panel2);color:var(--ink2);
  border-radius:6px;padding:6px 8px;font:500 12.5px Archivo,system-ui,sans-serif;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.seg.small button{padding:4px 7px;font-size:11.5px;flex:0 1 auto}
button.link{background:none;border:0;color:var(--acc);cursor:pointer;font:inherit;font-size:12px;padding:0}
button.wide{width:100%;border:1px solid var(--line);background:var(--panel2);color:var(--ink);
  border-radius:6px;padding:7px;font:500 13px Archivo,system-ui,sans-serif;cursor:pointer}
button.wide.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
button.wide:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--muted);font-size:11.5px;margin:6px 0 0;line-height:1.4}
.layers{display:grid;gap:2px}
.layers label{display:grid;grid-template-columns:14px 12px 1fr auto;gap:8px;align-items:center;
  font-size:12.5px;padding:2px 0;cursor:pointer;color:var(--ink2)}
.layers input{margin:0;accent-color:var(--acc)}
.sw{width:12px;height:12px;border-radius:3px;border:1px solid var(--line)}
.layers .n{color:var(--muted);font-size:11px}
.rowline{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-top:8px}
input[type=range]{width:100%;margin:2px 0 0;accent-color:var(--acc)}
img.basemap{display:none}

#status{font-size:11.5px;padding:2px 8px;border-radius:999px;background:var(--panel2);color:var(--muted);
  border:1px solid var(--line)}
#status.ok{color:var(--ok);border-color:var(--ok)}
#status.bad{color:var(--bad);border-color:var(--bad)}
#banner{margin:10px 16px 0;padding:8px 10px;border:1px solid var(--warn);border-radius:6px;
  color:var(--warn);font-size:12px;background:var(--panel2)}

.rlist{display:grid;gap:6px}
.r{border:1px solid var(--line);border-radius:7px;background:var(--panel2);padding:8px 9px;cursor:pointer}
.r:hover,.r.hi{border-color:var(--acc)}
.r.sel{border-color:var(--acc);box-shadow:inset 0 0 0 1px var(--acc)}
.r.done{opacity:.72}
.r .top{display:flex;gap:6px;align-items:baseline}
.r .ttl{font-weight:600;font-size:13px;flex:1;min-width:0;overflow-wrap:anywhere}
.r .kd{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.r .meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;align-items:center}
.chip{font-size:10.5px;padding:1px 6px;border-radius:999px;background:var(--acc-soft);color:var(--acc)}
.chip.p-high{background:rgba(179,38,30,.14);color:var(--bad)}
.chip.p-low{background:var(--panel);color:var(--muted);border:1px solid var(--line)}
.chip.st{background:rgba(28,122,75,.14);color:var(--ok)}
.r .cm{font-size:12px;color:var(--ink2);margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}
.r .xy{font-size:10.5px;color:var(--muted);margin-top:4px}
.r .acts{display:flex;gap:10px;margin-top:6px;align-items:center}
.replies{margin-top:7px;border-top:1px solid var(--line);padding-top:6px;display:grid;gap:5px}
.rep{font-size:12px;color:var(--ink2);white-space:pre-wrap;overflow-wrap:anywhere}
.rep b{font:500 10px Archivo,system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;
  padding:1px 5px;border-radius:4px;background:var(--panel);border:1px solid var(--line);color:var(--muted)}
.rep.claude b{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.rep time{color:var(--muted);font-size:10.5px;margin-left:5px}
.empty{color:var(--muted);font-size:12.5px}

main{position:relative;min-width:0;min-height:0;background:var(--canvas)}
canvas{display:block;width:100%;height:100%;touch-action:none}
canvas.draw{cursor:crosshair}
.overlay{position:absolute;pointer-events:none;font-size:11.5px}
#hud{left:12px;bottom:12px;display:flex;gap:10px;align-items:flex-end}
.card{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:6px 9px;
  color:var(--ink2);box-shadow:0 1px 3px rgba(0,0,0,.10)}
#scalebar .bar{height:5px;border:1px solid var(--ink2);border-top:0;margin-top:3px}
#readout div{display:flex;gap:6px;justify-content:space-between}
#readout span:first-child{color:var(--muted)}
#tip{position:absolute;background:var(--panel);border:1px solid var(--line);border-radius:6px;
  padding:5px 8px;box-shadow:0 2px 8px rgba(0,0,0,.16);color:var(--ink);max-width:250px;z-index:3}
#drawhint{left:50%;top:12px;transform:translateX(-50%)}
#zoombox{position:absolute;right:12px;bottom:12px;display:grid;gap:4px}
#zoombox button{width:30px;height:30px;border:1px solid var(--line);background:var(--panel);
  color:var(--ink);border-radius:6px;cursor:pointer;font:500 15px Archivo,system-ui,sans-serif}

#form{position:absolute;inset:auto 0 0 0;background:var(--panel);border-top:2px solid var(--acc);
  padding:12px 16px 14px;display:grid;gap:8px;box-shadow:0 -6px 18px rgba(0,0,0,.12);z-index:4}
#form h2{margin:0;font:600 13px Archivo,system-ui,sans-serif}
#form .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}
#form .fl{font-size:11px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:3px}
aside{position:relative}

@media (max-width:860px){
  body{grid-template-columns:1fr;grid-template-rows:minmax(0,45vh) minmax(0,55vh)}
  aside{border-right:0;border-bottom:1px solid var(--line)}
}
@media (prefers-reduced-motion: no-preference){.r{transition:border-color .15s}}
"""

JS = r"""
(() => {
"use strict";
const MAPS = {}, BASE = {}, DETAIL = {};
document.querySelectorAll('script[type="application/json"][data-map]').forEach(s => {
  const d = JSON.parse(s.textContent); MAPS[d.carla_map] = d;
});
document.querySelectorAll("img[data-mapimg]").forEach(im => { BASE[im.dataset.mapimg] = im; });
// level-1 sources stay as *text*: making an Image out of one decodes 17 megapixels, so that is
// done lazily, only for the tiles on screen, and undone again when they scroll away.
document.querySelectorAll('script[type="text/plain"][data-detail]').forEach(s => {
  const cut = s.dataset.detail.indexOf("|");
  const map = s.dataset.detail.slice(0, cut), key = s.dataset.detail.slice(cut + 1);
  (DETAIL[map] = DETAIL[map] || {})[key] = s.textContent.trim();
});
const ORDER = Object.keys(MAPS);
const LS = "twin-region-review:";
const CATEGORIES = ["materials","signals","signs","geometry","buildings","vegetation","other"];
const PRIORITIES = ["low","normal","high"];
const TILE = 250;                     // baker material tile size, model metres

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
// The base layer is the real CARLA render; the vectors are overlays you switch on to check it.
const DEFAULT_ON = {image:1,ground:0,verge:0,median:0,island:0,parking:0,drivable:0,sidewalk:0,
  crossing:0,buildings:0,curbs:0,markings:0,roads:0,trees:0,signals:1,junctions:1,grid:0};
const DEFAULT_VEC_OPACITY = 70;       // percent, the overlay alpha; the base image is never faded
// Level 0 is 5 image px per metre, so it goes soft once the view passes about half that; level 1
// is 16.7 px/m and takes over there.  4 resident tiles is ~280 MB decoded, which is the ceiling.
const DETAIL_K = 2.5;                 // screen px per model metre at which level 1 takes over
const MAX_DETAIL_TILES = 4;
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
const store = {
  get(k, d){ try { const v = localStorage.getItem(LS+k); return v === null ? d : JSON.parse(v); } catch(e){ return d; } },
  set(k, v){ try { localStorage.setItem(LS+k, JSON.stringify(v)); } catch(e){} },
};

const S = {
  map: null, cx: 0, cy: 0, k: 1, on: Object.assign({}, DEFAULT_ON, store.get("layers", {})),
  op: store.get("vecopacity", DEFAULT_VEC_OPACITY), detail: 0,
  mode: null, draft: [], cursor: null, regions: new Map(), hover: null, sel: null,
  pending: null, ro: true, mouse: null,
};
let db = null, unsub = null, PAL = {};

/* ------------------------------------------------------------------ payload preparation */
function prepare(m){
  if (m._ready) return m;
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

/* ------------------------------------------------------------------------- canvas & view */
const cv = $("#cv"), g = cv.getContext("2d");
let DPR = 1, W = 0, H = 0;

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

function fit(m){
  // the data bounds, not the render's: the mosaic is padded out to whole 250 m tiles and its
  // corners are mostly the empty void outside the map
  const b = m.bounds, pad = 40;
  const w = Math.max(1, b[2]-b[0]), h = Math.max(1, b[3]-b[1]);
  S.cx = (b[0]+b[2])/2; S.cy = (b[1]+b[3])/2;
  S.k = Math.min((W-2*pad)/w, (H-2*pad)/h) || 1;
}
function zoomTo(minx, miny, maxx, maxy){
  const pad = 90;
  S.cx = (minx+maxx)/2; S.cy = (miny+maxy)/2;
  const w = Math.max(6, maxx-minx), h = Math.max(6, maxy-miny);
  S.k = Math.max(0.02, Math.min(14, Math.min((W-2*pad)/w, (H-2*pad)/h)));
  draw();
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
/* Every render is north-up over its own model-frame rectangle, so it goes through exactly the
   transform the vectors use: its top-left corner is (bounds[0], bounds[3]).  Mirrors
   `model_to_image_pixel` in the generator. */
const TCACHE = new Map();             // "Map|i,j" -> {img, used}
let FRAME = 0;

function detailImage(map, key){
  const id = map + "|" + key;
  let e = TCACHE.get(id);
  if (!e){
    const src = (DETAIL[map] || {})[key];
    if (!src) return null;
    const img = new Image();
    img.decoding = "async";
    img.addEventListener("load", () => draw());
    img.src = src;                    // this is the decode; nothing before it costs memory
    e = {img: img};
    TCACHE.set(id, e);
  }
  e.used = FRAME;
  return e.img;
}
function evictDetail(){
  if (TCACHE.size <= MAX_DETAIL_TILES) return;
  const rows = [...TCACHE.entries()].sort((a, b) => (a[1].used || 0) - (b[1].used || 0));
  for (const [id, e] of rows){
    if (TCACHE.size <= MAX_DETAIL_TILES) break;
    if (e.used === FRAME) continue;   // still on screen
    e.img.src = "";                   // drop the decoded bitmap, keep the string in the DOM
    TCACHE.delete(id);
  }
}
function drawBase(m){
  const img = BASE[S.map], meta = m.image;
  let drew = 0;
  if (img && meta && img.complete && img.naturalWidth){
    const b = meta.bounds;
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
    g.drawImage(img, sx(b[0]), sy(b[3]), (b[2]-b[0])*S.k, (b[3]-b[1])*S.k);
    drew = 1;
  }
  S.detail = 0;
  if (m.detail && S.k >= DETAIL_K){
    FRAME++;
    const vx0 = wx(0), vx1 = wx(W), vy0 = wy(H), vy1 = wy(0);
    for (const t of m.detail.tiles){
      const b = t.b;
      if (b[2] < vx0 || b[0] > vx1 || b[3] < vy0 || b[1] > vy1) continue;
      const di = detailImage(S.map, t.i + "," + t.j);
      if (di && di.complete && di.naturalWidth){
        g.drawImage(di, sx(b[0]), sy(b[3]), (b[2]-b[0])*S.k, (b[3]-b[1])*S.k);
        S.detail++;
      }
    }
    evictDetail();
  }
  return drew || S.detail > 0;
}
function draw(){
  const m = MAPS[S.map]; if (!m || !W) return;
  g.setTransform(DPR,0,0,DPR,0,0);
  g.globalAlpha = 1;
  g.fillStyle = PAL["--canvas"]; g.fillRect(0,0,W,H);
  if (S.on.image) drawBase(m);
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
  g.globalAlpha = 1;                       // regions are feedback, never faded with the overlays
  drawRegions();
  g.globalAlpha = Math.max(0.05, Math.min(1, S.op/100));
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
    g.fillStyle = PAL["--jn"]; g.font = '500 11px "JetBrains Mono",monospace';
    g.textAlign = "center"; g.textBaseline = "middle";
    for (const j of MAPS[S.map].junctions){
      const px = j.x*a+bx, py = by - j.y*a;
      if (px < 0 || px > W || py < 0 || py > H) continue;
      g.fillText(j.id, px, py);
    }
  }
  g.globalAlpha = 1;
  drawDraft();
  updateScale();
}
function drawGrid(a, bx, by){
  const t = TILE/ (MAPS[S.map].scale);   // tile size in payload units
  const x0 = Math.floor((0-bx)/a/t), x1 = Math.ceil((W-bx)/a/t);
  const y0 = Math.floor((by-H)/a/t), y1 = Math.ceil(by/a/t);
  if ((x1-x0)*(y1-y0) > 4000) return;
  g.strokeStyle = PAL["--grid"]; g.lineWidth = 1; g.setLineDash([2,4]); g.beginPath();
  for (let i=x0;i<=x1;i++){ const px = i*t*a+bx; g.moveTo(px,0); g.lineTo(px,H); }
  for (let j=y0;j<=y1;j++){ const py = by - j*t*a; g.moveTo(0,py); g.lineTo(W,py); }
  g.stroke(); g.setLineDash([]);
  if (t*a > 70){
    g.fillStyle = PAL["--grid-ink"]; g.font = '400 10px "JetBrains Mono",monospace';
    g.textAlign = "left"; g.textBaseline = "top";
    for (let i=x0;i<x1;i++) for (let j=y0;j<y1;j++){
      const px = i*t*a+bx, py = by - (j+1)*t*a;
      if (px > W || py > H || px < -80 || py < -30) continue;
      g.fillText(i + "," + j, px+4, py+4);
    }
  }
}
function regionColors(r, hi){
  const done = r.status === "done";
  return {stroke: hi ? PAL["--rg-hi"] : PAL[done ? "--rg-done" : "--rg"],
          fill: PAL[done ? "--rg-done-fill" : "--rg-fill"]};
}
function drawRegions(){
  for (const r of S.regions.values()){
    if (r.map !== S.map && r.carla_map !== S.map) continue;
    const pts = r.coords_model || []; if (!pts.length) continue;
    const hi = (S.hover === r.id || S.sel === r.id);
    const c = regionColors(r, hi);
    g.lineWidth = hi ? 3 : 2;
    g.strokeStyle = c.stroke; g.fillStyle = c.fill;
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
  if (S.mode === "polygon" && pts.length > 2) g.closePath();
  if (S.mode === "polygon" && pts.length > 2) g.fill();
  g.stroke(); g.setLineDash([]);
  g.fillStyle = PAL["--draft"];
  for (const p of S.draft){ g.beginPath(); g.rect(sx(p[0])-3, sy(p[1])-3, 6, 6); g.fill(); }
}
function updateScale(){
  const want = 130;
  let best = 1;
  for (const p of [1,2,5,10,20,50,100,200,500,1000,2000,5000]){ if (p*S.k <= want) best = p; }
  $("#scalebar .bar").style.width = (best*S.k).toFixed(1) + "px";
  $("#scalebar .lab").textContent = best >= 1000 ? (best/1000) + " km" : best + " m";
  const m = MAPS[S.map], el = $("#ro-level");
  if (!el) return;
  if (!S.on.image || !m.image) el.textContent = "off";
  else if (S.detail) el.textContent = "L1 " + Math.round(100/m.detail.px_per_m) + " cm/px ×" + S.detail;
  else el.textContent = "L0 " + Math.round(100/m.image.px_per_m) + " cm/px" +
    (m.detail ? " · zoom in for L1" : "");
}

/* ----------------------------------------------------------------------------- interaction */
let dragging = false, dragMoved = 0, lastX = 0, lastY = 0;
cv.addEventListener("pointerdown", e => {
  cv.setPointerCapture(e.pointerId);
  dragging = true; dragMoved = 0; lastX = e.clientX; lastY = e.clientY;
});
cv.addEventListener("pointermove", e => {
  const r = cv.getBoundingClientRect(), px = e.clientX-r.left, py = e.clientY-r.top;
  S.mouse = [px, py];
  if (dragging){
    dragMoved += Math.abs(e.clientX-lastX) + Math.abs(e.clientY-lastY);
    S.cx -= (e.clientX-lastX)/S.k; S.cy += (e.clientY-lastY)/S.k;
    lastX = e.clientX; lastY = e.clientY; draw();
  } else if (S.mode && S.draft.length){
    S.cursor = [wx(px), wy(py)]; draw();
  }
  readout(px, py); tooltip(px, py);
});
cv.addEventListener("pointerup", e => {
  dragging = false;
  const r = cv.getBoundingClientRect();
  if (dragMoved < 5) onClick(e.clientX-r.left, e.clientY-r.top);
});
cv.addEventListener("pointerleave", () => { $("#tip").hidden = true; S.mouse = null; });
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), px = e.clientX-r.left, py = e.clientY-r.top;
  const bx = wx(px), by = wy(py);
  const f = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0016));
  S.k = Math.max(0.02, Math.min(30, S.k*f));
  S.cx = bx - (px - W/2)/S.k; S.cy = by + (py - H/2)/S.k;
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
const round2 = v => Math.round(v*100)/100;

function hitRegion(px, py){
  const x = wx(px), y = wy(py), tol = 8/S.k;
  let best = null, bestD = Infinity;
  for (const r of S.regions.values()){
    if (r.map !== S.map && r.carla_map !== S.map) continue;
    const pts = r.coords_model || []; if (!pts.length) continue;
    if (r.kind === "point"){
      const d = Math.hypot(pts[0][0]-x, pts[0][1]-y);
      if (d < Math.max(tol, 3) && d < bestD){ best = r; bestD = d; }
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
  $("#ro-model").textContent = x.toFixed(1) + ", " + y.toFixed(1);
  $("#ro-carla").textContent = x.toFixed(1) + ", " + (-y).toFixed(1);
  $("#ro-tile").textContent = Math.floor(x/TILE) + ", " + Math.floor(y/TILE);
}
function tooltip(px, py){
  const tip = $("#tip"), m = MAPS[S.map];
  if (!S.on.signals || !m){ tip.hidden = true; return; }
  const a = m.scale*S.k, bx = W/2 - S.cx*S.k, by = H/2 + S.cy*S.k;
  let best = null, bd = 11;
  for (const s of m.layers.signals){
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
  if (S.draft.length < need){ return; }
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
  if (!S.ro && !S.mode && (e.key === "g" || e.key === "l" || e.key === "p")){
    setMode(e.key === "g" ? "polygon" : e.key === "l" ? "polyline" : "point");
  }
});

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
function centroid(pts){
  let x = 0, y = 0;
  for (const p of pts){ x += p[0]; y += p[1]; }
  return [round2(x/pts.length), round2(y/pts.length)];
}
function hex8(){
  const a = new Uint8Array(4); (crypto || window.crypto).getRandomValues(a);
  return Array.from(a, v => v.toString(16).padStart(2, "0")).join("");
}
$("#f-save").addEventListener("click", async () => {
  if (!S.pending || !db) return;
  const m = MAPS[S.map];
  const pts = S.pending.coords;
  const now = new Date().toISOString();
  const id = S.map + "-" + hex8();
  const obj = {
    id, map: m.name, carla_map: m.carla_map, kind: S.pending.kind,
    coords_model: pts, coords_carla: pts.map(p => [p[0], round2(-p[1])]),
    centroid_model: centroid(pts), centroid_carla: (c => [c[0], round2(-c[1])])(centroid(pts)),
    title: $("#f-title").value.trim() || "Untitled region",
    category: $("#f-cat").value, priority: $("#f-prio").value,
    comment: $("#f-comment").value.trim(),
    status: "open", created_at: now, updated_at: now, replies: [],
  };
  $("#f-save").disabled = true;
  try {
    await db.doc("regions/" + id).set(obj);
    S.regions.set(id, obj); S.sel = id;
    closeForm(); renderList(); draw();
  } catch(err){ setStatus("save failed: " + (err && err.code || err), "bad"); }
  $("#f-save").disabled = false;
});
$("#f-cancel").addEventListener("click", closeForm);

/* ------------------------------------------------------------------------- regions list */
function visible(){
  return Array.from(S.regions.values())
    .filter(r => r.map === MAPS[S.map].name || r.carla_map === S.map)
    .sort((a, b) => (a.status === b.status ? String(b.created_at||"").localeCompare(String(a.created_at||""))
                                           : (a.status === "done" ? 1 : -1)));
}
function renderList(){
  const list = $("#regions"), rows = visible();
  $("#rcount").textContent = rows.length ? rows.filter(r => r.status !== "done").length + " open / " + rows.length : "0";
  if (!rows.length){
    list.innerHTML = '<p class="empty">' + (S.ro
      ? "No regions loaded."
      : "Nothing yet. Pick a draw tool above, outline something on the map and describe it.") + "</p>";
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
      '<button class="link" data-act="zoom">zoom</button>' +
      (S.ro ? "" :
        '<button class="link" data-act="status">' + (r.status === "done" ? "reopen" : "mark done") + "</button>" +
        '<button class="link" data-act="reply">reply</button>' +
        '<button class="link" data-act="del">delete</button>') +
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
    try { await db.doc("regions/" + r.id).delete(); S.regions.delete(r.id); renderList(); draw(); }
    catch(err){ setStatus("delete failed: " + (err && err.code || err), "bad"); }
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
async function patch(r, fields){
  const next = Object.assign({}, r, fields, {updated_at: new Date().toISOString()});
  try { await db.doc("regions/" + r.id).set(next); S.regions.set(r.id, next); renderList(); draw(); }
  catch(err){ setStatus("update failed: " + (err && err.code || err), "bad"); }
}
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
    '<span class="sw" style="background:var(--acc)"></span><span>CARLA top-down render</span>' +
    '<span class="n" data-n="image"></span></label>';
  const box = $("#layers");
  const rows = POLY_LAYERS.map(l => [l[0], l[1], l[2]]).concat(EXTRA_LAYERS);
  box.innerHTML = rows.map(([id, label, tok]) =>
    '<label><input type="checkbox" data-layer="' + id + '"' + (S.on[id] ? " checked" : "") + '>' +
    '<span class="sw" style="background:var(' + tok + ')"></span>' +
    "<span>" + esc(label) + '</span><span class="n" data-n="' + id + '"></span></label>').join("");
  const flip = e => {
    const id = e.target.dataset.layer; if (!id) return;
    S.on[id] = e.target.checked ? 1 : 0; store.set("layers", S.on); draw();
  };
  box.addEventListener("change", flip);
  base.addEventListener("change", flip);
  const sl = $("#vecop");
  sl.value = S.op;
  $("#vecopv").textContent = S.op + "%";
  sl.addEventListener("input", () => {
    S.op = Number(sl.value); $("#vecopv").textContent = S.op + "%";
    store.set("vecopacity", S.op); draw();
  });
  $("#alloff").addEventListener("click", () => {
    for (const [id] of POLY_LAYERS.concat(EXTRA_LAYERS)) S.on[id] = 0;
    document.querySelectorAll("#layers input[data-layer]").forEach(i => { i.checked = false; });
    store.set("layers", S.on); draw();
  });
}
function refreshCounts(){
  const m = MAPS[S.map];
  for (const el of document.querySelectorAll("[data-n]")){
    const id = el.dataset.n;
    if (id === "image"){
      el.textContent = m.image ? Math.round(100/m.image.px_per_m) + " cm/px" : "none";
      continue;
    }
    const n = id === "junctions" ? m.junctions.length : (m.counts[id] != null ? m.counts[id] : "");
    el.textContent = n === "" ? "" : n;
  }
  const miss = $("#nobase");
  miss.hidden = !!(m.image && BASE[S.map]);
  const hint = $("#basehint");
  hint.innerHTML = !m.image ? "" : m.detail
    ? ("Two levels: " + Math.round(100/m.image.px_per_m) + " cm/px over the whole map, and " +
       m.detail.tiles.length + " tiles at " + Math.round(100/m.detail.px_per_m) +
       " cm/px that swap in when you zoom past the scale bar's 50 m mark.")
    : ("One level at " + Math.round(100/m.image.px_per_m) + " cm/px. The detail tiles live on " +
       "each map's own page.");
}
function setMap(name, keepView){
  S.map = name;
  if ($("#mapsel")) store.set("map", name);
  const m = prepare(MAPS[name]);
  $("#mapmeta").innerHTML =
    'origin <span class="mono">' + m.origin.lat + ", " + m.origin.lon + "</span> · " +
    Math.round(m.bounds[2]-m.bounds[0]) + " × " + Math.round(m.bounds[3]-m.bounds[1]) + " m · " +
    'level <span class="mono">' + esc(m.carla_map) + "</span>" +
    (m.image ? " · render " + m.image.size[0] + "×" + m.image.size[1] + " px @ " +
      Math.round(100/m.image.px_per_m) + " cm/px" : " · no render");
  if (!keepView) fit(m);
  refreshCounts(); setMode(null); S.sel = null; renderList(); draw();
}
// a per-map page has a static label instead of the picker
if ($("#mapsel")) $("#mapsel").addEventListener("change", e => setMap(e.target.value));
$("#tools").addEventListener("click", e => {
  const b = e.target.closest("button[data-mode]"); if (!b || S.ro) return;
  setMode(S.mode === b.dataset.mode ? null : b.dataset.mode);
});
$("#fitbtn").addEventListener("click", () => { fit(MAPS[S.map]); draw(); });
$("#zin").addEventListener("click", () => { S.k = Math.min(30, S.k*1.4); draw(); });
$("#zout").addEventListener("click", () => { S.k = Math.max(0.02, S.k/1.4); draw(); });
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
$("#export").addEventListener("click", async () => {
  const text = JSON.stringify(visible(), null, 2);
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch(e){
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch(e2){}
    ta.remove();
  }
  $("#export").textContent = ok ? "Copied ✓" : "Copy failed — see console";
  if (!ok) console.log(text);
  setTimeout(() => { $("#export").textContent = "Copy regions as JSON"; }, 2200);
});

/* -------------------------------------------------------------------------------- boot */
function applyStoredTheme(){
  const v = store.get("theme", "auto");
  if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
  document.querySelectorAll("#theme button").forEach(x =>
    x.setAttribute("aria-pressed", String(x.dataset.theme === v)));
}
function setReadOnly(on, why){
  S.ro = on;
  $("#banner").hidden = !on;
  if (on) $("#banner").textContent = why;
  document.querySelectorAll("#tools button").forEach(b => { b.disabled = on; });
  if (on) setMode(null);
  renderList();
}
applyStoredTheme();
readPalette();
buildLayerUI();
$("#f-cat").innerHTML = CATEGORIES.map(c => '<option value="' + c + '">' + c + "</option>").join("");
$("#f-prio").innerHTML = PRIORITIES.map(c => '<option value="' + c + '"' +
  (c === "normal" ? " selected" : "") + ">" + c + "</option>").join("");
let startMap = ORDER[0];
if ($("#mapsel")){
  $("#mapsel").innerHTML = ORDER.map(k =>
    '<option value="' + esc(k) + '">' + esc(MAPS[k].name) + "</option>").join("");
  const saved = store.get("map", null);
  $("#mapsel").value = ORDER.includes(saved) ? saved : ORDER[0];
  startMap = $("#mapsel").value;
}
// read-only view of the pyramid's state, so the headless check can assert what is decoded
window.twinReviewState = () => ({map: S.map, k: S.k, detailDrawn: S.detail,
  detailCached: TCACHE.size, detailAvailable: Object.keys(DETAIL[S.map] || {}).length,
  maxDetail: MAX_DETAIL_TILES, layers: Object.assign({}, S.on)});
Object.values(BASE).forEach(im => im.addEventListener("load", () => draw()));
new ResizeObserver(resize).observe(cv);
window.addEventListener("resize", resize);
if (window.matchMedia) try {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readPalette(); draw(); });
} catch(e){}
resize();
setMap(startMap);
setReadOnly(true, "Connecting to the shared store… drawing is disabled until it answers.");
setStatus("connecting…", "");

(async () => {
  try { db = await claude.use("db"); } catch(e){ db = null; }
  if (!db){
    setStatus("read-only", "bad");
    setReadOnly(true, "The shared store is unavailable in this view, so regions cannot be drawn or saved. " +
      "Existing regions cannot be loaded either.");
    return;
  }
  setStatus("connected", "ok");
  setReadOnly(false, "");
  try {
    unsub = db.collection("regions").onSnapshot(snap => {
      S.regions = new Map();
      snap.docs.forEach(d => { if (d.exists){ const v = d.data(); if (v && v.id) S.regions.set(v.id, v); } });
      renderList(); draw();
    }, err => {
      setStatus("live updates stopped: " + (err && err.code || err), "bad");
    });
  } catch(err){
    setStatus("could not subscribe: " + (err && err.code || err), "bad");
    try {
      const snap = await db.collection("regions").get();
      snap.docs.forEach(d => { const v = d.data(); if (v && v.id) S.regions.set(v.id, v); });
      renderList(); draw();
    } catch(e2){}
  }
})();
})();
"""

BODY = """
<aside>
  <header>
    <h1>Twin Region Review</h1>
    <div class="tag">Draw over the CARLA top-down render, leave notes for Claude.</div>
    <div class="rowline"><span id="status">connecting…</span>
      <span class="seg small" id="theme">
        <button type="button" data-theme="auto" aria-pressed="true">Auto</button>
        <button type="button" data-theme="light" aria-pressed="false">Light</button>
        <button type="button" data-theme="dark" aria-pressed="false">Dark</button>
      </span></div>
  </header>
  <p id="banner" hidden></p>
  <div class="scroll">
    <section>
      <h2>Map</h2>
      {{PICKER}}
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
      <p class="hint" id="basehint"></p>
      <p class="hint" id="nobase" hidden>No render captured for this map — run
        <span class="mono">tools/carla_topview.py</span> against a running server and rebuild.</p>
    </section>
    <section>
      <h2>Vector overlays <button type="button" class="link" id="alloff">all off</button></h2>
      <div class="layers" id="layers"></div>
      <div class="rowline"><label for="vecop" class="hint" style="margin:0">Overlay opacity</label>
        <span class="mono tag" id="vecopv">70%</span></div>
      <input type="range" id="vecop" min="5" max="100" step="5" value="70"
             aria-label="Vector overlay opacity">
      <p class="hint">The overlays are the twin model's own geometry. Fade them over the render to
        check what CARLA actually drew.</p>
    </section>
    <section>
      <h2>Regions <span id="rcount" class="tag"></span></h2>
      <div class="rlist" id="regions"></div>
      <div class="rowline"><button type="button" class="wide" id="export">Copy regions as JSON</button></div>
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
      <div><span>render</span><span id="ro-level">–</span></div>
    </div>
  </div>
  <div id="zoombox">
    <button type="button" id="zin" title="Zoom in" aria-label="Zoom in">+</button>
    <button type="button" id="zout" title="Zoom out" aria-label="Zoom out">−</button>
  </div>
</main>
"""


def build_page(maps: Sequence[dict], images: dict[str, str] | None = None,
               details: dict[str, dict[str, str]] | None = None, single: bool = False) -> str:
    """``maps`` are :func:`extract_twin` payloads (each may carry ``image``/``detail`` meta blocks);
    ``images`` maps the CARLA level name to the level-0 data URI and ``details`` maps it to
    ``{"i,j": level-1 data URI}``.  ``single`` swaps the map picker for a static label."""
    images = images or {}
    details = details or {}
    one = maps[0] if (single and len(maps) == 1) else None
    picker = ('<p class="mono" id="maplabel">%s</p>' % _esc(one["name"])) if one else \
        '<label><select id="mapsel"></select></label>'
    blocks = []
    for m in maps:
        src = images.get(m["carla_map"])
        if src:
            blocks.append('<img class="basemap" data-mapimg="%s" alt="" src="%s">'
                          % (m["carla_map"], src))
        blocks.append('<script type="application/json" data-map="%s">%s</script>'
                      % (m["carla_map"], json.dumps(m, separators=(",", ":"))))
        for key, dsrc in (details.get(m["carla_map"]) or {}).items():
            blocks.append('<script type="text/plain" data-detail="%s|%s">%s</script>'
                          % (m["carla_map"], key, dsrc))
    title = ("%s Twin Review" % one["name"]) if one else "Twin Region Review"
    return "\n".join([
        "<title>%s</title>" % _esc(title),
        FONTS,
        "<style>%s</style>" % CSS,
        BODY.replace("{{PICKER}}", picker),
        *blocks,
        "<script>%s</script>" % JS,
    ])


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="out/review/region_review.html")
    ap.add_argument("--map", action="append", metavar="NAME=LEVEL=DIR", default=[],
                    help="override / add a map (repeatable); default is the three baked twins")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="TwinModel root that relative twin dirs are resolved against")
    ap.add_argument("--images", default=None, metavar="DIR",
                    help="directory with carla_topview.py output (default: the page's own "
                         "output directory); pass --no-images to build the vector-only page")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-per-map", action="store_true",
                    help="skip the per-map region_review_<Level>.html pages (detail tiles)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    root = Path(args.root)
    specs = list(DEFAULT_MAPS)
    if args.map:
        specs = []
        for s in args.map:
            parts = s.split("=")
            if len(parts) != 3:
                ap.error("--map wants NAME=LEVEL=DIR, got %r" % s)
            specs.append((parts[0], parts[1], parts[2]))

    out = Path(args.out)
    if not out.is_absolute():
        out = root / args.out
    img_dir = None
    if not args.no_images:
        img_dir = Path(args.images) if args.images else out.parent
        if not img_dir.is_absolute():
            img_dir = root / img_dir

    maps, images, details, report = [], {}, {}, []
    for name, level, rel in specs:
        d = Path(rel)
        if not d.is_absolute():
            d = root / rel
        if not d.exists():
            log.warning("skipping %s: %s missing", name, d)
            continue
        m = extract_twin(d, name, level)
        meta, src = load_image(img_dir, level) if img_dir else (None, None)
        if meta:
            m["image"] = meta
            images[level] = src
        else:
            log.warning("%s: no topview_%s.jpg/.json in %s -- vector-only", name, level, img_dir)
        dmeta, dsrcs = load_detail(img_dir, level) if img_dir else (None, {})
        if dmeta:
            m["detail"] = dmeta
            details[level] = dsrcs
        size = len(json.dumps(m, separators=(",", ":")).encode())
        report.append((name, level, d, size, meta["bytes"] if meta else 0,
                       dmeta["bytes"] if dmeta else 0, len(dsrcs), m["counts"]))
        maps.append(m)
    if not maps:
        ap.error("no twin directories found")

    out.parent.mkdir(parents=True, exist_ok=True)
    # the combined page is the overview: every map, level 0 only.  Three maps' worth of level-1
    # tiles in one page would be ~25 MB, so the detail lives on the per-map pages.
    overview = [{k: v for k, v in m.items() if k != "detail"} for m in maps]
    out.write_text(build_page(overview, images))
    written = [(out.name, "all maps, level 0", out.stat().st_size)]

    if not args.no_per_map:
        for m in maps:
            p = out.parent / ("%s_%s.html" % (out.stem, m["carla_map"]))
            p.write_text(build_page([m], {m["carla_map"]: images.get(m["carla_map"])}
                                    if images.get(m["carla_map"]) else {},
                                    {m["carla_map"]: details.get(m["carla_map"]) or {}},
                                    single=True))
            written.append((p.name, "%s, level 0+1" % m["name"], p.stat().st_size))

    for name, level, d, size, img_bytes, det_bytes, det_n, counts in report:
        log.info("%-14s %6.0f kB json  %6.0f kB L0  %6.0f kB L1 (%d tiles)  %s",
                 name, size / 1e3, img_bytes / 1e3, det_bytes / 1e3, det_n,
                 " ".join("%s=%s" % kv for kv in sorted(counts.items())))
    for fname, what, nbytes in written:
        flag = "  OVER BUDGET" if nbytes > 12_000_000 else ""
        log.info("wrote %-34s %-24s %5.2f MB%s", fname, what, nbytes / 1e6, flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
