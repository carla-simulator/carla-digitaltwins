"""Aerial-photo ground textures for a twin's ground tiles (tools/ground_photo.py).

The ground of a twin (kind ``ground``: open lots and courtyard mouths; kind ``groundplane``: the slab
under everything, seen in block interiors, gardens and the apron) gets one texture per World
Partition tile, cut from the aerial photo like the 3DBAG roofs are (facades.ortho_tile/roof_uv).
Roads, sidewalks and markings keep CARLA's materials.

An aerial photo has things baked in that a 3D scene draws itself. Two are handled here:
  * trees, cars on lots, bus shelters and the like (anything standing on the ground): the scenery
    places real trees at the same spots, so their crowns would be painted on the ground under the
    3D trees. Pixels where the surface model stands TREE_MIN_H above the terrain (AHN DSM minus
    DTM in the Netherlands) and outside the building footprints are replaced by the ground around
    them (pull-push fill, plus grain with the photo's own fine-detail strength);
  * cast shadows: the photo's shadows fall in another direction than the simulator's sun. Dark
    pixels (well under the tile's median brightness) are brightened per channel towards the sunlit
    ground (gain from the medians of both), with a soft edge.
Pure numpy/scipy; no network (the tool fetches).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from . import facades

TREE_MIN_H = 2.0       # m above the terrain: trees, shelters, vans; not kerbs, hedges or cars' roofs
TREE_GROW_M = 0.8      # m the object mask grows (crown edges, the photo's lean against the lidar)
TREE_MIN_AREA_M2 = 1.0   # smaller blobs (poles, lamp heads) are left in the photo
TREE_OPEN_M = 1.0      # thinner than this: lidar edges along walls, catenary, fences (not trees)
GRAIN = 0.6            # grain in filled areas, relative to the photo's own fine detail
FEATHER_M = 0.5        # m soft edge of the fill and the shadow lift
SHADOW_FRAC = 0.55     # a pixel darker than this fraction of the tile's median ground is in shadow
SHADOW_MAX_GAIN = 2.6  # per-channel brightening of shadows is capped at this
SHADOW_STRENGTH = 0.8  # share of the way from shadow to sunlit (a little shade stays: ambient)
SHADOW_SMOOTH_M = 0.4  # brightness smoothing before the shadow test (texture of the paving)


@dataclass
class TileStats:
    masked_frac: float = 0.0       # share of the tile replaced (standing objects)
    shadow_frac: float = 0.0       # share of the tile in shadow (brightened)
    shadow_gain: tuple = (1.0, 1.0, 1.0)
    notes: list = field(default_factory=list)


def tile_uv(xy: np.ndarray, tile: tuple[int, int], tile_m: float) -> np.ndarray:
    """Top-down UVs of model xy (metres) into the tile's texture (row 0 = north), as the roofs."""
    return facades.roof_uv(np.asarray(xy, dtype=np.float64), tile, tile_m)


def resample(arr: np.ndarray, shape: tuple[int, int], order: int = 1) -> np.ndarray:
    """A 2-D raster (NaN allowed) resampled to ``shape``; cells next to NaN stay NaN."""
    arr = np.asarray(arr, dtype=np.float32)
    zy, zx = shape[0] / arr.shape[0], shape[1] / arr.shape[1]
    nan = ~np.isfinite(arr)
    filled = np.where(nan, 0.0, arr)
    out = ndimage.zoom(filled, (zy, zx), order=order, mode="nearest", grid_mode=True)
    if nan.any():
        nn = ndimage.zoom(nan.astype(np.float32), (zy, zx), order=1, mode="nearest", grid_mode=True)
        out[nn > 0.01] = np.nan
    return out[:shape[0], :shape[1]]


def object_mask(ndsm: np.ndarray, building: np.ndarray, px_m: float, min_h: float = TREE_MIN_H,
                grow_m: float = TREE_GROW_M, min_area_m2: float = TREE_MIN_AREA_M2) -> np.ndarray:
    """Pixels of standing objects (trees, shelters, parked vans) in a tile: the normalised surface
    model (surface minus terrain, metres, NaN where unknown) above ``min_h``, outside the building
    footprints, small blobs dropped, grown by ``grow_m``."""
    tall = np.isfinite(ndsm) & (np.nan_to_num(ndsm, nan=0.0) > min_h) & ~building
    k = int(round(TREE_OPEN_M / px_m / 2))
    if k > 0 and tall.any():
        tall = ndimage.binary_opening(tall, iterations=k)
    lab, n = ndimage.label(tall)
    if n:
        sizes = ndimage.sum(tall, lab, index=np.arange(1, n + 1))
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = sizes * px_m * px_m >= min_area_m2
        tall = keep[lab]
    grow = int(round(grow_m / px_m))
    if grow > 0 and tall.any():
        tall = ndimage.binary_dilation(tall, structure=ndimage.generate_binary_structure(2, 1),
                                       iterations=grow)
    return tall & ~building


def pull_push_fill(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Fill the pixels where ``valid`` is False from the valid ones around them (pull-push: a
    weighted pyramid down to where every cell has data, then back up). Valid pixels are kept."""
    img = np.asarray(img, dtype=np.float32)
    w = valid.astype(np.float32)
    if w.all() or not w.any():
        return img.copy()
    levels = []
    c, ww = img * w[..., None], w
    while True:
        levels.append((c, ww))
        h, wd = ww.shape
        if (ww > 0).all() or min(h, wd) <= 1:
            break
        ph, pw = h % 2, wd % 2
        c2 = np.pad(c, ((0, ph), (0, pw), (0, 0)), mode="edge")
        w2 = np.pad(ww, ((0, ph), (0, pw)), mode="edge")
        c = c2.reshape(c2.shape[0] // 2, 2, c2.shape[1] // 2, 2, 3).sum(axis=(1, 3))
        ww = w2.reshape(w2.shape[0] // 2, 2, w2.shape[1] // 2, 2).sum(axis=(1, 3))
    c, ww = levels[-1]
    cur = c / np.maximum(ww, 1e-6)[..., None]
    for c, ww in reversed(levels[:-1]):
        up = np.repeat(np.repeat(cur, 2, axis=0), 2, axis=1)[:ww.shape[0], :ww.shape[1]]
        up = ndimage.uniform_filter(up, size=(3, 3, 1), mode="nearest")
        a = np.clip(ww, 0.0, 1.0)[..., None]
        cur = a * (c / np.maximum(ww, 1e-6)[..., None]) + (1.0 - a) * up
    out = img.copy()
    out[~valid] = cur[~valid]
    return out


def detail_strength(img: np.ndarray, valid: np.ndarray, px: float = 1.5) -> float:
    """Standard deviation of the photo's fine detail (image minus its blur) over valid pixels."""
    lum = img.mean(axis=-1)
    hp = lum - ndimage.gaussian_filter(lum, px)
    v = hp[valid]
    return float(v.std()) if v.size else 0.0


def soft(mask: np.ndarray, px_m: float, feather_m: float = FEATHER_M) -> np.ndarray:
    """A 0..1 version of a mask with a soft edge of ``feather_m``."""
    s = feather_m / px_m / 2.0
    m = mask.astype(np.float32)
    return np.clip(ndimage.gaussian_filter(m, s), 0.0, 1.0) if s > 0.3 else m


def lift_shadows(img: np.ndarray, valid: np.ndarray, px_m: float, frac: float = SHADOW_FRAC,
                 max_gain: float = SHADOW_MAX_GAIN):
    """Brighten cast shadows towards the sunlit ground. Returns (image, shadow mask, gains)."""
    img = np.asarray(img, dtype=np.float32)
    lum = ndimage.gaussian_filter(img.mean(axis=-1), max(0.5, SHADOW_SMOOTH_M / px_m))
    if valid.sum() < 100:
        return img, np.zeros(valid.shape, bool), (1.0, 1.0, 1.0)
    ref = float(np.median(lum[valid]))
    shadow = valid & (lum < frac * ref)
    sunlit = valid & (lum > 0.8 * ref) & (lum < 1.6 * ref)
    if shadow.sum() < 0.005 * valid.sum() or sunlit.sum() < 0.05 * valid.sum():
        return img, np.zeros(valid.shape, bool), (1.0, 1.0, 1.0)
    # close the speckle of paving joints in a shadow, drop isolated dark pixels (dark cars, drains)
    shadow = ndimage.binary_closing(shadow, iterations=2) & valid
    shadow = ndimage.binary_opening(shadow, iterations=2)
    if not shadow.any():
        return img, shadow, (1.0, 1.0, 1.0)
    sun_med = np.median(img[sunlit], axis=0)
    sh_med = np.median(img[shadow], axis=0)
    gain = 1.0 + SHADOW_STRENGTH * (np.clip(sun_med / np.maximum(sh_med, 1.0), 1.0, max_gain) - 1.0)
    a = soft(shadow, px_m)[..., None]
    out = img * (1.0 + (gain[None, None, :] - 1.0) * a)
    return out, shadow, tuple(round(float(g), 3) for g in gain)


def grain(shape: tuple[int, int], strength: float, seed: int) -> np.ndarray:
    """Luminance grain (H, W, 1) with standard deviation ``strength``, slightly smoothed."""
    rng = np.random.default_rng(seed)
    n = ndimage.gaussian_filter(rng.standard_normal(shape).astype(np.float32), 0.7)
    sd = float(n.std()) or 1.0
    return (n / sd * strength)[..., None]


def process_tile(photo: np.ndarray, px_m: float, ndsm: np.ndarray | None = None,
                 building: np.ndarray | None = None, covered: np.ndarray | None = None,
                 seed: int = 0, shadows: bool = True):
    """The ground texture of one tile from its photo (HxWx3 uint8, row 0 north): standing objects
    filled from the ground around them, shadows lifted, building footprints filled with ground.
    ``ndsm`` (metres above the terrain, NaN unknown: water, and buildings in AHN), ``building``
    (footprints) and ``covered`` (where the twin's roads, sidewalks, kerbs and verges lie over the
    ground) are on the photo's grid. Only the visible ground (none of those) is used as the source
    of fills and of the shadow statistics; water (NaN outside buildings) is never brightened.
    Without ``ndsm`` nothing is filled. Returns (uint8 image, TileStats)."""
    img = photo[..., :3].astype(np.float32)
    h, w = img.shape[:2]
    st = TileStats()
    building = np.zeros((h, w), bool) if building is None else building.astype(bool)
    covered = np.zeros((h, w), bool) if covered is None else covered.astype(bool) & ~building
    mask = np.zeros((h, w), bool)
    unknown = np.zeros((h, w), bool)
    if ndsm is not None:
        mask = object_mask(ndsm, building, px_m)
        unknown = ~np.isfinite(ndsm) & ~building
        st.masked_frac = round(float((mask & ~covered).mean()), 4)
    else:
        st.notes.append("no surface model: standing objects left in the photo")
    visible = ~mask & ~building & ~covered
    if shadows:
        img, sh, gain = lift_shadows(img, visible & ~unknown, px_m)
        st.shadow_frac = round(float(sh.mean()), 4)
        st.shadow_gain = gain
    if mask.any() or building.any():
        # fill from outside a band around the objects, blend over the band. Footprints are filled
        # too (hidden under the buildings, but their roof colours would bleed into the mipmaps),
        # and the fill draws on the visible ground only (not the asphalt under the road meshes)
        band = max(1, int(round(FEATHER_M / px_m)))
        big = ndimage.binary_dilation(mask, iterations=band) & ~building if mask.any() else mask
        hole = big | building
        source = ~hole & ~covered
        if source.sum() < 0.02 * source.size:   # hardly any visible ground: use everything
            source = ~hole
        strength = GRAIN * detail_strength(img, source)
        filled = pull_push_fill(img, source)
        filled = filled + grain((h, w), strength, seed) * hole[..., None]
        a = np.maximum(soft(big, px_m), (mask | building).astype(np.float32))[..., None]
        img = img * (1.0 - a) + filled * a
    return np.clip(img + 0.5, 0, 255).astype(np.uint8), st
