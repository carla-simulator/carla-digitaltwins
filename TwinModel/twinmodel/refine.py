"""Road-surface mask from imagery and drivable-boundary refinement (worker D).

Pipeline::

    ortho = ingest.imagery.fetch_ortho(frame, bbox)
    mask  = refine.road_mask(ortho, prior=drivable_polygon)          # bool (H, W), ortho grid
    poly  = refine.mask_to_polygon(mask, ortho)                       # MultiPolygon, model space
    refined, stats = refine.refine_drivable(prior, mask, ortho)      # prior boundary snapped to mask edge
    surfaces.build_surfaces(model, refined_drivable=refined)

or, layer-aware (what ``build`` does — see the "layers" section below)::

    refined, stats, mask = refine.refine_layers(model, ortho)        # {ground layer: polygon}
    surfaces.build_surfaces(model, refined_drivable=refined)

Mask methods (``road_mask(..., method=)``):

* ``"classical"`` (default, always available): per-pixel quadratic discriminant on
  CIELab colour + local texture (std of L in a 1 m window). With a ``prior`` polygon the
  class statistics are *learned from the image itself*: pixels under the prior's inner core
  (eroded 2 m) are the asphalt samples, pixels in a 3–6 m band outside it the non-road
  samples. Without a prior, fixed Lab thresholds tuned for grey asphalt vs. Barcelona's
  pale 'panot' sidewalk tiles are used. Then morphology (open/close 1 m), small-hole fill
  (cars, manholes), and blob removal (< 20 m²).
* ``"sam"`` (optional; needs ``segment_anything`` + torch + a checkpoint in
  ``~/.cache/twinmodel`` or ``SAM_CHECKPOINT``): SAM prompted per 1024 px tile with positive
  points on the prior core and negative points in the outer band. Falls back to classical
  on any failure.
* ``"auto"``: SAM if available and a prior is given, else classical.

The lane graph stays the authority on topology: ``refine_drivable`` moves boundary
vertices along their outward normal by at most ``max_shift`` (2.5 m), never splits or
merges polygons and never lets the local carriageway width drop below ``min_lane_width``.
Per polygon part it additionally (a) reverts to the prior when the refined part would lose
more than 30 % of its area or its minimum inscribed width (negative-buffer test) would fall
below ``min_lane_width``, and (b) leaves parts with less than 40 % of their prior area under
the mask untouched (``stats["low_coverage_parts"]``). Boundary shifts are smoothed with a
5-vertex (2.5 m) moving average and the refined rings simplified to 0.1 m so the boundary
stays smooth enough for ``surfaces.py`` to share edges with the sidewalk bands.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .ingest.imagery import OrthoImage
from .model import Surface, TwinModel

log = logging.getLogger("twinmodel.refine")

MIN_BLOB_M2 = 20.0
MORPH_M = 1.0
SMOOTH_K = 5             # moving-average window (vertices, 0.5 m apart) on the boundary shifts
OUTPUT_SIMPLIFY_M = 0.1  # simplify tolerance on the refined rings
MIN_AREA_RATIO = 0.7     # a part may not lose more than 30 % of its prior area
MIN_COVERAGE = 0.4       # parts with less of their prior area under the mask are left untouched
CORE_ERODE_M = 2.0
# an elevated deck's footprint (every surface on OSM layer > 0) is grown by this before it is
# cut out of the ground mask: parapets, the deck's shadow and the ortho's off-nadir lean
DECK_MASK_MARGIN_M = 1.5
BAND_M = (3.0, 6.0)
SAM_CKPTS = {"vit_h": "sam_vit_h_4b8939.pth", "vit_l": "sam_vit_l_0b3195.pth",
             "vit_b": "sam_vit_b_01ec64.pth"}


# --------------------------------------------------------------------------- colour features

def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB uint8 (..., 3) -> CIELab float32 (D65). Pure numpy."""
    c = rgb.astype(np.float32) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
    xyz = lin @ M.T
    xyz /= np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def pixel_features(ortho: OrthoImage) -> np.ndarray:
    """(H, W, 4) float32: L, a, b, local std of L over a ~1 m window (texture)."""
    lab = rgb_to_lab(ortho.array)
    L = lab[..., 0]
    win = max(3, int(round(1.0 / ortho.dx)) | 1)
    mean = ndimage.uniform_filter(L, win)
    sq = ndimage.uniform_filter(L * L, win)
    std = np.sqrt(np.maximum(sq - mean * mean, 0.0))
    return np.concatenate([lab, std[..., None]], axis=-1).astype(np.float32)


# --------------------------------------------------------------------------- raster helpers

def _south_up_transform(ortho: OrthoImage):
    """Affine mapping (col, row) of ``ortho.array`` (rows increase with y) -> model xy edges."""
    from rasterio.transform import Affine
    xmin, ymin, _, _ = ortho.bounds()
    return Affine(ortho.dx, 0.0, xmin, 0.0, ortho.dy, ymin)


def rasterize(geom, ortho: OrthoImage) -> np.ndarray:
    """Boolean raster of ``geom`` on the ortho grid (rows increase with y)."""
    from rasterio.features import rasterize as _rasterize
    if geom is None or geom.is_empty:
        return np.zeros(ortho.array.shape[:2], dtype=bool)
    out = _rasterize([(geom, 1)], out_shape=ortho.array.shape[:2],
                     transform=_south_up_transform(ortho), fill=0, dtype="uint8", all_touched=False)
    return out.astype(bool)


def _disk(radius_px: int) -> np.ndarray:
    r = max(1, int(radius_px))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def _remove_small(mask: np.ndarray, min_px: int) -> np.ndarray:
    lab, n = ndimage.label(mask)
    if n == 0:
        return mask
    sizes = ndimage.sum(mask, lab, index=np.arange(1, n + 1))
    keep = np.zeros(n + 1, dtype=bool)
    keep[1:] = sizes >= min_px
    return keep[lab]


def _fill_small_holes(mask: np.ndarray, max_px: int) -> np.ndarray:
    holes = ~mask
    lab, n = ndimage.label(holes)
    if n == 0:
        return mask
    sizes = ndimage.sum(holes, lab, index=np.arange(1, n + 1))
    # holes touching the image border are background, never fill them
    border = np.zeros(n + 1, dtype=bool)
    for edge in (lab[0], lab[-1], lab[:, 0], lab[:, -1]):
        border[np.unique(edge)] = True
    fill = np.zeros(n + 1, dtype=bool)
    fill[1:] = sizes <= max_px
    fill &= ~border
    fill[0] = False
    return mask | fill[lab]


def _postprocess(mask: np.ndarray, ortho: OrthoImage, morph_m: float = MORPH_M,
                 min_blob_m2: float = MIN_BLOB_M2, fill_hole_m2: float = 15.0) -> np.ndarray:
    px_area = ortho.dx * ortho.dy
    se = _disk(round(morph_m / ortho.dx))
    m = ndimage.binary_opening(mask, structure=se)
    m = ndimage.binary_closing(m, structure=se)
    m = _fill_small_holes(m, int(fill_hole_m2 / px_area))
    m = _remove_small(m, int(min_blob_m2 / px_area))
    return m


# --------------------------------------------------------------------------- classical classifier

class QDA:
    """Two-class quadratic discriminant in feature space (Gaussian per class)."""

    def __init__(self, pos: np.ndarray, neg: np.ndarray, prior_pos: float = 0.5):
        self.mu = [pos.mean(0), neg.mean(0)]
        eps = 1e-3 * np.eye(pos.shape[1])
        self.icov = [np.linalg.inv(np.cov(pos.T) + eps), np.linalg.inv(np.cov(neg.T) + eps)]
        self.logdet = [np.linalg.slogdet(np.cov(pos.T) + eps)[1],
                       np.linalg.slogdet(np.cov(neg.T) + eps)[1]]
        self.logprior = [np.log(prior_pos), np.log(1 - prior_pos)]

    def llr(self, X: np.ndarray) -> np.ndarray:
        """log p(road|x) - log p(nonroad|x) (unnormalised)."""
        out = []
        for k in range(2):
            d = X - self.mu[k]
            q = np.einsum("...i,ij,...j->...", d, self.icov[k], d)
            out.append(-0.5 * q - 0.5 * self.logdet[k] + self.logprior[k])
        return out[0] - out[1]


class GMM:
    """Small full-covariance Gaussian mixture fitted by EM (numpy only; K <= ~10, dims <= ~8)."""

    def __init__(self, X: np.ndarray, k: int, iters: int = 25, seed: int = 0):
        rng = np.random.default_rng(seed)
        n, d = X.shape
        k = max(1, min(k, n // 50))
        # k-means++ init
        centers = [X[rng.integers(n)]]
        for _ in range(1, k):
            d2 = np.min(((X[:, None, :] - np.asarray(centers)[None]) ** 2).sum(-1), axis=1)
            centers.append(X[rng.choice(n, p=d2 / d2.sum())])
        mu = np.asarray(centers, dtype=np.float64)
        cov = np.repeat((np.cov(X.T) + 1e-3 * np.eye(d))[None], k, axis=0)
        w = np.full(k, 1.0 / k)
        eps = 1e-3 * np.eye(d)
        for _ in range(iters):
            ll = self._component_logpdf(X, mu, cov, w)          # (n, k)
            m = ll.max(1, keepdims=True)
            r = np.exp(ll - m); r /= r.sum(1, keepdims=True)
            nk = r.sum(0) + 1e-9
            w = nk / n
            mu = (r.T @ X) / nk[:, None]
            for j in range(k):
                dx = X - mu[j]
                cov[j] = (r[:, j, None] * dx).T @ dx / nk[j] + eps
        self.mu, self.cov, self.w = mu, cov, w
        self.icov = np.linalg.inv(cov)
        self.logdet = np.linalg.slogdet(cov)[1]

    @staticmethod
    def _component_logpdf(X, mu, cov, w):
        icov = np.linalg.inv(cov)
        logdet = np.linalg.slogdet(cov)[1]
        out = np.empty((X.shape[0], len(w)))
        for j in range(len(w)):
            dx = X - mu[j]
            out[:, j] = -0.5 * np.einsum("ni,ij,nj->n", dx, icov[j], dx) - 0.5 * logdet[j] + np.log(w[j])
        return out

    def logpdf(self, X: np.ndarray) -> np.ndarray:
        X2 = X.reshape(-1, X.shape[-1]).astype(np.float64)
        out = np.empty((X2.shape[0], len(self.w)))
        for j in range(len(self.w)):
            dx = X2 - self.mu[j]
            out[:, j] = (-0.5 * np.einsum("ni,ij,nj->n", dx, self.icov[j], dx)
                         - 0.5 * self.logdet[j] + np.log(self.w[j]))
        m = out.max(1)
        return (m + np.log(np.exp(out - m[:, None]).sum(1))).reshape(X.shape[:-1])


def signed_distance(prior_raster: np.ndarray, px_m: float) -> np.ndarray:
    """Signed distance (m) to the prior boundary: negative inside, positive outside."""
    inside = ndimage.distance_transform_edt(prior_raster) * px_m
    outside = ndimage.distance_transform_edt(~prior_raster) * px_m
    return outside - inside


def spatial_bias(sd: np.ndarray, free_m: float = 2.5, inside_bonus: float = 1.5,
                 outside_slope: float = 0.6, outside_cap: float = 5.0) -> np.ndarray:
    """Log-odds bias from the prior: zero within +-free_m of the boundary (the refinement
    window, so the edge position is decided by the image alone), a mild bonus deep inside,
    and a growing penalty far outside (suppresses roofs/courtyards)."""
    b = np.zeros_like(sd)
    deep = sd < -free_m
    b[deep] = np.minimum(inside_bonus, (-sd[deep] - free_m) * 0.5)
    far = sd > free_m
    b[far] = -np.minimum(outside_cap, (sd[far] - free_m) * outside_slope)
    return b


def _fixed_threshold_mask(feat: np.ndarray) -> np.ndarray:
    """No-prior fallback: greyish (low chroma), mid-tone, smooth = asphalt. Measured on the
    ICGC 2025 Eixample ortho (warm colour cast): asphalt L p10/50/90 = 24/54/73, chroma
    6/9/18, b ~ 8, texture p90 ~ 15; 'panot' sidewalks L > ~75. Weak separation by design —
    pass a prior whenever possible."""
    L, a, b, tex = feat[..., 0], feat[..., 1], feat[..., 2], feat[..., 3]
    chroma = np.hypot(a, b)
    return (L > 25) & (L < 72) & (chroma < 12) & (b < 14) & (tex < 10)


def _sample_features(feat: np.ndarray, sel: np.ndarray, max_n: int = 60000,
                     rng: Optional[np.random.Generator] = None) -> np.ndarray:
    idx = np.flatnonzero(sel.ravel())
    if idx.size > max_n:
        rng = rng or np.random.default_rng(0)
        idx = rng.choice(idx, max_n, replace=False)
    return feat.reshape(-1, feat.shape[-1])[idx]


def classical_road_mask(ortho: OrthoImage, prior=None, core_erode_m: float = CORE_ERODE_M,
                        band_m: tuple[float, float] = BAND_M, llr_threshold: float = 0.0,
                        k_pos: int = 3, k_neg: int = 8, use_spatial_bias: bool = True,
                        free_m: float = 2.5, postprocess: bool = True,
                        return_llr: bool = False, ignore=None):
    """Prior-guided per-pixel classifier (see module docstring). ``free_m`` is the half-width
    of the bias-free window around the prior boundary (match ``refine_drivable(max_shift)``).
    ``ignore`` (optional geometry): pixels that train neither class — the footprint of the
    elevated decks, whose asphalt would otherwise land in the non-road band of the ground
    roads they cross (see :func:`refine_layers`)."""
    feat = pixel_features(ortho)
    llr = None
    if prior is None or prior.is_empty:
        log.info("road_mask: no prior; fixed Lab thresholds")
        raw = _fixed_threshold_mask(feat)
    else:
        prior_r = rasterize(prior, ortho)
        core = rasterize(prior.buffer(-core_erode_m), ortho)
        band = rasterize(prior.buffer(band_m[1]).difference(prior.buffer(band_m[0])), ortho)
        far = ~rasterize(prior.buffer(2 * band_m[1]), ortho)
        if ignore is not None and not ignore.is_empty:
            ign = rasterize(ignore, ortho)
            core &= ~ign
            band &= ~ign
            far &= ~ign
        if core.sum() < 500 or band.sum() < 500:
            log.warning("road_mask: prior too small for training (%d/%d px); fixed thresholds",
                        core.sum(), band.sum())
            raw = _fixed_threshold_mask(feat)
        else:
            rng = np.random.default_rng(0)
            pos = _sample_features(feat, core, 40000, rng)
            neg_band = _sample_features(feat, band, 30000, rng)
            neg_far = _sample_features(feat, far, 20000, rng)
            # robustify the asphalt class: drop the brightest / most colourful / most textured
            # core samples (cars, markings, zebra crossings, tree crowns over the road)
            chroma = np.hypot(pos[:, 1], pos[:, 2])
            keep = ((pos[:, 0] < np.percentile(pos[:, 0], 80)) & (chroma < np.percentile(chroma, 85))
                    & (pos[:, 3] < np.percentile(pos[:, 3], 85)))
            pos = pos[keep]
            # the band must not be polluted by asphalt that leaked outside a too-narrow prior:
            # drop band samples the asphalt-vs-band QDA already calls asphalt
            qda0 = QDA(pos, neg_band)
            neg_band = neg_band[qda0.llr(neg_band) < 0]
            neg = np.vstack([neg_band, neg_far])
            g_pos = GMM(pos, k_pos)
            g_neg = GMM(neg, k_neg)
            llr = (g_pos.logpdf(feat) - g_neg.logpdf(feat)).astype(np.float32)
            if use_spatial_bias:
                llr += spatial_bias(signed_distance(prior_r, ortho.dx), free_m=free_m)
            raw = llr > llr_threshold
            log.info("road_mask: GMM(%d/%d) on %d asphalt / %d non-road samples; raw road "
                     "fraction %.3f", k_pos, k_neg, len(pos), len(neg), raw.mean())
    mask = _postprocess(raw, ortho) if postprocess else raw
    return (mask, llr) if return_llr else mask


# --------------------------------------------------------------------------- SAM (optional)

def _find_sam_checkpoint(model_type: Optional[str] = None) -> Optional[tuple[str, Path]]:
    env = os.environ.get("SAM_CHECKPOINT")
    if env and Path(env).is_file():
        for k, v in SAM_CKPTS.items():
            if v in env:
                return k, Path(env)
        return (model_type or "vit_h"), Path(env)
    cache = Path(os.environ.get("TWINMODEL_CACHE", Path.home() / ".cache" / "twinmodel"))
    order = [model_type] if model_type else ["vit_h", "vit_l", "vit_b"]
    for k in order:
        p = cache / SAM_CKPTS[k]
        if p.is_file() and p.stat().st_size > 1e6:
            return k, p
    return None


def sam_available() -> bool:
    try:
        import torch  # noqa: F401
        import segment_anything  # noqa: F401
    except ImportError:
        return False
    return _find_sam_checkpoint() is not None


def sam_road_mask(ortho: OrthoImage, prior, tile: int = 1024, overlap: int = 128,
                  prompt_spacing_m: float = 6.0, model_type: Optional[str] = None,
                  postprocess: bool = True) -> Optional[np.ndarray]:
    """SAM prompted per tile: positives on the prior core, negatives in the outer band.
    Returns None if SAM cannot run (caller falls back to classical)."""
    try:
        import torch
        from segment_anything import SamPredictor, sam_model_registry
    except ImportError as exc:
        log.warning("sam_road_mask: %s", exc)
        return None
    found = _find_sam_checkpoint(model_type)
    if found is None:
        log.warning("sam_road_mask: no checkpoint (set SAM_CHECKPOINT or put one in ~/.cache/twinmodel)")
        return None
    mtype, ckpt = found
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        sam = sam_model_registry[mtype](checkpoint=str(ckpt)).to(device)
        predictor = SamPredictor(sam)
    except Exception as exc:
        log.warning("sam_road_mask: cannot load %s (%s)", ckpt, exc)
        return None
    H, W = ortho.array.shape[:2]
    core = rasterize(prior.buffer(-CORE_ERODE_M), ortho)
    band = rasterize(prior.buffer(BAND_M[1]).difference(prior.buffer(BAND_M[0])), ortho)
    step = max(2, int(round(prompt_spacing_m / ortho.dx)))
    grid = np.zeros((H, W), dtype=bool)
    grid[step // 2::step, step // 2::step] = True
    pos_r, pos_c = np.nonzero(core & grid)
    neg_r, neg_c = np.nonzero(band & grid)
    votes = np.zeros((H, W), dtype=np.int16)
    counts = np.zeros((H, W), dtype=np.int16)
    img_north_up = ortho.array[::-1]  # SAM wants a normal image; row index r_n = H-1-r
    stride = tile - overlap
    n_tiles = 0
    for y0 in range(0, max(1, H - overlap), stride):
        for x0 in range(0, max(1, W - overlap), stride):
            y1, x1 = min(y0 + tile, H), min(x0 + tile, W)
            sub = img_north_up[y0:y1, x0:x1]
            # prompts inside this tile (convert south-up rows to north-up rows)
            pr, pc = H - 1 - pos_r, pos_c
            nr, nc = H - 1 - neg_r, neg_c
            sp = (pr >= y0) & (pr < y1) & (pc >= x0) & (pc < x1)
            sn = (nr >= y0) & (nr < y1) & (nc >= x0) & (nc < x1)
            if sp.sum() < 3:
                continue
            pts = np.c_[np.r_[pc[sp], nc[sn]] - x0, np.r_[pr[sp], nr[sn]] - y0].astype(np.float32)
            labels = np.r_[np.ones(sp.sum()), np.zeros(sn.sum())].astype(np.int32)
            try:
                predictor.set_image(sub)
                m, scores, _ = predictor.predict(point_coords=pts, point_labels=labels,
                                                 multimask_output=False)
            except Exception as exc:
                log.warning("sam_road_mask: predict failed on tile (%d,%d): %s", x0, y0, exc)
                continue
            votes[y0:y1, x0:x1] += m[0].astype(np.int16)
            counts[y0:y1, x0:x1] += 1
            n_tiles += 1
    if n_tiles == 0:
        return None
    mask_north_up = votes * 2 > counts
    mask = mask_north_up[::-1]
    log.info("sam_road_mask: %s on %s, %d tiles, %d/%d prompts, road fraction %.3f",
             mtype, device, n_tiles, len(pos_r), len(neg_r), mask.mean())
    try:
        del predictor, sam
        torch.cuda.empty_cache()
    except Exception:
        pass
    return _postprocess(mask, ortho) if postprocess else mask


# --------------------------------------------------------------------------- public API

def road_mask(ortho: OrthoImage, prior: Optional[Polygon | MultiPolygon] = None,
              method: str = "classical", **kw) -> np.ndarray:
    """Asphalt/road-surface mask (bool, same grid as ``ortho.array``)."""
    if method not in ("classical", "sam", "auto"):
        raise ValueError(method)
    ignore = kw.pop("ignore", None)
    if method in ("sam", "auto") and prior is not None:
        if method == "sam" or sam_available():
            m = sam_road_mask(ortho, prior, **kw)
            if m is not None:
                return m
            log.warning("road_mask: SAM unavailable/failed; using classical classifier")
    return classical_road_mask(ortho, prior, ignore=ignore, **kw)


def mask_to_polygon(mask: np.ndarray, ortho: OrthoImage, simplify_m: float = 0.15,
                    min_area_m2: float = MIN_BLOB_M2) -> MultiPolygon:
    """Vectorise a boolean mask on the ortho grid into a model-space MultiPolygon."""
    from rasterio.features import shapes
    polys = []
    if mask.any():
        for geom, val in shapes(mask.astype(np.uint8), mask=mask, transform=_south_up_transform(ortho),
                                connectivity=4):
            if val != 1:
                continue
            p = shape(geom)
            if p.area < min_area_m2:
                continue
            p = p.simplify(simplify_m, preserve_topology=True)
            if not p.is_valid:
                p = p.buffer(0)
            if p.is_empty:
                continue
            polys.extend(p.geoms if p.geom_type == "MultiPolygon" else [p])
    return MultiPolygon([p for p in polys if p.area >= min_area_m2])


def iou(a, b) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    u = a.union(b).area
    return float(a.intersection(b).area / u) if u > 0 else 0.0


# --------------------------------------------------------------------------- boundary refinement

def _densify_ring(coords: np.ndarray, step: float) -> np.ndarray:
    """Closed ring (first == last) -> vertices every <= step m (last duplicate dropped)."""
    out = []
    for p, q in zip(coords[:-1], coords[1:]):
        seg = q - p
        n = max(1, int(np.ceil(np.hypot(*seg) / step)))
        for k in range(n):
            out.append(p + seg * (k / n))
    return np.asarray(out)


def _ring_normals(v: np.ndarray) -> np.ndarray:
    """Outward normals for a ring whose interior is to the LEFT of travel (shapely orient(sign=1)):
    outward = right-hand normal, averaged over the two adjacent edges."""
    d = np.roll(v, -1, axis=0) - np.roll(v, 1, axis=0)
    n = np.c_[d[:, 1], -d[:, 0]]
    ln = np.hypot(n[:, 0], n[:, 1])
    ln[ln == 0] = 1.0
    return n / ln[:, None]


def _sample_bool(raster: np.ndarray, ortho: OrthoImage, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    r, c = ortho.xy_to_rc(x, y)
    r = np.rint(r).astype(int); c = np.rint(c).astype(int)
    inside = (r >= 0) & (r < raster.shape[0]) & (c >= 0) & (c < raster.shape[1])
    out = np.zeros(x.shape, dtype=bool)
    out[inside] = raster[r[inside], c[inside]]
    return out


def _edge_shifts(v: np.ndarray, n: np.ndarray, mask: np.ndarray, ortho: OrthoImage,
                 max_shift: float, step: float, support_m: float = 1.5,
                 min_support: float = 0.6) -> tuple[np.ndarray, np.ndarray]:
    """For each vertex, the signed distance t along the outward normal to the nearest
    road->non-road transition of the mask within +-max_shift (0 if none). Returns (t, found)."""
    ts = np.arange(-max_shift, max_shift + step / 2, step)
    px = v[:, 0, None] + n[:, 0, None] * ts[None, :]
    py = v[:, 1, None] + n[:, 1, None] * ts[None, :]
    m = _sample_bool(mask, ortho, px, py)                      # (V, T)
    # transition where m[k] is road and m[k+1] is not
    trans = m[:, :-1] & ~m[:, 1:]
    t_edge = 0.5 * (ts[:-1] + ts[1:])
    # support: fraction of road pixels in the support_m stretch inward of the candidate edge
    ns = max(1, int(round(support_m / step)))
    cs = np.cumsum(np.c_[np.zeros((m.shape[0], 1), dtype=int), m.astype(int)], axis=1)
    k = np.arange(m.shape[1] - 1)
    lo = np.maximum(k + 1 - ns, 0)
    supp = (cs[:, k + 1] - cs[:, lo]) / np.maximum(k + 1 - lo, 1)
    cand = trans & (supp >= min_support)
    cost = np.where(cand, np.abs(t_edge)[None, :], np.inf)
    best = np.argmin(cost, axis=1)
    found = np.isfinite(cost[np.arange(len(v)), best])
    t = np.where(found, t_edge[best], 0.0)
    return t, found


def _local_width(v: np.ndarray, n: np.ndarray, prior_raster: np.ndarray, ortho: OrthoImage,
                 step: float, max_w: float = 40.0) -> np.ndarray:
    """Distance from each vertex along the INWARD normal until leaving the prior polygon."""
    ts = np.arange(step, max_w + step / 2, step)
    px = v[:, 0, None] - n[:, 0, None] * ts[None, :]
    py = v[:, 1, None] - n[:, 1, None] * ts[None, :]
    inside = _sample_bool(prior_raster, ortho, px, py)
    first_out = np.argmin(inside, axis=1)  # first False (0 if all True -> handle)
    all_in = inside.all(axis=1)
    w = ts[first_out]
    w[all_in] = max_w
    return w


def _smooth_closed(t: np.ndarray, k: int = 3) -> np.ndarray:
    if len(t) < k:
        return t
    kern = np.ones(k) / k
    pad = k // 2
    tp = np.r_[t[-pad:], t, t[:pad]]
    return np.convolve(tp, kern, mode="valid")


def _refine_ring(coords, mask, prior_raster, ortho, max_shift, min_lane_width, step, stats,
                 smooth_k: int = SMOOTH_K, keep=None, freeze=None):
    v = _densify_ring(np.asarray(coords, dtype=float)[:, :2], 0.5)
    if len(v) < 4:
        return v, np.zeros(len(v))
    n = _ring_normals(v)
    t, found = _edge_shifts(v, n, mask, ortho, max_shift, step)
    if freeze is not None and not freeze.is_empty:
        # under an elevated deck the imagery shows the deck, not this boundary: no shift, and
        # (before smoothing) no influence on the neighbours either
        frozen = shapely.contains_xy(freeze, v[:, 0], v[:, 1])
        t[frozen] = 0.0
        found &= ~frozen
        stats["n_frozen"] += int(frozen.sum())
    t = _smooth_closed(t, max(1, smooth_k) | 1)
    t = np.clip(t, -max_shift, max_shift)
    if freeze is not None and not freeze.is_empty:
        t[frozen] = 0.0
    # width guard: shrinking (t < 0) must keep local width >= min_lane_width
    w0 = _local_width(v, n, prior_raster, ortho, step)
    shrink = t < 0
    reject = shrink & (w0 + t < min_lane_width)
    # never grow more than 25 % of the local width either (keeps narrow lanes sane)
    t[reject] = 0.0
    # keep-out guard: a shrinking vertex may not end up inside ``keep`` (lane centrelines)
    n_keep = 0
    if keep is not None and not keep.is_empty:
        moved = v + n * t[:, None]
        hit = (t < 0) & shapely.contains_xy(keep, moved[:, 0], moved[:, 1])
        n_keep = int(hit.sum())
        t[hit] = 0.0
    stats["n_vertices"] += len(v)
    stats["n_found"] += int(found.sum())
    stats["n_width_rejected"] += int(reject.sum())
    stats["n_keep_rejected"] += n_keep
    return v + n * t[:, None], t


def _clean_polygon(g, sliver_m2: float = 2.0):
    """make_valid + drop sliver parts/holes (< sliver_m2) produced by local self-intersections
    of a moved ring. Returns the largest polygon (with its surviving holes) or None."""
    from shapely.validation import make_valid
    if not g.is_valid:
        g = make_valid(g)
    polys = []
    if g.geom_type == "Polygon":
        polys = [g]
    elif hasattr(g, "geoms"):
        polys = [p for p in g.geoms if p.geom_type == "Polygon"]
    polys = [p for p in polys if p.area > sliver_m2]
    if not polys:
        return None
    main = max(polys, key=lambda p: p.area)
    holes = [h for h in main.interiors if Polygon(h).area > sliver_m2]
    return Polygon(main.exterior, holes)


def _rebuild(polys_rings):
    """[(shell, [holes...])] -> valid Polygon or None (moved rings are cleaned individually:
    shell first, then each hole is re-applied as a difference so hole self-intersections
    cannot corrupt the shell)."""
    out = []
    for shell, holes in polys_rings:
        p = _clean_polygon(Polygon(shell))
        if p is None:
            return None
        for h in holes:
            hp = _clean_polygon(Polygon(h))
            if hp is None:
                continue
            p = p.difference(hp)
            p = _clean_polygon(p)
            if p is None:
                return None
        out.append(p)
    return unary_union(out)


def _n_parts(g) -> tuple[int, int]:
    polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    return len(polys), sum(len(p.interiors) for p in polys)


def part_coverage(part, mask: np.ndarray, ortho: OrthoImage) -> float:
    """Fraction of ``part``'s area (on the ortho grid) that is under the mask."""
    r = rasterize(part, ortho)
    n = int(r.sum())
    return float((mask & r).sum() / n) if n else 0.0


def min_width_ok(g, min_lane_width: float) -> bool:
    """Approximate minimum-inscribed-width test: the part survives a negative buffer of half
    the minimum lane width."""
    return not g.buffer(-min_lane_width / 2.0).is_empty


def _simplify_part(g, tol: float):
    """Simplify a refined part's rings (keeps topology); None when it degenerates."""
    if tol <= 0:
        return g
    out = g.simplify(tol, preserve_topology=True)
    if out.is_empty or not out.is_valid:
        return None
    return out


def refine_drivable(prior: Polygon | MultiPolygon, mask: np.ndarray, ortho: OrthoImage,
                    max_shift: float = 2.5, min_lane_width: float = 2.5,
                    step: Optional[float] = None, min_area_ratio: float = MIN_AREA_RATIO,
                    min_coverage: float = MIN_COVERAGE, smooth_k: int = SMOOTH_K,
                    simplify_m: float = OUTPUT_SIMPLIFY_M, keep=None, freeze=None,
                    ) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Snap the prior's boundary to the mask edge (<= max_shift along the outward normal),
    keeping topology. Returns (refined polygon, stats).

    Per part: parts with mask coverage < ``min_coverage`` are left untouched
    (``stats["low_coverage_parts"]``); a refined part that would drop below
    ``min_area_ratio`` of its prior area or whose minimum inscribed width falls below
    ``min_lane_width`` (negative-buffer test) reverts to the prior
    (``stats["reverted_parts"]``). Shifts are smoothed over ``smooth_k`` vertices (0.5 m
    apart) and the refined rings simplified to ``simplify_m``. ``keep`` (optional polygon,
    see :func:`lane_keep_out`) is never entered by a shrinking boundary vertex. Boundary
    vertices inside ``freeze`` (optional polygon: the elevated decks' footprint, see
    :func:`refine_layers`) are not moved at all."""
    step = step or ortho.dx / 2
    if keep is not None and not keep.is_empty:
        shapely.prepare(keep)
    if freeze is not None and not freeze.is_empty:
        shapely.prepare(freeze)
    prior = prior if prior.is_valid else prior.buffer(0)
    # normalise the prior: drop sliver parts / degenerate holes so the topology target is
    # well defined (unary_union + simplify of buffers can leave zero-area holes)
    parts = [_clean_polygon(p) for p in (prior.geoms if prior.geom_type == "MultiPolygon" else [prior])]
    parts = [p for p in parts if p is not None]
    if not parts:
        return prior, {"error": "empty prior"}
    prior = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    prior_raster = rasterize(prior, ortho)
    mask_poly = mask_to_polygon(mask, ortho)
    stats: dict[str, Any] = {"n_vertices": 0, "n_found": 0, "n_width_rejected": 0,
                             "n_keep_rejected": 0, "n_frozen": 0,
                             "n_topology_fallback": 0, "max_shift": max_shift,
                             "min_lane_width": min_lane_width,
                             "n_parts": 0, "low_coverage_parts": 0, "reverted_parts": 0,
                             "part_coverage": [], "part_reverted": [],
                             "min_coverage": min_coverage, "min_area_ratio": min_area_ratio,
                             "smooth_k": smooth_k, "simplify_m": simplify_m,
                             "iou_before": iou(prior, mask_poly)}
    polys = list(prior.geoms) if prior.geom_type == "MultiPolygon" else [prior]
    refined_polys = []
    all_t = []
    for p in polys:
        p = orient(p, sign=1.0)  # exterior CCW, holes CW -> interior always on the left
        stats["n_parts"] += 1
        # (b) sparse mask under this part (isolated street the classifier missed): untouched
        cov = part_coverage(p, mask, ortho)
        stats["part_coverage"].append(round(cov, 3))
        if cov < min_coverage:
            stats["low_coverage_parts"] += 1
            stats["part_reverted"].append("low_coverage")
            log.info("refine_drivable: part (%.0f m2) has mask coverage %.2f < %.2f; untouched",
                     p.area, cov, min_coverage)
            refined_polys.append(p)
            continue
        rings = [p.exterior] + list(p.interiors)
        moved = []
        ts = []
        for ring in rings:
            v, t = _refine_ring(ring.coords, mask, prior_raster, ortho, max_shift, min_lane_width,
                                step, stats, smooth_k=smooth_k, keep=keep, freeze=freeze)
            moved.append((v, t))
            ts.append(t)
        # progressively damp the shifts until the polygon is valid and topology is kept
        target = _n_parts(p)
        result = None
        used_scale = 0.0
        for scale in (1.0, 0.5, 0.25):
            shell_v = _densify_ring(np.asarray(p.exterior.coords)[:, :2], 0.5)
            shell_n = _ring_normals(shell_v)
            shell = shell_v + shell_n * (moved[0][1] * scale)[:, None]
            holes = []
            for (hv, ht), ring in zip(moved[1:], rings[1:]):
                rv = _densify_ring(np.asarray(ring.coords)[:, :2], 0.5)
                holes.append(rv + _ring_normals(rv) * (ht * scale)[:, None])
            g = _rebuild([(shell, holes)])
            if g is not None and g.is_valid and _n_parts(g) == target and g.area > 0:
                g2 = _simplify_part(g, simplify_m)
                if g2 is not None and _n_parts(g2) == target and g2.area > 0:
                    g = g2
                result = g
                used_scale = scale
                if scale < 1.0:
                    stats["n_topology_fallback"] += 1
                break
        # (a) never collapse a part: area >= min_area_ratio * prior, inscribed width >= lane
        reason = None
        if result is None:
            reason = "topology"
            stats["n_topology_fallback"] += 1
        elif result.area < min_area_ratio * p.area:
            reason = "area"
        elif not min_width_ok(result, min_lane_width):
            reason = "width"
        if reason is not None:
            if reason != "topology":
                stats["reverted_parts"] += 1
                log.info("refine_drivable: part (%.0f m2) reverted to prior (%s: refined area "
                         "%.0f m2)", p.area, reason, result.area)
            result = p
            used_scale = 0.0
        stats["part_reverted"].append(reason)
        all_t.extend(np.abs(np.concatenate(ts)) * used_scale)
        refined_polys.extend(result.geoms if result.geom_type == "MultiPolygon" else [result])
    refined = unary_union(refined_polys)
    if _n_parts(refined)[0] != _n_parts(prior)[0]:
        # union merged neighbouring parts -> topology changed; keep the prior
        log.warning("refine_drivable: refined parts %s != prior %s; keeping prior",
                    _n_parts(refined), _n_parts(prior))
        stats["n_topology_fallback"] += 1
        refined = prior
        all_t = [0.0]
    stats["iou_after"] = iou(refined, mask_poly)
    stats["mean_abs_shift"] = float(np.mean(all_t)) if all_t else 0.0
    stats["max_abs_shift"] = float(np.max(all_t)) if all_t else 0.0
    stats["area_before"] = float(prior.area)
    stats["area_after"] = float(refined.area)
    log.info("refine_drivable: IoU %.3f -> %.3f, mean|shift| %.2f m, max %.2f m, %d/%d vertices "
             "snapped, %d width-rejected, %d keep-out-rejected, %d topology fallbacks, %d parts "
             "(%d low-coverage, %d reverted)",
             stats["iou_before"], stats["iou_after"], stats["mean_abs_shift"],
             stats["max_abs_shift"], stats["n_found"], stats["n_vertices"],
             stats["n_width_rejected"], stats["n_keep_rejected"], stats["n_topology_fallback"],
             stats["n_parts"], stats["low_coverage_parts"], stats["reverted_parts"])
    return refined, stats


def lane_keep_out(model: TwinModel, margin: float = 1.0):
    """Union of every driving-lane centreline (all roads, connecting ones included) buffered
    by ``margin``: the refined drivable boundary must not enter it, so the xodr lane centres
    stay inside the surface (``validate.lane_in_drivable``) by construction."""
    from .surfaces import lane_bands
    lines = []
    for r in model.roads:
        ref = shapely.force_2d(r.reference_line)
        if ref.length <= 0:
            continue
        for b in lane_bands(r):
            if b.lane.type != "driving":
                continue
            off = 0.5 * (b.inner + b.outer)
            g = ref.offset_curve(off if b.left else -off, join_style="mitre", mitre_limit=2.0)
            if g is not None and not g.is_empty:
                lines.append(g)
    if not lines:
        return Polygon()
    return unary_union(lines).buffer(margin, join_style="mitre", mitre_limit=2.0)


# --------------------------------------------------------------------------- layers
#
# Grade separation (DESIGN.md): a model with an overpass carries one drivable surface per OSM
# ``layer`` (``surfaces.build_surfaces``, ``Surface.tags["layer"]``). The ortho only ever shows
# the topmost surface, so refinement is per layer and only the *ground* layer is refined:
#
# * the deck footprint (every surface on layer > 0, grown by ``DECK_MASK_MARGIN_M``) is cut out
#   of the ground mask — whatever the imagery shows there is the deck, not the street under it
#   — and its pixels train neither class of the classifier (``classical_road_mask(ignore=)``);
#   inside it the ground mask *is* the ground prior, and the ground boundary vertices under it
#   are frozen (``refine_drivable(freeze=)``), so a ground road keeps its OSM geometry under a
#   deck and is refined against the imagery everywhere else;
# * elevated layers (layer > 0, ``bridge=*``) keep their OSM-derived geometry. Refining a deck
#   only where no lower road runs under it was considered and rejected: the mask classifier is
#   trained on the ground prior (deck asphalt at a different exposure and with parapet shadows
#   is not the same class), deck widths come from ``lanes=*`` on the bridge way and are usually
#   right, and a deck edge moved over a street below would eat into that street's mask;
# * tunnel layers (layer < 0) are not refined either; the ground above them is refined as usual
#   (nothing is masked there — the imagery shows the ground). The tunnel roads' own geometry
#   and z are the tunnels lane's business; everything here keys on ``Surface.tags["layer"]``.

def surface_layer(surface) -> Optional[int]:
    """The OSM stacking level a surface belongs to (``None`` in a single-layer model)."""
    v = (surface.tags or {}).get("layer")
    return None if v is None else int(v)


def drivable_by_layer(model: TwinModel) -> dict[Optional[int], Polygon | MultiPolygon]:
    """Union of the ``drivable`` surfaces per layer (``{None: ...}`` in a single-layer model)."""
    groups: dict[Optional[int], list] = {}
    for s in model.surfaces_of("drivable"):
        if not s.geometry.is_empty:
            groups.setdefault(surface_layer(s), []).append(s.geometry)
    return {lay: unary_union(gs) for lay, gs in groups.items()}


def ground_layer(layers) -> Optional[int]:
    """The layer refined against the imagery: 0 when present (the ground), else the single
    untagged layer, else the lowest non-negative one, else the highest (all underground)."""
    layers = list(layers)
    if 0 in layers:
        return 0
    if None in layers:
        return None
    above = [l for l in layers if l >= 0]
    return min(above) if above else max(layers)


def deck_footprint(model: TwinModel, margin: float = DECK_MASK_MARGIN_M):
    """Footprint of the elevated structures: every surface (any kind) on OSM layer > 0, grown
    by ``margin``. Empty polygon when the model has none."""
    geoms = [s.geometry for s in model.surfaces
             if (surface_layer(s) or 0) > 0 and not s.geometry.is_empty]
    if not geoms:
        return Polygon()
    return unary_union(geoms).buffer(margin, join_style="mitre", mitre_limit=2.0)


def mask_out_decks(mask: np.ndarray, deck, prior, ortho: OrthoImage) -> np.ndarray:
    """Inside the deck footprint the ortho is uninformative about the ground: replace the mask
    there by the ground prior itself (so the boundary search finds its edge exactly where the
    prior is, and part coverage is not skewed by the deck)."""
    if deck is None or deck.is_empty:
        return mask
    out = mask.copy()
    d = rasterize(deck, ortho)
    out[d] = rasterize(prior, ortho)[d]
    return out


def refine_layers(model: TwinModel, ortho: OrthoImage, mask: Optional[np.ndarray] = None,
                  method: str = "classical", deck_margin: float = DECK_MASK_MARGIN_M,
                  keep_margin: float = 1.0, **kw
                  ) -> tuple[dict[Optional[int], Polygon | MultiPolygon], dict[str, Any], np.ndarray]:
    """Layer-aware refinement of ``model``'s drivable surfaces against ``ortho`` (see the
    section comment above). ``mask``: a ready road mask (tests); default ``road_mask`` learned
    from the ground prior with the deck footprint ignored. Returns ``({ground_layer: refined},
    stats, ground_mask)`` — a dict for ``surfaces.build_surfaces(refined_drivable=...)``; the
    other layers are absent from it and keep their lane-graph surfaces. ``stats["layers"]``
    records what was refined, kept and masked."""
    groups = drivable_by_layer(model)
    if not groups:
        return {}, {"error": "no drivable surfaces"}, np.zeros(ortho.array.shape[:2], dtype=bool)
    ground = ground_layer(groups)
    prior = groups[ground]
    deck = deck_footprint(model, deck_margin)
    if mask is None:
        mask = road_mask(ortho, prior=prior, method=method, ignore=deck)
    mask = mask_out_decks(mask, deck, prior, ortho)
    refined, st = refine_drivable(prior, mask, ortho, keep=lane_keep_out(model, keep_margin),
                                  freeze=deck, **kw)
    st["layers"] = {
        "refined": ground,
        "kept": sorted((l for l in groups if l != ground), key=lambda l: (l is None, l)),
        "deck_footprint_m2": float(deck.area), "deck_margin_m": float(deck_margin),
        "ground_prior_under_deck_m2": float(prior.intersection(deck).area) if not deck.is_empty else 0.0,
        "area_by_layer": {str(l): float(g.area) for l, g in groups.items()},
    }
    if not deck.is_empty:
        log.info("refine_layers: layer %s refined, %s kept; %.0f m2 of deck footprint masked "
                 "(%.0f m2 of ground road under it frozen, %d boundary vertices)", ground,
                 st["layers"]["kept"], deck.area, st["layers"]["ground_prior_under_deck_m2"],
                 st["n_frozen"])
    return {ground: refined}, st, mask


def refine_surfaces(model: TwinModel, mask: np.ndarray, ortho: OrthoImage, **kw) -> TwinModel:
    """Refine every ground-layer ``drivable`` Surface of ``model`` in place (DESIGN.md: keeps the
    original area as ``tags['prior_area']``, sets source='imagery', records stats in metadata).
    Surfaces on other layers (decks, tunnels) are left alone and the deck footprint is masked
    out of the ground mask, as in :func:`refine_layers`."""
    all_stats = {}
    layers = {surface_layer(s) for s in model.surfaces_of("drivable")}
    ground = ground_layer(layers) if layers else None
    deck = deck_footprint(model)
    prior = drivable_by_layer(model).get(ground)
    if prior is not None:
        mask = mask_out_decks(mask, deck, prior, ortho)
    for s in model.surfaces_of("drivable"):
        if surface_layer(s) != ground:
            continue
        refined, st = refine_drivable(s.geometry, mask, ortho, freeze=deck, **kw)
        s.tags["prior_area"] = float(s.geometry.area)
        s.geometry = refined
        s.source = "imagery"
        s.confidence = min(1.0, max(0.3, st["iou_after"]))
        all_stats[s.id] = st
    model.metadata.setdefault("refine", {}).update(all_stats)
    return model


# --------------------------------------------------------------------------- quicklooks

def save_overlay(ortho: OrthoImage, mask: np.ndarray, path: Path | str, prior=None, refined=None,
                 alpha: float = 0.4, max_px: int = 2400) -> Path:
    """Ortho + mask (red) overlay PNG; optional prior (cyan) and refined (yellow) outlines."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    H, W = mask.shape
    scale = max(1.0, max(H, W) / max_px)
    fig, ax = plt.subplots(figsize=(W / scale / 100, H / scale / 100), dpi=100)
    ax.imshow(ortho.array, extent=ortho.extent(), origin="lower")
    rgba = np.zeros((H, W, 4), dtype=np.float32)
    rgba[mask] = (1.0, 0.0, 0.0, alpha)
    ax.imshow(rgba, extent=ortho.extent(), origin="lower", interpolation="nearest")
    for g, col in ((prior, "cyan"), (refined, "yellow")):
        if g is None or g.is_empty:
            continue
        for p in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            for ring in [p.exterior] + list(p.interiors):
                xy = np.asarray(ring.coords)
                ax.plot(xy[:, 0], xy[:, 1], color=col, linewidth=0.6)
    ax.set_axis_off()
    ax.set_xlim(ortho.extent()[:2]); ax.set_ylim(ortho.extent()[2:])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=100, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    return path
