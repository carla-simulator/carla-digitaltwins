"""DEM fetch -> ``twinmodel.model.Elevation`` on a model-space grid (worker D).

Sources are tried in order; the first that yields a raster wins. Every source degrades to
``None`` with a logged warning (never raises on network failure).

1. **ICGC 2 m LiDAR DTM via WMS GetFeatureInfo point sampling** (real elevation values).
   Investigation 2026-09-01: ICGC publishes no WCS. Its two DTM WMS services render
   8-bit colour ramps for GetMap (``image/tiff`` included), but ``GetFeatureInfo`` returns the
   raw float pixel value:

   * ``https://geoserveis.icgc.cat/serveis/catalunya/icgc_mdt2m/wms`` (ArcGIS WMS; layer
     ``MET2m`` = "Mapa d'Elevacions del Terreny 2 m", EPSG:25831; ~12 ms/request effective
     with 8 parallel connections). Response: ``@MET2m Stretch.Pixel Value; 31.14;``.
   * ``https://geoserveis.icgc.cat/servei/catalunya/elevacions-territorial/wms`` (MapServer;
     layer ``model-elevacions-terreny-catalunya-lidar-50cm-2021-2023`` = 50 cm LiDAR DTM, newer
     but ~35 ms/request; needs ``STYLES=``). Response: ``value_0 = '29.06'``.

   We sample a coarse grid (``sample_spacing``, default 8 m) with a thread pool and fit a
   bicubic spline (``scipy.interpolate.RectBivariateSpline``) to resample onto the 2 m model
   grid. A DTM in a city is smooth at that scale, so this loses little.
2. **OpenTopography Copernicus GLO-30** (``OPENTOPO_API_KEY`` env var required).
3. **IGN Spain WCS** ``https://servicios.idee.es/wcs-inspire/mdt`` coverage
   ``Elevacion25830_5`` (MDT05, 5 m, int16 metres, no login) — whole raster in one
   GetCoverage.
4. **Copernicus GLO-30 COG on AWS** (``copernicus-dem-30m`` bucket, no key). It is a DSM
   (buildings included) at 30 m — last resort, clearly labelled in ``Elevation.source``.
5. **USGS 3DEP** (US; 1 m where LiDAR exists, else 1/3 arc-second) via the ArcGIS ImageServer
   ``https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage``
   (``format=tiff&pixelType=F32&interpolation=RSP_BilinearInterpolation&f=image``, EPSG:3857,
   ``maxImageWidth`` 8000). Verified 2026-09-01 on Sunnyvale CA: an 890x700 request at 1 m
   returns a float32 GeoTIFF, 32-40 m a.s.l. Requested at USGS_3DEP_RES mercator-metres then
   warped bilinearly onto the model grid.
6. ``None`` -> pipeline uses z = 0.

``fetch_dem(..., sources=(...))`` walks the chain in the given order (the profile's
``sources.dem``); provider names: ``icgc_mdt2m``, ``icgc_terr``, ``opentopo``, ``ign_wcs``,
``copernicus_aws``, ``usgs_3dep``. The winner's *name* is stored in ``Elevation.source``; the
long description is attached as ``Elevation.detail`` and logged.

The result is cached as ``<cache_dir>/dem_<bbox>_<res>m.npz`` (``Elevation.to_npz``).
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

from ..frame import LocalFrame
from ..model import Elevation
from .imagery import model_grid

log = logging.getLogger("twinmodel.ingest.elevation")

ICGC_MDT2M_WMS = "https://geoserveis.icgc.cat/serveis/catalunya/icgc_mdt2m/wms"
ICGC_MDT2M_LAYER = "MET2m"
ICGC_TERR_WMS = "https://geoserveis.icgc.cat/servei/catalunya/elevacions-territorial/wms"
ICGC_TERR_LAYER = "model-elevacions-terreny-catalunya-lidar-50cm-2021-2023"
ICGC_CRS = "EPSG:25831"
IGN_WCS = "https://servicios.idee.es/wcs-inspire/mdt"
IGN_COVERAGE = "Elevacion25830_5"
IGN_CRS = "EPSG:25830"
OPENTOPO_URL = "https://portal.opentopography.org/API/globaldem"
COPERNICUS_AWS = ("https://copernicus-dem-30m.s3.amazonaws.com/"
                  "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM/Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM.tif")

USGS_3DEP_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/"
                 "exportImage")
USGS_3DEP_CRS = "EPSG:3857"
USGS_3DEP_RES = 1.0      # request resolution in EPSG:3857 units (service native 1 m)
USGS_3DEP_MAX_PX = 8000
DEFAULT_SOURCES: tuple[str, ...] = ("icgc_mdt2m", "icgc_terr", "opentopo", "ign_wcs", "copernicus_aws")
DEFAULT_RES = 2.0
DEFAULT_SAMPLE_SPACING = 8.0
GFI_THREADS = 12
GFI_TIMEOUT = 30
MAX_GFI_POINTS = 20000  # refuse to hammer the service beyond this


# --------------------------------------------------------------------------- helpers

def _grid_bounds(transform, width: int, height: int):
    """(xmin, ymin, xmax, ymax) of a north-up grid."""
    return (transform.c, transform.f - height * transform.a, transform.c + width * transform.a,
            transform.f)


def _elevation_from_north_up(z_north_up: np.ndarray, x0: float, y0: float, res: float,
                             source: str) -> Elevation:
    return Elevation(np.ascontiguousarray(z_north_up[::-1]).astype(np.float64), x0, y0, res, res,
                     source=source)


def _reproject_to_grid(src_arr: np.ndarray, src_transform, src_crs, src_nodata,
                       dst_transform, dst_crs, width: int, height: int) -> np.ndarray:
    from rasterio.warp import reproject, Resampling
    dst = np.full((height, width), np.nan, dtype=np.float32)
    reproject(src_arr.astype(np.float32), dst, src_transform=src_transform, src_crs=src_crs,
              src_nodata=src_nodata, dst_transform=dst_transform, dst_crs=dst_crs,
              dst_nodata=np.nan, resampling=Resampling.bilinear)
    return dst


def _fill_nan(z: np.ndarray) -> np.ndarray:
    """Nearest-neighbour fill of NaN holes (edge effects of the warp)."""
    if not np.isnan(z).any():
        return z
    from scipy import ndimage
    mask = np.isnan(z)
    if mask.all():
        return z
    idx = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
    return z[tuple(idx)]


# --------------------------------------------------------------------------- source 1: ICGC GFI

_RE_MDT2M = re.compile(r"Pixel Value;\s*([-+]?\d+(?:\.\d+)?)")
_RE_TERR = re.compile(r"value_0\s*=\s*'([^']*)'")


def _parse_gfi(text: str, kind: str) -> float:
    m = (_RE_MDT2M if kind == "mdt2m" else _RE_TERR).search(text)
    if not m:
        return np.nan
    try:
        return float(m.group(1))
    except ValueError:
        return np.nan


def _icgc_gfi_sample(frame: LocalFrame, grid, sample_spacing: float, service: str = "mdt2m",
                     threads: int = GFI_THREADS) -> Optional[Elevation]:
    import requests
    from pyproj import Transformer
    from scipy.interpolate import RectBivariateSpline

    x0, y0, width, height, transform = grid
    res = transform.a
    xmin, ymin, xmax, ymax = _grid_bounds(transform, width, height)
    # coarse sample grid, one spacing beyond the fine grid so the spline never extrapolates
    sx = np.arange(xmin - sample_spacing, xmax + 2 * sample_spacing, sample_spacing)
    sy = np.arange(ymin - sample_spacing, ymax + 2 * sample_spacing, sample_spacing)
    if sx.size * sy.size > MAX_GFI_POINTS:
        log.warning("elevation: %d GFI samples exceeds MAX_GFI_POINTS; increase sample_spacing",
                    sx.size * sy.size)
        return None
    gx, gy = np.meshgrid(sx, sy)
    to_utm = Transformer.from_crs(frame.crs, ICGC_CRS, always_xy=True)
    ux, uy = to_utm.transform(gx.ravel(), gy.ravel())

    if service == "mdt2m":
        url, layer, kind = ICGC_MDT2M_WMS, ICGC_MDT2M_LAYER, "mdt2m"
    else:
        url, layer, kind = ICGC_TERR_WMS, ICGC_TERR_LAYER, "terr"
    session = requests.Session()
    try:
        from requests.adapters import HTTPAdapter
        session.mount("https://", HTTPAdapter(pool_connections=threads, pool_maxsize=threads))
    except Exception:
        pass

    def one(i: int) -> float:
        x, y = ux[i], uy[i]
        params = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetFeatureInfo", LAYERS=layer,
                      QUERY_LAYERS=layer, STYLES="", CRS=ICGC_CRS,
                      BBOX=f"{x - 1:.3f},{y - 1:.3f},{x + 1:.3f},{y + 1:.3f}", WIDTH=2, HEIGHT=2,
                      I=1, J=1, INFO_FORMAT="text/plain")
        try:
            r = session.get(url, params=params, timeout=GFI_TIMEOUT)
            if r.status_code != 200:
                return np.nan
            return _parse_gfi(r.text, kind)
        except Exception:
            return np.nan

    # probe one point first so an unreachable service fails fast
    probe = one(ux.size // 2)
    if not np.isfinite(probe):
        log.warning("elevation: ICGC %s GetFeatureInfo probe failed", service)
        return None
    log.info("elevation: sampling ICGC %s via GetFeatureInfo: %d points @ %.1f m (%d threads)",
             service, ux.size, sample_spacing, threads)
    with ThreadPoolExecutor(threads) as ex:
        vals = np.fromiter(ex.map(one, range(ux.size)), dtype=np.float64, count=ux.size)
    zs = vals.reshape(gy.shape)
    bad = ~np.isfinite(zs)
    if bad.mean() > 0.05:
        log.warning("elevation: ICGC %s: %.1f%% of samples failed; giving up on this source",
                    service, 100 * bad.mean())
        return None
    if bad.any():
        zs = _fill_nan(zs)
    spline = RectBivariateSpline(sy, sx, zs, kx=3, ky=3, s=0)
    fx = x0 + np.arange(width) * res
    fy = y0 + np.arange(height) * res  # south -> north (rows increase with y)
    z = spline(fy, fx)  # (H, W), rows increase with y
    src = ("ICGC MET2m (LiDAR 2 m DTM) via WMS GetFeatureInfo" if kind == "mdt2m"
           else "ICGC LiDAR 50cm DTM 2021-2023 via WMS GetFeatureInfo")
    return Elevation(z, x0, y0, res, res,
                     source=f"{src}, point-sampled @{sample_spacing:g} m, bicubic to {res:g} m")


# --------------------------------------------------------------------------- source 2: OpenTopography

def _opentopo(frame: LocalFrame, bbox_swne, grid) -> Optional[Elevation]:
    key = os.environ.get("OPENTOPO_API_KEY")
    if not key:
        log.info("elevation: OPENTOPO_API_KEY not set; skipping OpenTopography")
        return None
    import requests
    from rasterio.io import MemoryFile
    s, w, n, e = bbox_swne
    pad = 0.002
    r = requests.get(OPENTOPO_URL, params=dict(demtype="COP30", south=s - pad, north=n + pad,
                                                west=w - pad, east=e + pad, outputFormat="GTiff",
                                                API_Key=key), timeout=120)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        log.warning("elevation: OpenTopography returned %s: %s", r.status_code, r.text[:200])
        return None
    x0, y0, width, height, transform = grid
    with MemoryFile(r.content) as mem, mem.open() as ds:
        z = _reproject_to_grid(ds.read(1), ds.transform, ds.crs, ds.nodata, transform, frame.crs,
                               width, height)
    return _elevation_from_north_up(_fill_nan(z), x0, y0, transform.a,
                                    "Copernicus GLO-30 DSM via OpenTopography")


# --------------------------------------------------------------------------- source 3: IGN WCS

def _ign_wcs(frame: LocalFrame, bbox_swne, grid) -> Optional[Elevation]:
    import requests
    from rasterio.io import MemoryFile
    from rasterio.warp import transform_bounds
    x0, y0, width, height, transform = grid
    xmin, ymin, xmax, ymax = _grid_bounds(transform, width, height)
    bx0, by0, bx1, by1 = transform_bounds(frame.crs, IGN_CRS, xmin, ymin, xmax, ymax, densify_pts=21)
    pad = 30.0
    params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
              ("coverageId", IGN_COVERAGE), ("format", "image/tiff"),
              ("subset", f"x({bx0 - pad},{bx1 + pad})"), ("subset", f"y({by0 - pad},{by1 + pad})")]
    r = requests.get(IGN_WCS, params=params, timeout=120)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        log.warning("elevation: IGN WCS returned %s: %s", r.status_code, r.text[:200])
        return None
    with MemoryFile(r.content) as mem, mem.open() as ds:
        arr = ds.read(1)
        nodata = ds.nodata
        if nodata is None and arr.dtype.kind == "i":
            nodata = arr.min() if arr.min() < -1000 else None
        z = _reproject_to_grid(arr, ds.transform, ds.crs or IGN_CRS, nodata, transform, frame.crs,
                               width, height)
    return _elevation_from_north_up(_fill_nan(z), x0, y0, transform.a,
                                    f"IGN MDT05 WCS {IGN_COVERAGE} (5 m, int16)")


# --------------------------------------------------------------------------- source 4: Copernicus AWS

def _copernicus_aws(frame: LocalFrame, bbox_swne, grid) -> Optional[Elevation]:
    import rasterio
    from rasterio.windows import from_bounds
    s, w, n, e = bbox_swne
    x0, y0, width, height, transform = grid
    tiles = {(int(np.floor(la)), int(np.floor(lo))) for la in (s, n) for lo in (w, e)}
    zs = []
    pad = 0.002
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        for la, lo in sorted(tiles):
            lat = f"{'N' if la >= 0 else 'S'}{abs(la):02d}"
            lon = f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}"
            url = "/vsicurl/" + COPERNICUS_AWS.format(lat=lat, lon=lon)
            with rasterio.open(url) as ds:
                win = from_bounds(max(w - pad, ds.bounds.left), max(s - pad, ds.bounds.bottom),
                                  min(e + pad, ds.bounds.right), min(n + pad, ds.bounds.top),
                                  ds.transform)
                arr = ds.read(1, window=win)
                z = _reproject_to_grid(arr, ds.window_transform(win), ds.crs, ds.nodata, transform,
                                       frame.crs, width, height)
            zs.append(z)
    z = zs[0]
    for other in zs[1:]:
        z = np.where(np.isnan(z), other, z)
    return _elevation_from_north_up(_fill_nan(z), x0, y0, transform.a,
                                    "Copernicus GLO-30 DSM (AWS COG, 30 m; buildings included)")


# --------------------------------------------------------------------------- source 5: USGS 3DEP

def _usgs_3dep(frame: LocalFrame, bbox_swne, grid) -> Optional[Elevation]:
    import requests
    from rasterio.io import MemoryFile
    from rasterio.warp import transform_bounds
    x0, y0, width, height, transform = grid
    xmin, ymin, xmax, ymax = _grid_bounds(transform, width, height)
    bx0, by0, bx1, by1 = transform_bounds(frame.crs, USGS_3DEP_CRS, xmin, ymin, xmax, ymax,
                                          densify_pts=21)
    pad = 4 * USGS_3DEP_RES
    bx0, by0, bx1, by1 = bx0 - pad, by0 - pad, bx1 + pad, by1 + pad
    w = int(np.ceil((bx1 - bx0) / USGS_3DEP_RES)); h = int(np.ceil((by1 - by0) / USGS_3DEP_RES))
    if w > USGS_3DEP_MAX_PX or h > USGS_3DEP_MAX_PX:
        log.warning("elevation: 3DEP request %dx%d exceeds the %d px cap", w, h, USGS_3DEP_MAX_PX)
        return None
    params = dict(bbox=f"{bx0:.3f},{by0:.3f},{bx1:.3f},{by1:.3f}", bboxSR=3857, imageSR=3857,
                  size=f"{w},{h}", format="tiff", pixelType="F32", noData="",
                  interpolation="RSP_BilinearInterpolation", f="image")
    r = requests.get(USGS_3DEP_URL, params=params, timeout=120)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/tiff"):
        log.warning("elevation: 3DEP exportImage returned %s %s: %s", r.status_code,
                    r.headers.get("content-type"), r.text[:200])
        return None
    with MemoryFile(r.content) as mem, mem.open() as ds:
        arr = ds.read(1).astype(np.float32)
        nodata = ds.nodata
        arr[~np.isfinite(arr) | (arr < -1000)] = np.nan   # service nodata is unset; guard anyway
        z = _reproject_to_grid(arr, ds.transform, ds.crs or USGS_3DEP_CRS, nodata, transform,
                               frame.crs, width, height)
    if np.isnan(z).mean() > 0.5:
        log.warning("elevation: 3DEP returned mostly nodata (%.0f%%)", 100 * np.isnan(z).mean())
        return None
    return _elevation_from_north_up(_fill_nan(z), x0, y0, transform.a,
                                    f"USGS 3DEP ImageServer exportImage @{USGS_3DEP_RES:g} m "
                                    f"({USGS_3DEP_CRS} -> local, bilinear)")


# --------------------------------------------------------------------------- public API

# name -> provider(frame, bbox_swne, grid, sample_spacing) -> Elevation | None
PROVIDERS = {
    "icgc_mdt2m": lambda frame, bbox, grid, spacing: _icgc_gfi_sample(frame, grid, spacing, "mdt2m"),
    "icgc_terr": lambda frame, bbox, grid, spacing: _icgc_gfi_sample(frame, grid, spacing, "terr"),
    "opentopo": lambda frame, bbox, grid, spacing: _opentopo(frame, bbox, grid),
    "ign_wcs": lambda frame, bbox, grid, spacing: _ign_wcs(frame, bbox, grid),
    "copernicus_aws": lambda frame, bbox, grid, spacing: _copernicus_aws(frame, bbox, grid),
    "usgs_3dep": lambda frame, bbox, grid, spacing: _usgs_3dep(frame, bbox, grid),
}

def _cache_path(cache_dir: Path, bbox_swne, res: float) -> Path:
    s, w, n, e = bbox_swne
    return cache_dir / f"dem_{s:.5f}_{w:.5f}_{n:.5f}_{e:.5f}_{res:g}m.npz"


def fetch_dem(frame: LocalFrame, bbox_swne: tuple[float, float, float, float],
              cache_dir: Path | str = "data", resolution: float = DEFAULT_RES,
              sample_spacing: float = DEFAULT_SAMPLE_SPACING, pad_m: float = 10.0,
              sources: Optional[tuple[str, ...] | list[str]] = DEFAULT_SOURCES,
              use_cache: bool = True) -> Optional[Elevation]:
    """Return an ``Elevation`` on a ``resolution`` m model-space grid covering ``bbox_swne``
    (plus ``pad_m``), or ``None`` if no source is reachable.

    ``sources`` is the ordered provider chain (names in ``PROVIDERS``; the profile's
    ``sources.dem`` is the usual argument); default ``DEFAULT_SOURCES``. The winner's name is
    stored in ``Elevation.source``.
    """
    cache_dir = Path(cache_dir)
    cpath = _cache_path(cache_dir, bbox_swne, resolution)
    if use_cache and cpath.exists():
        try:
            el = Elevation.from_npz(cpath)
            log.info("dem cache hit %s (%s)", cpath, el.source)
            return el
        except Exception as exc:
            log.warning("dem cache %s unreadable (%s); refetching", cpath, exc)

    try:
        grid = model_grid(frame, bbox_swne, resolution, pad_m)
    except Exception as exc:
        log.warning("elevation: could not build model grid (%s)", exc)
        return None

    order = DEFAULT_SOURCES if sources is None else sources
    for name in order:
        fn = PROVIDERS.get(name)
        if fn is None:
            log.warning("elevation: unknown source %r (known: %s)", name, sorted(PROVIDERS))
            continue
        try:
            el = fn(frame, bbox_swne, grid, sample_spacing)
        except Exception as exc:  # network, parse, GDAL ... all degrade
            log.warning("elevation: source %s failed: %s", name, exc)
            el = None
        if el is not None and np.isfinite(el.z).all():
            el.detail = el.source
            el.source = name
            log.info("elevation: using %s -> %s", name, el.detail)
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                el.to_npz(cpath)
            except Exception as exc:
                log.warning("elevation: could not cache %s (%s)", cpath, exc)
            return el
    log.warning("elevation: no DEM source reachable; pipeline should use z=0")
    return None


fetch = fetch_dem


# --------------------------------------------------------------------------- diagnostics

def plane_fit(el: Elevation) -> dict:
    """Least-squares plane z = a*x + b*y + c. Returns slope (%) and uphill azimuth
    (degrees, compass: 0 = N, 90 = E) plus min/max/mean."""
    H, W = el.z.shape
    xs = el.x0 + np.arange(W) * el.dx
    ys = el.y0 + np.arange(H) * el.dy
    gx, gy = np.meshgrid(xs, ys)
    A = np.c_[gx.ravel(), gy.ravel(), np.ones(gx.size)]
    a, b, c = np.linalg.lstsq(A, el.z.ravel(), rcond=None)[0]
    slope = float(np.hypot(a, b))
    az = float((np.degrees(np.arctan2(a, b)) + 360) % 360)  # atan2(east, north)
    return {"z_min": float(el.z.min()), "z_max": float(el.z.max()), "z_mean": float(el.z.mean()),
            "slope_pct": 100 * slope, "uphill_azimuth_deg": az,
            "uphill_toward": _compass(az), "source": el.source}


def _compass(az: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((az + 22.5) % 360) // 45)]


def save_quicklook(el: Elevation, path: Path | str, contour_m: float = 1.0) -> Path:
    """Hillshade + contours PNG for eyeballing."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource
    H, W = el.z.shape
    extent = (el.x0 - el.dx / 2, el.x0 + (W - 0.5) * el.dx, el.y0 - el.dy / 2, el.y0 + (H - 0.5) * el.dy)
    ls = LightSource(azdeg=315, altdeg=45)
    fig, ax = plt.subplots(figsize=(9, 8))
    rgb = ls.shade(el.z, cmap=plt.cm.terrain, blend_mode="overlay", vert_exag=3,
                   dx=el.dx, dy=el.dy)
    ax.imshow(rgb, extent=extent, origin="lower")
    levels = np.arange(np.floor(el.z.min()), np.ceil(el.z.max()) + contour_m, contour_m)
    xs = el.x0 + np.arange(W) * el.dx
    ys = el.y0 + np.arange(H) * el.dy
    cs = ax.contour(xs, ys, el.z, levels=levels, colors="k", linewidths=0.5, alpha=0.7)
    ax.clabel(cs, fmt="%.0f", fontsize=7)
    st = plane_fit(el)
    ax.set_title(f"{el.source}\nz {st['z_min']:.1f}..{st['z_max']:.1f} m, slope {st['slope_pct']:.2f}% "
                 f"uphill toward {st['uphill_toward']} ({st['uphill_azimuth_deg']:.0f} deg)", fontsize=9)
    ax.set_xlabel("x east (m)"); ax.set_ylabel("y north (m)")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path
