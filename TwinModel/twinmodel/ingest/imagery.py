"""ICGC orthophoto WMS -> RGB raster in model space (worker D).

Service discovery (GetCapabilities on 2026-09-01):

    https://geoserveis.icgc.cat/servei/catalunya/orto-territorial/wms

* MapServer WMS 1.3.0; ``MaxWidth`` = ``MaxHeight`` = 4096 px per GetMap.
* GetMap formats: ``image/tiff`` (georeferenced GeoTIFF, what we use), ``image/png``,
  ``image/jpeg``.
* CRS advertised: EPSG:25831 (ETRS89 / UTM 31N — the native CRS of the product),
  EPSG:4258, EPSG:4326, EPSG:3857 and a few others.
* Layers of interest (RGB):
    - ``ortofoto_25cm_color_2025``  current 25 cm RGB ortho (2025 flight)   <- DEFAULT_LAYER
    - ``ortofoto_color_vigent``     "current" alias (resolves to the newest year)
    - ``ortofoto_25cm_color_<year>`` yearly series 2009..2025, ``ortofoto_10cm_color_2020``
    - ``ortofoto_infraroig_*`` NIR variants, ``ortofoto_gris_vigent`` greyscale.
  Note: ``STYLES=`` (empty) must be sent — MapServer rejects GetMap without it.

The fetch requests GeoTIFF tiles in EPSG:25831 at the native 0.25 m (each tile <= TILE_PX
pixels a side, well under the 4096 cap), then warps them with ``rasterio.warp.reproject``
into a regular grid in the model frame (``LocalFrame.crs``). The result is cached as a
GeoTIFF in ``cache_dir`` keyed by bbox + resolution + layer.

Everything degrades gracefully: any network/HTTP/format problem logs a warning and
``fetch_ortho`` returns ``None``.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..frame import LocalFrame

log = logging.getLogger("twinmodel.ingest.imagery")

WMS_URL = "https://geoserveis.icgc.cat/servei/catalunya/orto-territorial/wms"
DEFAULT_LAYER = "ortofoto_25cm_color_2025"
FALLBACK_LAYER = "ortofoto_color_vigent"
SOURCE_CRS = "EPSG:25831"
SERVER_MAX_PX = 4096
TILE_PX = 2048           # request tiles of this size (safety margin under SERVER_MAX_PX)
NATIVE_RES = 0.25        # m/px of the 25 cm product
REQUEST_TIMEOUT = 90     # seconds per tile


@dataclass
class OrthoImage:
    """RGB raster on a regular model-space grid.

    ``array[j, i]`` is the pixel whose *centre* is at ``x = x0 + i*dx``, ``y = y0 + j*dy``
    with ``dy > 0`` — rows increase with y (south row first), the same convention as
    ``twinmodel.model.Elevation``. For matplotlib use ``ax.imshow(img.array,
    extent=img.extent(), origin="lower")``.
    """
    array: np.ndarray          # (H, W, 3) uint8
    x0: float
    y0: float
    dx: float
    dy: float
    source: str = ""

    # -- geometry helpers ------------------------------------------------------
    @property
    def height(self) -> int:
        return int(self.array.shape[0])

    @property
    def width(self) -> int:
        return int(self.array.shape[1])

    def bounds(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) outer pixel edges in model space."""
        return (self.x0 - self.dx / 2, self.y0 - self.dy / 2,
                self.x0 + (self.width - 0.5) * self.dx, self.y0 + (self.height - 0.5) * self.dy)

    def extent(self) -> tuple[float, float, float, float]:
        """matplotlib ``extent`` = (xmin, xmax, ymin, ymax); use with ``origin="lower"``."""
        xmin, ymin, xmax, ymax = self.bounds()
        return (xmin, xmax, ymin, ymax)

    def xy_to_rc(self, x, y):
        """Model xy -> (row, col) float pixel indices (centre convention)."""
        return (np.asarray(y) - self.y0) / self.dy, (np.asarray(x) - self.x0) / self.dx

    def rc_to_xy(self, row, col):
        return self.x0 + np.asarray(col) * self.dx, self.y0 + np.asarray(row) * self.dy

    def north_up_transform(self):
        """rasterio Affine for the north-up (row 0 = north) view of ``array[::-1]``."""
        from rasterio.transform import from_origin
        xmin, _, _, ymax = self.bounds()
        return from_origin(xmin, ymax, self.dx, self.dy)

    # -- I/O -------------------------------------------------------------------
    def save_geotiff(self, path: Path | str, frame: Optional[LocalFrame] = None,
                     crs: Optional[str] = None) -> Path:
        """Write a north-up 3-band uint8 GeoTIFF; CRS = ``frame.proj4`` (or ``crs``)."""
        import rasterio
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if crs is None:
            crs = frame.proj4 if frame is not None else None
        data = np.ascontiguousarray(self.array[::-1].transpose(2, 0, 1))
        with rasterio.open(path, "w", driver="GTiff", width=self.width, height=self.height,
                           count=3, dtype="uint8", crs=crs, transform=self.north_up_transform(),
                           compress="deflate", tiled=True, blockxsize=256, blockysize=256) as dst:
            dst.write(data)
            dst.update_tags(source=self.source)
        return path

    @classmethod
    def from_geotiff(cls, path: Path | str) -> "OrthoImage":
        import rasterio
        with rasterio.open(path) as ds:
            arr = ds.read(out_dtype="uint8")[:3]
            t = ds.transform
            source = ds.tags().get("source", "")
        if t.e >= 0:  # south-up file (unusual): rows already increase with y
            array = arr.transpose(1, 2, 0)
            dy = float(t.e)
            y0 = float(t.f) + dy / 2
        else:
            array = arr.transpose(1, 2, 0)[::-1]
            dy = float(-t.e)
            y0 = float(t.f) - (arr.shape[1] - 0.5) * dy
        return cls(np.ascontiguousarray(array), x0=float(t.c) + t.a / 2, y0=y0,
                   dx=float(t.a), dy=dy, source=source)

    def save_quicklook(self, path: Path | str, max_px: int = 2400) -> Path:
        """PNG (north-up) for eyeballing; downsampled to at most ``max_px`` a side."""
        from PIL import Image
        img = Image.fromarray(self.array[::-1])
        if max(img.size) > max_px:
            f = max_px / max(img.size)
            img = img.resize((int(img.width * f), int(img.height * f)), Image.BILINEAR)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        return path


# --------------------------------------------------------------------------- grid helpers

def model_grid(frame: LocalFrame, bbox_swne: tuple[float, float, float, float],
               resolution: float, pad_m: float = 0.0):
    """Regular model-space grid covering the WGS84 bbox: (x0, y0, width, height, north_up_transform)."""
    from rasterio.transform import from_origin
    s, w, n, e = bbox_swne
    lons = np.array([w, e, e, w]); lats = np.array([s, s, n, n])
    xs, ys = frame.to_local(lons, lats)
    xmin = np.floor((xs.min() - pad_m) / resolution) * resolution
    ymin = np.floor((ys.min() - pad_m) / resolution) * resolution
    xmax = np.ceil((xs.max() + pad_m) / resolution) * resolution
    ymax = np.ceil((ys.max() + pad_m) / resolution) * resolution
    width = int(round((xmax - xmin) / resolution))
    height = int(round((ymax - ymin) / resolution))
    transform = from_origin(xmin, ymax, resolution, resolution)
    return float(xmin + resolution / 2), float(ymin + resolution / 2), width, height, transform


def _cache_path(cache_dir: Path, bbox_swne, resolution: float, layer: str) -> Path:
    s, w, n, e = bbox_swne
    key = hashlib.sha1(f"{layer}|{resolution}".encode()).hexdigest()[:8]
    return cache_dir / f"ortho_{s:.5f}_{w:.5f}_{n:.5f}_{e:.5f}_{resolution:g}m_{key}.tif"


def _tiles(minx: float, miny: float, maxx: float, maxy: float, res: float, tile_px: int):
    """Split a source-CRS bbox into <= tile_px tiles aligned to ``res``; yields (bbox, w, h)."""
    minx = np.floor(minx / res) * res
    miny = np.floor(miny / res) * res
    maxx = np.ceil(maxx / res) * res
    maxy = np.ceil(maxy / res) * res
    nx = int(np.ceil((maxx - minx) / res)); ny = int(np.ceil((maxy - miny) / res))
    for jy in range(0, ny, tile_px):
        for ix in range(0, nx, tile_px):
            w = min(tile_px, nx - ix); h = min(tile_px, ny - jy)
            x0 = minx + ix * res; y0 = miny + jy * res
            yield (x0, y0, x0 + w * res, y0 + h * res), w, h


def _getmap_tiff(session, layer: str, crs: str, bbox, width: int, height: int):
    params = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=layer, STYLES="",
                  CRS=crs, BBOX=",".join(f"{v:.3f}" for v in bbox), WIDTH=width, HEIGHT=height,
                  FORMAT="image/tiff")
    r = session.get(WMS_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/tiff"):
        raise RuntimeError(f"WMS returned {ctype}: {r.text[:300]}")
    return r.content


def fetch_ortho(frame: LocalFrame, bbox_swne: tuple[float, float, float, float],
                resolution: float = NATIVE_RES, cache_dir: Path | str = "data",
                layer: str = DEFAULT_LAYER, pad_m: float = 2.0,
                use_cache: bool = True) -> Optional[OrthoImage]:
    """Fetch the ICGC ortho for ``bbox_swne`` (S, W, N, E in WGS84) into a model-space grid.

    Returns ``None`` (with a logged warning) on any network/service failure.
    """
    cache_dir = Path(cache_dir)
    cpath = _cache_path(cache_dir, bbox_swne, resolution, layer)
    if use_cache and cpath.exists():
        try:
            img = OrthoImage.from_geotiff(cpath)
            log.info("ortho cache hit %s (%dx%d)", cpath, img.width, img.height)
            return img
        except Exception as exc:  # corrupt cache -> refetch
            log.warning("ortho cache %s unreadable (%s); refetching", cpath, exc)

    try:
        import requests
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.warp import reproject, transform_bounds, Resampling
    except ImportError as exc:
        log.warning("imagery: missing dependency (%s); no ortho", exc)
        return None

    x0, y0, width, height, dst_transform = model_grid(frame, bbox_swne, resolution, pad_m)
    dst_crs = frame.crs
    log.info("ortho grid %dx%d @ %.2f m, layer %s", width, height, resolution, layer)

    # Source bbox in the WMS CRS (densified edge transform to be safe).
    xmin, ymin, xmax, ymax = (dst_transform.c, dst_transform.f - height * resolution,
                              dst_transform.c + width * resolution, dst_transform.f)
    try:
        sminx, sminy, smaxx, smaxy = transform_bounds(dst_crs, SOURCE_CRS, xmin, ymin, xmax, ymax,
                                                     densify_pts=21)
    except Exception as exc:
        log.warning("imagery: CRS transform failed (%s)", exc)
        return None
    margin = 2 * max(resolution, NATIVE_RES)
    src_res = max(NATIVE_RES, resolution)  # never ask for finer than the product
    dst = np.zeros((3, height, width), dtype=np.uint8)
    session = requests.Session()
    n_tiles = 0
    try:
        for bbox, w, h in _tiles(sminx - margin, sminy - margin, smaxx + margin, smaxy + margin,
                                 src_res, TILE_PX):
            try:
                data = _getmap_tiff(session, layer, SOURCE_CRS, bbox, w, h)
            except Exception as exc:
                if layer != FALLBACK_LAYER:
                    log.warning("GetMap %s failed (%s); retrying with %s", layer, exc, FALLBACK_LAYER)
                    data = _getmap_tiff(session, FALLBACK_LAYER, SOURCE_CRS, bbox, w, h)
                else:
                    raise
            with MemoryFile(data) as mem, mem.open() as src:
                src_arr = src.read()[:3]
                src_transform = src.transform
                src_crs = src.crs or rasterio.crs.CRS.from_string(SOURCE_CRS)
            n_tiles += 1
            tile_dst = np.zeros_like(dst)
            reproject(src_arr, tile_dst, src_transform=src_transform, src_crs=src_crs,
                      dst_transform=dst_transform, dst_crs=dst_crs,
                      resampling=Resampling.bilinear, src_nodata=None, dst_nodata=0)
            hit = tile_dst.max(axis=0) > 0
            dst[:, hit] = tile_dst[:, hit]
    except Exception as exc:
        log.warning("imagery: ICGC WMS fetch failed (%s); no ortho", exc)
        return None

    if n_tiles == 0 or dst.max() == 0:
        log.warning("imagery: WMS returned no usable pixels")
        return None
    # rows increasing with y: flip the north-up warp output
    array = np.ascontiguousarray(dst.transpose(1, 2, 0)[::-1])
    img = OrthoImage(array, x0=x0, y0=y0, dx=resolution, dy=resolution,
                     source=f"ICGC WMS {layer} ({SOURCE_CRS} -> local)")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        img.save_geotiff(cpath, frame=frame)
        log.info("ortho cached -> %s (%d tiles)", cpath, n_tiles)
    except Exception as exc:
        log.warning("imagery: could not write cache %s (%s)", cpath, exc)
    return img


# Backwards-friendly alias used in DESIGN.md
fetch = fetch_ortho
