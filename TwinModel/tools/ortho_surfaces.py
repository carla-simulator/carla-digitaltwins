#!/usr/bin/env python
"""SAM prompted by the twin's own surfaces -> real surface masks from the ortho, and the kerb error.

    python tools/ortho_surfaces.py out/v10_eixample eixample                 # whole bbox, ICGC 10 cm
    python tools/ortho_surfaces.py out/v10_eixample eixample --window -30 30 90 150 --preview

Idea: the twin already claims where sidewalks, crossings and carriageway are (``surfaces.geojson``).
Those claims are wrong by a metre here and there, but they are excellent *prompts*: sample a few
points well inside each claimed polygon, a few negative points in the neighbouring surface of a
different kind, and let SAM return the real region from the 10 cm imagery.  The SAM boundary is
the real kerb; the twin boundary is the claimed kerb; their distance is the correction we need.

Per 1024 px crop (SAM's native size, 102 m at 10 cm) the image is embedded once, then every twin
surface piece intersecting the crop core is prompted (``--pos`` positive / ``--neg`` negative
points, plus a box around the claim).  Each of SAM's three proposals is clipped to the corridor
and the one with the highest IoU against the claim is kept.  Masks are cleaned (component
touching the positive points, small holes filled), polygonised, clipped to the crop core and
unioned per twin surface id.  Outputs in ``<build_dir>/detect/``:

    surfaces_sam.geojson   one feature per twin surface (kind, twin id) with the SAM geometry and
                           iou / area_ratio / boundary offset stats (metres) against the twin
    curb_offsets.geojson   the twin kerb lines with signed offset to the SAM sidewalk edge, sampled
                           every metre: offset_med, offset_p90, frac_inside (kerb point inside the
                           real sidewalk = twin sidewalk too narrow there)
    surfaces_summary.json  per-kind IoU / offset distributions, worst surfaces, run parameters
    surfaces_preview.png   (--preview) twin outline (yellow) vs SAM fill (per kind) on the ortho

``tools/geo_overlay.py`` picks the two GeoJSONs up automatically as layers.

Verdict on EixampleDemo (2026-09-03): crossings come out at IoU ~0.7 (paint), sidewalks do not
(IoU ~0.35, fragments): a 45 px strip half hidden by tree crowns and toned like the roofs next to
it is not what SAM was built for.  Use ``tools/kerb_edges.py`` or Tile2Net for kerbs.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twinmodel.frame import LocalFrame               # noqa: E402
from twinmodel.ingest.imagery import fetch_ortho     # noqa: E402

log = logging.getLogger("ortho_surfaces")

SAM_ID = "facebook/sam-vit-huge"
KINDS = ("sidewalk", "crossing", "drivable", "median", "island", "parking", "verge")
CROP = 1024
OVERLAP = 192
MIN_PIECE_M2 = 4.0
CORRIDOR_M = 3.0          # SAM output is clipped to the twin claim grown by this much: a refiner, not a free segmenter


# ------------------------------------------------------------------------------------ prompts

def sample_inside(poly, n: int, rng: np.random.Generator, shrink_m: float):
    """``n`` points well inside ``poly`` (negative buffer first, falls back to the raw polygon)."""
    from shapely.geometry import Point
    core = poly.buffer(-shrink_m)
    if core.is_empty or core.area < 0.5:
        core = poly.buffer(-min(shrink_m, 0.3))
    if core.is_empty:
        core = poly
    pts = [core.representative_point()]
    xmin, ymin, xmax, ymax = core.bounds
    tries = 0
    while len(pts) < n and tries < 400:
        tries += 1
        p = Point(rng.uniform(xmin, xmax), rng.uniform(ymin, ymax))
        if core.contains(p) and all(p.distance(q) > 0.8 for q in pts):
            pts.append(p)
    while len(pts) < n:                       # tiny piece: repeat what we have
        pts.append(pts[len(pts) % max(1, len(pts))])
    return [(p.x, p.y) for p in pts[:n]]


def neighbours_of(piece, kind: str, pieces: list[tuple[str, str, Any]], reach_m: float):
    """Pieces of another kind within ``reach_m``."""
    ring = piece.buffer(reach_m)
    return [g for k, _sid, g in pieces if k != kind and g.intersects(ring)]


# ------------------------------------------------------------------------------------ model

class Sam:
    def __init__(self, sam_id: str = SAM_ID, device: str | None = None):
        import torch
        from transformers import SamModel, SamProcessor
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        t = time.time()
        self.proc = SamProcessor.from_pretrained(sam_id)
        self.model = SamModel.from_pretrained(sam_id).to(self.device).eval()
        log.info("sam %s on %s (%.1fs)", sam_id, self.device, time.time() - t)

    def embed(self, image):
        inputs = self.proc(image, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            emb = self.model.get_image_embeddings(inputs["pixel_values"])
        return emb, inputs

    def masks(self, image, emb, base_inputs, points: list[list[tuple[float, float]]], labels: list[list[int]],
              boxes: list[list[float]] | None = None):
        """points/labels: one list per prompt (same length within a call); boxes: one xyxy per prompt.
        -> (masks (P,3,H,W) bool, iou (P,3))."""
        kw = dict(input_points=[points], input_labels=[labels])
        if boxes:
            kw["input_boxes"] = [boxes]
        inputs = self.proc(image, return_tensors="pt", **kw).to(self.device)
        inputs.pop("pixel_values", None)
        inputs["image_embeddings"] = emb
        with self.torch.no_grad():
            out = self.model(**inputs, multimask_output=True)
        masks = self.proc.image_processor.post_process_masks(
            out.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu())[0]
        return masks.numpy().astype(bool), out.iou_scores[0].cpu().numpy()


# ------------------------------------------------------------------------------------ geometry

def clean_mask(mask: np.ndarray, seeds_px: Sequence[tuple[int, int]], hole_px: int) -> np.ndarray:
    """Keep the connected component(s) under the positive seeds, fill holes smaller than ``hole_px``."""
    from scipy import ndimage
    lab, n = ndimage.label(mask)
    if n == 0:
        return mask
    keep = set()
    for c, r in seeds_px:
        if 0 <= r < lab.shape[0] and 0 <= c < lab.shape[1] and lab[r, c] > 0:
            keep.add(int(lab[r, c]))
    if not keep:                              # seeds fell off the mask: keep the largest component
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        keep = {int(np.argmax(sizes)) + 1}
    out = np.isin(lab, list(keep))
    holes, nh = ndimage.label(~out)
    if nh:
        sizes = ndimage.sum(~out, holes, range(1, nh + 1))
        small = [i + 1 for i, s in enumerate(sizes) if s < hole_px]
        # never fill the background component touching the border
        border = set(np.unique(np.concatenate([holes[0], holes[-1], holes[:, 0], holes[:, -1]])))
        small = [i for i in small if i not in border]
        if small:
            out |= np.isin(holes, small)
    return out


def rasterize_geom(geom, aff, shape_hw) -> np.ndarray:
    from rasterio import features
    return features.rasterize([(geom, 1)], out_shape=shape_hw, transform=aff, fill=0, dtype="uint8", all_touched=True).astype(bool)


def mask_polygon(mask: np.ndarray, aff, simplify_m: float = 0.08):
    from rasterio import features
    from shapely.geometry import shape
    from shapely.ops import unary_union
    polys = [shape(g) for g, v in features.shapes(mask.astype(np.uint8), mask=mask, transform=aff) if v == 1]
    if not polys:
        return None
    u = unary_union(polys).simplify(simplify_m, preserve_topology=True)
    return None if u.is_empty else u


def boundary_offsets(sam_geom, twin_geom, step_m: float = 1.0) -> dict[str, float]:
    """Distances from points along the SAM boundary to the twin boundary."""
    b = sam_geom.boundary
    n = max(4, int(b.length / step_m))
    d = np.array([b.interpolate(i / n, normalized=True).distance(twin_geom.boundary) for i in range(n)])
    return {"off_med": round(float(np.median(d)), 2), "off_p90": round(float(np.percentile(d, 90)), 2),
            "off_max": round(float(d.max()), 2)}


# ------------------------------------------------------------------------------------ run

def run(build_dir: Path, name: str, *, layer: str, res: float, kinds: Sequence[str], n_pos: int, n_neg: int,
        window: Sequence[float] | None, preview: bool, out_dir: Path | None, data_dir: Path, sam_id: str,
        device: str | None, seed: int) -> Path:
    from PIL import Image
    from rasterio.transform import Affine
    from shapely.geometry import box as shp_box, mapping, shape
    from shapely.ops import unary_union

    rng = np.random.default_rng(seed)
    twin_dir = build_dir / f"{name}.twin"
    model = json.loads((twin_dir / "model.json").read_text())
    frame = LocalFrame(model["origin_lat"], model["origin_lon"])
    bbox = tuple(float(v) for v in model["bbox_wgs84"])
    out_dir = out_dir or (build_dir / "detect")
    out_dir.mkdir(parents=True, exist_ok=True)

    img = fetch_ortho(frame, bbox, resolution=res, sources=("icgc", "ign_es", "naip"), layer=layer, cache_dir=data_dir)
    if img is None:
        raise SystemExit("no orthophoto available")
    north_up = np.ascontiguousarray(img.array[::-1])
    xmin, ymin, xmax, ymax = img.bounds()
    dx = img.dx
    log.info("ortho %dx%d @ %.2f m  %s", img.width, img.height, dx, img.label())

    surfaces = [(f["properties"]["kind"], f["properties"]["id"], shape(f["geometry"]))
                for f in json.loads((twin_dir / "surfaces.geojson").read_text())["features"]
                if f["properties"]["kind"] in kinds]
    curbs = [(f["properties"], shape(f["geometry"])) for f in json.loads((twin_dir / "curbs.geojson").read_text())["features"]]
    # extra negative sources: what is never the surface we ask for
    negatives = [("building", f["properties"].get("id", ""), shape(f["geometry"]))
                 for f in json.loads((twin_dir / "buildings.geojson").read_text())["features"]] if (twin_dir / "buildings.geojson").exists() else []
    negatives += [(f["properties"]["kind"], f["properties"]["id"], shape(f["geometry"]))
                  for f in json.loads((twin_dir / "surfaces.geojson").read_text())["features"]
                  if f["properties"]["kind"] not in kinds]
    log.info("twin: %d surfaces of kinds %s, %d kerb lines", len(surfaces), sorted({k for k, _, _ in surfaces}), len(curbs))

    region = shp_box(xmin, ymin, xmax, ymax)
    if window:
        region = shp_box(*window).intersection(region)
    rx0, ry0, rx1, ry1 = region.bounds

    sam = Sam(sam_id, device)
    per_surface: dict[str, list] = {}
    pieces_done = 0
    t0 = time.time()
    core_margin = OVERLAP // 2
    px_x0, px_x1 = int((rx0 - xmin) / dx), int(np.ceil((rx1 - xmin) / dx))
    px_y0, px_y1 = int((ymax - ry1) / dx), int(np.ceil((ymax - ry0) / dx))
    step = CROP - OVERLAP
    ys = list(range(px_y0, max(px_y0 + 1, px_y1 - OVERLAP), step))
    xs = list(range(px_x0, max(px_x0 + 1, px_x1 - OVERLAP), step))
    n_crops = len(xs) * len(ys)
    ci = 0
    for cy in ys:
        for cx in xs:
            ci += 1
            cx0, cy0 = min(cx, max(0, img.width - CROP)), min(cy, max(0, img.height - CROP))
            cx1, cy1 = min(img.width, cx0 + CROP), min(img.height, cy0 + CROP)
            tile = north_up[cy0:cy1, cx0:cx1]
            if tile.size == 0:
                continue
            aff = Affine(dx, 0.0, xmin + cx0 * dx, 0.0, -dx, ymax - cy0 * dx)
            crop_geom = shp_box(xmin + cx0 * dx, ymax - cy1 * dx, xmin + cx1 * dx, ymax - cy0 * dx)
            # core: where this crop is authoritative (interior crops shrink by the half-overlap)
            core = shp_box(xmin + (cx0 + (core_margin if cx0 > 0 else 0)) * dx,
                           ymax - (cy1 - (core_margin if cy1 < img.height else 0)) * dx,
                           xmin + (cx1 - (core_margin if cx1 < img.width else 0)) * dx,
                           ymax - (cy0 + (core_margin if cy0 > 0 else 0)) * dx).intersection(region)
            if core.is_empty:
                continue
            pieces = []
            for kind, sid, g in surfaces:
                if not g.intersects(core):
                    continue
                piece = g.intersection(crop_geom)
                if piece.is_empty:
                    continue
                geoms = list(piece.geoms) if piece.geom_type.startswith("Multi") or piece.geom_type == "GeometryCollection" else [piece]
                for pg in geoms:
                    if pg.geom_type == "Polygon" and pg.area >= MIN_PIECE_M2 and pg.intersection(core).area > 0.5:
                        pieces.append((kind, sid, pg))
            if not pieces:
                continue
            pil = Image.fromarray(tile)
            emb, base_inputs = sam.embed(pil)
            # build prompts
            prompts, labels, boxes, meta = [], [], [], []
            neg_local = [(k, i, g.intersection(crop_geom)) for k, i, g in negatives if g.intersects(crop_geom)]
            neg_local = [(k, i, g) for k, i, g in neg_local if not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon")]
            for kind, sid, pg in pieces:
                shrink = 0.9 if kind in ("sidewalk", "drivable") else 0.4
                pos = sample_inside(pg, n_pos, rng, shrink)
                negs: list[tuple[float, float]] = []
                nbs = neighbours_of(pg, kind, pieces, 6.0) + neighbours_of(pg, kind, neg_local, 6.0)
                nbs.sort(key=lambda g: g.distance(pg))
                for nb in nbs:
                    nbp = nb if nb.geom_type == "Polygon" else max(nb.geoms, key=lambda g: g.area)
                    negs.extend(sample_inside(nbp, 1, rng, 0.5))
                    if len(negs) >= n_neg:
                        break
                if not negs:                                  # fall back: points just outside the piece
                    ring = pg.buffer(2.0).difference(pg.buffer(0.8))
                    negs = sample_inside(ring, n_neg, rng, 0.0) if not ring.is_empty else []
                negs = (negs * n_neg)[:n_neg] if negs else []
                pts = pos + negs
                lbl = [1] * len(pos) + [0] * len(negs)
                # to crop pixels (x right, y down)
                pxs = [(float((x - aff.c) / aff.a), float((y - aff.f) / aff.e)) for x, y in pts]
                bx0, by0, bx1, by1 = pg.buffer(CORRIDOR_M * 0.5).bounds        # box prompt: the claim, slightly grown
                boxes.append([float((bx0 - aff.c) / aff.a), float((by1 - aff.f) / aff.e),
                              float((bx1 - aff.c) / aff.a), float((by0 - aff.f) / aff.e)])
                prompts.append(pxs); labels.append(lbl); meta.append((kind, sid, pg, pos))
            # equalise prompt lengths (processor wants a rectangular batch)
            L = max(len(p) for p in prompts)
            for p, l in zip(prompts, labels):
                while len(p) < L:
                    p.append(p[0]); l.append(l[0])
            B = 24
            for i in range(0, len(prompts), B):
                masks, ious = sam.masks(pil, emb, base_inputs, prompts[i:i + B], labels[i:i + B], boxes[i:i + B])
                for k in range(masks.shape[0]):
                    kind, sid, pg, pos = meta[i + k]
                    corridor = pg.buffer(CORRIDOR_M)
                    corr_mask = rasterize_geom(corridor, aff, tile.shape[:2])
                    claim_mask = rasterize_geom(pg, aff, tile.shape[:2])
                    best, best_score = None, -1.0
                    for j in range(masks.shape[1]):
                        m = masks[k, j] & corr_mask
                        inter = np.count_nonzero(m & claim_mask); union = np.count_nonzero(m | claim_mask)
                        score = inter / union if union else 0.0
                        if score > best_score:
                            best, best_score = m, score
                    seeds = [(int((x - aff.c) / aff.a), int((y - aff.f) / aff.e)) for x, y in pos]
                    chosen = clean_mask(best, seeds, hole_px=int(2.0 / (dx * dx)))
                    poly = mask_polygon(chosen, aff)
                    if poly is None:
                        continue
                    poly = poly.intersection(core)
                    if poly.is_empty:
                        continue
                    per_surface.setdefault(sid, []).append((kind, poly))
                    pieces_done += 1
            log.info("crop %d/%d: %d pieces (%.0fs)", ci, n_crops, len(pieces), time.time() - t0)

    # ---- assemble per surface
    twin_by_id = {sid: (kind, g) for kind, sid, g in surfaces}
    feats = []
    stats: dict[str, list] = {}
    sam_by_kind: dict[str, list] = {}
    for sid, parts in per_surface.items():
        kind, tg = twin_by_id[sid]
        sg = unary_union([p for _, p in parts]).buffer(0)
        tgr = tg.intersection(region)
        if sg.is_empty or tgr.is_empty:
            continue
        inter = sg.intersection(tgr).area
        union = sg.union(tgr).area
        iou = inter / union if union > 0 else 0.0
        offs = boundary_offsets(sg, tgr)
        props = {"twin_id": sid, "kind": kind, "iou": round(iou, 3), "area_twin": round(tgr.area, 1),
                 "area_sam": round(sg.area, 1), "area_ratio": round(sg.area / max(tgr.area, 1e-6), 2), **offs}
        feats.append({"type": "Feature", "properties": props, "geometry": mapping(sg)})
        stats.setdefault(kind, []).append(props)
        sam_by_kind.setdefault(kind, []).append(sg)
    (out_dir / "surfaces_sam.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": feats,
        "ortho": {"source": img.source, "detail": img.detail, "res_m": dx},
        "params": {"sam": sam_id, "pos": n_pos, "neg": n_neg, "crop": CROP, "overlap": OVERLAP, "kinds": list(kinds)}},
        separators=(",", ":")))

    # ---- kerb offsets: twin kerb vs real sidewalk edge
    curb_feats = []
    sidewalks_sam = unary_union(sam_by_kind.get("sidewalk", [])) if sam_by_kind.get("sidewalk") else None
    curb_rows = []
    if sidewalks_sam is not None and not sidewalks_sam.is_empty:
        sw_boundary = sidewalks_sam.boundary
        for props, line in curbs:
            if not line.intersects(region):
                continue
            n = max(2, int(line.length))
            samples = [line.interpolate(i / n, normalized=True) for i in range(n + 1)]
            samples = [p for p in samples if region.contains(p)]
            if len(samples) < 2:
                continue
            d = np.array([p.distance(sw_boundary) for p in samples])
            inside = np.array([sidewalks_sam.contains(p) for p in samples])
            signed = np.where(inside, d, -d)               # + : twin kerb lies inside the real sidewalk
            row = {**{k: v for k, v in props.items() if k in ("id", "height", "low_side_kind", "high_side_kind")},
                   "n": int(len(samples)), "offset_med": round(float(np.median(signed)), 2),
                   "offset_abs_med": round(float(np.median(d)), 2), "offset_p90": round(float(np.percentile(d, 90)), 2),
                   "frac_inside": round(float(inside.mean()), 2)}
            curb_rows.append(row)
            curb_feats.append({"type": "Feature", "properties": row, "geometry": mapping(line)})
    (out_dir / "curb_offsets.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": curb_feats}, separators=(",", ":")))

    # ---- summary
    def dist(vals):
        a = np.array(vals, dtype=float)
        return {"n": int(a.size), "med": round(float(np.median(a)), 2), "p10": round(float(np.percentile(a, 10)), 2),
                "p90": round(float(np.percentile(a, 90)), 2)} if a.size else {"n": 0}
    summary: dict[str, Any] = {"pieces": pieces_done, "surfaces": len(feats), "seconds": round(time.time() - t0, 1),
                               "per_kind": {}, "curbs": {}}
    for kind, rows in stats.items():
        summary["per_kind"][kind] = {"iou": dist([r["iou"] for r in rows]), "off_med_m": dist([r["off_med"] for r in rows]),
                                     "off_p90_m": dist([r["off_p90"] for r in rows]),
                                     "area_ratio": dist([r["area_ratio"] for r in rows]),
                                     "worst_iou": sorted(rows, key=lambda r: r["iou"])[:5]}
    if curb_rows:
        summary["curbs"] = {"offset_abs_med_m": dist([r["offset_abs_med"] for r in curb_rows]),
                            "offset_signed_med_m": dist([r["offset_med"] for r in curb_rows]),
                            "offset_p90_m": dist([r["offset_p90"] for r in curb_rows]),
                            "frac_inside": dist([r["frac_inside"] for r in curb_rows]),
                            "worst": sorted(curb_rows, key=lambda r: -r["offset_abs_med"])[:8]}
    summary.update({"ortho": {"detail": img.detail, "res_m": dx}, "params": {"sam": sam_id, "pos": n_pos, "neg": n_neg}})
    (out_dir / "surfaces_summary.json").write_text(json.dumps(summary, indent=1))
    log.info("summary: %s", json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "worst_iou"} for k, v in summary["per_kind"].items()}))
    if curb_rows:
        log.info("kerbs: %s", json.dumps({k: v for k, v in summary["curbs"].items() if k != "worst"}))

    if preview:
        _preview(north_up, dx, xmin, ymax, region, feats, surfaces, out_dir / "surfaces_preview.png")
    return out_dir


def _preview(north_up, dx, xmin, ymax, region, feats, surfaces, path: Path) -> None:
    from PIL import Image, ImageDraw
    from shapely.geometry import shape
    rx0, ry0, rx1, ry1 = region.bounds
    c0, r0 = int((rx0 - xmin) / dx), int((ymax - ry1) / dx)
    c1, r1 = int(np.ceil((rx1 - xmin) / dx)), int(np.ceil((ry0 * -1 + ymax) / dx))
    sub = north_up[r0:r1, c0:c1]
    scale = min(1.0, 3000 / max(sub.shape[:2]))
    im = Image.fromarray(sub).convert("RGBA")
    if scale < 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.BILINEAR)
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0)); d = ImageDraw.Draw(ov)
    KC = {"sidewalk": (60, 200, 255), "crossing": (255, 255, 255), "drivable": (255, 80, 80), "median": (120, 255, 120),
          "island": (120, 255, 120), "parking": (255, 200, 60), "verge": (60, 255, 60)}

    def px(x, y):
        return ((x - rx0) / dx * scale, (ry1 - y) / dx * scale)

    def draw(geom, fill, outline, w):
        polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
        for pg in polys:
            d.polygon([px(x, y) for x, y, *_ in pg.exterior.coords], fill=fill, outline=outline, width=w)
            for ring in pg.interiors:
                d.polygon([px(x, y) for x, y, *_ in ring.coords], fill=(0, 0, 0, 0), outline=outline, width=w)
    for f in feats:
        c = KC.get(f["properties"]["kind"], (200, 200, 200))
        draw(shape(f["geometry"]), c + (70,), c + (230,), 2)
    for kind, sid, g in surfaces:
        if g.intersects(region):
            draw(g.intersection(region), (0, 0, 0, 0), (255, 230, 0, 255), 2)
    im = Image.alpha_composite(im, ov).convert("RGB")
    im.save(path)
    log.info("preview %s (%dx%d)", path, im.width, im.height)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir"); ap.add_argument("name")
    ap.add_argument("--layer", default="ortofoto_10cm_color_2020"); ap.add_argument("--res", type=float, default=0.10)
    ap.add_argument("--kinds", nargs="+", default=["sidewalk", "crossing", "drivable"])
    ap.add_argument("--pos", type=int, default=4, help="positive prompt points per piece")
    ap.add_argument("--neg", type=int, default=4, help="negative prompt points (in neighbouring surfaces of another kind)")
    ap.add_argument("--window", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--preview", action="store_true"); ap.add_argument("--out")
    ap.add_argument("--data", default="data"); ap.add_argument("--device"); ap.add_argument("--sam", default=SAM_ID)
    ap.add_argument("--seed", type=int, default=7); ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(name)s %(message)s")
    for noisy in ("urllib3", "rasterio", "transformers", "huggingface_hub", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = run(Path(a.build_dir), a.name, layer=a.layer, res=a.res, kinds=a.kinds, n_pos=a.pos, n_neg=a.neg, window=a.window,
              preview=a.preview, out_dir=Path(a.out) if a.out else None, data_dir=Path(a.data), sam_id=a.sam, device=a.device,
              seed=a.seed)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
