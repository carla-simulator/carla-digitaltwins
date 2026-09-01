"""Twin Model -> Unreal-ready bake export: one glTF 2.0 binary (``.glb``) per (layer, kind, tile)
plus ``manifest.json`` for ``ue/bake_level.py`` (editor Python, UE 5.8).

DESIGN.md §Baker. Everything here is offline (no Unreal), so it is unit-testable.

Coordinates. Model space is local ENU metres (x east, y north, z up). Unreal is left-handed
Z-up centimetres and CARLA's convention is ``UE = (x, -y, z) * 100``. The glTF importer
(Interchange ``GLTF::ConvertVec3``) maps ``UE = (gltf.x, gltf.z, gltf.y) * 100``, so every vertex
is written as ``gltf = (x, z, -y)`` in metres and lands in Unreal at exactly ``(x, -y, z) * 100``
without any transform on the actors. The manifest stores UE centimetres.

Assets. ``<name>_L<layer>_<kind>_<i>_<j>``: ``kind`` is the surface kind (``drivable``,
``sidewalk``, ``crossing``, ``island``, ``median``, ``verge``, ``parking``, ``ground``), ``curb``,
``marking_white`` / ``marking_yellow`` (thin quads lifted ``profile.marking.z`` above the
datum; zebra stripes of every crossing surface are added to ``marking_white``), or
``building`` (footprints extruded to ``Building.effective_height``); ``<i>_<j>`` is the
``tile_m`` grid cell (World Partition streams per cell). UVs are metric planar (1 unit = 1 m,
``u = x``, ``v = -y`` on horizontal faces; ``u`` = distance along, ``v`` = height on walls/curbs).
"""
from __future__ import annotations

import json
import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .. import profiles
from ..model import Building, CurbLine, Road, TwinModel, road_osm_layer
from .mesh import (MATERIALS, _add_marking, _add_surface, _grid_split, _polygons, _z,
                   triangulate_polygon)

log = logging.getLogger("twinmodel.export.ue")

MANIFEST_SCHEMA = "twinmodel-ue/1"
UE_SCALE = 100.0  # metres -> centimetres

# asset kind -> (material key the baker resolves to a CARLA material, CARLA semantic tag folder)
# semantic folders are the ATagger folder names (Road / Sidewalk / RoadLine / Building / Terrain)
KIND_MATERIAL: dict[str, tuple[str, str]] = {
    "drivable": ("road", "Road"),
    "parking": ("road", "Road"),
    "crossing": ("road", "Road"),
    "sidewalk": ("sidewalk", "SideWalk"),   # ATagger folder name has a capital W
    "island": ("sidewalk", "SideWalk"),
    "median": ("sidewalk", "SideWalk"),
    "verge": ("grass", "Terrain"),
    "ground": ("ground", "Terrain"),
    "curb": ("curb", "SideWalk"),
    "marking_white": ("marking_white", "RoadLine"),
    "marking_yellow": ("marking_yellow", "RoadLine"),
    "building": ("building", "Building"),
    "groundplane": ("ground", "Terrain"),
}
# glTF base colours (preview only; the baker swaps in CARLA materials)
_BASE_COLORS = dict(MATERIALS)
_BASE_COLORS["building"] = (0.72, 0.66, 0.58)
_BASE_COLORS["groundplane"] = (0.45, 0.50, 0.40)

ZEBRA_STRIPE = 0.5   # m, stripe width along the crossing
ZEBRA_GAP = 0.5      # m
ZEBRA_MIN_LEN = 2.0  # m, crossings shorter than this get no stripes
# raised plates (sidewalk / island / median / verge / grass fill, all at curb-top level) get a
# vertical skirt along every free edge — without it they read as planes floating over the
# ground slab; the drivable-side edges already carry the curb strips
SKIRT_KINDS = frozenset({"sidewalk", "island", "median", "verge", "ground"})
BUILDING_SINK = 0.3  # m, walls start this far below the lowest datum sample so no gap shows
GROUND_PLANE_DROP = 0.35  # m below the datum: closes the block courtyards without z-fighting
SKIRT_DROP = GROUND_PLANE_DROP + 0.15  # m below the datum: the skirt ends under the ground slab
GROUND_PLANE_GRID = 20.0  # m subdivision so the slab follows the datum
SPAWN_SPACING = 30.0  # m between spawn points along a lane
SPAWN_MARGIN = 10.0   # m kept free at both road ends
SPAWN_Z = 0.5         # m above the datum (CARLA vehicles need ~0.5 m clearance to spawn)


# --------------------------------------------------------------------------- mesh builder

@dataclass
class MeshBuilder:
    """Accumulates one glTF primitive. Implements the ``_ObjWriter`` interface
    (``add_vertices`` / ``add_faces``) so :mod:`.mesh` tessellators can write into it;
    vertices added that way get a planar UV and an up normal (``finish_planar``)."""
    positions: list[np.ndarray] = field(default_factory=list)  # (n, 3) model space, metres
    normals: list[np.ndarray] = field(default_factory=list)
    uvs: list[np.ndarray] = field(default_factory=list)
    faces: list[np.ndarray] = field(default_factory=list)      # (m, 3) int64, CCW seen from +z
    _count: int = 0

    # -- _ObjWriter interface (planar geometry)
    def add_vertices(self, xyz: np.ndarray) -> int:
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        base = self._count
        self.positions.append(xyz)
        self.normals.append(np.tile([0.0, 0.0, 1.0], (len(xyz), 1)))
        self.uvs.append(np.column_stack([xyz[:, 0], -xyz[:, 1]]))
        self._count += len(xyz)
        return base

    def add_faces(self, group: str, faces: np.ndarray, base: int) -> None:
        f = np.asarray(faces, dtype=np.int64).reshape(-1, 3) + base
        if len(f):
            self.faces.append(f)

    # -- explicit geometry (walls, curbs)
    def add(self, xyz: np.ndarray, normals: np.ndarray, uvs: np.ndarray, faces: np.ndarray) -> None:
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        if len(xyz) == 0 or len(faces) == 0:
            return
        base = self._count
        self.positions.append(xyz)
        self.normals.append(np.asarray(normals, dtype=np.float64).reshape(-1, 3))
        self.uvs.append(np.asarray(uvs, dtype=np.float64).reshape(-1, 2))
        self.faces.append(np.asarray(faces, dtype=np.int64).reshape(-1, 3) + base)
        self._count += len(xyz)

    @property
    def n_vertices(self) -> int:
        return self._count

    @property
    def n_faces(self) -> int:
        return int(sum(len(f) for f in self.faces))

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        if not self.positions:
            return (np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64))
        return (np.concatenate(self.positions), np.concatenate(self.normals),
                np.concatenate(self.uvs), np.concatenate(self.faces))


def model_to_ue(xyz: np.ndarray) -> np.ndarray:
    """Model metres (x east, y north, z up) -> UE centimetres (x, -y, z) * 100."""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    return np.column_stack([xyz[:, 0], -xyz[:, 1], xyz[:, 2]]) * UE_SCALE


def model_to_gltf(xyz: np.ndarray) -> np.ndarray:
    """Model metres -> glTF metres (right-handed, Y up) such that Interchange's
    ``UE = (gx, gz, gy) * 100`` yields ``(x, -y, z) * 100``: ``g = (x, z, -y)``."""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    return np.column_stack([xyz[:, 0], xyz[:, 2], -xyz[:, 1]])


def gltf_to_model(g: np.ndarray) -> np.ndarray:
    g = np.asarray(g, dtype=np.float64).reshape(-1, 3)
    return np.column_stack([g[:, 0], -g[:, 2], g[:, 1]])


# --------------------------------------------------------------------------- GLB writer

def _pad4(b: bytes, fill: bytes = b"\x00") -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def write_glb(path: Path, name: str, positions: np.ndarray, normals: np.ndarray, uvs: np.ndarray,
              faces: np.ndarray, material: str, base_color=(0.5, 0.5, 0.5)) -> dict:
    """Write a single-mesh, single-primitive glTF 2.0 binary. ``positions`` are model space
    metres; converted with :func:`model_to_gltf`. Faces are CCW seen from the outside in model
    space; the reflection in the axis swap flips them, which the importer compensates for
    (Interchange re-derives winding from the handedness change), so indices are written as is.
    Returns the accessor bbox in UE centimetres."""
    pos = model_to_gltf(positions).astype(np.float32)
    nrm = model_to_gltf(normals).astype(np.float32)
    uv = np.asarray(uvs, dtype=np.float32).reshape(-1, 2)
    # glTF UV origin is top-left; UE flips v on import so metric planar UVs survive
    idx = np.asarray(faces, dtype=np.uint32).reshape(-1)
    bufs = [pos.tobytes(), nrm.tobytes(), uv.tobytes(), idx.tobytes()]
    views, offset = [], 0
    for i, b in enumerate(bufs):
        b = _pad4(b)
        v = {"buffer": 0, "byteOffset": offset, "byteLength": len(bufs[i])}
        if i < 3:
            v["target"] = 34962  # ARRAY_BUFFER
        else:
            v["target"] = 34963  # ELEMENT_ARRAY_BUFFER
        views.append(v)
        offset += len(b)
    bin_chunk = b"".join(_pad4(b) for b in bufs)
    pmin = pos.min(axis=0).tolist() if len(pos) else [0, 0, 0]
    pmax = pos.max(axis=0).tolist() if len(pos) else [0, 0, 0]
    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
         "min": [float(v) for v in pmin], "max": [float(v) for v in pmax]},
        {"bufferView": 1, "componentType": 5126, "count": len(nrm), "type": "VEC3"},
        {"bufferView": 2, "componentType": 5126, "count": len(uv), "type": "VEC2"},
        {"bufferView": 3, "componentType": 5125, "count": len(idx), "type": "SCALAR"},
    ]
    r, g, b = base_color
    gltf = {
        "asset": {"version": "2.0", "generator": "twinmodel.export.ue"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
            "indices": 3, "material": 0, "mode": 4}]}],
        "materials": [{"name": material, "doubleSided": False,
                       "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, 1.0],
                                                "metallicFactor": 0.0, "roughnessFactor": 0.9}}],
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    json_chunk = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(json_chunk), 0x4E4F534A))
        f.write(json_chunk)
        f.write(struct.pack("<II", len(bin_chunk), 0x004E4942))
        f.write(bin_chunk)
    ue = model_to_ue(positions)
    return {"min": [round(float(v), 1) for v in ue.min(axis=0)],
            "max": [round(float(v), 1) for v in ue.max(axis=0)]} if len(ue) else {"min": [0, 0, 0], "max": [0, 0, 0]}


def read_glb(path: Path) -> dict:
    """Parse a GLB written by :func:`write_glb` back into numpy arrays (tests, tooling)."""
    data = Path(path).read_bytes()
    magic, version, total = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67 and version == 2 and total == len(data)
    jlen, jtype = struct.unpack_from("<II", data, 12)
    gltf = json.loads(data[20:20 + jlen].decode("utf-8"))
    blen, btype = struct.unpack_from("<II", data, 20 + jlen)
    blob = data[28 + jlen:28 + jlen + blen]

    def acc(i: int, dtype, width: int):
        a = gltf["accessors"][i]
        v = gltf["bufferViews"][a["bufferView"]]
        arr = np.frombuffer(blob, dtype=dtype, count=a["count"] * width, offset=v["byteOffset"])
        return arr.reshape(a["count"], width) if width > 1 else arr

    prim = gltf["meshes"][0]["primitives"][0]
    return {
        "gltf": gltf,
        "positions": acc(prim["attributes"]["POSITION"], np.float32, 3),
        "normals": acc(prim["attributes"]["NORMAL"], np.float32, 3),
        "uvs": acc(prim["attributes"]["TEXCOORD_0"], np.float32, 2),
        "faces": acc(prim["indices"], np.uint32, 1).reshape(-1, 3),
        "material": gltf["materials"][0]["name"],
    }


# --------------------------------------------------------------------------- geometry

def _tile_index(x: float, y: float, tile_m: float) -> tuple[int, int]:
    if tile_m <= 0:
        return (0, 0)
    return (int(math.floor(x / tile_m)), int(math.floor(y / tile_m)))


def _tiles_of(geom: BaseGeometry, tile_m: float) -> Iterable[tuple[tuple[int, int], BaseGeometry]]:
    """Split a polygonal geometry by the tile grid: yields ``((i, j), piece)``."""
    if tile_m <= 0 or geom.is_empty:
        yield (0, 0), geom
        return
    minx, miny, maxx, maxy = geom.bounds
    i0, j0 = _tile_index(minx, miny, tile_m)
    i1, j1 = _tile_index(maxx, maxy, tile_m)
    if (i0, j0) == (i1, j1):
        yield (i0, j0), geom
        return
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            cell = box(i * tile_m, j * tile_m, (i + 1) * tile_m, (j + 1) * tile_m)
            piece = geom.intersection(cell)
            if not piece.is_empty and piece.area > 1e-6:
                yield (i, j), piece


def zebra_stripes(poly: Polygon, stripe: float = ZEBRA_STRIPE, gap: float = ZEBRA_GAP,
                  along: Optional[np.ndarray] = None) -> list[Polygon]:
    """Zebra stripes of a crossing polygon: bands of width ``stripe`` whose bars run
    perpendicular to the pedestrian walking direction (i.e. parallel to the road being
    crossed), stepped along the walking direction every ``stripe + gap``.

    ``along`` is the walking direction (unit 2-vector). Pass it whenever the road is known
    (``crossing_walk_dir``): inferring it as the long axis of the minimum rotated rectangle
    flips on near-square crossings — a 4 m crossing over a ~3.5 m carriageway lays its bars
    across the road instead of along it. Without ``along`` the long-axis fallback applies."""
    if poly.is_empty or poly.area < 0.5:
        return []
    if along is not None:
        u = np.asarray(along, dtype=np.float64).reshape(2)
        n = float(np.hypot(*u))
        if n < 1e-9:
            return []
        u = u / n
        v = np.array([-u[1], u[0]])
        cc = np.asarray(poly.exterior.coords)[:, :2]
        su, sv = cc @ u, cc @ v
        L = float(su.max() - su.min())
        W = float(sv.max() - sv.min())
        if L < ZEBRA_MIN_LEN or W < 1e-6:
            return []
        origin = u * float(su.min()) + v * float(sv.min())
    else:
        rect = poly.minimum_rotated_rectangle
        if not isinstance(rect, Polygon):
            return []
        c = np.asarray(rect.exterior.coords)[:4]
        e0, e1 = c[1] - c[0], c[2] - c[1]
        l0, l1 = float(np.hypot(*e0)), float(np.hypot(*e1))
        if max(l0, l1) < ZEBRA_MIN_LEN or min(l0, l1) < 1e-6:
            return []
        # long axis u (walking direction), short axis v (traffic direction)
        if l0 >= l1:
            origin, u, L, v, W = c[0], e0 / l0, l0, e1 / l1, l1
        else:
            origin, u, L, v, W = c[1], e1 / l1, l1, -e0 / l0, l0
    out: list[Polygon] = []
    s = gap / 2.0
    while s + stripe <= L + 1e-6:
        a = origin + u * s
        b = origin + u * (s + stripe)
        band = Polygon([a, b, b + v * W, a + v * W])
        piece = band.intersection(poly)
        out.extend(p for p in _polygons(piece) if p.area > 0.01)
        s += stripe + gap
    return out


def crossing_walk_dir(model: TwinModel, s, poly: Polygon) -> Optional[np.ndarray]:
    """Pedestrian walking direction of a crossing surface: the crossed road's left normal at
    the crossing polygon, so the zebra bars run parallel to the road no matter how the clipped
    polygon is shaped (``surfaces.crossing_polygon`` builds the rect from the road, but the
    intersection with the drivable surface can leave a near-square or skewed piece). None when
    the surface references no known road (polygon-PCA fallback applies)."""
    for rid in getattr(s, "road_ids", None) or ():
        try:
            road = model.road(rid)
        except KeyError:
            continue
        ref = shapely.force_2d(road.reference_line)
        if ref.length < 1e-6:
            continue
        t = ref.project(poly.centroid)
        p0 = ref.interpolate(max(0.0, t - 0.5))
        p1 = ref.interpolate(min(ref.length, t + 0.5))
        d = np.array([p1.x - p0.x, p1.y - p0.y], dtype=np.float64)
        n = float(np.hypot(*d))
        if n < 1e-9:
            continue
        d /= n
        return np.array([-d[1], d[0]])
    return None


def _boundary_lines(geom: BaseGeometry) -> list[LineString]:
    """Oriented boundary rings of a polygonal geometry: exteriors CCW, holes CW, so the
    right-hand normal of every segment points out of the material."""
    out: list[LineString] = []
    for p in _polygons(geom):
        p = shapely.geometry.polygon.orient(shapely.force_2d(p), 1.0)
        out.append(LineString(p.exterior.coords))
        for h in p.interiors:
            out.append(LineString(shapely.geometry.polygon.orient(Polygon(h), -1.0).exterior.coords))
    return out


def skirt_lines(geom: BaseGeometry, cover: Optional[BaseGeometry]) -> list[LineString]:
    """Free edges of a raised plate: its boundary minus the segments already carrying a curb
    strip (``cover``: the curb lines buffered a little)."""
    out: list[LineString] = []
    for ring in _boundary_lines(geom):
        stack = [ring.difference(cover) if cover is not None else ring]
        while stack:
            g = stack.pop()
            if g.is_empty:
                continue
            if isinstance(g, LineString):
                if g.length > 0.05:
                    out.append(g)
            elif hasattr(g, "geoms"):
                stack.extend(g.geoms)
    return out


def _add_skirt_strip(mb: MeshBuilder, model: TwinModel, line: LineString, z_top_offset: float,
                     layer: Optional[int], subdivide: bool) -> int:
    """Vertical wall from a raised plate's top edge down to under the ground slab
    (``SKIRT_DROP``): the riser that keeps sidewalks / islands / grass from reading as planes
    floating over the slab. Same construction as the curb strips: flat per-segment normals
    facing away from the material, (along, height) UVs."""
    if subdivide:
        line = line.segmentize(profiles.get().elevation.mesh_grid_m)
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    if len(xy) < 2:
        return 0
    z1 = _z(model, xy, z_top_offset, layer)
    z0 = _z(model, xy, 0.0, layer) - SKIRT_DROP
    seg = np.diff(xy, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    along = np.concatenate([[0.0], np.cumsum(seg_len)])
    verts, nrms, uvs, faces = [], [], [], []
    k = 0
    for i in np.nonzero(seg_len > 1e-6)[0]:
        d = seg[i] / seg_len[i]
        n = np.array([d[1], -d[0], 0.0])
        a, b = xy[i], xy[i + 1]
        quad = np.array([[a[0], a[1], z0[i]], [b[0], b[1], z0[i + 1]],
                         [b[0], b[1], z1[i + 1]], [a[0], a[1], z1[i]]])
        verts.append(quad)
        nrms.append(np.tile(n, (4, 1)))
        uvs.append(np.array([[along[i], 0.0], [along[i + 1], 0.0],
                             [along[i + 1], z1[i + 1] - z0[i + 1]], [along[i], z1[i] - z0[i]]]))
        faces.append(np.array([[k, k + 2, k + 1], [k, k + 3, k + 2]]))
        k += 4
    if not verts:
        return 0
    mb.add(np.concatenate(verts), np.concatenate(nrms), np.concatenate(uvs), np.concatenate(faces))
    return 2 * len(verts)


def _add_curb_strip(mb: MeshBuilder, model: TwinModel, curb: CurbLine, subdivide: bool) -> int:
    """Vertical curb face as a strip of quads with flat per-segment normals and (along, height) UVs."""
    line = shapely.force_2d(curb.geometry)
    if subdivide:
        line = line.segmentize(profiles.get().elevation.mesh_grid_m)
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    if len(xy) < 2:
        return 0
    z0 = _z(model, xy, 0.0, curb.layer)
    z1 = z0 + curb.height
    # the curb face looks towards the low side; by construction (surfaces.py) the high side
    # is on the left of the line, so the outward normal is the right-hand normal
    seg = np.diff(xy, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    keep = seg_len > 1e-6
    along = np.concatenate([[0.0], np.cumsum(seg_len)])
    verts, nrms, uvs, faces = [], [], [], []
    k = 0
    for i in np.nonzero(keep)[0]:
        d = seg[i] / seg_len[i]
        n = np.array([d[1], -d[0], 0.0])
        a, b = xy[i], xy[i + 1]
        quad = np.array([[a[0], a[1], z0[i]], [b[0], b[1], z0[i + 1]],
                         [b[0], b[1], z1[i + 1]], [a[0], a[1], z1[i]]])
        verts.append(quad)
        nrms.append(np.tile(n, (4, 1)))
        uvs.append(np.array([[along[i], 0.0], [along[i + 1], 0.0],
                             [along[i + 1], curb.height], [along[i], curb.height]]))
        # outward-facing winding: CCW when seen from the normal side
        faces.append(np.array([[k, k + 2, k + 1], [k, k + 3, k + 2]]) )
        k += 4
    if not verts:
        return 0
    mb.add(np.concatenate(verts), np.concatenate(nrms), np.concatenate(uvs), np.concatenate(faces))
    return 2 * len(verts)


def building_geometry(model: TwinModel, b: Building, level_height: float, default_levels: int
                      ) -> tuple[float, float]:
    """(base z, roof z) of a building: base = lowest datum sample on the footprint minus
    ``BUILDING_SINK`` so the walls meet the terrain, roof = base + effective height."""
    pts = []
    for p in _polygons(b.footprint):
        pts.append(np.asarray(p.exterior.coords)[:, :2])
    xy = np.concatenate(pts) if pts else np.zeros((0, 2))
    if len(xy) == 0:
        return 0.0, 0.0
    z = np.asarray(model.sample_z(xy[:, 0], xy[:, 1]), dtype=np.float64)
    base = float(z.min()) - BUILDING_SINK
    return base, base + b.effective_height(level_height, default_levels) + BUILDING_SINK


def clip_building_footprint(b: Building, drivable) -> Optional[BaseGeometry]:
    """Footprint minus the drivable network (OSM buildings and mapped roads/aisles overlap
    routinely around malls — an extruded wall across a driving lane pins the traffic against
    ``static.building``). Canopies (``building=roof``) are skipped outright. Returns None
    when nothing worth extruding remains."""
    if (b.tags or {}).get("building") == "roof":
        return None
    fp = b.footprint
    if drivable is not None and fp.intersects(drivable):
        fp = fp.difference(drivable)
    if fp.is_empty or fp.area < 4.0:
        return None
    return fp


def _add_building(mb: MeshBuilder, model: TwinModel, b: Building, level_height: float,
                  default_levels: int, footprint: Optional[BaseGeometry] = None) -> int:
    base, roof = building_geometry(model, b, level_height, default_levels)
    n = 0
    for poly in _polygons(b.footprint if footprint is None else footprint):
        poly = shapely.force_2d(poly)
        if poly.area < 1.0:
            continue
        # walls: exterior CCW so the right-hand normal of each edge points outwards
        rings = [shapely.geometry.polygon.orient(poly, 1.0).exterior] + \
                [shapely.geometry.polygon.orient(Polygon(r), -1.0).exterior for r in
                 (Polygon(h) for h in poly.interiors)]
        for ring in rings:
            xy = np.asarray(ring.coords, dtype=np.float64)[:, :2]
            seg = np.diff(xy, axis=0)
            seg_len = np.hypot(seg[:, 0], seg[:, 1])
            along = np.concatenate([[0.0], np.cumsum(seg_len)])
            verts, nrms, uvs, faces = [], [], [], []
            k = 0
            for i in np.nonzero(seg_len > 1e-6)[0]:
                d = seg[i] / seg_len[i]
                nrm = np.array([d[1], -d[0], 0.0])
                a, c = xy[i], xy[i + 1]
                verts.append(np.array([[a[0], a[1], base], [c[0], c[1], base],
                                       [c[0], c[1], roof], [a[0], a[1], roof]]))
                nrms.append(np.tile(nrm, (4, 1)))
                uvs.append(np.array([[along[i], 0.0], [along[i + 1], 0.0],
                                     [along[i + 1], roof - base], [along[i], roof - base]]))
                faces.append(np.array([[k, k + 2, k + 1], [k, k + 3, k + 2]]))
                k += 4
            if verts:
                mb.add(np.concatenate(verts), np.concatenate(nrms), np.concatenate(uvs),
                       np.concatenate(faces))
                n += 2 * len(verts)
        # roof
        verts2, faces2 = triangulate_polygon(poly)
        if len(faces2):
            xyz = np.column_stack([verts2, np.full(len(verts2), roof)])
            b0 = mb.add_vertices(xyz)
            mb.add_faces("building", faces2, b0)
            n += len(faces2)
    return n


# --------------------------------------------------------------------------- spawn points

def spawn_points(model: TwinModel, spacing: float = SPAWN_SPACING, margin: float = SPAWN_MARGIN,
                 z_above: float = SPAWN_Z, limit: int = 400) -> list[dict]:
    """Vehicle spawn transforms on the driving lanes of non-junction roads, every ``spacing``
    metres of s, at the lane centre, heading along the lane's travel direction. Returned in
    **UE** units (cm, yaw degrees, y flipped) as the baker places them; ``model`` keys carry
    the model-space (x, y, z, heading rad) for tooling."""
    out: list[dict] = []
    for r in sorted(model.roads, key=lambda r: r.id):
        if r.junction_id is not None or not r.lanes:
            continue
        line = shapely.force_2d(r.reference_line)
        L = line.length
        if L < 2 * margin + 1.0:
            continue
        layer = road_osm_layer(r)
        ss = np.arange(margin, L - margin + 1e-6, spacing)
        for side in (1, -1):
            lanes = r.lanes_left() if side > 0 else r.lanes_right()
            inner = 0.0
            for lane in lanes:
                t = side * (inner + lane.width / 2.0)
                inner += lane.width
                if lane.type != "driving":
                    continue
                for s in ss:
                    p = line.interpolate(float(s))
                    p1 = line.interpolate(min(L, float(s) + 0.5))
                    p0 = line.interpolate(max(0.0, float(s) - 0.5))
                    dx, dy = p1.x - p0.x, p1.y - p0.y
                    h = math.atan2(dy, dx)
                    if lane.direction == "backward":
                        h += math.pi
                    nx, ny = -math.sin(math.atan2(dy, dx)), math.cos(math.atan2(dy, dx))
                    x, y = p.x + t * nx, p.y + t * ny
                    z = float(np.asarray(model.sample_z(x, y, layer=layer))) + z_above
                    out.append({"x": round(x * UE_SCALE, 1), "y": round(-y * UE_SCALE, 1),
                                "z": round(z * UE_SCALE, 1), "yaw": round(-math.degrees(h), 2),
                                "road": r.id, "lane": lane.id,
                                "model": [round(x, 3), round(y, 3), round(z, 3), round(h, 4)]})
    if len(out) > limit:
        step = len(out) / limit
        out = [out[int(i * step)] for i in range(limit)]
    return out


# --------------------------------------------------------------------------- export

def _profile_building_rules() -> tuple[float, int]:
    P = profiles.get()
    b = getattr(P, "building", None)
    if b is None:
        return 3.5, 3
    return float(b.level_height_m), int(b.default_levels)


def export_ue(model: TwinModel, out_dir: Path | str, name: Optional[str] = None,
              xodr_path: Optional[Path | str] = None, tile_m: float = 250.0,
              buildings: bool = True) -> dict:
    """Write ``<out_dir>/meshes/*.glb`` and ``<out_dir>/manifest.json``; returns the manifest."""
    out_dir = Path(out_dir)
    mesh_dir = out_dir / "meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    name = name or model.name
    datum = model.rebuild_datum()
    subdivide = model.elevation is not None or datum is not None
    level_height, default_levels = _profile_building_rules()
    builders: dict[tuple[int, str, tuple[int, int]], MeshBuilder] = {}

    def mb(layer: Optional[int], kind: str, tile: tuple[int, int]) -> MeshBuilder:
        key = (int(layer or 0), kind, tile)
        if key not in builders:
            builders[key] = MeshBuilder()
        return builders[key]

    # surfaces (each kind its own asset; crossings also get zebra stripes)
    M = profiles.get().marking
    for s in model.surfaces:
        kind = s.kind if s.kind in KIND_MATERIAL else "drivable"
        layer = s.tags.get("layer")
        for tile, piece in _tiles_of(s.geometry, tile_m):
            _add_surface(mb(layer, kind, tile), model, piece, s.z_offset, kind, subdivide, layer)
        if s.kind == "crossing":
            for poly in _polygons(s.geometry):
                walk = crossing_walk_dir(model, s, poly)
                for stripe in zebra_stripes(poly, along=walk):
                    c = stripe.centroid
                    _add_surface(mb(layer, "marking_white", _tile_index(c.x, c.y, tile_m)), model,
                                 stripe, s.z_offset + M.z, "marking_white", subdivide, layer)
    for c in model.curbs:
        cen = c.geometry.centroid
        _add_curb_strip(mb(c.layer, "curb", _tile_index(cen.x, cen.y, tile_m)), model, c, subdivide)
    # skirts: the free edges of every raised plate get a vertical face down under the ground
    # slab. One union per (layer, top height) so flush neighbours (sidewalk | grass fill)
    # produce no interior double wall; the drivable-side edges are excluded — they already
    # carry the curb strips on the same boundary (a second coplanar face would z-fight).
    # Skirts live in the ``curb`` assets: visually they are the curb/riser band.
    curb_by_layer: dict[int, list[BaseGeometry]] = {}
    for c in model.curbs:
        curb_by_layer.setdefault(int(c.layer or 0), []).append(c.geometry)
    curb_cover = {lay: unary_union(gs).buffer(0.06) for lay, gs in curb_by_layer.items()}
    skirt_groups: dict[tuple[int, float], list[BaseGeometry]] = {}
    for s in model.surfaces:
        if s.kind in SKIRT_KINDS:
            key = (int(s.tags.get("layer") or 0), round(float(s.z_offset), 4))
            skirt_groups.setdefault(key, []).append(s.geometry)
    for (layer, z_top), geoms in sorted(skirt_groups.items()):
        for line in skirt_lines(unary_union(geoms), curb_cover.get(layer)):
            cen = line.centroid
            _add_skirt_strip(mb(layer, "curb", _tile_index(cen.x, cen.y, tile_m)), model, line,
                             z_top, layer, subdivide)
    for mk in model.markings:
        if mk.geometry is None or mk.geometry.is_empty:
            continue
        kind = "marking_yellow" if mk.color == "yellow" else "marking_white"
        cen = mk.geometry.centroid
        _add_marking(mb(mk.layer, kind, _tile_index(cen.x, cen.y, tile_m)), model, mk, subdivide)
    # a datum-following slab under everything: block courtyards and map borders would
    # otherwise be holes straight to the sky
    minx, miny, maxx, maxy = None, None, None, None
    for srf in model.surfaces:
        b0 = srf.geometry.bounds
        if minx is None:
            minx, miny, maxx, maxy = b0
        else:
            minx, miny = min(minx, b0[0]), min(miny, b0[1])
            maxx, maxy = max(maxx, b0[2]), max(maxy, b0[3])
    if minx is not None:
        slab = box(minx - 5.0, miny - 5.0, maxx + 5.0, maxy + 5.0)
        for tile, piece in _tiles_of(slab, tile_m):
            b = mb(0, "groundplane", tile)
            for poly in _grid_split(piece, GROUND_PLANE_GRID):
                verts, faces = triangulate_polygon(poly)
                if len(faces) == 0:
                    continue
                zz = np.asarray(model.sample_z(verts[:, 0], verts[:, 1]), dtype=np.float64)
                base0 = b.add_vertices(np.column_stack([verts, zz - GROUND_PLANE_DROP]))
                b.add_faces("groundplane", faces, base0)
    n_clipped = n_skipped = 0
    if buildings:
        drivable = unary_union([s.geometry for s in model.surfaces
                                if s.kind in ("drivable", "parking", "crossing")])
        drivable = drivable.buffer(0.25) if not drivable.is_empty else None
        for b in model.buildings:
            fp = clip_building_footprint(b, drivable)
            if fp is None:
                n_skipped += 1
                continue
            if fp is not b.footprint:
                n_clipped += 1
            cen = fp.centroid
            _add_building(mb(0, "building", _tile_index(cen.x, cen.y, tile_m)), model, b,
                          level_height, default_levels, footprint=fp)

    assets = []
    for (layer, kind, tile), b in sorted(builders.items()):
        if b.n_faces == 0:
            continue
        pos, nrm, uv, faces = b.arrays()
        asset_name = f"{name}_L{layer}_{kind}_{tile[0]}_{tile[1]}".replace("-", "m")
        mat_key, semantic = KIND_MATERIAL[kind]
        rel = f"meshes/{asset_name}.glb"
        bbox = write_glb(mesh_dir / f"{asset_name}.glb", asset_name, pos, nrm, uv, faces, mat_key,
                         _BASE_COLORS.get(kind, (0.5, 0.5, 0.5)))
        assets.append({"file": rel, "asset": asset_name, "layer": layer, "kind": kind,
                       "material": mat_key, "semantic": semantic, "tile": list(tile),
                       "vertices": int(len(pos)), "triangles": int(len(faces)), "bbox_ue": bbox})

    sp = spawn_points(model)
    junctions = []
    for j in sorted(model.junctions, key=lambda j: -(j.polygon.area if j.polygon is not None else 0)):
        if j.polygon is None or "centre" not in j.tags:
            continue
        cx, cy = j.tags["centre"]
        z = float(np.asarray(model.sample_z(cx, cy)))
        junctions.append({"id": j.id, "area": round(float(j.polygon.area), 1),
                          "ue": [round(cx * UE_SCALE, 1), round(-cy * UE_SCALE, 1), round(z * UE_SCALE, 1)],
                          "model": [round(cx, 2), round(cy, 2), round(z, 2)]})
    allmin = np.array([a["bbox_ue"]["min"] for a in assets]).min(axis=0).tolist() if assets else [0, 0, 0]
    allmax = np.array([a["bbox_ue"]["max"] for a in assets]).max(axis=0).tolist() if assets else [0, 0, 0]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": name,
        "origin": {"lat": model.origin_lat, "lon": model.origin_lon},
        "bbox_wgs84": list(model.bbox_wgs84),
        "geo_reference": model.geo_reference,
        "units": "cm", "axes": "UE left-handed Z-up: x east, y south (model -y), z up",
        "conversion": "ue = (x, -y, z) * 100; glb vertices already converted (gltf = (x, z, -y) m)",
        "uv": "metric planar, 1 uv unit = 1 m",
        "xodr": str(Path(xodr_path).resolve()) if xodr_path else None,
        "tile_m": tile_m,
        "profile": profiles.get().name,
        "bbox_ue": {"min": [round(v, 1) for v in allmin], "max": [round(v, 1) for v in allmax]},
        "materials": {k: {"semantic": KIND_MATERIAL[kk][1]} for kk, (k, _) in KIND_MATERIAL.items()},
        "kinds": {kk: {"material": k, "semantic": sem} for kk, (k, sem) in KIND_MATERIAL.items()},
        "assets": assets,
        "spawn_points": sp,
        "junctions": junctions,
        "stats": {"assets": len(assets), "triangles": int(sum(a["triangles"] for a in assets)),
                  "spawn_points": len(sp), "buildings": len(model.buildings) if buildings else 0,
                  "buildings_clipped_by_roads": n_clipped, "buildings_skipped": n_skipped,
                  "layers": sorted({a["layer"] for a in assets})},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log.info("wrote %d glb assets (%d triangles), %d spawn points -> %s", len(assets),
             manifest["stats"]["triangles"], len(sp), out_dir)
    return manifest
