"""OpenStreetMap raster tiles (openstreetmap-carto) -> RGB raster in model space (worker F).

Used only as a *visual-debug* reference layer for ``twinmodel.compare``: the standard tile
layer at z19 shows the OSM road network as the carto style draws it, so we can eyeball
whether our lane graph / surfaces are registered against the same data the ortho is compared
with. Note that the carto road *fill width* at z19 is a cartographic style width, not the
real carriageway width, so ``road_mask_from_tiles`` is a centreline/shape reference, not a
width reference.

Pipeline (mirrors ``imagery.fetch_ortho`` so pixel (i, j) of both rasters is the same model
point):

1. tile range covering the bbox at ``zoom`` (slippy-map XYZ scheme),
2. download (<= 2 concurrent, proper User-Agent per the OSM tile usage policy), each tile
   PNG cached on disk under ``cache_dir/osmtiles/<z>/<x>/<y>.png``,
3. stitch into one EPSG:3857 array (tile x/y -> mercator bounds),
4. ``rasterio.warp.reproject`` into the ``imagery.model_grid`` at ``resolution``,
5. return an ``OrthoImage`` (rows increase with y) and cache the warped GeoTIFF.

Any network failure logs a warning and returns ``None``.
"""
from __future__ import annotations

import hashlib
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

from ..frame import LocalFrame
from .imagery import OrthoImage, model_grid

log = logging.getLogger("twinmodel.ingest.osmtiles")

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = ("twinmodel/0.1 (CARLA digital twins; "
              "+https://github.com/carla-simulator/carla-digitaltwins)")
MAX_CONCURRENT = 2
REQUEST_TIMEOUT = 30
TILE_PX = 256
EARTH_R = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_R  # half the mercator world width
PAD_M = 2.0                       # same grid padding as imagery.fetch_ortho

# openstreetmap-carto road fill colours at z19 (roads.mss); verified by sampling fetched
# Eixample tiles (see compare report): white residential/service fills, #dddde8 pedestrian.
OSM_ROAD_COLORS: dict[str, tuple[int, int, int]] = {
    "motorway": (0xE8, 0x92, 0xA2),
    "trunk": (0xF9, 0xB2, 0x9C),
    "primary": (0xFC, 0xD6, 0xA4),
    "secondary": (0xF7, 0xFA, 0xBF),
    "tertiary": (0xFF, 0xFF, 0xFF),
    "residential": (0xFF, 0xFF, 0xFF),
    "unclassified": (0xFF, 0xFF, 0xFF),
    "living_street": (0xED, 0xED, 0xED),
    "service": (0xFF, 0xFF, 0xFF),
    "pedestrian": (0xDD, 0xDD, 0xE8),
}


# --------------------------------------------------------------------------- tile maths

def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional slippy-map tile coordinates (x east, y south)."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_bounds_3857(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) of tile (x, y) in EPSG:3857 metres."""
    n = 2 ** zoom
    size = 2 * ORIGIN_SHIFT / n
    xmin = -ORIGIN_SHIFT + x * size
    ymax = ORIGIN_SHIFT - y * size
    return xmin, ymax - size, xmin + size, ymax


def tile_range(bbox_swne, zoom: int, pad_tiles: int = 1) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) inclusive tile index range covering the bbox (+pad)."""
    s, w, n, e = bbox_swne
    xa, ya = lonlat_to_tile(w, n, zoom)  # north-west corner -> smallest x, y
    xb, yb = lonlat_to_tile(e, s, zoom)
    nmax = 2 ** zoom - 1
    x0 = max(0, int(math.floor(xa)) - pad_tiles)
    y0 = max(0, int(math.floor(ya)) - pad_tiles)
    x1 = min(nmax, int(math.floor(xb)) + pad_tiles)
    y1 = min(nmax, int(math.floor(yb)) + pad_tiles)
    return x0, y0, x1, y1


# --------------------------------------------------------------------------- download

def _tile_path(cache_dir: Path, zoom: int, x: int, y: int) -> Path:
    return cache_dir / "osmtiles" / str(zoom) / str(x) / f"{y}.png"


def _fetch_tile(session, tile_url: str, zoom: int, x: int, y: int, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    url = tile_url.format(z=zoom, x=x, y=y)
    r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        raise RuntimeError(f"{url} returned {ctype}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def _decode_png(data: bytes) -> np.ndarray:
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def _cache_path(cache_dir: Path, bbox_swne, resolution: float, zoom: int, tile_url: str) -> Path:
    s, w, n, e = bbox_swne
    key = hashlib.sha1(f"{tile_url}|{zoom}|{resolution}".encode()).hexdigest()[:8]
    return cache_dir / f"osmtiles_{s:.5f}_{w:.5f}_{n:.5f}_{e:.5f}_z{zoom}_{resolution:g}m_{key}.tif"


def fetch_osm_tiles(frame: LocalFrame, bbox_swne: tuple[float, float, float, float],
                    zoom: int = 19, resolution: float = 0.25, cache_dir: Path | str = "data",
                    tile_url: str = TILE_URL, use_cache: bool = True) -> Optional[OrthoImage]:
    """OSM tiles for ``bbox_swne`` (S, W, N, E) warped into the model grid at ``resolution``.

    The grid is ``imagery.model_grid(frame, bbox, resolution, pad_m=2.0)`` — identical to
    the ortho's — so pixel (i, j) of both refer to the same model-space point.
    Returns ``None`` (warning logged) when the tiles cannot be fetched.
    """
    cache_dir = Path(cache_dir)
    cpath = _cache_path(cache_dir, bbox_swne, resolution, zoom, tile_url)
    if use_cache and cpath.exists():
        try:
            img = OrthoImage.from_geotiff(cpath)
            log.info("osm tiles cache hit %s (%dx%d)", cpath, img.width, img.height)
            return img
        except Exception as exc:
            log.warning("osm tiles cache %s unreadable (%s); refetching", cpath, exc)

    try:
        import requests
        from rasterio.transform import from_origin
        from rasterio.warp import reproject, Resampling
    except ImportError as exc:
        log.warning("osmtiles: missing dependency (%s)", exc)
        return None

    x0, y0, width, height, dst_transform = model_grid(frame, bbox_swne, resolution, PAD_M)
    tx0, ty0, tx1, ty1 = tile_range(bbox_swne, zoom)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    log.info("osm tiles z%d: %dx%d tiles (%d..%d, %d..%d) -> grid %dx%d @ %.2f m",
             zoom, nx, ny, tx0, tx1, ty0, ty1, width, height, resolution)

    mosaic = np.zeros((ny * TILE_PX, nx * TILE_PX, 3), dtype=np.uint8)
    session = requests.Session()
    jobs = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]

    def work(xy):
        x, y = xy
        return x, y, _decode_png(_fetch_tile(session, tile_url, zoom, x, y,
                                             _tile_path(cache_dir, zoom, x, y)))

    n_ok = 0
    try:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
            for x, y, arr in pool.map(work, jobs):
                if arr.shape[:2] != (TILE_PX, TILE_PX):
                    raise RuntimeError(f"tile {x}/{y} has shape {arr.shape}")
                r, c = (y - ty0) * TILE_PX, (x - tx0) * TILE_PX
                mosaic[r:r + TILE_PX, c:c + TILE_PX] = arr
                n_ok += 1
    except Exception as exc:
        log.warning("osmtiles: tile fetch failed (%s); no tile layer", exc)
        return None
    if n_ok == 0:
        return None

    # georeference the mosaic in EPSG:3857
    mxmin, _, _, mymax = tile_bounds_3857(tx0, ty0, zoom)
    tile_size = 2 * ORIGIN_SHIFT / 2 ** zoom
    src_transform = from_origin(mxmin, mymax, tile_size / TILE_PX, tile_size / TILE_PX)
    src = np.ascontiguousarray(mosaic.transpose(2, 0, 1))
    dst = np.zeros((3, height, width), dtype=np.uint8)
    try:
        reproject(src, dst, src_transform=src_transform, src_crs="EPSG:3857",
                  dst_transform=dst_transform, dst_crs=frame.crs,
                  resampling=Resampling.bilinear, src_nodata=None, dst_nodata=0)
    except Exception as exc:
        log.warning("osmtiles: reprojection failed (%s)", exc)
        return None

    array = np.ascontiguousarray(dst.transpose(1, 2, 0)[::-1])  # rows increasing with y
    img = OrthoImage(array, x0=x0, y0=y0, dx=resolution, dy=resolution,
                     source=f"OSM tiles z{zoom} {tile_url} (EPSG:3857 -> local)")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        img.save_geotiff(cpath, frame=frame)
        log.info("osm tiles cached -> %s (%d tiles)", cpath, n_ok)
    except Exception as exc:
        log.warning("osmtiles: could not write cache %s (%s)", cpath, exc)
    return img


# --------------------------------------------------------------------------- road mask

def _disk(radius_px: int) -> np.ndarray:
    r = max(1, int(radius_px))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r * r


def road_mask_from_tiles(img: OrthoImage | np.ndarray, tol: int = 4,
                         open_m: float = 0.5, close_m: float = 0.5,
                         resolution: Optional[float] = None,
                         colors: Optional[dict[str, tuple[int, int, int]]] = None) -> np.ndarray:
    """Boolean (H, W) mask of pixels within ``tol`` per channel of any carto road fill colour,
    then a morphological opening of ``open_m`` (metres) to drop text halos and thin edges and
    a closing of ``close_m`` to heal the holes street-name labels punch into the fill.

    ``tol`` defaults to 4, not the 18 first proposed: on the Eixample tiles the bare land
    background (#f2efe9, 13 % of all pixels) is only 5 per channel from the living_street
    fill (#ededed), ``landuse=residential`` (#e0dfdf, 9 %) is 9-14 from the pedestrian
    (#dddde8) and living_street fills, and parking (#eeeeee) is 17 from white, so anything
    above 4 floods the mask with land. Carto fills are exact colours, only the bilinear warp
    blends the edges (the closing heals those). Surface parking (#eeeeee) is inseparable from
    living_street (1 apart) and stays in the mask."""
    from scipy import ndimage

    if isinstance(img, OrthoImage):
        arr = img.array
        res = img.dx if resolution is None else resolution
    else:
        arr = np.asarray(img)
        res = 0.25 if resolution is None else resolution
    arr = arr[..., :3].astype(np.int16)
    mask = np.zeros(arr.shape[:2], dtype=bool)
    for rgb in (colors or OSM_ROAD_COLORS).values():
        d = np.abs(arr - np.asarray(rgb, dtype=np.int16)).max(axis=-1)
        mask |= d <= tol
    radius = int(round(open_m / max(res, 1e-6)))
    if radius >= 1:
        mask = ndimage.binary_opening(mask, structure=_disk(radius))
    radius = int(round(close_m / max(res, 1e-6)))
    if radius >= 1:
        mask = ndimage.binary_closing(mask, structure=_disk(radius))
    return mask
