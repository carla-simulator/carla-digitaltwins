#!/usr/bin/env python
"""Kerb offset from the ortho: 1-D step search across each twin kerb line -> refined kerb + error stats.

    python tools/kerb_edges.py out/v10_eixample eixample                      # whole bbox, ICGC 10 cm
    python tools/kerb_edges.py out/v10_eixample eixample --window -30 30 90 150 --preview

At 10 cm the kerb is the one road feature that is always a clean edge from above: dark asphalt on
the carriageway side, lighter paving (Barcelona "panot") plus a bright granite kerb stone on the
sidewalk side.  Region segmenters (SAM) leak from a 45 px sidewalk strip onto roofs and tree
crowns; a 1-D search does not.  For every twin kerb line (``curbs.geojson``, all drivable|sidewalk):

  1. sample the line every ``--step`` m, take the local normal, decide which side is the sidewalk
     from the twin surfaces (so the expected step is dark -> light towards the sidewalk);
  2. read the luminance profile along the normal over ±``--reach`` m (bilinear, 10 cm spacing);
  3. correlate with a step kernel of half-width ``--edge`` m, weight by a Gaussian prior around
     the claimed position (sigma ``--prior`` m), take the best peak; its contrast (step height /
     profile std) is the sample's confidence;
  4. per kerb line: drop low-confidence samples, median-filter the offsets along the line, reject
     outliers > ``--outlier`` m from the running median, report median / p90 / coverage and write
     the refined kerb polyline.

Sign: **positive offset = real kerb lies towards the sidewalk**, i.e. the twin carriageway is too
wide / sidewalk too narrow there; negative = the twin gives the sidewalk too much.

Outputs in ``<build_dir>/detect/``: ``kerb_edges.geojson`` (refined polylines with offset_med,
offset_p90, frac_valid, n), ``kerb_edge_samples.geojson`` (every accepted sample as a point with
its offset and confidence), ``kerb_summary.json``, ``kerb_preview.png`` (``--preview``: twin kerb
yellow, refined kerb cyan, rejected samples red).  ``tools/geo_overlay.py`` shows the GeoJSONs.
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

log = logging.getLogger("kerb_edges")


def luminance(rgb: np.ndarray) -> np.ndarray:
    return (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.float32)


def run(build_dir: Path, name: str, *, layer: str, res: float, step: float, reach: float, edge: float, prior: float,
        min_contrast: float, outlier: float, window: Sequence[float] | None, preview: bool, out_dir: Path | None,
        data_dir: Path) -> Path:
    from scipy import ndimage
    from shapely.geometry import LineString, Point, box as shp_box, mapping, shape
    from shapely.strtree import STRtree

    twin_dir = build_dir / f"{name}.twin"
    model = json.loads((twin_dir / "model.json").read_text())
    frame = LocalFrame(model["origin_lat"], model["origin_lon"])
    s_, w_, n_, e_ = (float(v) for v in model["bbox_wgs84"])
    bbox = (s_, w_, n_, e_)
    out_dir = out_dir or (build_dir / "detect")
    out_dir.mkdir(parents=True, exist_ok=True)

    img = fetch_ortho(frame, bbox, resolution=res, sources=("icgc", "ign_es", "naip"), layer=layer, cache_dir=data_dir)
    if img is None:
        raise SystemExit("no orthophoto available")
    north_up = np.ascontiguousarray(img.array[::-1])
    lum = luminance(north_up)
    xmin, ymin, xmax, ymax = img.bounds()
    dx = img.dx
    log.info("ortho %dx%d @ %.2f m  %s", img.width, img.height, dx, img.label())

    def to_px(x, y):                       # model metres -> (row, col) fractional
        return ((ymax - y) / dx, (x - xmin) / dx)

    region = shp_box(xmin, ymin, xmax, ymax)
    if window:
        wx0, wy0, wx1, wy1 = window
        region = shp_box(wx0, wy0, wx1, wy1).intersection(region)

    surfaces = json.loads((twin_dir / "surfaces.geojson").read_text())["features"]
    sidewalks = [shape(f["geometry"]) for f in surfaces if f["properties"]["kind"] == "sidewalk"]
    sw_tree = STRtree(sidewalks)
    curbs = [(f["properties"], shape(f["geometry"])) for f in json.loads((twin_dir / "curbs.geojson").read_text())["features"]]

    n_prof = int(round(2 * reach / dx)) + 1
    offs = np.linspace(-reach, reach, n_prof)                 # metres along the normal, + towards sidewalk
    half = max(1, int(round(edge / dx)))
    kernel = np.concatenate([-np.ones(half), np.zeros(1), np.ones(half)]) / (2 * half)   # dark -> light step
    prior_w = np.exp(-0.5 * (offs / prior) ** 2)
    sigma_px = max(0.5, 0.15 / dx)                            # smooth the profile a little (15 cm)

    lines_out, samples_out, rejected_pts = [], [], []
    per_line = []
    t0 = time.time()
    for props, line in curbs:
        if not line.intersects(region):
            continue
        L = line.length
        n = max(2, int(L / step))
        ss = np.linspace(0, L, n + 1)
        pts = [line.interpolate(s) for s in ss]
        offsets, confs, normals = [], [], []
        for i, s in enumerate(ss):
            p = pts[i]
            if not region.contains(p):
                normals.append(None); offsets.append(np.nan); confs.append(0.0); continue
            a = line.interpolate(max(0.0, s - 0.5)); b = line.interpolate(min(L, s + 0.5))
            tx, ty = b.x - a.x, b.y - a.y
            nrm = np.hypot(tx, ty)
            if nrm < 1e-6:
                normals.append(None); offsets.append(np.nan); confs.append(0.0); continue
            nx, ny = -ty / nrm, tx / nrm
            # which side is the sidewalk? probe 0.6 m either way
            side = 0
            for sgn in (1, -1):
                q = Point(p.x + sgn * 0.6 * nx, p.y + sgn * 0.6 * ny)
                hits = sw_tree.query(q)
                if any(sidewalks[h].contains(q) for h in hits):
                    side = sgn; break
            if side == 0:
                normals.append(None); offsets.append(np.nan); confs.append(0.0); continue
            nx, ny = side * nx, side * ny
            xs = p.x + offs * nx; ys = p.y + offs * ny
            rows, cols = to_px(xs, ys)
            if rows.min() < 0 or cols.min() < 0 or rows.max() >= lum.shape[0] - 1 or cols.max() >= lum.shape[1] - 1:
                normals.append(None); offsets.append(np.nan); confs.append(0.0); continue
            prof = ndimage.map_coordinates(lum, [rows, cols], order=1, mode="nearest")
            prof = ndimage.gaussian_filter1d(prof, sigma_px)
            resp = np.convolve(prof, kernel[::-1], mode="same")          # + where luminance steps up towards sidewalk
            resp[:half] = 0; resp[-half:] = 0
            score = resp * prior_w
            j = int(np.argmax(score))
            std = float(prof.std()) + 1e-3
            contrast = float(resp[j]) / std
            normals.append((nx, ny)); offsets.append(float(offs[j])); confs.append(contrast)
        offsets = np.array(offsets); confs = np.array(confs)
        valid = np.isfinite(offsets) & (confs >= min_contrast)
        if valid.sum() >= 3:
            # running median along the line, reject outliers
            med_line = np.full_like(offsets, np.nan)
            idx = np.where(valid)[0]
            vals = offsets[idx]
            k = 7 if len(vals) >= 7 else (len(vals) | 1)
            run_med = ndimage.median_filter(vals, size=k, mode="nearest")
            keep = np.abs(vals - run_med) <= outlier
            acc = idx[keep]
            med_line[acc] = offsets[acc]
        else:
            acc = np.array([], dtype=int)
        for i in range(len(ss)):
            if np.isfinite(offsets[i]) and i not in set(acc.tolist()):
                rejected_pts.append((pts[i].x, pts[i].y))
        if len(acc) >= 3:
            o = offsets[acc]
            row = {**{k: v for k, v in props.items() if k in ("id", "height", "low_side_kind", "high_side_kind")},
                   "n": int(len(ss)), "n_valid": int(len(acc)), "frac_valid": round(float(len(acc) / len(ss)), 2),
                   "offset_med": round(float(np.median(o)), 2), "offset_p10": round(float(np.percentile(o, 10)), 2),
                   "offset_p90": round(float(np.percentile(o, 90)), 2), "offset_abs_med": round(float(np.median(np.abs(o))), 2),
                   "length_m": round(L, 1)}
            per_line.append(row)
            refined = [(pts[i].x + offsets[i] * normals[i][0], pts[i].y + offsets[i] * normals[i][1]) for i in acc]
            geom = LineString(refined) if len(refined) >= 2 else Point(refined[0])
            lines_out.append({"type": "Feature", "properties": row, "geometry": mapping(geom)})
            for i in acc:
                samples_out.append({"type": "Feature",
                                    "properties": {"curb": props.get("id"), "offset": round(float(offsets[i]), 2),
                                                   "contrast": round(float(confs[i]), 2)},
                                    "geometry": {"type": "Point", "coordinates": [round(pts[i].x + offsets[i] * normals[i][0], 2),
                                                                                   round(pts[i].y + offsets[i] * normals[i][1], 2)]}})
        else:
            per_line.append({**{k: v for k, v in props.items() if k in ("id",)}, "n": int(len(ss)), "n_valid": int(len(acc)),
                             "frac_valid": round(float(len(acc) / max(1, len(ss))), 2), "length_m": round(L, 1), "offset_med": None})

    meta = {"ortho": {"detail": img.detail, "res_m": dx},
            "params": {"step": step, "reach": reach, "edge": edge, "prior": prior, "min_contrast": min_contrast, "outlier": outlier}}
    (out_dir / "kerb_edges.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": lines_out, **meta}, separators=(",", ":")))
    (out_dir / "kerb_edge_samples.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": samples_out}, separators=(",", ":")))

    good = [r for r in per_line if r.get("offset_med") is not None]

    def dist(vals):
        a = np.array(vals, dtype=float)
        return {"n": int(a.size), "med": round(float(np.median(a)), 2), "p10": round(float(np.percentile(a, 10)), 2),
                "p90": round(float(np.percentile(a, 90)), 2)} if a.size else {"n": 0}
    all_off = np.array([f["properties"]["offset"] for f in samples_out])
    summary: dict[str, Any] = {
        "kerb_lines": len(per_line), "with_result": len(good), "samples": int(all_off.size), "seconds": round(time.time() - t0, 1),
        "sample_offset_m": {"signed": dist(all_off), "abs": dist(np.abs(all_off))} if all_off.size else {},
        "line_offset_med_m": dist([r["offset_med"] for r in good]),
        "line_offset_abs_med_m": dist([r["offset_abs_med"] for r in good]),
        "line_frac_valid": dist([r["frac_valid"] for r in per_line]),
        "length_weighted_abs_offset_m": round(float(sum(r["offset_abs_med"] * r["length_m"] for r in good) / max(1e-6, sum(r["length_m"] for r in good))), 2) if good else None,
        "worst": sorted(good, key=lambda r: -r["offset_abs_med"])[:10], **meta}
    (out_dir / "kerb_summary.json").write_text(json.dumps(summary, indent=1))
    log.info("kerb lines %d, with result %d, samples %d; sample offset signed med %.2f m, abs med %.2f m, abs p90 %.2f m; "
             "length-weighted abs %.2f m",
             len(per_line), len(good), all_off.size,
             float(np.median(all_off)) if all_off.size else float("nan"), float(np.median(np.abs(all_off))) if all_off.size else float("nan"),
             float(np.percentile(np.abs(all_off), 90)) if all_off.size else float("nan"), summary["length_weighted_abs_offset_m"] or float("nan"))

    if preview:
        _preview(north_up, dx, xmin, ymax, region, curbs, lines_out, rejected_pts, out_dir / "kerb_preview.png")
    return out_dir


def _preview(north_up, dx, xmin, ymax, region, curbs, lines_out, rejected, path: Path) -> None:
    from PIL import Image, ImageDraw
    rx0, ry0, rx1, ry1 = region.bounds
    c0, r0 = int((rx0 - xmin) / dx), int((ymax - ry1) / dx)
    c1, r1 = int(np.ceil((rx1 - xmin) / dx)), int(np.ceil((ymax - ry0) / dx))
    sub = north_up[r0:r1, c0:c1]
    scale = min(1.0, 3000 / max(sub.shape[:2]))
    im = Image.fromarray(sub).convert("RGB")
    if scale < 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.BILINEAR)
    d = ImageDraw.Draw(im)

    def px(x, y):
        return ((x - rx0) / dx * scale, (ry1 - y) / dx * scale)
    for _props, line in curbs:
        if line.intersects(region):
            d.line([px(x, y) for x, y, *_ in line.coords], fill=(255, 230, 0), width=2)
    for f in lines_out:
        g = f["geometry"]
        if g["type"] == "LineString":
            d.line([px(x, y) for x, y in g["coordinates"]], fill=(0, 255, 255), width=3)
    for x, y in rejected:
        X, Y = px(x, y); d.ellipse([X - 3, Y - 3, X + 3, Y + 3], outline=(255, 60, 60), width=2)
    im.save(path)
    log.info("preview %s (%dx%d)", path, im.width, im.height)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir"); ap.add_argument("name")
    ap.add_argument("--layer", default="ortofoto_10cm_color_2020"); ap.add_argument("--res", type=float, default=0.10)
    ap.add_argument("--step", type=float, default=1.0, help="sample spacing along the kerb, m")
    ap.add_argument("--reach", type=float, default=3.0, help="half-width of the search across the kerb, m")
    ap.add_argument("--edge", type=float, default=0.4, help="step kernel half-width, m")
    ap.add_argument("--prior", type=float, default=1.5, help="Gaussian prior sigma around the claimed kerb, m")
    ap.add_argument("--min-contrast", type=float, default=0.35, help="min step height / profile std to accept a sample")
    ap.add_argument("--outlier", type=float, default=0.8, help="reject samples farther than this from the running median, m")
    ap.add_argument("--window", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--preview", action="store_true"); ap.add_argument("--out"); ap.add_argument("--data", default="data")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(name)s %(message)s")
    for noisy in ("urllib3", "rasterio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = run(Path(a.build_dir), a.name, layer=a.layer, res=a.res, step=a.step, reach=a.reach, edge=a.edge, prior=a.prior,
              min_contrast=a.min_contrast, outlier=a.outlier, window=a.window, preview=a.preview,
              out_dir=Path(a.out) if a.out else None, data_dir=Path(a.data))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
