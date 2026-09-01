"""Visual-debug rasters: OSM tiles | ICGC ortho | mesh top view | diff, all on one grid (worker F).

``compare_build(build_dir, name)`` loads ``<build_dir>/<name>.twin`` and ``<name>.obj``,
fetches the OSM tile layer (``ingest.osmtiles``) and the ortho (``ingest.imagery``) — both
cached — renders the OBJ from above on the *same* model-space grid, and writes everything
north-up into ``<build_dir>/compare/`` together with ``layers.json`` (grid + stats + file
index) so a viewer can overlay the layers directly.

Internally every raster is kept **south-up** (``array[j, i]`` at ``x0 + i*dx, y0 + j*dy``,
the ``OrthoImage``/``Elevation`` convention) and flipped only when written to disk.

Agreement stats compare the mesh *road* pixels (``drivable`` ∪ ``crossing`` ∪ ``parking``
groups; ``ground`` and the raised groups are never road) against ``osmtiles.road_mask_from_tiles``. Caveat: the carto fill width at z19 is a
style width, not the real carriageway width — the IoU is a registration/shape indicator, not
a width metric. ``estimate_shift`` gives the translation (metres) that best aligns two masks
(FFT cross-correlation), which is what you want when something looks mis-registered.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping

from .frame import LocalFrame
from .ingest.imagery import OrthoImage, model_grid
from .model import TwinModel

log = logging.getLogger("twinmodel.compare")

PAD_M = 2.0                  # grid padding, must match imagery.fetch_ortho / osmtiles
OVERLAY_ALPHA = 0.55
JUNCTION_WINDOW_M = 120.0
N_JUNCTIONS = 3
ROAD_GROUPS = ("drivable", "crossing")  # parking lots are not road
# rasterisation order: later groups paint over earlier ones
DRAW_ORDER = ("ground", "drivable", "parking", "verge", "sidewalk", "island", "median", "crossing",
              "curb", "marking_white", "marking_yellow")
FALLBACK_COLORS: dict[str, tuple[int, int, int]] = {
    "drivable": (56, 56, 59), "sidewalk": (158, 156, 148), "island": (140, 158, 128),
    "crossing": (217, 217, 209), "median": (133, 148, 122), "verge": (92, 133, 69),
    "parking": (77, 77, 79),
    "curb": (128, 128, 128), "marking_white": (242, 242, 242), "marking_yellow": (242, 204, 38),
    "ground": (120, 133, 107),
}
DIFF_COLORS = {"agree": (150, 150, 150), "mesh_only": (230, 40, 40), "osm_only": (40, 80, 230)}


# --------------------------------------------------------------------------- grid

@dataclass(frozen=True)
class GridSpec:
    """Regular model-space raster grid, pixel-centre convention, rows increasing with y."""
    x0: float
    y0: float
    dx: float
    dy: float
    width: int
    height: int

    @classmethod
    def of(cls, grid) -> "GridSpec":
        if isinstance(grid, GridSpec):
            return grid
        if isinstance(grid, (tuple, list)):
            x0, y0, dx, dy, w, h = grid
            return cls(float(x0), float(y0), float(dx), float(dy), int(w), int(h))
        return cls(float(grid.x0), float(grid.y0), float(grid.dx), float(grid.dy),
                   int(grid.width), int(grid.height))

    @classmethod
    def from_bbox(cls, frame: LocalFrame, bbox_swne, resolution: float,
                  pad_m: float = PAD_M) -> "GridSpec":
        x0, y0, w, h, _ = model_grid(frame, bbox_swne, resolution, pad_m)
        return cls(x0, y0, resolution, resolution, w, h)

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x0 - self.dx / 2, self.y0 - self.dy / 2,
                self.x0 + (self.width - 0.5) * self.dx, self.y0 + (self.height - 0.5) * self.dy)

    def extent(self) -> tuple[float, float, float, float]:
        xmin, ymin, xmax, ymax = self.bounds()
        return (xmin, xmax, ymin, ymax)

    def north_up_transform(self):
        from rasterio.transform import from_origin
        xmin, _, _, ymax = self.bounds()
        return from_origin(xmin, ymax, self.dx, self.dy)

    def pixel_area(self) -> float:
        return self.dx * self.dy

    def matches(self, img: OrthoImage) -> bool:
        return (img.width == self.width and img.height == self.height
                and abs(img.x0 - self.x0) < 1e-6 and abs(img.y0 - self.y0) < 1e-6
                and abs(img.dx - self.dx) < 1e-9 and abs(img.dy - self.dy) < 1e-9)

    def regrid(self, img: OrthoImage) -> np.ndarray:
        """Nearest-neighbour resample of ``img`` onto this grid (0 outside)."""
        if self.matches(img):
            return img.array
        xs = self.x0 + np.arange(self.width) * self.dx
        ys = self.y0 + np.arange(self.height) * self.dy
        cols = np.rint((xs - img.x0) / img.dx).astype(int)
        rows = np.rint((ys - img.y0) / img.dy).astype(int)
        out = np.zeros((self.height, self.width, img.array.shape[2]), dtype=img.array.dtype)
        okc = (cols >= 0) & (cols < img.width)
        okr = (rows >= 0) & (rows < img.height)
        out[np.ix_(okr, okc)] = img.array[np.ix_(rows[okr], cols[okc])]
        return out


def _rasterize(shapes, grid: GridSpec, all_touched: bool = False) -> np.ndarray:
    """Rasterize GeoJSON-like shapes -> bool (H, W) south-up."""
    from rasterio.features import rasterize
    shapes = list(shapes)
    if not shapes:
        return np.zeros((grid.height, grid.width), dtype=bool)
    arr = rasterize(((s, 1) for s in shapes), out_shape=(grid.height, grid.width),
                    transform=grid.north_up_transform(), fill=0, all_touched=all_touched,
                    dtype="uint8")
    return arr[::-1].astype(bool)


# --------------------------------------------------------------------------- materials

def read_mtl_colors(mtl_path: Path | str | None) -> dict[str, tuple[int, int, int]]:
    """``newmtl`` name -> Kd as 0-255 RGB; falls back to FALLBACK_COLORS for missing ones."""
    colors = dict(FALLBACK_COLORS)
    if mtl_path is None or not Path(mtl_path).exists():
        return colors
    name = None
    for line in Path(mtl_path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "newmtl" and len(parts) > 1:
            name = parts[1]
        elif parts[0] == "Kd" and name and len(parts) >= 4:
            try:
                colors[name] = tuple(int(round(float(v) * 255)) for v in parts[1:4])
            except ValueError:
                pass
    return colors


# --------------------------------------------------------------------------- mesh top view

def _group_of(name: str) -> str:
    """trimesh geometry name -> OBJ group (it may append suffixes when names collide)."""
    for g in sorted(DRAW_ORDER, key=len, reverse=True):
        if name == g or name.startswith(g + "_") or name.startswith(g + "."):
            return g
    return re.sub(r"[._]\d+$", "", name)


def load_obj_groups(obj_path: Path | str) -> dict[str, np.ndarray]:
    """OBJ -> {group: (n, 3, 3) float triangles in model space}; groups merged by name."""
    import trimesh
    scene = trimesh.load(str(obj_path), force="scene", process=False)
    groups: dict[str, list[np.ndarray]] = {}
    for node in scene.graph.nodes_geometry:
        T, gname = scene.graph[node]
        geom = scene.geometry[gname]
        if not hasattr(geom, "faces") or len(geom.faces) == 0:
            continue
        v = np.asarray(geom.vertices, dtype=np.float64)
        if T is not None and not np.allclose(T, np.eye(4)):
            v = trimesh.transform_points(v, T)
        groups.setdefault(_group_of(gname), []).append(v[np.asarray(geom.faces)])
    return {g: np.concatenate(t, axis=0) for g, t in groups.items()}


def _tri_polygons(tris: np.ndarray):
    for t in tris[:, :, :2]:
        yield {"type": "Polygon", "coordinates": [[tuple(p) for p in t] + [tuple(t[0])]]}


def _tri_outlines(tris: np.ndarray):
    for t in tris[:, :, :2]:
        yield {"type": "LineString", "coordinates": [tuple(p) for p in t] + [tuple(t[0])]}


def mesh_group_masks(obj_path: Path | str, grid) -> dict[str, np.ndarray]:
    """{group: bool (H, W)} south-up masks of the OBJ's groups projected onto the grid.
    Curbs (vertical quads, zero plan area) are drawn as their outline; markings use
    ``all_touched`` so 12 cm quads survive a 25 cm grid."""
    grid = GridSpec.of(grid)
    masks: dict[str, np.ndarray] = {}
    for group, tris in load_obj_groups(obj_path).items():
        if group == "curb":
            masks[group] = _rasterize(_tri_outlines(tris), grid, all_touched=True)
        elif group.startswith("marking"):
            masks[group] = _rasterize(_tri_polygons(tris), grid, all_touched=True)
        else:
            masks[group] = _rasterize(_tri_polygons(tris), grid)
    return masks


def compose_masks(masks: dict[str, np.ndarray], colors: dict[str, tuple[int, int, int]],
                  shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Paint masks in DRAW_ORDER -> (rgb (H, W, 3) uint8, alpha (H, W) uint8)."""
    rgb = np.zeros((*shape, 3), dtype=np.uint8)
    alpha = np.zeros(shape, dtype=np.uint8)
    order = list(DRAW_ORDER) + [g for g in masks if g not in DRAW_ORDER]
    for group in order:
        m = masks.get(group)
        if m is None or not m.any():
            continue
        rgb[m] = colors.get(group, FALLBACK_COLORS.get(group, (200, 0, 200)))
        alpha[m] = 255
    return rgb, alpha


def render_mesh_top(obj_path: Path | str, grid, mtl_path: Path | str | None = None,
                    with_masks: bool = False):
    """Top view of an OBJ on ``grid`` (OrthoImage-like or (x0, y0, dx, dy, W, H)).

    Returns ``(rgb (H, W, 3) uint8, alpha (H, W) uint8)`` south-up (rows increase with y),
    colours from the sibling ``.mtl`` (or ``mtl_path``), transparent background. With
    ``with_masks=True`` also returns the per-group bool masks."""
    grid = GridSpec.of(grid)
    obj_path = Path(obj_path)
    if mtl_path is None:
        mtl_path = obj_path.with_suffix(".mtl")
    colors = read_mtl_colors(mtl_path)
    masks = mesh_group_masks(obj_path, grid)
    rgb, alpha = compose_masks(masks, colors, (grid.height, grid.width))
    return (rgb, alpha, masks) if with_masks else (rgb, alpha)


def surface_group_masks(model: TwinModel, grid) -> dict[str, np.ndarray]:
    """Same as ``mesh_group_masks`` but straight from ``model.surfaces``/curbs/markings."""
    grid = GridSpec.of(grid)
    by_kind: dict[str, list] = {}
    for s in model.surfaces:
        if s.geometry is None or s.geometry.is_empty:
            continue
        by_kind.setdefault(s.kind, []).append(mapping(shapely.force_2d(s.geometry)))
    masks = {k: _rasterize(v, grid) for k, v in by_kind.items()}
    curbs = [mapping(shapely.force_2d(c.geometry)) for c in model.curbs if not c.geometry.is_empty]
    if curbs:
        masks["curb"] = _rasterize(curbs, grid, all_touched=True)
    for color in ("white", "yellow"):
        mk = [mapping(shapely.force_2d(m.geometry).buffer((m.width or 0.12) / 2, cap_style="flat"))
              for m in model.markings if m.geometry is not None and m.color == color]
        if mk:
            masks[f"marking_{color}"] = _rasterize(mk, grid, all_touched=True)
    return masks


def render_surfaces_top(model: TwinModel, grid, colors: dict | None = None,
                        with_masks: bool = False):
    """Top view from the TwinModel surfaces (cross-check of the OBJ); same return as
    ``render_mesh_top``."""
    grid = GridSpec.of(grid)
    masks = surface_group_masks(model, grid)
    rgb, alpha = compose_masks(masks, colors or FALLBACK_COLORS, (grid.height, grid.width))
    return (rgb, alpha, masks) if with_masks else (rgb, alpha)


# --------------------------------------------------------------------------- diff / stats

def diff_masks(mesh_road: np.ndarray, osm_road: np.ndarray, pixel_area: float
               ) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """(rgb, alpha, stats) — grey agree, red mesh-only, blue OSM-only, transparent else."""
    mesh_road = mesh_road.astype(bool)
    osm_road = osm_road.astype(bool)
    both = mesh_road & osm_road
    mesh_only = mesh_road & ~osm_road
    osm_only = osm_road & ~mesh_road
    union = mesh_road | osm_road
    rgb = np.zeros((*mesh_road.shape, 3), dtype=np.uint8)
    alpha = np.zeros(mesh_road.shape, dtype=np.uint8)
    rgb[both] = DIFF_COLORS["agree"]
    rgb[mesh_only] = DIFF_COLORS["mesh_only"]
    rgb[osm_only] = DIFF_COLORS["osm_only"]
    alpha[union] = 255
    n_union = int(union.sum())
    stats = {
        "iou": float(both.sum() / n_union) if n_union else 0.0,
        "agree_m2": float(both.sum() * pixel_area),
        "mesh_only_m2": float(mesh_only.sum() * pixel_area),
        "osm_only_m2": float(osm_only.sum() * pixel_area),
        "mesh_road_m2": float(mesh_road.sum() * pixel_area),
        "osm_road_m2": float(osm_road.sum() * pixel_area),
        "mesh_covered_by_osm": float(both.sum() / mesh_road.sum()) if mesh_road.any() else 0.0,
        "osm_covered_by_mesh": float(both.sum() / osm_road.sum()) if osm_road.any() else 0.0,
    }
    return rgb, alpha, stats


def estimate_shift(a: np.ndarray, b: np.ndarray, resolution: float, max_shift_m: float = 6.0
                   ) -> dict[str, float]:
    """Translation (dx, dy metres, model axes) that moves mask ``a`` onto mask ``b`` best,
    by FFT cross-correlation of the mean-removed masks within ±``max_shift_m``. Returns also
    the normalised correlation at the peak and at zero shift (south-up inputs)."""
    fa = a.astype(np.float32); fb = b.astype(np.float32)
    fa -= fa.mean(); fb -= fb.mean()
    corr = np.fft.irfft2(np.fft.rfft2(fb) * np.conj(np.fft.rfft2(fa)), s=a.shape)
    corr = np.fft.fftshift(corr)
    cy, cx = a.shape[0] // 2, a.shape[1] // 2
    r = max(1, int(round(max_shift_m / resolution)))
    win = corr[cy - r:cy + r + 1, cx - r:cx + r + 1]
    j, i = np.unravel_index(np.argmax(win), win.shape)
    norm = float(np.sqrt((fa * fa).sum() * (fb * fb).sum())) or 1.0
    return {"dx_m": float((i - r) * resolution), "dy_m": float((j - r) * resolution),
            "peak_corr": float(win[j, i] / norm), "zero_corr": float(win[r, r] / norm)}


# --------------------------------------------------------------------------- writers

def _save_png(path: Path, rgb: np.ndarray, alpha: np.ndarray | None = None) -> Path:
    """Write a south-up array north-up (row 0 = north)."""
    from PIL import Image
    if alpha is not None:
        arr = np.dstack([rgb, alpha])[::-1]
        Image.fromarray(np.ascontiguousarray(arr), "RGBA").save(path)
    else:
        Image.fromarray(np.ascontiguousarray(rgb[::-1])).save(path)
    return path


def _save_jpg(path: Path, rgb: np.ndarray, quality: int = 85) -> Path:
    from PIL import Image
    Image.fromarray(np.ascontiguousarray(rgb[::-1])).save(path, quality=quality)
    return path


def blend(base: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, opacity: float = OVERLAY_ALPHA
          ) -> np.ndarray:
    a = (alpha.astype(np.float32) / 255.0 * opacity)[..., None]
    out = base.astype(np.float32) * (1 - a) + rgb.astype(np.float32) * a
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _crop(arr: np.ndarray, grid: GridSpec, cx: float, cy: float, size_m: float
          ) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Window of ``size_m`` around (cx, cy) from a south-up array; returns (crop, extent)."""
    half = size_m / 2
    c0 = int(np.floor((cx - half - grid.x0) / grid.dx)); c1 = int(np.ceil((cx + half - grid.x0) / grid.dx))
    r0 = int(np.floor((cy - half - grid.y0) / grid.dy)); r1 = int(np.ceil((cy + half - grid.y0) / grid.dy))
    c0, c1 = max(0, c0), min(grid.width, c1)
    r0, r1 = max(0, r0), min(grid.height, r1)
    extent = (grid.x0 + (c0 - 0.5) * grid.dx, grid.x0 + (c1 - 0.5) * grid.dx,
              grid.y0 + (r0 - 0.5) * grid.dy, grid.y0 + (r1 - 0.5) * grid.dy)
    return arr[r0:r1, c0:c1], extent


def _polys(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _outline(ax, geom, **kw):
    for p in _polys(geom):
        xy = np.asarray(p.exterior.coords)[:, :2]
        ax.plot(xy[:, 0], xy[:, 1], **kw)


def _stats_title(stats: dict[str, float]) -> str:
    return (f"IoU {stats['iou']:.3f}   agree {stats['agree_m2']:.0f} m²   "
            f"mesh-only {stats['mesh_only_m2']:.0f} m²   OSM-only {stats['osm_only_m2']:.0f} m²")


def write_side_by_side(path: Path, grid: GridSpec, osm: np.ndarray, ortho: np.ndarray,
                       mesh_rgb: np.ndarray, mesh_alpha: np.ndarray, diff_rgb: np.ndarray,
                       diff_alpha: np.ndarray, title: str, dpi: int = 130,
                       junction_polys: list | None = None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ext = grid.extent()
    aspect = (ext[3] - ext[2]) / max(ext[1] - ext[0], 1e-6)
    w = 16.0
    fig, axes = plt.subplots(2, 2, figsize=(w, w * aspect + 1.2), sharex=True, sharey=True)
    panels = [
        ("OSM tiles (carto z19)", osm, None),
        (f"ortho ({getattr(ortho, 'source', 'n/a')})", ortho, None),
        ("mesh top view (.obj)", np.dstack([mesh_rgb, mesh_alpha]), "#f2efe6"),
        ("diff: grey agree · red mesh-only · blue OSM-only", np.dstack([diff_rgb, diff_alpha]), "#ffffff"),
    ]
    for ax, (label, arr, bg) in zip(axes.ravel(), panels):
        if bg:
            ax.set_facecolor(bg)
        ax.imshow(arr, extent=ext, origin="lower", interpolation="nearest")
        if junction_polys:
            for jid, poly in junction_polys:
                _outline(ax, poly, color="#ff2d55", lw=0.7, ls="--")
        ax.set_title(label, fontsize=10)
        ax.set_aspect("equal")
        ax.grid(True, color="#00000022", lw=0.4)
    for ax in axes[1]:
        ax.set_xlabel("x east [m]")
    for ax in axes[:, 0]:
        ax.set_ylabel("y north [m]")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def write_triptych(path: Path, extent, osm: np.ndarray, ortho: np.ndarray, mesh_rgba: np.ndarray,
                   title: str, junction_poly=None, dpi: int = 130) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(18, 6.6), sharex=True, sharey=True)
    for ax, (label, arr) in zip(axes, [("OSM tiles", osm), ("ortho", ortho), ("mesh", mesh_rgba)]):
        ax.set_facecolor("#f2efe6")
        ax.imshow(arr, extent=extent, origin="lower", interpolation="nearest")
        if junction_poly is not None:
            _outline(ax, junction_poly, color="#ff2d55", lw=1.0, ls="--")
        ax.set_title(label, fontsize=10)
        ax.set_aspect("equal")
        ax.set_xlabel("x east [m]")
        ax.grid(True, color="#00000033", lw=0.4)
    axes[0].set_ylabel("y north [m]")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- driver

def _largest_junctions(model: TwinModel, n: int) -> list:
    js = [j for j in model.junctions if j.polygon is not None and not j.polygon.is_empty]
    js.sort(key=lambda j: j.polygon.area, reverse=True)
    return js[:n]


def compare_build(build_dir: Path | str, name: str, out_dir: Path | str | None = None,
                  resolution: float = 0.25, zoom: int = 19, cache_dir: Path | str = "data",
                  n_junctions: int = N_JUNCTIONS, junction_window_m: float = JUNCTION_WINDOW_M
                  ) -> dict[str, Any]:
    """Build the comparison rasters for ``<build_dir>/<name>.{twin,obj,mtl}``; returns the
    ``layers.json`` dict (grid, stats, file index)."""
    build_dir = Path(build_dir)
    out_dir = Path(out_dir) if out_dir is not None else build_dir / "compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    twin_dir = build_dir / f"{name}.twin"
    obj_path = build_dir / f"{name}.obj"
    if not twin_dir.exists():
        raise FileNotFoundError(twin_dir)
    if not obj_path.exists():
        raise FileNotFoundError(obj_path)

    model = TwinModel.load(twin_dir)
    bbox = tuple(model.bbox_wgs84)
    frame = LocalFrame(model.origin_lat, model.origin_lon)
    grid = GridSpec.from_bbox(frame, bbox, resolution)
    shape = (grid.height, grid.width)
    log.info("compare grid %dx%d @ %.2f m, bounds %s", grid.width, grid.height, resolution,
             tuple(round(v, 2) for v in grid.bounds()))

    from .ingest.osmtiles import fetch_osm_tiles, road_mask_from_tiles
    from .ingest.imagery import fetch_ortho
    tiles = fetch_osm_tiles(frame, bbox, zoom=zoom, resolution=resolution, cache_dir=cache_dir)
    from . import profiles
    prof_name = (model.metadata.get("profile") or {}).get("name")
    prof = profiles.by_name(prof_name) if prof_name in profiles.PROFILES else profiles.get()
    log.info("compare: imagery sources from profile %s: %s", prof.name, prof.sources.ortho)
    ortho = fetch_ortho(frame, bbox, resolution=resolution, cache_dir=cache_dir, sources=prof.sources.ortho)
    files: dict[str, str] = {}
    layers: dict[str, Any] = {}

    osm_arr = grid.regrid(tiles) if tiles is not None else None
    ortho_arr = grid.regrid(ortho) if ortho is not None else None
    if osm_arr is None:
        log.warning("no OSM tiles: diff/overlay_osm skipped")
    if ortho_arr is None:
        log.warning("no ortho: overlay_ortho skipped")

    # mesh top view + cross-check against the model surfaces
    mesh_rgb, mesh_alpha, masks = render_mesh_top(obj_path, grid, with_masks=True)
    mesh_road = np.zeros(shape, dtype=bool)
    for g in ROAD_GROUPS:
        if g in masks:
            mesh_road |= masks[g]
    surf_masks = surface_group_masks(model, grid)
    surf_road = np.zeros(shape, dtype=bool)
    for g in ROAD_GROUPS:
        if g in surf_masks:
            surf_road |= surf_masks[g]
    xu = int((mesh_road | surf_road).sum())
    mesh_vs_surfaces_iou = float((mesh_road & surf_road).sum() / xu) if xu else 1.0
    log.info("mesh road vs model surfaces IoU %.4f (mesh %.0f m², surfaces %.0f m²)",
             mesh_vs_surfaces_iou, mesh_road.sum() * grid.pixel_area(),
             surf_road.sum() * grid.pixel_area())
    group_area = {g: float(m.sum() * grid.pixel_area()) for g, m in masks.items()}

    files["mesh_top"] = _save_png(out_dir / "mesh_top.png", mesh_rgb, mesh_alpha).name
    if osm_arr is not None:
        files["osm_tiles"] = _save_png(out_dir / "osm_tiles.png", osm_arr).name
        files["overlay_osm"] = _save_png(out_dir / "overlay_osm.png",
                                         blend(osm_arr, mesh_rgb, mesh_alpha)).name
    if ortho_arr is not None:
        files["ortho"] = _save_jpg(out_dir / "ortho.jpg", ortho_arr).name
        files["overlay_ortho"] = _save_jpg(out_dir / "overlay_ortho.jpg",
                                           blend(ortho_arr, mesh_rgb, mesh_alpha)).name

    stats: dict[str, Any] = {"mesh_vs_surfaces_iou": mesh_vs_surfaces_iou,
                             "mesh_group_area_m2": group_area}
    diff_rgb = np.zeros((*shape, 3), dtype=np.uint8)
    diff_alpha = np.zeros(shape, dtype=np.uint8)
    if osm_arr is not None:
        osm_road = road_mask_from_tiles(osm_arr, resolution=resolution)
        diff_rgb, diff_alpha, dstats = diff_masks(mesh_road, osm_road, grid.pixel_area())
        stats.update(dstats)
        stats["shift_mesh_to_osm"] = estimate_shift(mesh_road, osm_road, resolution)
        files["diff"] = _save_png(out_dir / "diff.png", diff_rgb, diff_alpha).name
        files["osm_road_mask"] = _save_png(out_dir / "osm_road_mask.png",
                                           np.repeat(osm_road[..., None] * 255, 3, axis=-1).astype(np.uint8)).name
        log.info("mesh road vs OSM tiles: %s; shift %s", _stats_title(dstats),
                 stats["shift_mesh_to_osm"])
    if ortho_arr is not None:
        # best-effort registration check against the ortho's own asphalt mask
        try:
            from .refine import road_mask as ortho_road_mask
            prior = shapely.union_all([shapely.force_2d(s.geometry)
                                       for s in model.surfaces_of("drivable")]) or None
            oimg = OrthoImage(ortho_arr, grid.x0, grid.y0, grid.dx, grid.dy, source="regrid")
            omask = ortho_road_mask(oimg, prior, method="classical")
            stats["shift_mesh_to_ortho"] = estimate_shift(mesh_road, omask, resolution)
            if osm_arr is not None:
                stats["shift_osm_to_ortho"] = estimate_shift(osm_road, omask, resolution)
            files["ortho_road_mask"] = _save_png(
                out_dir / "ortho_road_mask.png",
                np.repeat(omask[..., None] * 255, 3, axis=-1).astype(np.uint8)).name
            log.info("shift mesh->ortho %s", stats["shift_mesh_to_ortho"])
        except Exception as exc:  # refine is optional for this tool
            log.warning("ortho road mask unavailable (%s)", exc)

    # side by side
    junctions = _largest_junctions(model, n_junctions)
    jpolys = [(j.id, j.polygon) for j in model.junctions if j.polygon is not None]
    blank = np.full((*shape, 3), 242, dtype=np.uint8)
    title = f"{name}  {grid.width}x{grid.height} @ {resolution} m"
    if "iou" in stats:
        title += "   |   " + _stats_title(stats)
    files["side_by_side"] = write_side_by_side(
        out_dir / "side_by_side.png", grid, osm_arr if osm_arr is not None else blank,
        ortho_arr if ortho_arr is not None else blank, mesh_rgb, mesh_alpha, diff_rgb, diff_alpha,
        title, junction_polys=jpolys).name

    # junction crops
    jinfo = []
    mesh_rgba = np.dstack([mesh_rgb, mesh_alpha])
    for j in junctions:
        c = j.polygon.centroid
        cx, cy = float(c.x), float(c.y)
        entry: dict[str, Any] = {"id": j.id, "centre": [cx, cy], "area_m2": float(j.polygon.area),
                                 "window_m": junction_window_m, "files": {}}
        crop_m, extent = _crop(mesh_rgba, grid, cx, cy, junction_window_m)
        entry["extent"] = list(extent)
        entry["files"]["mesh"] = _save_png(out_dir / f"junction_{j.id}_mesh.png",
                                           crop_m[..., :3], crop_m[..., 3]).name
        crop_o = crop_t = None
        if osm_arr is not None:
            crop_t, _ = _crop(osm_arr, grid, cx, cy, junction_window_m)
            entry["files"]["osm"] = _save_png(out_dir / f"junction_{j.id}_osm.png", crop_t).name
        if ortho_arr is not None:
            crop_o, _ = _crop(ortho_arr, grid, cx, cy, junction_window_m)
            entry["files"]["ortho"] = _save_png(out_dir / f"junction_{j.id}_ortho.png", crop_o).name
        blank_c = np.full((*crop_m.shape[:2], 3), 242, dtype=np.uint8)
        entry["files"]["triptych"] = write_triptych(
            out_dir / f"junction_{j.id}_triptych.png", extent,
            crop_t if crop_t is not None else blank_c, crop_o if crop_o is not None else blank_c,
            crop_m, f"{name} junction {j.id}  ({j.polygon.area:.0f} m², {junction_window_m:.0f} m window)",
            junction_poly=j.polygon).name
        jinfo.append(entry)

    xmin, ymin, xmax, ymax = grid.bounds()
    layers = {
        "name": name, "bbox_swne": list(bbox), "origin_lat": model.origin_lat,
        "origin_lon": model.origin_lon, "geo_reference": frame.proj4,
        "x0": grid.x0, "y0": grid.y0, "dx": grid.dx, "dy": grid.dy,
        "width": grid.width, "height": grid.height, "resolution": resolution,
        "bounds": [xmin, ymin, xmax, ymax], "north_up": True, "ortho_source": getattr(ortho, "source", None), "zoom": zoom,
        "note": "PNG/JPG row 0 is the north edge (y = bounds[3]); model arrays are south-up",
        "zoom": zoom, "osm_tiles": tiles is not None, "ortho": ortho is not None,
        "stats": stats, "files": files, "junctions": jinfo,
        "sources": {"osm_tiles": tiles.source if tiles else None,
                    "ortho": ortho.source if ortho else None},
    }
    (out_dir / "layers.json").write_text(json.dumps(_json_safe(layers), indent=2))
    log.info("wrote %s", out_dir / "layers.json")
    return layers


def _json_safe(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return o
