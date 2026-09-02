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
``u = x``, ``v = -y`` on horizontal faces; ``u`` = distance along, ``v`` = height on building
walls; curbs and the plate risers (``riser``) map onto the stone band of CARLA's curb texture,
see ``CURB_TEX_*``).
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
    # side walls of the raised plates (sidewalk / island / median / verge / grass): concrete
    # like the curbs, so a plate edge without a curb reads as a riser, not as paving on end.
    # Own material key since 2026-09-02 (it shared "curb" before): the baker gives kerbs
    # Town15's kerb material and leaves the risers on the CurbDirty atlas these UVs are
    # calibrated against, so a plate edge no longer reads as a kerb (ue/twin_materials.py).
    "riser": ("riser", "SideWalk"),
    "marking_white": ("marking_white", "RoadLine"),
    "marking_yellow": ("marking_yellow", "RoadLine"),
    "building": ("building", "Building"),
    "groundplane": ("ground", "Terrain"),
    # invisible collision wall around the ground slab (the baker hides the actor in game)
    "boundary": ("ground", "Static"),
}
# glTF base colours (preview only; the baker swaps in CARLA materials)
_BASE_COLORS = dict(MATERIALS)
_BASE_COLORS["building"] = (0.72, 0.66, 0.58)
_BASE_COLORS["riser"] = MATERIALS.get("curb", (0.6, 0.6, 0.6))
_BASE_COLORS["groundplane"] = (0.45, 0.50, 0.40)

ZEBRA_STRIPE = 0.5   # m, stripe width along the crossing
ZEBRA_GAP = 0.5      # m
ZEBRA_MIN_LEN = 2.0  # m, crossings shorter than this get no stripes
# raised plates (sidewalk / island / median / verge / grass fill, all at curb-top level) are
# closed prisms: the top plate plus a vertical wall along the WHOLE perimeter (outer rings and
# holes) from the plate top down to under the ground slab. Earlier bakes only skirted the
# "free" edges and relied on the curb strips for the road side; every edge the curb lines
# missed (7.5 % of the shared road/sidewalk boundary on Eixample) showed the void underneath
# as a black slot, and the plates read as floating planes.
SKIRT_KINDS = frozenset({"sidewalk", "island", "median", "verge", "ground"})
PLATE_WALL_INSET = 0.02  # m, the wall sits this far inside the plate edge so the curb strips (on
                         # the exact boundary, 15 cm tall) stay in front of it instead of z-fighting
# curb / riser UVs follow CARLA's curb material (MI_CurbDirty01 -> T_CurbDirty01_d, Scale 1):
# the texture is an atlas whose curb-stone face is the bottom band (UE v 0.846..1.0, the rest
# is streaky filler); the stock SM_Curb maps its 15.3 cm face onto v 0.813..0.994 with one
# repeat per 0.86 m of length, which puts the filler on the top quarter of the face (measured
# on out/look_demo/v11/curb_edge_a_rgb), so the band here starts at the filler boundary.
# Metric (along, height) UVs put the whole face on the filler.
CURB_TEX_REPEAT_M = 0.86   # m of curb per texture repeat along the face
CURB_TEX_V_TOP = 0.85      # UE v at the top edge of the stone face
CURB_TEX_V_BOTTOM = 0.994  # UE v at its bottom edge
CURB_TEX_BAND_M = 0.153    # m of face height the band spans (below that: the bottom row)
BUILDING_SINK = 0.3  # m, walls start this far below the lowest datum sample so no gap shows
GROUND_PLANE_DROP = 0.35  # m below the datum: closes the block courtyards without z-fighting
SKIRT_DROP = GROUND_PLANE_DROP + 0.15  # m below the datum: the skirt ends under the ground slab
GROUND_PLANE_GRID = 20.0  # m subdivision so the slab follows the datum
GROUND_PLANE_MARGIN = 50.0  # m of apron around the outermost surface: an actor that leaves the
                            # road at the map edge lands on terrain, not in the void
BOUNDARY_WALL_HEIGHT = 6.0  # m, invisible collision wall on the apron's perimeter
# on-road overlays (crossing plates, zebra bars, lane markings) take their z from the road
# triangles they lie on, not from the datum function: two different triangulations of the same
# datum disagree by up to (h^2 / 8) * curvature between vertices, and a 3 mm overlay sampled at
# its own corners ended up under the road surface on every slope (missing zebra bars). Fine
# subdivision keeps the overlay's own interpolation error negligible.
OVERLAY_GRID = 1.0    # m, crossing plates and zebra bars
MARKING_GRID = 2.5    # m, lane markings
ROAD_GAP_SEARCH = 0.3  # m, slots narrower than this between the road and a raised plate are paved
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


def _curb_band_uv(along: float, z_top: float, z: float, band_m: float = CURB_TEX_BAND_M) -> list[float]:
    """UV on a curb-material face: ``u`` along the face in texture repeats, ``v`` on the stone
    band of ``T_CurbDirty01`` measured down from the top edge; below ``band_m`` the bottom row
    (plain concrete) is stretched -- that part is under the road surface or a foundation."""
    t = min(1.0, max(0.0, (z_top - z) / band_m)) if band_m > 0 else 0.0
    return [along / CURB_TEX_REPEAT_M, CURB_TEX_V_TOP + t * (CURB_TEX_V_BOTTOM - CURB_TEX_V_TOP)]


def _add_wall_ring(mb: MeshBuilder, line: LineString, z_top: np.ndarray, z_bottom: np.ndarray,
                   outward_right: bool = True, curb_uv: Optional[float] = None) -> int:
    """Vertical quad strip along ``line`` between per-vertex ``z_bottom`` and ``z_top``. The
    outward normal is the right-hand normal of each segment (``outward_right``), or the left one.
    UVs are metric (along, height), or with ``curb_uv`` (band height in m) the curb-material
    band mapping of :func:`_curb_band_uv`."""
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    if len(xy) < 2:
        return 0
    seg = np.diff(xy, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    along = np.concatenate([[0.0], np.cumsum(seg_len)])
    verts, nrms, uvs, faces = [], [], [], []
    k = 0
    sign = 1.0 if outward_right else -1.0
    for i in np.nonzero(seg_len > 1e-6)[0]:
        d = seg[i] / seg_len[i]
        n = np.array([sign * d[1], -sign * d[0], 0.0])
        a, b = xy[i], xy[i + 1]
        quad = np.array([[a[0], a[1], z_bottom[i]], [b[0], b[1], z_bottom[i + 1]],
                         [b[0], b[1], z_top[i + 1]], [a[0], a[1], z_top[i]]])
        verts.append(quad)
        nrms.append(np.tile(n, (4, 1)))
        if curb_uv is None:
            uvs.append(np.array([[along[i], 0.0], [along[i + 1], 0.0],
                                 [along[i + 1], z_top[i + 1] - z_bottom[i + 1]], [along[i], z_top[i] - z_bottom[i]]]))
        else:
            uvs.append(np.array([_curb_band_uv(along[i], z_top[i], z_bottom[i], curb_uv),
                                 _curb_band_uv(along[i + 1], z_top[i + 1], z_bottom[i + 1], curb_uv),
                                 _curb_band_uv(along[i + 1], z_top[i + 1], z_top[i + 1], curb_uv),
                                 _curb_band_uv(along[i], z_top[i], z_top[i], curb_uv)]))
        # front face on the normal side: with (a_bottom, b_bottom, b_top, a_top) the CCW order
        # seen from the right-hand side of a->b is (0, 1, 2), (0, 2, 3)
        if outward_right:
            faces.append(np.array([[k, k + 1, k + 2], [k, k + 2, k + 3]]))
        else:
            faces.append(np.array([[k, k + 2, k + 1], [k, k + 3, k + 2]]))
        k += 4
    if not verts:
        return 0
    mb.add(np.concatenate(verts), np.concatenate(nrms), np.concatenate(uvs), np.concatenate(faces))
    return 2 * len(verts)


def plate_wall_rings(geom: BaseGeometry, inset: float = PLATE_WALL_INSET) -> list[LineString]:
    """Perimeter rings (outer CCW, holes CW -> right-hand normal points out of the material) of a
    raised plate, inset by ``inset`` so the wall hides behind the curb strips on the road side."""
    g = geom.buffer(-inset, join_style="mitre", mitre_limit=2.0) if inset > 0 else geom
    return _boundary_lines(g)


def _add_plate_walls(mb_of, model: TwinModel, geom: BaseGeometry, z_top_offset: float,
                     layer: Optional[int], subdivide: bool, tile_m: float) -> int:
    """Closed-prism side walls of a raised plate: every perimeter ring, plate top down to
    ``SKIRT_DROP`` under the datum. ``mb_of(tile)`` returns the builder for a tile."""
    n = 0
    for ring in plate_wall_rings(geom):
        if ring.length < 0.05:
            continue
        line = ring.segmentize(profiles.get().elevation.mesh_grid_m) if subdivide else ring
        xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
        z1 = _z(model, xy, z_top_offset, layer)
        z0 = _z(model, xy, 0.0, layer) - SKIRT_DROP
        cen = line.centroid
        n += _add_wall_ring(mb_of(_tile_index(cen.x, cen.y, tile_m)), line, z1, z0,
                            curb_uv=CURB_TEX_BAND_M)
    return n


class RoadMeshZ:
    """z lookup on an already tessellated road surface (the drivable / parking triangles of
    one layer), so overlays sit exactly on the triangles they cover. Points outside every
    triangle fall back to the datum."""

    def __init__(self, tris: np.ndarray):
        # tris: (n, 3, 3) xyz
        self.tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
        polys = [Polygon(t[:, :2]) for t in self.tris]
        self.tree = shapely.STRtree(polys) if polys else None

    @classmethod
    def from_builders(cls, builders: Iterable[MeshBuilder]) -> "RoadMeshZ":
        tris = []
        for b in builders:
            pos, _, _, faces = b.arrays()
            if len(faces):
                tris.append(pos[faces])
        return cls(np.concatenate(tris) if tris else np.zeros((0, 3, 3)))

    def z(self, model: TwinModel, xy: np.ndarray, layer: Optional[int]) -> np.ndarray:
        xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        out = _z(model, xy, 0.0, layer)
        if self.tree is None or len(xy) == 0:
            return out
        pts = shapely.points(xy[:, 0], xy[:, 1])
        qi, ti = self.tree.query(pts, predicate="intersects")
        done = np.zeros(len(xy), dtype=bool)
        for q, t in zip(qi, ti):
            if done[q]:
                continue
            a, b, c = self.tris[t]
            v0, v1 = b[:2] - a[:2], c[:2] - a[:2]
            v2 = xy[q] - a[:2]
            den = v0[0] * v1[1] - v1[0] * v0[1]
            if abs(den) < 1e-12:
                continue
            l1 = (v2[0] * v1[1] - v1[0] * v2[1]) / den
            l2 = (v0[0] * v2[1] - v2[0] * v0[1]) / den
            l0 = 1.0 - l1 - l2
            out[q] = l0 * a[2] + l1 * b[2] + l2 * c[2]
            done[q] = True
        return out


def _add_overlay(mb: MeshBuilder, model: TwinModel, geom: BaseGeometry, z_offset: float,
                 layer: Optional[int], roadz: Optional[RoadMeshZ], grid: float) -> int:
    """A thin on-road overlay (crossing plate, zebra bar, marking) tessellated on a fine grid
    with z from the road mesh (+ ``z_offset``)."""
    n = 0
    for poly in _grid_split(geom, grid):
        verts, faces = triangulate_polygon(poly)
        if len(faces) == 0:
            continue
        z = roadz.z(model, verts, layer) if roadz is not None else _z(model, verts, 0.0, layer)
        base = mb.add_vertices(np.column_stack([verts, z + z_offset]))
        mb.add_faces("overlay", faces, base)
        n += len(faces)
    return n


def _add_marking_overlay(mb_of, model: TwinModel, mk, roadz: Optional[RoadMeshZ], tile_m: float) -> int:
    """``mesh._add_marking`` on the road mesh: dash pieces / continuous quads via ``_add_overlay``."""
    from shapely.ops import substring
    M = profiles.get().marking
    line = shapely.force_2d(mk.geometry)
    if mk.kind == "broken":
        pieces = []
        s, L = 0.0, line.length
        while s < L:
            e = min(L, s + M.broken_dash)
            if e - s > 0.05:
                pieces.append(substring(line, s, e))
            s += M.broken_dash + M.broken_gap
    else:
        pieces = [line]
    width = mk.width or M.width
    n = 0
    for piece in pieces:
        quad = piece.buffer(width / 2.0, cap_style="flat", join_style="mitre", mitre_limit=2.0)
        cen = quad.centroid
        n += _add_overlay(mb_of(_tile_index(cen.x, cen.y, tile_m)), model, quad, M.z, mk.layer,
                          roadz, MARKING_GRID)
    return n


def road_plate_gaps(road: BaseGeometry, plates: BaseGeometry, cover: BaseGeometry,
                    search: float = ROAD_GAP_SEARCH) -> BaseGeometry:
    """Slots between the road and the raised plates that no surface covers (polygon slivers
    left by the two independent boundary constructions): paved as drivable so the ground slab
    never shows through the curb line."""
    if road.is_empty or plates.is_empty:
        return shapely.Polygon()
    band = road.buffer(search).intersection(plates.buffer(search))
    gap = band.difference(cover)
    return gap.buffer(0.005)  # a hair of overlap into both neighbours: no hairline seam


def _add_curb_strip(mb: MeshBuilder, model: TwinModel, curb: CurbLine, subdivide: bool) -> int:
    """Vertical curb face: a wall ring from the datum up ``curb.height`` with the curb-material
    band spanning the whole face. The face looks towards the low side; by construction
    (surfaces.py) the high side is on the left of the line, so the outward normal is the
    right-hand one."""
    line = shapely.force_2d(curb.geometry)
    if subdivide:
        line = line.segmentize(profiles.get().elevation.mesh_grid_m)
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    if len(xy) < 2:
        return 0
    z0 = _z(model, xy, 0.0, curb.layer)
    return _add_wall_ring(mb, line, z0 + curb.height, z0, curb_uv=curb.height)


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


def _footprint_ring_ue(poly: Polygon) -> Optional[list[list[float]]]:
    """Exterior ring of a footprint polygon in the UE frame for the manifest ``buildings``
    array: ``[[x_cm, y_cm], ...]`` with ``ue = (x, -y) * 100``, open (no repeated last
    point), wound counter-clockwise in UE coordinates (positive shoelace area over
    ``(x_ue, y_ue)``). The y-flip reverses orientation, so the model-frame ring is
    oriented clockwise (``orient(poly, -1.0)``) before converting. None for degenerate
    rings (< 3 points or area < 4 m^2)."""
    poly = shapely.force_2d(poly)
    if poly.is_empty or poly.area < 4.0:
        return None
    ring = shapely.geometry.polygon.orient(poly, -1.0).exterior
    xy = np.asarray(ring.coords, dtype=np.float64)[:-1, :2]
    if len(xy) < 3:
        return None
    return [[round(float(x) * UE_SCALE, 1), round(float(-y) * UE_SCALE, 1)] for x, y in xy]


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
                faces.append(np.array([[k, k + 1, k + 2], [k, k + 2, k + 3]]))  # CCW from outside
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

    # base surfaces: roads (drivable / parking) and the raised plates; crossings that lie on
    # the drivable surface are overlays (below), crossings that are cut out of it are road.
    M = profiles.get().marking
    road_union = unary_union([s.geometry for s in model.surfaces if s.kind in ("drivable", "parking")])
    road_cover = road_union.buffer(0.02) if not road_union.is_empty else None
    plates_union = unary_union([s.geometry for s in model.surfaces if s.kind in SKIRT_KINDS])
    cover_union = unary_union([s.geometry for s in model.surfaces])
    overlays: list[tuple] = []   # (surface, polygon) crossings drawn on the road mesh
    n_cross_overlay = n_cross_base = 0
    for s in model.surfaces:
        kind = s.kind if s.kind in KIND_MATERIAL else "drivable"
        layer = s.tags.get("layer")
        if s.kind == "crossing":
            for poly in _polygons(s.geometry):
                if road_cover is not None and road_cover.contains(poly):
                    overlays.append((s, poly))
                    n_cross_overlay += 1
                else:
                    n_cross_base += 1
                    for tile, piece in _tiles_of(poly, tile_m):
                        _add_surface(mb(layer, kind, tile), model, piece, s.z_offset, kind, subdivide, layer)
            continue
        for tile, piece in _tiles_of(s.geometry, tile_m):
            _add_surface(mb(layer, kind, tile), model, piece, s.z_offset, kind, subdivide, layer)
    # slots between the road and the raised plates get paved (see road_plate_gaps)
    gaps = road_plate_gaps(road_union, plates_union, cover_union)
    n_gap_m2 = float(gaps.area) if not gaps.is_empty else 0.0
    for poly in _polygons(gaps):
        for tile, piece in _tiles_of(poly, tile_m):
            _add_surface(mb(0, "drivable", tile), model, piece, 0.0, "drivable", subdivide, None)
    # the road mesh per layer: what overlays sit on
    roadz: dict[int, RoadMeshZ] = {}
    for layer in sorted({int(k[0]) for k in builders}):
        roadz[layer] = RoadMeshZ.from_builders(b for (lay, kind, _), b in builders.items()
                                               if lay == layer and kind in ("drivable", "parking", "crossing"))
    def rz(layer):
        return roadz.get(int(layer or 0))
    # crossing plates + zebra bars on the road mesh
    for s, poly in overlays:
        layer = s.tags.get("layer")
        for tile, piece in _tiles_of(poly, tile_m):
            _add_overlay(mb(layer, "crossing", tile), model, piece, s.z_offset, layer, rz(layer), OVERLAY_GRID)
    for s in model.surfaces:
        if s.kind != "crossing":
            continue
        layer = s.tags.get("layer")
        for poly in _polygons(s.geometry):
            walk = crossing_walk_dir(model, s, poly)
            for stripe in zebra_stripes(poly, along=walk):
                c = stripe.centroid
                _add_overlay(mb(layer, "marking_white", _tile_index(c.x, c.y, tile_m)), model,
                             stripe, s.z_offset + M.z, layer, rz(layer), OVERLAY_GRID)
    for c in model.curbs:
        cen = c.geometry.centroid
        _add_curb_strip(mb(c.layer, "curb", _tile_index(cen.x, cen.y, tile_m)), model, c, subdivide)
    # raised plates are closed prisms: one union per (layer, top height) so flush neighbours
    # (sidewalk | grass fill) share no interior wall, then a wall along the whole perimeter of
    # every polygon, inset a hair behind the curb strips. Walls live in the ``riser`` assets
    # (curb concrete, SideWalk semantic).
    plate_groups: dict[tuple[int, float], list[BaseGeometry]] = {}
    for s in model.surfaces:
        if s.kind in SKIRT_KINDS:
            key = (int(s.tags.get("layer") or 0), round(float(s.z_offset), 4))
            plate_groups.setdefault(key, []).append(s.geometry)
    n_wall_faces = 0
    for (layer, z_top), geoms in sorted(plate_groups.items()):
        n_wall_faces += _add_plate_walls(lambda tile, lay=layer: mb(lay, "riser", tile), model,
                                         unary_union(geoms), z_top, layer, subdivide, tile_m)
    for mk in model.markings:
        if mk.geometry is None or mk.geometry.is_empty:
            continue
        kind = "marking_yellow" if mk.color == "yellow" else "marking_white"
        _add_marking_overlay(lambda tile, k=kind, lay=mk.layer: mb(lay, k, tile), model, mk,
                             rz(mk.layer), tile_m)
    # a datum-following slab under everything, with a wide apron: block courtyards and map
    # borders would otherwise be holes straight to the sky, and an actor leaving the road at
    # the edge would fall into the void. An invisible wall on the apron's rim catches the rest.
    minx, miny, maxx, maxy = None, None, None, None
    for srf in model.surfaces:
        b0 = srf.geometry.bounds
        if minx is None:
            minx, miny, maxx, maxy = b0
        else:
            minx, miny = min(minx, b0[0]), min(miny, b0[1])
            maxx, maxy = max(maxx, b0[2]), max(maxy, b0[3])
    if minx is not None:
        slab = box(minx - GROUND_PLANE_MARGIN, miny - GROUND_PLANE_MARGIN,
                   maxx + GROUND_PLANE_MARGIN, maxy + GROUND_PLANE_MARGIN)
        for tile, piece in _tiles_of(slab, tile_m):
            b = mb(0, "groundplane", tile)
            for poly in _grid_split(piece, GROUND_PLANE_GRID):
                verts, faces = triangulate_polygon(poly)
                if len(faces) == 0:
                    continue
                zz = np.asarray(model.sample_z(verts[:, 0], verts[:, 1]), dtype=np.float64)
                base0 = b.add_vertices(np.column_stack([verts, zz - GROUND_PLANE_DROP]))
                b.add_faces("groundplane", faces, base0)
        # boundary wall: CW ring so the right-hand normal points inward (faces the map)
        rim = LineString(shapely.geometry.polygon.orient(slab, -1.0).exterior.coords).segmentize(GROUND_PLANE_GRID)
        xy = np.asarray(rim.coords)[:, :2]
        zg = np.asarray(model.sample_z(xy[:, 0], xy[:, 1]), dtype=np.float64) - GROUND_PLANE_DROP
        n_boundary = _add_wall_ring(mb(0, "boundary", (0, 0)), rim, zg + BOUNDARY_WALL_HEIGHT, zg)
    else:
        n_boundary = 0
    log.info("crossings: %d on the road mesh, %d as road; paved %.1f m2 of road/plate slots; "
             "%d plate wall faces; %d boundary wall faces", n_cross_overlay, n_cross_base,
             n_gap_m2, n_wall_faces, n_boundary)
    n_clipped = n_skipped = 0
    buildings_out: list[dict] = []
    if buildings:
        drivable = unary_union([s.geometry for s in model.surfaces
                                if s.kind in ("drivable", "parking", "crossing")])
        drivable = drivable.buffer(0.25) if not drivable.is_empty else None
        for idx, b in enumerate(model.buildings):
            fp = clip_building_footprint(b, drivable)
            if fp is None:
                n_skipped += 1
                continue
            if fp is not b.footprint:
                n_clipped += 1
            cen = fp.centroid
            _add_building(mb(0, "building", _tile_index(cen.x, cen.y, tile_m)), model, b,
                          level_height, default_levels, footprint=fp)
            # per-building footprint contours for an editor-side procedural building
            # generator (an alternative to the baked slabs in the ``building`` assets)
            rings = [r for r in (_footprint_ring_ue(p) for p in _polygons(fp)) if r is not None]
            if not rings:
                continue
            raw_polys = sorted(_polygons(b.footprint), key=lambda p: p.area, reverse=True)
            raw_ring = next((r for r in (_footprint_ring_ue(p) for p in raw_polys)
                             if r is not None), None)
            base, roof = building_geometry(model, b, level_height, default_levels)
            buildings_out.append({
                "id": b.osm_id if b.osm_id is not None else idx,
                "rings_ue": rings,
                "raw_ring_ue": raw_ring,
                "base_z_cm": round(base * UE_SCALE, 1),
                "roof_z_cm": round(roof * UE_SCALE, 1),
                "levels": int(b.levels) if b.levels is not None else 0,
                "height_m": round(float(b.effective_height(level_height, default_levels)), 3),
                "category": str((b.tags or {}).get("building") or ""),
            })

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
        "buildings": buildings_out,
        "stats": {"assets": len(assets), "triangles": int(sum(a["triangles"] for a in assets)),
                  "spawn_points": len(sp), "buildings": len(model.buildings) if buildings else 0,
                  "buildings_clipped_by_roads": n_clipped, "buildings_skipped": n_skipped,
                  "layers": sorted({a["layer"] for a in assets})},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log.info("wrote %d glb assets (%d triangles), %d spawn points -> %s", len(assets),
             manifest["stats"]["triangles"], len(sp), out_dir)
    return manifest
