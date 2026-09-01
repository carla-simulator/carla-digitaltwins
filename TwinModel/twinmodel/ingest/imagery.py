"""Orthophoto providers -> RGB raster in model space (worker D).

``fetch_ortho(frame, bbox, sources=(...))`` walks an ordered list of providers; the first that
returns a raster wins and its *name* is recorded in ``OrthoImage.source`` (the long
description goes to ``OrthoImage.detail``). Every provider degrades gracefully: any
network/HTTP/format problem logs a warning and the next provider is tried; ``None`` when all
fail. Each provider caches its warped result as a GeoTIFF in ``cache_dir`` keyed by
bbox + resolution + provider/layer, and a cache hit short-circuits the chain.

Providers (names understood by ``profiles.DataSources.ortho``):

* ``icgc`` — ICGC Catalunya 25 cm RGB ortho. GetCapabilities 2026-09-01:
  ``https://geoserveis.icgc.cat/servei/catalunya/orto-territorial/wms`` (MapServer WMS 1.3.0,
  ``MaxWidth``/``MaxHeight`` 4096, GeoTIFF GetMap, native EPSG:25831). Layers:
  ``ortofoto_25cm_color_2025`` (DEFAULT_LAYER), ``ortofoto_color_vigent`` (newest alias),
  ``ortofoto_25cm_color_<year>`` 2009..2025, ``ortofoto_10cm_color_2020``, NIR/grey variants.
  ``STYLES=`` (empty) must be sent — MapServer rejects GetMap without it.
* ``ign_es`` — IGN Spain PNOA máxima actualidad, whole country, ~25 cm:
  ``https://www.ign.es/wms-inspire/pnoa-ma`` (MapServer WMS 1.3.0, 4096 px cap, GeoTIFF,
  layer ``OI.OrthoimageCoverage``, EPSG:25830/25831/4326/3857). Verified 2026-09-01: a
  2000x1800 EPSG:25830 GetMap over the Eixample returns a georeferenced 3-band GeoTIFF.
* ``naip`` — USGS NAIP Plus (US, 0.3–0.6 m RGBN, EPSG:3857 native), ArcGIS ImageServer
  ``https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage``
  (``format=tiff&f=image``, 4-band uint8 GeoTIFF, ``maxImageWidth`` 4000; bands 1-3 = RGB,
  identical to the WMS ``USGSNAIPPlus:NaturalColor`` rendering). Fallback inside the provider:
  the WMS 1.3.0 endpoint ``.../services/USGSNAIPPlus/ImageServer/WMSServer`` with layer
  ``USGSNAIPPlus:NaturalColor`` (``image/tiff``, EPSG:3857/4326). Requested at NAIP_RES
  (0.6 mercator-metres, ~0.5 m ground at 37°N) then resampled to the model grid.
* ``osm_tiles`` — openstreetmap-carto z19 tiles (``osmtiles.fetch_osm_tiles``), a last-resort
  *visual* layer, not imagery.

All providers request tiles in their native CRS then ``rasterio.warp.reproject`` into the
regular model grid (``model_grid``; rows increase with y — south-up — like ``Elevation``).
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
    source: str = ""           # provider name: icgc | ign_es | naip | osm_tiles
    detail: str = ""           # human-readable provenance (service, layer, CRS)

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
            dst.update_tags(source=self.source, detail=self.detail)
        return path

    @classmethod
    def from_geotiff(cls, path: Path | str) -> "OrthoImage":
        import rasterio
        with rasterio.open(path) as ds:
            arr = ds.read(out_dtype="uint8")[:3]
            t = ds.transform
            tags = ds.tags()
            source = tags.get("source", "")
            detail = tags.get("detail", "")
        if t.e >= 0:  # south-up file (unusual): rows already increase with y
            array = arr.transpose(1, 2, 0)
            dy = float(t.e)
            y0 = float(t.f) + dy / 2
        else:
            array = arr.transpose(1, 2, 0)[::-1]
            dy = float(-t.e)
            y0 = float(t.f) - (arr.shape[1] - 0.5) * dy
        return cls(np.ascontiguousarray(array), x0=float(t.c) + t.a / 2, y0=y0,
                   dx=float(t.a), dy=dy, source=source, detail=detail)

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



IGN_ES_WMS = "https://www.ign.es/wms-inspire/pnoa-ma"
IGN_ES_LAYER = "OI.OrthoimageCoverage"
IGN_ES_CRS = "EPSG:25830"
IGN_ES_RES = 0.25
NAIP_EXPORT_URL = ("https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/"
                   "exportImage")
NAIP_WMS_URL = "https://imagery.nationalmap.gov/arcgis/services/USGSNAIPPlus/ImageServer/WMSServer"
NAIP_WMS_LAYER = "USGSNAIPPlus:NaturalColor"
NAIP_CRS = "EPSG:3857"
NAIP_RES = 0.6           # request resolution in EPSG:3857 units (service native 0.3)
NAIP_MAX_PX = 4000       # ImageServer maxImageWidth/Height
DEFAULT_SOURCES: tuple[str, ...] = ("icgc", "ign_es", "osm_tiles")


def _cache_path(cache_dir: Path, bbox_swne, resolution: float, layer: str,
                provider: str = "icgc") -> Path:
    s, w, n, e = bbox_swne
    tag = f"{layer}|{resolution}" if provider == "icgc" else f"{provider}|{layer}|{resolution}"
    key = hashlib.sha1(tag.encode()).hexdigest()[:8]
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


def _getmap_tiff(session, layer: str, crs: str, bbox, width: int, height: int,
                 url: Optional[str] = None):
    params = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=layer, STYLES="",
                  CRS=crs, BBOX=",".join(f"{v:.3f}" for v in bbox), WIDTH=width, HEIGHT=height,
                  FORMAT="image/tiff")
    r = session.get(url or WMS_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/tiff"):
        raise RuntimeError(f"WMS returned {ctype}: {r.text[:300]}")
    return r.content


def _source_bounds(frame: LocalFrame, grid, resolution: float, src_crs: str):
    """Model grid outline -> (xmin, ymin, xmax, ymax) in the provider CRS."""
    from rasterio.warp import transform_bounds
    _, _, width, height, dst_transform = grid
    xmin, ymin, xmax, ymax = (dst_transform.c, dst_transform.f - height * resolution,
                              dst_transform.c + width * resolution, dst_transform.f)
    return transform_bounds(frame.crs, src_crs, xmin, ymin, xmax, ymax, densify_pts=21)


def _mosaic_tiles(frame: LocalFrame, grid, resolution: float, src_crs: str, src_res: float,
                  tile_px: int, get_tile, margin: Optional[float] = None) -> Optional[np.ndarray]:
    """Fetch every tile covering the grid with ``get_tile(bbox, w, h) -> tiff bytes`` and warp
    them into a north-up (3, H, W) uint8 array; ``None`` if nothing usable came back."""
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import reproject, Resampling
    _, _, width, height, dst_transform = grid
    sminx, sminy, smaxx, smaxy = _source_bounds(frame, grid, resolution, src_crs)
    margin = 2 * max(resolution, src_res) if margin is None else margin
    dst = np.zeros((3, height, width), dtype=np.uint8)
    n_tiles = 0
    for bbox, w, h in _tiles(sminx - margin, sminy - margin, smaxx + margin, smaxy + margin,
                             src_res, tile_px):
        data = get_tile(bbox, w, h)
        with MemoryFile(data) as mem, mem.open() as src:
            src_arr = src.read()[:3]
            if src_arr.shape[0] == 1:
                src_arr = np.repeat(src_arr, 3, axis=0)
            src_transform = src.transform
            file_crs = src.crs or rasterio.crs.CRS.from_string(src_crs)
        n_tiles += 1
        tile_dst = np.zeros_like(dst)
        reproject(src_arr, tile_dst, src_transform=src_transform, src_crs=file_crs,
                  dst_transform=dst_transform, dst_crs=frame.crs,
                  resampling=Resampling.bilinear, src_nodata=None, dst_nodata=0)
        hit = tile_dst.max(axis=0) > 0
        dst[:, hit] = tile_dst[:, hit]
    if n_tiles == 0 or dst.max() == 0:
        log.warning("imagery: provider returned no usable pixels")
        return None
    return dst


def _to_image(dst: np.ndarray, grid, resolution: float, source: str, detail: str) -> OrthoImage:
    x0, y0 = grid[0], grid[1]
    array = np.ascontiguousarray(dst.transpose(1, 2, 0)[::-1])  # rows increasing with y
    return OrthoImage(array, x0=x0, y0=y0, dx=resolution, dy=resolution, source=source,
                      detail=detail)


# --------------------------------------------------------------------------- providers

def _fetch_icgc(frame: LocalFrame, bbox_swne, resolution: float, grid, layer: str = DEFAULT_LAYER,
                **_) -> Optional[OrthoImage]:
    import requests
    session = requests.Session()

    def get_tile(bbox, w, h):
        try:
            return _getmap_tiff(session, layer, SOURCE_CRS, bbox, w, h, url=WMS_URL)
        except Exception as exc:
            if layer == FALLBACK_LAYER:
                raise
            log.warning("GetMap %s failed (%s); retrying with %s", layer, exc, FALLBACK_LAYER)
            return _getmap_tiff(session, FALLBACK_LAYER, SOURCE_CRS, bbox, w, h, url=WMS_URL)

    dst = _mosaic_tiles(frame, grid, resolution, SOURCE_CRS, max(NATIVE_RES, resolution), TILE_PX,
                        get_tile)
    if dst is None:
        return None
    return _to_image(dst, grid, resolution, "icgc",
                     f"ICGC WMS {layer} ({SOURCE_CRS} -> local)")


def _fetch_ign_es(frame: LocalFrame, bbox_swne, resolution: float, grid, **_) -> Optional[OrthoImage]:
    import requests
    session = requests.Session()
    dst = _mosaic_tiles(frame, grid, resolution, IGN_ES_CRS, max(IGN_ES_RES, resolution), TILE_PX,
                        lambda bbox, w, h: _getmap_tiff(session, IGN_ES_LAYER, IGN_ES_CRS, bbox, w, h,
                                                        url=IGN_ES_WMS))
    if dst is None:
        return None
    return _to_image(dst, grid, resolution, "ign_es",
                     f"IGN PNOA-MA WMS {IGN_ES_LAYER} ({IGN_ES_CRS} -> local)")


def _naip_export_tile(session, bbox, w: int, h: int) -> bytes:
    params = dict(bbox=",".join(f"{v:.3f}" for v in bbox), bboxSR=3857, imageSR=3857,
                  size=f"{w},{h}", format="tiff", f="image")
    r = session.get(NAIP_EXPORT_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/tiff"):
        raise RuntimeError(f"NAIP exportImage returned {ctype}: {r.text[:300]}")
    return r.content


def _fetch_naip(frame: LocalFrame, bbox_swne, resolution: float, grid, **_) -> Optional[OrthoImage]:
    import requests
    session = requests.Session()
    src_res = max(NAIP_RES, resolution)
    try:
        dst = _mosaic_tiles(frame, grid, resolution, NAIP_CRS, src_res, NAIP_MAX_PX,
                            lambda bbox, w, h: _naip_export_tile(session, bbox, w, h))
        detail = f"USGS NAIP Plus ImageServer exportImage @{src_res:g} m ({NAIP_CRS} -> local)"
    except Exception as exc:
        log.warning("imagery: NAIP exportImage failed (%s); trying the WMS endpoint", exc)
        dst = _mosaic_tiles(frame, grid, resolution, NAIP_CRS, src_res, NAIP_MAX_PX,
                            lambda bbox, w, h: _getmap_tiff(session, NAIP_WMS_LAYER, NAIP_CRS, bbox,
                                                            w, h, url=NAIP_WMS_URL))
        detail = f"USGS NAIP Plus WMS {NAIP_WMS_LAYER} @{src_res:g} m ({NAIP_CRS} -> local)"
    if dst is None:
        return None
    return _to_image(dst, grid, resolution, "naip", detail)


def _fetch_osm_tiles(frame: LocalFrame, bbox_swne, resolution: float, grid, cache_dir: Path = Path("data"),
                     use_cache: bool = True, **_) -> Optional[OrthoImage]:
    from .osmtiles import fetch_osm_tiles
    img = fetch_osm_tiles(frame, bbox_swne, resolution=resolution, cache_dir=cache_dir,
                          use_cache=use_cache)
    if img is None:
        return None
    img.detail = img.source
    img.source = "osm_tiles"
    return img


# name -> provider(frame, bbox_swne, resolution, grid, **opts) -> OrthoImage | None
PROVIDERS = {
    "icgc": _fetch_icgc,
    "ign_es": _fetch_ign_es,
    "naip": _fetch_naip,
    "osm_tiles": _fetch_osm_tiles,
}
# providers whose own module caches the result (skip the imagery-level cache for them)
_SELF_CACHING = {"osm_tiles"}


# --------------------------------------------------------------------------- public API

def fetch_ortho(frame: LocalFrame, bbox_swne: tuple[float, float, float, float],
                resolution: float = NATIVE_RES, cache_dir: Path | str = "data",
                sources: tuple[str, ...] | list[str] = DEFAULT_SOURCES,
                layer: str = DEFAULT_LAYER, pad_m: float = 2.0,
                use_cache: bool = True) -> Optional[OrthoImage]:
    """Fetch an orthophoto for ``bbox_swne`` (S, W, N, E in WGS84) into a model-space grid at
    ``resolution`` m, trying the named ``sources`` in order (see module doc; the profile's
    ``sources.ortho`` is the usual argument). ``layer`` is the ICGC layer.

    The winner's name is in ``OrthoImage.source``; ``None`` (warnings logged) when every
    provider fails.
    """
    cache_dir = Path(cache_dir)
    try:
        grid = model_grid(frame, bbox_swne, resolution, pad_m)
    except Exception as exc:
        log.warning("imagery: could not build model grid (%s)", exc)
        return None
    log.info("ortho grid %dx%d @ %.2f m, sources %s", grid[2], grid[3], resolution, list(sources))

    for name in sources:
        fn = PROVIDERS.get(name)
        if fn is None:
            log.warning("imagery: unknown source %r (known: %s)", name, sorted(PROVIDERS))
            continue
        cpath = _cache_path(cache_dir, bbox_swne, resolution, layer if name == "icgc" else "",
                            provider=name)
        if use_cache and name not in _SELF_CACHING and cpath.exists():
            try:
                img = OrthoImage.from_geotiff(cpath)
                img.source = name
                log.info("ortho cache hit %s [%s] (%dx%d)", cpath, name, img.width, img.height)
                return img
            except Exception as exc:  # corrupt cache -> refetch
                log.warning("ortho cache %s unreadable (%s); refetching", cpath, exc)
        try:
            img = fn(frame, bbox_swne, resolution, grid, layer=layer, cache_dir=cache_dir,
                     use_cache=use_cache)
        except Exception as exc:  # network, GDAL, format ... all degrade
            log.warning("imagery: source %s failed: %s", name, exc)
            img = None
        if img is None:
            continue
        img.source = name
        log.info("imagery: using %s -> %s", name, img.detail)
        if name not in _SELF_CACHING:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                img.save_geotiff(cpath, frame=frame)
                log.info("ortho cached -> %s", cpath)
            except Exception as exc:
                log.warning("imagery: could not write cache %s (%s)", cpath, exc)
        return img
    log.warning("imagery: no ortho source succeeded (%s)", list(sources))
    return None


# Backwards-friendly alias used in DESIGN.md
fetch = fetch_ortho
