"""Surfaces -> triangulated Wavefront .obj (+ .mtl), and a top-down preview PNG.

DESIGN.md §Mesh. The .obj is written in model space (x east, y north, z up, metres); whoever
loads it into Unreal does ``(x, -y, z) * 100``. Groups: ``drivable``, ``sidewalk``, ``island``,
``crossing``, ``median``, ``parking``, ``curb``, ``marking_white``, ``marking_yellow``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..model import CurbLine, Marking, Surface, TwinModel

log = logging.getLogger("twinmodel.export.mesh")

MARKING_Z = 0.002
MARKING_WIDTH = 0.12
BROKEN_DASH = 2.0
BROKEN_GAP = 4.0
ELEVATION_GRID = 5.0
MIN_TRI_AREA = 1e-7

# material name -> (Kd r g b)
MATERIALS: dict[str, tuple[float, float, float]] = {
    "drivable": (0.22, 0.22, 0.23),
    "sidewalk": (0.62, 0.61, 0.58),
    "island": (0.55, 0.62, 0.50),
    "crossing": (0.85, 0.85, 0.82),
    "median": (0.52, 0.58, 0.48),
    "parking": (0.30, 0.30, 0.31),
    "curb": (0.50, 0.50, 0.50),
    "marking_white": (0.95, 0.95, 0.95),
    "marking_yellow": (0.95, 0.80, 0.15),
}


# --------------------------------------------------------------------------- triangulation

def _ring_xy(ring) -> np.ndarray:
    c = np.asarray(ring.coords, dtype=np.float64)[:, :2]
    if len(c) > 1 and np.allclose(c[0], c[-1]):
        c = c[:-1]
    return c


def _signed_area(tri: np.ndarray) -> np.ndarray:
    """Signed area of (n, 3, 2) triangles (positive = CCW)."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    return 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))


def _earcut(poly: Polygon) -> tuple[np.ndarray, np.ndarray] | None:
    import mapbox_earcut

    rings = [_ring_xy(poly.exterior)] + [_ring_xy(h) for h in poly.interiors]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    verts = np.concatenate(rings, axis=0)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    idx = mapbox_earcut.triangulate_float64(verts, ends)
    if len(idx) == 0:
        return None
    return verts, np.asarray(idx, dtype=np.int64).reshape(-1, 3)


def _delaunay(poly: Polygon) -> tuple[np.ndarray, np.ndarray] | None:
    tris = shapely.delaunay_triangles(poly)
    verts: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    index: dict[tuple[float, float], int] = {}
    for t in tris.geoms:
        if not poly.contains(t.representative_point()):
            continue
        ids = []
        for x, y in np.asarray(t.exterior.coords)[:3, :2]:
            key = (float(x), float(y))
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            ids.append(index[key])
        faces.append(tuple(ids))
    if not faces:
        return None
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def triangulate_polygon(poly: Polygon) -> tuple[np.ndarray, np.ndarray]:
    """(vertices (n,2), faces (m,3)) covering ``poly``, CCW faces. Earcut with a containment
    check, Delaunay + inside filter as fallback."""
    if poly.is_empty or poly.area <= 0:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    result = _earcut(poly)
    ok = False
    if result is not None:
        verts, faces = result
        tri = verts[faces]
        area = _signed_area(tri)
        keep = np.abs(area) > MIN_TRI_AREA
        faces, tri, area = faces[keep], tri[keep], area[keep]
        if len(faces):
            cent = tri.mean(axis=1)
            inside = shapely.contains_xy(poly, cent[:, 0], cent[:, 1])
            area_ok = abs(float(np.abs(area).sum()) - poly.area) <= max(1e-3, 0.005 * poly.area)
            ok = bool(inside.all()) and area_ok
    if not ok:
        result = _delaunay(poly)
        if result is None:
            log.warning("triangulation failed for polygon with area %.3f", poly.area)
            return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
        verts, faces = result
        tri = verts[faces]
        area = _signed_area(tri)
        keep = np.abs(area) > MIN_TRI_AREA
        faces, area = faces[keep], area[keep]
    # enforce CCW
    flip = area < 0
    faces[flip] = faces[flip][:, [0, 2, 1]]
    return verts, faces


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_polygons(g))
        return out
    return []


def _grid_split(geom: BaseGeometry, cell: float) -> list[Polygon]:
    """Cut a polygon by a square grid so a flat triangulation can follow the terrain."""
    out: list[Polygon] = []
    for poly in _polygons(geom):
        poly = poly.segmentize(cell)
        minx, miny, maxx, maxy = poly.bounds
        x0 = np.floor(minx / cell) * cell
        y0 = np.floor(miny / cell) * cell
        xs = np.arange(x0, maxx + cell, cell)
        ys = np.arange(y0, maxy + cell, cell)
        for x in xs:
            for y in ys:
                piece = poly.intersection(box(x, y, x + cell, y + cell))
                out.extend(p for p in _polygons(piece) if p.area > 1e-6)
    return out


# --------------------------------------------------------------------------- OBJ writer

class _ObjWriter:
    def __init__(self):
        self.vertices: list[tuple[float, float, float]] = []
        self.groups: dict[str, list[tuple[int, int, int]]] = {}

    def add_vertices(self, xyz: np.ndarray) -> int:
        base = len(self.vertices)
        self.vertices.extend((float(x), float(y), float(z)) for x, y, z in xyz)
        return base

    def add_faces(self, group: str, faces: np.ndarray, base: int) -> None:
        g = self.groups.setdefault(group, [])
        g.extend((int(a) + base + 1, int(b) + base + 1, int(c) + base + 1) for a, b, c in faces)

    def write(self, path_obj: Path, mtl_name: str) -> None:
        with open(path_obj, "w") as f:
            f.write("# twinmodel surface mesh — model space (x east, y north, z up, metres)\n")
            f.write(f"mtllib {mtl_name}\n")
            for x, y, z in self.vertices:
                f.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
            for group in MATERIALS:
                faces = self.groups.get(group)
                if not faces:
                    continue
                f.write(f"g {group}\nusemtl {group}\n")
                for a, b, c in faces:
                    f.write(f"f {a} {b} {c}\n")


def _write_mtl(path_mtl: Path) -> None:
    with open(path_mtl, "w") as f:
        f.write("# twinmodel materials\n")
        for name, (r, g, b) in MATERIALS.items():
            f.write(f"newmtl {name}\nKa {r:.3f} {g:.3f} {b:.3f}\nKd {r:.3f} {g:.3f} {b:.3f}\n"
                    f"Ks 0.050 0.050 0.050\nNs 10\nd 1.0\nillum 2\n\n")


def _z(model: TwinModel, xy: np.ndarray, z_offset: float) -> np.ndarray:
    if len(xy) == 0:
        return np.zeros(0)
    z = model.sample_z(xy[:, 0], xy[:, 1])
    return np.asarray(z, dtype=np.float64) + z_offset


def _add_surface(w: _ObjWriter, model: TwinModel, geom: BaseGeometry, z_offset: float,
                 group: str, subdivide: bool) -> int:
    polys = _grid_split(geom, ELEVATION_GRID) if subdivide else _polygons(geom)
    n = 0
    for poly in polys:
        verts, faces = triangulate_polygon(poly)
        if len(faces) == 0:
            continue
        xyz = np.column_stack([verts, _z(model, verts, z_offset)])
        base = w.add_vertices(xyz)
        w.add_faces(group, faces, base)
        n += len(faces)
    return n


def _add_curb(w: _ObjWriter, model: TwinModel, curb: CurbLine, subdivide: bool) -> int:
    line = shapely.force_2d(curb.geometry)
    if subdivide:
        line = line.segmentize(ELEVATION_GRID)
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    if len(xy) < 2:
        return 0
    z0 = _z(model, xy, 0.0)
    z1 = z0 + curb.height
    low = np.column_stack([xy, z0])
    high = np.column_stack([xy, z1])
    base = w.add_vertices(np.concatenate([low, high], axis=0))
    n = len(xy)
    faces = []
    for i in range(n - 1):
        a, b, c, d = i, i + 1, n + i + 1, n + i
        faces.append((a, b, c))
        faces.append((a, c, d))
    w.add_faces("curb", np.asarray(faces, dtype=np.int64), base)
    return len(faces)


def _add_marking(w: _ObjWriter, model: TwinModel, mk: Marking, subdivide: bool) -> int:
    if mk.geometry is None or mk.geometry.is_empty:
        return 0
    from shapely.ops import substring

    line = shapely.force_2d(mk.geometry)
    pieces: list[LineString]
    if mk.kind == "broken":
        pieces = []
        s, L = 0.0, line.length
        while s < L:
            e = min(L, s + BROKEN_DASH)
            if e - s > 0.05:
                pieces.append(substring(line, s, e))
            s += BROKEN_DASH + BROKEN_GAP
    else:
        pieces = [line]
    group = "marking_yellow" if mk.color == "yellow" else "marking_white"
    width = mk.width or MARKING_WIDTH
    n = 0
    for piece in pieces:
        quad = piece.buffer(width / 2.0, cap_style="flat", join_style="mitre", mitre_limit=2.0)
        n += _add_surface(w, model, quad, MARKING_Z, group, subdivide)
    return n


def export_obj(model: TwinModel, path_obj: Path | str) -> None:
    """Write ``path_obj`` and a sibling ``.mtl`` with one material per group."""
    path_obj = Path(path_obj)
    path_mtl = path_obj.with_suffix(".mtl")
    subdivide = model.elevation is not None
    w = _ObjWriter()
    counts: dict[str, int] = {}
    for s in model.surfaces:
        group = s.kind if s.kind in MATERIALS else "drivable"
        counts[group] = counts.get(group, 0) + _add_surface(w, model, s.geometry, s.z_offset, group, subdivide)
    for c in model.curbs:
        counts["curb"] = counts.get("curb", 0) + _add_curb(w, model, c, subdivide)
    for mk in model.markings:
        group = "marking_yellow" if mk.color == "yellow" else "marking_white"
        counts[group] = counts.get(group, 0) + _add_marking(w, model, mk, subdivide)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    w.write(path_obj, path_mtl.name)
    _write_mtl(path_mtl)
    log.info("wrote %s (%d vertices, faces per group: %s)", path_obj, len(w.vertices), counts)


# --------------------------------------------------------------------------- preview

PREVIEW_COLORS = {
    "drivable": "#3a3a3d",
    "sidewalk": "#c9c4b8",
    "island": "#9fb08a",
    "crossing": "#eeeeea",
    "median": "#8fa382",
    "parking": "#55555a",
}
SIGNAL_STYLE = {
    "traffic_light": dict(marker="o", color="#ff3b30", ms=6),
    "stop": dict(marker="8", color="#d0021b", ms=6),
    "yield": dict(marker="v", color="#f5a623", ms=6),
    "speed_limit": dict(marker="s", color="#4a90e2", ms=5),
    "crosswalk": dict(marker="x", color="#111111", ms=5),
    "crossing": dict(marker="x", color="#111111", ms=5),
}


def export_preview_png(model: TwinModel, path_png: Path | str, ortho: np.ndarray | None = None,
                       extent: tuple[float, float, float, float] | None = None,
                       dpi: int = 150, title: Optional[str] = None,
                       window: tuple[float, float, float, float] | None = None) -> None:
    """Top-down plot: surfaces filled by kind, curbs, markings, junction polygons outlined,
    lane reference lines thin, signals as markers. ``ortho`` (H, W[, 3]) is drawn underneath
    with ``extent = (xmin, xmax, ymin, ymax)`` in model space. ``window`` (same layout)
    restricts the plotted area without changing the ortho georeference (zooms)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    geoms: list[BaseGeometry] = [s.geometry for s in model.surfaces]
    geoms += [r.reference_line for r in model.roads]
    geoms += [b.footprint for b in model.buildings]
    if window is not None:
        plot_extent = tuple(window)
    elif extent is None:
        if geoms:
            minx, miny, maxx, maxy = unary_union([shapely.force_2d(g) for g in geoms]).bounds
        else:
            minx, miny, maxx, maxy = -50, -50, 50, 50
        pad = 0.03 * max(maxx - minx, maxy - miny, 10)
        plot_extent = (minx - pad, maxx + pad, miny - pad, maxy + pad)
    else:
        plot_extent = tuple(extent)
    wx, wy = plot_extent[1] - plot_extent[0], plot_extent[3] - plot_extent[2]
    size = 12.0
    figsize = (size, max(4.0, size * wy / max(wx, 1e-6)))
    fig, ax = plt.subplots(figsize=figsize)
    ax.set_facecolor("#f2efe6")
    if ortho is not None:
        ax.imshow(ortho, extent=extent if extent is not None else plot_extent, origin="upper",
                  interpolation="bilinear", zorder=0)

    def patch(poly: Polygon, **kw):
        verts, codes = [], []
        for ring in [poly.exterior] + list(poly.interiors):
            c = np.asarray(ring.coords)[:, :2]
            verts.extend(c.tolist())
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
        ax.add_patch(PathPatch(MplPath(verts, codes), **kw))

    for b in model.buildings:
        for p in _polygons(b.footprint):
            patch(p, facecolor="#d8b49a", edgecolor="#8a5a3c", lw=0.5, alpha=0.6, zorder=1)
    order = {"drivable": 2, "parking": 2.5, "island": 3, "median": 3, "sidewalk": 3, "crossing": 4}
    for s in model.surfaces:
        for p in _polygons(s.geometry):
            patch(p, facecolor=PREVIEW_COLORS.get(s.kind, "#888"), edgecolor="none",
                  alpha=0.85 if ortho is not None else 1.0, zorder=order.get(s.kind, 2))
    for c in model.curbs:
        xy = np.asarray(c.geometry.coords)[:, :2]
        ax.plot(xy[:, 0], xy[:, 1], color="#111111", lw=0.9, zorder=5)
    for m in model.markings:
        if m.geometry is None:
            continue
        xy = np.asarray(m.geometry.coords)[:, :2]
        ax.plot(xy[:, 0], xy[:, 1], color="#ffffff" if m.color == "white" else "#f0c020",
                lw=0.8, ls="-" if m.kind == "solid" else (0, (3, 4)), zorder=6)
    for r in model.roads:
        xy = np.asarray(r.reference_line.coords)[:, :2]
        if r.junction_id is not None:
            ax.plot(xy[:, 0], xy[:, 1], color="#00d0ff", lw=0.6, alpha=0.9, zorder=7)
        else:
            ax.plot(xy[:, 0], xy[:, 1], color="#ff8c00", lw=0.6, alpha=0.9, zorder=7)
            ax.plot(xy[-1, 0], xy[-1, 1], marker=">", color="#ff8c00", ms=3, zorder=7)
    for j in model.junctions:
        if j.polygon is None:
            continue
        for p in _polygons(j.polygon):
            xy = np.asarray(p.exterior.coords)[:, :2]
            ax.plot(xy[:, 0], xy[:, 1], color="#ff2d55", lw=1.0, ls="--", zorder=8)
            cx, cy = p.centroid.x, p.centroid.y
            ax.text(cx, cy, j.id, color="#ff2d55", fontsize=6, ha="center", va="center", zorder=9)
    for sig in model.signals:
        st = SIGNAL_STYLE.get(sig.kind, dict(marker=".", color="#000", ms=4))
        ax.plot(sig.position.x, sig.position.y, ls="none", zorder=9, **st)
    ax.set_xlim(plot_extent[0], plot_extent[1])
    ax.set_ylim(plot_extent[2], plot_extent[3])
    ax.set_aspect("equal")
    ax.set_xlabel("x east [m]")
    ax.set_ylabel("y north [m]")
    ax.set_title(title or f"{model.name}: {len(model.roads)} roads, {len(model.junctions)} junctions, "
                 f"{len(model.surfaces)} surfaces, {len(model.curbs)} curbs")
    ax.grid(True, color="#00000022", lw=0.4)
    fig.tight_layout()
    path_png = Path(path_png)
    path_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path_png, dpi=dpi)
    plt.close(fig)
    log.info("wrote %s", path_png)
