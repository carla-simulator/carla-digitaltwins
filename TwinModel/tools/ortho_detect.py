#!/usr/bin/env python
"""Grounding DINO + SAM on the twin's orthophoto -> georeferenced detections (GeoJSON, model space).

    python tools/ortho_detect.py out/v10_eixample eixample                    # ICGC 10 cm, default prompts
    python tools/ortho_detect.py out/v10_eixample eixample --prompts "car. bus. tree." --no-sam
    python tools/ortho_detect.py out/v10_eixample eixample --window -120 -80 120 80 --preview

Pipeline: ``ingest.imagery.fetch_ortho`` (cached GeoTIFF, model-space grid) -> north-up crops of
``--crop`` px with ``--overlap`` -> Grounding DINO (open-vocabulary boxes for the ``--prompts``
phrases) -> optional SAM (one mask per box, best of the three proposals) -> mask polygons in model
metres (rasterio.features.shapes) -> cross-crop NMS per label -> ``<build_dir>/detect/``:

    detections.geojson   FeatureCollection, geometry = SAM polygon (or the box), properties:
                         label, score, area_m2, box [x0,y0,x1,y1] model metres, crop id
    summary.json         counts and score stats per label, run parameters, ortho provenance
    preview.png          (``--preview``) boxes + masks drawn on the ortho window for a quick look

Models come from the Hugging Face hub (``IDEA-Research/grounding-dino-base``,
``facebook/sam-vit-huge`` by default) through ``transformers``; CUDA if present.  The ortho is the
same product the pipeline uses (``--layer`` / ``--res``), so detections land exactly on the twin's
model grid and ``tools/geo_overlay.py`` shows them as an overlay next to OSM and the twin.

Prompt notes: Grounding DINO wants lowercase phrases separated by ``. ``.  It resizes every crop to
800 px, so at 10 cm a 512 px crop (51 m) is the sweet spot: cars become ~70 px and recall jumps
(j7 window: 1 car at 1024 px -> 20 at 384-512 px).  "car", "van", "truck", "crosswalk", "tree",
"street lamp" work; "zebra crossing" does not (use "crosswalk"); abstract classes such as "bus
lane" or "parking bay" are weak and are better derived from car rows and marking geometry.
Returned labels can merge or truncate prompts ("car bus", "pedestrian"); ``canonical_label`` maps
them back to the prompt phrase.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twinmodel.frame import LocalFrame               # noqa: E402
from twinmodel.ingest.imagery import fetch_ortho     # noqa: E402

log = logging.getLogger("ortho_detect")

DEFAULT_PROMPTS = "car. van. bus. truck. motorcycle. crosswalk. tree. street lamp."
# plausibility caps in m² for the box area; anything larger is a hallucinated "region" box
# (Grounding DINO happily boxes a 100 m stretch of carriageway as "crosswalk", SAM then paints it all)
MAX_AREA_M2 = {"car": 16.0, "van": 30.0, "truck": 60.0, "bus": 60.0, "motorcycle": 4.0, "crosswalk": 150.0,
               "tree": 250.0, "street lamp": 12.0}
DEFAULT_MAX_AREA = 400.0
GDINO_ID = "IDEA-Research/grounding-dino-base"
SAM_ID = "facebook/sam-vit-huge"


# ------------------------------------------------------------------------------------ data

@dataclass
class Det:
    label: str
    score: float
    box: tuple[float, float, float, float]              # model metres x0 y0 x1 y1 (y up)
    crop: str
    poly: Any = None                                    # shapely Polygon in model metres or None
    mask_px: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def area_box(self) -> float:
        return (self.box[2] - self.box[0]) * (self.box[3] - self.box[1])


def _iou(a, b) -> float:
    ix0, iy0, ix1, iy1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def nms(dets: list[Det], iou_thr: float = 0.5) -> list[Det]:
    """Per-label greedy NMS in model space (drops the overlap duplicates from crop overlaps)."""
    out: list[Det] = []
    by_label: dict[str, list[Det]] = {}
    for d in dets:
        by_label.setdefault(d.label, []).append(d)
    for label, ds in by_label.items():
        ds.sort(key=lambda d: -d.score)
        keep: list[Det] = []
        for d in ds:
            if all(_iou(d.box, k.box) < iou_thr for k in keep):
                keep.append(d)
        out.extend(keep)
    return out


def canonical_label(raw: str, phrases: Sequence[str]) -> str | None:
    """Grounding DINO returns the decoded tokens above ``text_threshold``, which can merge two
    prompts ("car bus") or truncate one ("pedestrian"); map to the prompt phrase sharing the most
    words (ties -> first phrase), ``None`` when no prompt word matches."""
    words = set(raw.lower().replace(".", " ").split())
    best, best_n = None, 0
    for ph in phrases:
        n = len(words & set(ph.lower().split()))
        if n > best_n:
            best, best_n = ph, n
    return best


# ------------------------------------------------------------------------------------ models

class Detector:
    def __init__(self, gdino_id: str = GDINO_ID, sam_id: str | None = SAM_ID, device: str | None = None,
                 box_threshold: float = 0.3, text_threshold: float = 0.25):
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.box_threshold = box_threshold
        self.text_threshold = text_threshold
        t = time.time()
        self.gd_proc = AutoProcessor.from_pretrained(gdino_id)
        self.gd = AutoModelForZeroShotObjectDetection.from_pretrained(gdino_id).to(self.device).eval()
        log.info("grounding dino %s on %s (%.1fs)", gdino_id, self.device, time.time() - t)
        self.sam = self.sam_proc = None
        if sam_id:
            from transformers import SamModel, SamProcessor
            t = time.time()
            self.sam_proc = SamProcessor.from_pretrained(sam_id)
            self.sam = SamModel.from_pretrained(sam_id).to(self.device).eval()
            log.info("sam %s (%.1fs)", sam_id, time.time() - t)

    def detect(self, image, prompts: str) -> list[dict[str, Any]]:
        """-> [{label, score, box_px (x0,y0,x1,y1)}] on a PIL image."""
        torch = self.torch
        inputs = self.gd_proc(images=image, text=prompts, return_tensors="pt").to(self.device)
        with torch.no_grad():
            out = self.gd(**inputs)
        h, w = image.height, image.width
        kw: dict[str, Any] = dict(target_sizes=[(h, w)])
        try:      # transformers >= 4.51
            res = self.gd_proc.post_process_grounded_object_detection(
                out, inputs.input_ids, threshold=self.box_threshold, text_threshold=self.text_threshold, **kw)[0]
        except TypeError:  # older signature
            res = self.gd_proc.post_process_grounded_object_detection(
                out, inputs.input_ids, box_threshold=self.box_threshold, text_threshold=self.text_threshold, **kw)[0]
        labels = res.get("text_labels") or res.get("labels")
        phrases = [ph.strip() for ph in prompts.split(".") if ph.strip()]
        dets = []
        for score, box, label in zip(res["scores"].tolist(), res["boxes"].tolist(), labels):
            label = canonical_label(str(label), phrases)
            if not label:
                continue
            dets.append({"label": label, "score": float(score), "box_px": tuple(float(v) for v in box)})
        return dets

    def segment(self, image, boxes_px: Sequence[Sequence[float]]) -> list[np.ndarray]:
        """SAM masks (bool HxW), one per box, best-IoU proposal of the three."""
        if self.sam is None or not boxes_px:
            return []
        torch = self.torch
        masks_all: list[np.ndarray] = []
        B = 64                                           # boxes per forward pass
        for i in range(0, len(boxes_px), B):
            chunk = [list(map(float, b)) for b in boxes_px[i:i + B]]
            inputs = self.sam_proc(image, input_boxes=[chunk], return_tensors="pt").to(self.device)
            with torch.no_grad():
                out = self.sam(**inputs)
            masks = self.sam_proc.image_processor.post_process_masks(
                out.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu())[0]
            scores = out.iou_scores[0].cpu()             # (n_boxes, 3)
            best = scores.argmax(dim=1)
            for k in range(masks.shape[0]):
                masks_all.append(masks[k, int(best[k])].numpy().astype(bool))
        return masks_all


# ------------------------------------------------------------------------------------ geometry

def crops_for(width: int, height: int, crop: int, overlap: int):
    step = max(1, crop - overlap)
    xs = list(range(0, max(1, width - overlap), step))
    ys = list(range(0, max(1, height - overlap), step))
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            x0, y0 = min(x, max(0, width - crop)), min(y, max(0, height - crop))
            yield f"c{i}_{j}", x0, y0, min(width, x0 + crop), min(height, y0 + crop)


def mask_to_polygon(mask: np.ndarray, px_to_model, simplify_m: float = 0.05):
    """Largest polygon of a bool mask, in model metres.  ``px_to_model`` is an affine
    (rasterio.Affine) mapping north-up crop pixel (col,row) -> (x,y)."""
    from rasterio import features
    from shapely.geometry import shape
    best = None
    for geom, val in features.shapes(mask.astype(np.uint8), mask=mask, transform=px_to_model):
        if val != 1:
            continue
        pg = shape(geom)
        if best is None or pg.area > best.area:
            best = pg
    if best is None:
        return None
    best = best.simplify(simplify_m, preserve_topology=True)
    return best if not best.is_empty else None


# ------------------------------------------------------------------------------------ run

def run(build_dir: Path, name: str, *, layer: str, res: float, prompts: str, crop: int, overlap: int,
        use_sam: bool, box_threshold: float, text_threshold: float, window: Sequence[float] | None,
        max_crops: int | None, preview: bool, out_dir: Path | None, data_dir: Path,
        gdino_id: str, sam_id: str, device: str | None) -> Path:
    from PIL import Image
    from rasterio.transform import Affine
    from shapely.geometry import box as shp_box, mapping

    twin_dir = build_dir / f"{name}.twin"
    model = json.loads((twin_dir / "model.json").read_text())
    frame = LocalFrame(model["origin_lat"], model["origin_lon"])
    bbox = tuple(float(v) for v in model["bbox_wgs84"])
    out_dir = out_dir or (build_dir / "detect")
    out_dir.mkdir(parents=True, exist_ok=True)

    img = fetch_ortho(frame, bbox, resolution=res, sources=("icgc", "ign_es", "naip"), layer=layer,
                      cache_dir=data_dir)
    if img is None:
        raise SystemExit("no orthophoto available")
    log.info("ortho %dx%d @ %.2f m  %s%s", img.width, img.height, img.dx, img.label(),
             "  (cache)" if img.cached else "")
    north_up = np.ascontiguousarray(img.array[::-1])           # row 0 = north edge
    xmin, ymin, xmax, ymax = img.bounds()

    # optional model-space window -> pixel window
    px0, py0, px1, py1 = 0, 0, img.width, img.height
    if window:
        wx0, wy0, wx1, wy1 = window
        px0 = int(max(0, (wx0 - xmin) / img.dx)); px1 = int(min(img.width, np.ceil((wx1 - xmin) / img.dx)))
        py0 = int(max(0, (ymax - wy1) / img.dy)); py1 = int(min(img.height, np.ceil((ymax - wy0) / img.dy)))
    sub = north_up[py0:py1, px0:px1]
    sub_xmin, sub_ymax = xmin + px0 * img.dx, ymax - py0 * img.dy

    det = Detector(gdino_id, sam_id if use_sam else None, device, box_threshold, text_threshold)
    dets: list[Det] = []
    n_crops = 0
    t0 = time.time()
    for cid, cx0, cy0, cx1, cy1 in crops_for(sub.shape[1], sub.shape[0], crop, overlap):
        if max_crops is not None and n_crops >= max_crops:
            break
        n_crops += 1
        tile = sub[cy0:cy1, cx0:cx1]
        if tile.size == 0 or (tile.max() == 0):
            continue
        pil = Image.fromarray(tile)
        found = det.detect(pil, prompts)
        # crop pixel -> model metres (north-up): x = sub_xmin + (cx0+col)*dx ; y = sub_ymax - (cy0+row)*dy
        aff = Affine(img.dx, 0.0, sub_xmin + cx0 * img.dx, 0.0, -img.dy, sub_ymax - cy0 * img.dy)
        masks = det.segment(pil, [f["box_px"] for f in found]) if use_sam else []
        for k, f in enumerate(found):
            bx0, by0, bx1, by1 = f["box_px"]
            X0, Y1 = aff * (bx0, by0)
            X1, Y0 = aff * (bx1, by1)
            d = Det(f["label"], f["score"], (X0, Y0, X1, Y1), cid)
            if masks:
                m = masks[k]
                d.mask_px = int(m.sum())
                if d.mask_px > 0:
                    d.poly = mask_to_polygon(m, aff)
            dets.append(d)
        log.info("%s: %d boxes (%d crops, %.0fs)", cid, len(found), n_crops, time.time() - t0)

    n_raw = len(dets)
    dets = [d for d in dets if d.area_box <= MAX_AREA_M2.get(d.label, DEFAULT_MAX_AREA)]
    kept = nms(dets)
    log.info("%d detections -> %d plausible (area caps) -> %d after NMS in %.0fs", n_raw, len(dets), len(kept), time.time() - t0)

    feats = []
    for i, d in enumerate(kept):
        geom = d.poly if d.poly is not None else shp_box(*d.box)
        feats.append({"type": "Feature",
                      "properties": {"id": i, "label": d.label, "score": round(d.score, 3),
                                     "area_m2": round(float(geom.area), 2),
                                     "box": [round(v, 2) for v in d.box], "crop": d.crop,
                                     "geom_src": "sam" if d.poly is not None else "box"},
                      "geometry": mapping(geom)})
    fc = {"type": "FeatureCollection", "features": feats,
          "crs_note": "model space: local ENU metres, x east, y north (twin frame)",
          "ortho": {"source": img.source, "detail": img.detail, "res_m": img.dx},
          "params": {"prompts": prompts, "crop": crop, "overlap": overlap, "sam": use_sam,
                     "box_threshold": box_threshold, "text_threshold": text_threshold,
                     "gdino": gdino_id, "sam_model": sam_id if use_sam else None}}
    (out_dir / "detections.geojson").write_text(json.dumps(fc, separators=(",", ":")))

    summary: dict[str, Any] = {"n": len(kept), "crops": n_crops, "seconds": round(time.time() - t0, 1),
                               "per_label": {}}
    for d in kept:
        s = summary["per_label"].setdefault(d.label, {"n": 0, "scores": []})
        s["n"] += 1; s["scores"].append(d.score)
    for s in summary["per_label"].values():
        sc = np.array(s.pop("scores")); s["score_mean"] = round(float(sc.mean()), 3); s["score_min"] = round(float(sc.min()), 3)
    summary.update({"ortho": fc["ortho"], "params": fc["params"]})
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    log.info("per label: %s", {k: v["n"] for k, v in summary["per_label"].items()})

    if preview:
        _preview(sub, kept, img.dx, sub_xmin, sub_ymax, out_dir / "preview.png")
    return out_dir


def _preview(sub: np.ndarray, dets: list[Det], dx: float, xmin: float, ymax: float, path: Path) -> None:
    from PIL import Image, ImageDraw
    scale = min(1.0, 4000 / max(sub.shape[:2]))
    im = Image.fromarray(sub).convert("RGBA")
    if scale < 1.0:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.BILINEAR)
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(overlay)
    palette = {}
    colours = [(255, 60, 60), (60, 200, 255), (255, 220, 40), (60, 255, 120), (255, 120, 255), (255, 160, 40), (180, 180, 255)]

    def to_px(x, y):
        return ((x - xmin) / dx * scale, (ymax - y) / dx * scale)
    for d in dets:
        c = palette.setdefault(d.label, colours[len(palette) % len(colours)])
        if d.poly is not None:
            rings = [d.poly.exterior.coords] if d.poly.geom_type == "Polygon" else [g.exterior.coords for g in d.poly.geoms]
            for r in rings:
                dr.polygon([to_px(x, y) for x, y, *_ in r], fill=c + (70,), outline=c + (255,))
        x0, y0 = to_px(d.box[0], d.box[3]); x1, y1 = to_px(d.box[2], d.box[1])
        dr.rectangle([x0, y0, x1, y1], outline=c + (200,), width=1)
    im = Image.alpha_composite(im, overlay)
    dr = ImageDraw.Draw(im)
    y = 4
    for label, c in palette.items():
        dr.rectangle([4, y, 16, y + 12], fill=c + (255,)); dr.text((20, y), label, fill=(255, 255, 255, 255)); y += 16
    im.convert("RGB").save(path)
    log.info("preview %s (%dx%d)", path, im.width, im.height)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir"); ap.add_argument("name")
    ap.add_argument("--layer", default="ortofoto_10cm_color_2020", help="ICGC WMS layer (Catalonia)")
    ap.add_argument("--res", type=float, default=0.10, help="model grid resolution, m/px")
    ap.add_argument("--prompts", default=DEFAULT_PROMPTS, help='lowercase phrases, ". "-separated')
    ap.add_argument("--crop", type=int, default=512, help="px; DINO resizes to 800, so 512 upsamples 10 cm cars to ~70 px")
    ap.add_argument("--overlap", type=int, default=96)
    ap.add_argument("--no-sam", action="store_true", help="boxes only, skip SAM masks")
    ap.add_argument("--box-threshold", type=float, default=0.22); ap.add_argument("--text-threshold", type=float, default=0.25)
    ap.add_argument("--window", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), help="model-metre window")
    ap.add_argument("--max-crops", type=int); ap.add_argument("--preview", action="store_true")
    ap.add_argument("--out", help="output dir (default <build_dir>/detect)")
    ap.add_argument("--data", default="data"); ap.add_argument("--device")
    ap.add_argument("--gdino", default=GDINO_ID); ap.add_argument("--sam", default=SAM_ID)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(name)s %(message)s")
    for noisy in ("urllib3", "rasterio", "transformers", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = run(Path(a.build_dir), a.name, layer=a.layer, res=a.res, prompts=a.prompts, crop=a.crop, overlap=a.overlap,
              use_sam=not a.no_sam, box_threshold=a.box_threshold, text_threshold=a.text_threshold, window=a.window,
              max_crops=a.max_crops, preview=a.preview, out_dir=Path(a.out) if a.out else None, data_dir=Path(a.data),
              gdino_id=a.gdino, sam_id=a.sam, device=a.device)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
