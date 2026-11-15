"""Swap a twin's extruded OSM buildings for real 3D buildings from an open city or national model.

The providers (twinmodel.ingest.buildings: 3DBAG for the Netherlands, the NYC 3D Building Model for New
York City, PLATEAU for Japan) turn their data into ``RealBuilding`` records in the twin's model frame:
surfaces (rings of 3D points with a semantic kind: RoofSurface, WallSurface, ...), the footprint, the
ground and eaves heights, the construction year and the uses of the floor area. ``apply`` does the rest,
whatever the source:

* every building gets a facade style (twinmodel.export.facades) from its year, uses, height and size:
  walls are written per style with UVs that fit whole bays and storeys, party walls get a plain wall,
  buildings with shops a shopfront ground floor; the synthetic facade textures go to ue/textures/;
* roofs are textured with the twin's aerial photo, one texture per tile (or a plain roof material for
  buildings taller than ``roof_photo_max_m``: a city's aerial photo is not a true ortho, tall buildings
  lean in it);
* one glb per (tile, material) is written in the bake export's format and swapped in for the manifest's
  ``building`` assets (the OSM ones are kept in meshes/osm_buildings/); the manifest's ``buildings``
  become the real footprints with ground, eaves and roof heights (the OSM ones move to ``buildings_osm``),
  so ``bake_level.py --buildings procedural`` builds on them.
"""
from __future__ import annotations

import gzip
import json
import math
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# credits of the twin's ortho providers (report.json build.imagery.source), for the roof textures
ORTHO_CREDITS = {
    "pdok_luchtfoto": "PDOK Luchtfoto (CC BY 4.0)",
    "nys_orthos": "NYS ITS orthoimagery (public domain)",
    "naip": "USGS NAIP (public domain)",
    "icgc": "ICGC orthophoto (CC BY 4.0)",
    "ign_es": "IGN PNOA (CC BY 4.0)",
    "osm_tiles": "OpenStreetMap tiles (ODbL)",
}


@dataclass
class RealBuilding:
    """One building of a real 3D model, in the twin's model frame (metres, z = the source's heights,
    which match the twin's DEM: NAP with AHN, NAVD88 with USGS 3DEP)."""
    id: str
    surfaces: list                     # [(rings: [np.ndarray (n, 3)], kind: str | None)]
    poly: object = None                # shapely Polygon footprint (model xy) or None
    ground: Optional[float] = None     # ground height at the building
    height: float = 0.0                # ground to eaves (m)
    roof_z: Optional[float] = None     # highest roof point
    year: Optional[int] = None
    uses: dict = field(default_factory=dict)   # use -> floor area m2 (facades.classify vocabulary)
    levels: int = 0
    roof_type: Optional[str] = None
    parts: int = 1
    centre: Optional[tuple] = None     # (x, y) that picks the tile; default the mean of the surface points
    region: str = "nl"                 # facades.BuildingInfo.region


@dataclass
class Source:
    """What a provider tells apply about its data."""
    name: str                          # provider name ("3dbag", "nyc3d", "plateau")
    label: str                         # stats building_source, e.g. "3DBAG 2.2 via api.3dbag.nl"
    asset_source: str                  # per asset, with the licence: "3DBAG LoD 2.2 (CC BY 4.0)"
    roof_photo_max_m: Optional[float] = None   # taller buildings get the plain roof (None: all photo)
    skirt: bool = False                # extend the walls down to the twin's terrain where it is lower (skirt())
    extra_stats: dict = field(default_factory=dict)


SKIP_SURFACES = {"GroundSurface", "FloorSurface", "ClosureSurface"}


# --------------------------------------------------------------------------- geometry

def newell(pts):
    n = np.zeros(3)
    for a, b in zip(pts, np.roll(pts, -1, axis=0)):
        n += [(a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])]
    norm = np.linalg.norm(n)
    return n / norm if norm > 1e-12 else None


def triangulate(rings):
    """3D polygon (outer ring + holes, each an (n, 3) array) -> (vertices, triangles, normal)."""
    import mapbox_earcut as earcut
    normal = newell(rings[0])
    if normal is None:
        return None
    drop = int(np.argmax(np.abs(normal)))
    keep = [i for i in range(3) if i != drop]
    verts = np.vstack(rings)
    flat = verts[:, keep]
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    tri = np.asarray(earcut.triangulate_float64(flat, ends), dtype=np.int64).reshape(-1, 3)
    if len(tri) == 0:
        return None
    # keep the winding of the outer ring (counter-clockwise seen from outside)
    a, b, c = verts[tri[:, 0]], verts[tri[:, 1]], verts[tri[:, 2]]
    tn = np.cross(b - a, c - a)
    flip = (tn @ normal) < 0
    tri[flip] = tri[flip][:, ::-1]
    return verts, tri, normal


def roof_heights(surfaces, ground: float):
    """(eaves height above ground, highest roof point) from the roof surfaces: the eaves are the
    area-weighted 70th percentile of the roof heights, like 3DBAG's b3_h_dak_70p."""
    zs, ws, top = [], [], None
    for rings, kind in surfaces:
        if kind != "RoofSurface" or not rings:
            continue
        r = rings[0]
        nrm = np.zeros(3)
        for a, b in zip(r, np.roll(r, -1, axis=0)):
            nrm += np.cross(a, b)
        area = 0.5 * float(np.linalg.norm(nrm))
        zs.append(float(r[:, 2].mean()))
        ws.append(max(area, 1e-6))
        m = float(r[:, 2].max())
        top = m if top is None else max(top, m)
    if not zs:
        return 0.0, None
    order = np.argsort(zs)
    cw = np.cumsum(np.asarray(ws)[order])
    z70 = float(np.asarray(zs)[order][np.searchsorted(cw, 0.7 * cw[-1])])
    return max(0.0, z70 - ground), top


# --------------------------------------------------------------------------- caches and fetches

def cache_json(path: Path, fetch_fn: Callable):
    """JSON (gzip) cache beside the build: fetched once, reused by every rerun."""
    if path.exists():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    data = fetch_fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(data, f)
    tmp.replace(path)
    return data


def get_json(url, params, tries=4, user_agent="twinmodel (carla-digitaltwins)"):
    """GET with retries (open-data APIs answer the odd page with a 500)."""
    import time
    import requests
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=120, headers={"User-Agent": user_agent})
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2.0 * (k + 1))


def find_ortho(build_dir: Path, bbox_wgs84) -> Optional[Path]:
    """The twin pipeline's cached ortho for this bbox (data/ortho_<s>_<w>_<n>_<e>_<res>m_*.tif)."""
    s, w, n, e = bbox_wgs84
    stem = "ortho_%.5f_%.5f_%.5f_%.5f_" % (s, w, n, e)
    for d in [build_dir, *build_dir.resolve().parents][:4]:
        hits = sorted((d / "data").glob(stem + "*.tif")) if (d / "data").is_dir() else []
        if hits:
            # the finest resolution first
            return sorted(hits, key=lambda p: float(p.name[len(stem):].split("m_")[0]))[0]
    return None


def load_ortho(path: Path, proj: str):
    """(image HxWx3, transform) of an ortho GeoTIFF in the twin's model frame, or None when the
    file is in another frame (the twin origin must land on model (0, 0))."""
    import rasterio
    from pyproj import CRS, Transformer
    with rasterio.open(path) as src:
        img = np.moveaxis(src.read()[:3], 0, -1)
        tf = src.transform
        crs = CRS.from_wkt(src.crs.to_wkt())
    lat0 = float(proj.split("+lat_0=")[1].split()[0])
    lon0 = float(proj.split("+lon_0=")[1].split()[0])
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon0, lat0)
    if abs(x) > 1.0 or abs(y) > 1.0:
        print(f"ortho {path.name}: not in the model frame (origin at {x:.1f}, {y:.1f}); roofs untextured")
        return None
    return img, (tf.a, tf.b, tf.c, tf.d, tf.e, tf.f)


def find_dem(build_dir: Path, bbox_wgs84) -> Optional[Path]:
    """The twin pipeline's cached elevation grid for this bbox (data/dem_<s>_<w>_<n>_<e>_<res>m.npz)."""
    s, w, n, e = bbox_wgs84
    stem = "dem_%.5f_%.5f_%.5f_%.5f_" % (s, w, n, e)
    for d in [build_dir, *build_dir.resolve().parents][:4]:
        hits = sorted((d / "data").glob(stem + "*.npz")) if (d / "data").is_dir() else []
        if hits:
            return sorted(hits, key=lambda p: float(p.name[len(stem):].split("m")[0]))[0]
    return None


def skirt(buildings: list, dem_path: Path, below_m: float = 0.3, step_m: float = 1.0) -> int:
    """Extend each building's walls down to the twin's terrain where it lies below the building's ground.

    A city model stands every building on one ground height (the NYC model: a flat GroundSurface), while the
    twin's terrain (USGS 3DEP, AHN, ...) follows the street: on a slope the building would float above the
    low side. The wall vertices at the base move down to ``below_m`` under the lowest terrain along the
    footprint, so the walls reach into the ground; where the terrain is higher they stay hidden under it.
    Returns the number of buildings extended."""
    d = np.load(dem_path, allow_pickle=True)
    z, x0, y0, dx, dy = d["z"], float(d["x0"]), float(d["y0"]), float(d["dx"]), float(d["dy"])
    n = 0
    for b in buildings:
        if b.poly is None or b.poly.is_empty or b.ground is None:
            continue
        ring = b.poly.exterior
        pts = np.asarray([ring.interpolate(t).coords[0] for t in np.arange(0.0, ring.length, step_m)])
        if not len(pts):
            continue
        c = np.clip(np.round((pts[:, 0] - x0) / dx).astype(int), 0, z.shape[1] - 1)
        r = np.clip(np.round((pts[:, 1] - y0) / dy).astype(int), 0, z.shape[0] - 1)
        low = float(np.nanmin(z[r, c]))
        if not np.isfinite(low) or low >= b.ground - 0.05:
            continue
        base = low - below_m
        for rings, kind in b.surfaces:
            if kind not in ("WallSurface", None):
                continue
            if kind is None:   # untyped faces: walls only
                nrm = newell(rings[0])
                if nrm is None or abs(nrm[2]) > 0.3:
                    continue
            for rr in rings:
                rr[rr[:, 2] <= b.ground + 0.05, 2] = base
        n += 1
    return n


def ortho_credit(build_dir: Path) -> str:
    """Credit of the ortho the twin was built with (its report.json), for the roof textures."""
    try:
        src = json.loads((build_dir / "report.json").read_text())["build"]["imagery"]["source"]
    except Exception:  # noqa: BLE001
        return "the twin's aerial photo"
    return ORTHO_CREDITS.get(src, src)


def model_proj(man: dict) -> str:
    """The manifest's geo reference without the vertical datum terms pyproj cannot always resolve."""
    return " ".join(t for t in man["geo_reference"].split() if not t.startswith(("+geoidgrids", "+vunits")))


def is_real(b: dict) -> bool:
    """A manifest building written by apply (any provider; 3DBAG ids before the field existed)."""
    return bool(b.get("real")) or str(b.get("id", "")).startswith("NL.IMBAG")


# --------------------------------------------------------------------------- meshes and manifest

class _Builder:
    def __init__(self):
        self.pos, self.nrm, self.uv, self.faces, self.n = [], [], [], [], 0

    def add(self, v, normal, uv, tri):
        self.pos.append(v)
        self.nrm.append(np.repeat(np.asarray(normal)[None, :], len(v), axis=0))
        self.uv.append(uv)
        self.faces.append(tri + self.n)
        self.n += len(v)

    def arrays(self):
        return np.vstack(self.pos), np.vstack(self.nrm), np.vstack(self.uv), np.vstack(self.faces)


def apply(build: Path, man: dict, buildings: list, source: Source, *, looks: bool = True,
          ortho=None, ortho_label: str = "", texture_px: Optional[int] = None, roof_px: int = 1024,
          procedural_min_height: Optional[float] = None) -> dict:
    """Mesh, style and texture ``buildings`` and swap them into the bake export ``build``/ue (its
    manifest ``man`` is updated in place and written). Returns the manifest's stats."""
    import shapely
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    from twinmodel.export import facades
    from twinmodel.export.ue import _tile_index, write_glb

    texture_px = texture_px or facades.TEX_PX
    procedural_min_height = facades.PROCEDURAL_MIN_HEIGHT_M if procedural_min_height is None \
        else procedural_min_height
    out = build / "ue"
    name, tile_m = man["name"], float(man["tile_m"])

    # ---- the OSM buildings (their categories help the styles)
    already = bool(man["stats"].get("real_buildings")) or \
        str(man["stats"].get("building_source", "")).startswith("3DBAG")
    osm_blds = man.get("buildings_osm")
    if osm_blds is None:   # the bake export's OSM footprints (also after a --plain run)
        osm_blds = [b for b in man.get("buildings", []) if not is_real(b)]
    osm_polys, osm_cat = [], []
    for b in osm_blds or ():
        ring = b.get("raw_ring_ue") or (b.get("rings_ue") or [None])[0]
        if ring and len(ring) >= 3:
            poly = Polygon([(x / 100.0, -y / 100.0) for x, y in ring])
            if poly.is_valid and poly.area > 0:
                osm_polys.append(poly)
                osm_cat.append(b.get("category"))
    osm_tree = STRtree(osm_polys) if osm_polys else None

    # ---- styles
    polys = [b.poly if b.poly is not None else Polygon() for b in buildings]
    tree = STRtree(polys)
    styles_n = defaultdict(int)
    info = []
    for b in buildings:
        cat = None
        if osm_tree is not None and b.poly is not None and not b.poly.is_empty:
            hit = osm_tree.query(b.poly.representative_point(), predicate="within")
            cat = osm_cat[hit[0]] if len(hit) else None
        bi_ = facades.BuildingInfo(id=b.id, year=b.year, uses=b.uses, height_m=b.height,
                                   area_m2=b.poly.area if b.poly is not None else 0.0, osm_category=cat,
                                   region=b.region)
        style, shop = facades.classify(bi_) if looks else ("plain", False)
        info.append({"style": style, "shop": shop, "osm_category": cat})
        styles_n[style] += 1

    # ---- meshes
    tiles = defaultdict(lambda: defaultdict(_Builder))   # tile -> (material key, low) -> builder
    used_keys = set()
    n_buildings = n_parts = n_tris = n_party = n_plain_roof = 0
    boundaries = [p.exterior if p is not None and not p.is_empty else None for p in polys]
    for k, b in enumerate(buildings):
        i = info[k]
        allv = np.vstack([r for rings, _ in b.surfaces for r in rings]) if b.surfaces else None
        if allv is None or not len(allv):
            continue
        cx, cy = b.centre or (float(np.mean(allv[:, 0])), float(np.mean(allv[:, 1])))
        tile = _tile_index(cx, cy, tile_m)
        ground = float(b.ground) if b.ground is not None else float(allv[:, 2].min())
        photo_roof = ortho is not None and (source.roof_photo_max_m is None or b.height <= source.roof_photo_max_m)
        st = facades.STYLES.get(i["style"]) if looks else None
        if st is not None:
            var = facades.variant_of(st.name, b.id)
            low_h = facades.SHOP_FLOOR_M if i["shop"] else 0.0
            n_st, storey_h = facades.storeys_for(b.height - low_h, st.storey_m)
            blank = facades.STYLES[facades.BLANK_OF.get(st.name, "blank_brick")]
            blank_key = facades.material_key(blank.name, facades.variant_of(blank.name, b.id) if
                                             blank.name != st.name else var)
            shop_st = facades.STYLES["shopfront"]
            shop_key = facades.material_key("shopfront", facades.variant_of("shopfront", b.id))
            key = facades.material_key(st.name, var)
            eaves_z = ground + b.height
            i["procedural"] = facades.procedural_ok(st.name, b.height, procedural_min_height)
            near = []
            if b.poly is not None and not b.poly.is_empty:
                near = [boundaries[j] for j in tree.query(b.poly.buffer(facades.PARTY_TOL_M))
                        if j != k and boundaries[j] is not None]
        used = False
        for rings, kind in b.surfaces:
            if kind in SKIP_SURFACES or not rings:
                continue
            res = triangulate(rings)
            if res is None:
                continue
            v, tri, normal = res
            n_tris += len(tri)
            tb = tiles[tile]
            used = True

            def add_wall(mkey, v, tri, uvfn):
                """Walls below the eaves of a procedural-capable building go apart (kind
                'building', which --buildings procedural replaces); the rest is kept."""
                parts_ = [(v, tri, False)]
                if i.get("procedural"):
                    (vb, tb_), (va, ta) = facades.clip_triangles(v, tri, eaves_z)
                    parts_ = [(vb, tb_, True), (va, ta, False)]
                for vv, tt, low in parts_:
                    if len(tt):
                        tb[(mkey, low)].add(vv, normal, uvfn(vv), tt)
                used_keys.add(mkey)
            if not looks:
                if abs(normal[2]) > 0.7:
                    uv = v[:, :2]
                else:
                    along = np.array([-normal[1], normal[0], 0.0]) / max(1e-9, math.hypot(normal[0], normal[1]))
                    uv = np.column_stack([v @ along, v[:, 2]])
                tb[("building", True)].add(v, normal, uv, tri)
                used_keys.add("building")
                continue
            if kind == "RoofSurface" or abs(normal[2]) > 0.7:
                rkey = "roof_%d_%d" % tile if photo_roof else "roof"
                tb[(rkey, False)].add(v, normal, facades.roof_uv(v, tile, tile_m), tri)
                used_keys.add(rkey)
                continue
            # walls
            along = facades.wall_frame(normal)
            width = float(np.ptp(rings[0] @ along))
            foot = rings[0][rings[0][:, 2] <= rings[0][:, 2].min() + 0.5]
            mid = shapely.Point(float(foot[:, 0].mean()), float(foot[:, 1].mean()))
            party = any(bd.distance(mid) < facades.PARTY_TOL_M for bd in near)
            if party or width < 0.6 * st.bay_m or st.blank:
                n_party += int(party)
                add_wall(blank_key, v, tri, lambda vv: facades.blank_uv(vv, normal, blank))
                continue
            ring0 = rings[0]
            if i["shop"]:
                (vb, tb_), (va, ta) = facades.clip_triangles(v, tri, ground + facades.SHOP_FLOOR_M)
                if len(tb_):
                    add_wall(shop_key, vb, tb_, lambda vv: facades.wall_uv(
                        vv, normal, ground, shop_st.storey_m, shop_st.bay_m, ring=ring0))
                v, tri = va, ta
                if not len(tri):
                    continue
            add_wall(key, v, tri, lambda vv: facades.wall_uv(vv, normal, ground, storey_h, st.bay_m,
                                                             ring=ring0, z0=low_h))
        if used:
            n_parts += b.parts
            n_buildings += 1
            n_plain_roof += int(looks and ortho is not None and not photo_roof)

    # ---- textures
    tex_dir = out / "textures"
    textures = {}
    if looks:
        tex_dir.mkdir(exist_ok=True)
        for key in sorted(k for k in used_keys if k.startswith("facade_")):
            sname, var = key[len("facade_"):].rsplit("_", 1)
            alb, nrm_, orm = facades.synth_facade(facades.STYLES[sname], int(var), px=texture_px)
            entry = {}
            for suffix, arr in (("d", alb), ("n", nrm_), ("orm", orm)):
                rel = f"textures/T_{name}_{key}_{suffix}.png"
                facades.save_png(out / rel, arr)
                entry[{"d": "base_color", "n": "normal", "orm": "orm"}[suffix]] = rel
            st = facades.STYLES[sname]
            entry.update(style=sname, variant=int(var), bay_m=st.bay_m, storey_m=st.storey_m)
            textures[key] = entry
        if ortho:
            facades.save_png(out / f"textures/T_{name}_flat_n.png", facades.flat_normal())
            facades.save_png(out / f"textures/T_{name}_roof_orm.png", facades.constant_orm(0.8))
            for key in sorted(k for k in used_keys if k.startswith("roof_")):
                tx, ty = (int(t) for t in key.split("_")[1:3])
                img = facades.ortho_tile(ortho[0], ortho[1], (tx, ty), tile_m, roof_px)
                if img is None:
                    continue
                rel = f"textures/T_{name}_{key}_d.png"
                facades.save_png(out / rel, img)
                textures[key] = {"base_color": rel, "normal": f"textures/T_{name}_flat_n.png",
                                 "orm": f"textures/T_{name}_roof_orm.png", "source": ortho_label}

    # ---- glb files and the manifest
    mesh_dir = out / "meshes"
    old = [a for a in man["assets"] if a["kind"] in ("building", "building_3dbag", "roof")]
    template = next((a for a in old), None) or {"material": "building", "semantic": "Building", "layer": 0}
    if not already:
        backup = mesh_dir / "osm_buildings"
        backup.mkdir(exist_ok=True)
        for a in old:
            src = out / a["file"]
            if src.exists():
                shutil.move(str(src), str(backup / src.name))
    else:
        for a in old:
            (out / a["file"]).unlink(missing_ok=True)
    assets = [a for a in man["assets"] if a["kind"] not in ("building", "building_3dbag", "roof")]
    for (tx, ty), mats in sorted(tiles.items()):
        for (key, low), bld in sorted(mats.items()):
            if not bld.faces:
                continue
            pos, nrm, uv, faces = bld.arrays()
            is_roof = key.startswith("roof")
            if key == "building":
                asset = f"{name}_L0_building_{tx}_{ty}"
            else:
                asset = f"{name}_L0_{'roof' if is_roof else 'building'}_{tx}_{ty}" + \
                        ("" if is_roof else "_" + key[len("facade_"):] + ("_p" if low else ""))
                if key == "roof":
                    asset += "_plain"
            asset = asset.replace("-", "m")
            bbox = write_glb(mesh_dir / f"{asset}.glb", asset, pos, nrm, uv, faces, key)
            # kind 'building': what --buildings procedural replaces; 'building_3dbag': the real walls it keeps
            kind = "roof" if is_roof else ("building" if low else "building_3dbag")
            a = {"file": f"meshes/{asset}.glb", "asset": asset, "layer": template.get("layer", 0),
                 "kind": kind, "material": key,
                 "semantic": "Building", "tile": [tx, ty],
                 "vertices": int(len(pos)), "triangles": int(len(faces)), "bbox_ue": bbox,
                 "source": source.asset_source}
            if key in textures:
                a["texture"] = key
            assets.append(a)
    man["assets"] = assets
    if textures:
        man["textures"] = textures
    else:
        man.pop("textures", None)
    if not already:
        man["stats"]["buildings_osm_replaced"] = man["stats"].get("buildings", 0)
    if looks and "buildings_osm" not in man:
        man["buildings_osm"] = osm_blds
    if looks:
        man["buildings"] = []
        for b, i in zip(buildings, info):
            poly = b.poly
            if poly is None or poly.is_empty or b.ground is None:
                continue
            ring = np.asarray(poly.exterior.coords)[:-1]
            rgb = facades.mean_colour(ortho[0], ortho[1], ring) if ortho else None
            gen_ring = ring
            if i.get("procedural"):
                # the generator fills every segment shorter than one 5 m module with a plain quad:
                # drop the footprint's small jogs (and zero-length edges) first
                simple = poly.simplify(facades.PROCEDURAL_SIMPLIFY_M, preserve_topology=True)
                if simple.geom_type == "Polygon" and simple.is_valid and len(simple.exterior.coords) >= 4:
                    gen_ring = np.asarray(simple.exterior.coords)[:-1]
            man["buildings"].append({
                "id": b.id, "real": source.name,
                "rings_ue": [[[round(x * 100, 1), round(-y * 100, 1)] for x, y in gen_ring]],
                "raw_ring_ue": [[round(x * 100, 1), round(-y * 100, 1)] for x, y in ring],
                "base_z_cm": round(float(b.ground) * 100, 1),
                "eaves_z_cm": round((float(b.ground) + b.height) * 100, 1),
                "roof_z_cm": round(float(b.roof_z if b.roof_z is not None else b.ground + b.height) * 100, 1),
                "height_m": round(b.height, 2), "levels": b.levels or 0,
                "category": i["osm_category"] or facades.dominant_use(b.uses) or "yes",
                "year": b.year, "uses": {k: round(v) for k, v in b.uses.items()},
                "roof_type": b.roof_type, "style": i["style"], "shopfront": bool(i["shop"]),
                "roof_rgb": list(rgb) if rgb else None, "procedural": bool(i.get("procedural")),
                "family": facades.procedural_family(i["style"], b.height)})
    man["stats"]["buildings"] = n_buildings
    man["stats"]["real_buildings"] = source.name
    man["stats"]["building_source"] = f"{source.label} ({n_parts} parts, {n_tris} triangles)"
    if looks:
        man["stats"]["facade_styles"] = dict(sorted(styles_n.items()))
        man["stats"]["party_walls"] = n_party
        man["stats"]["roof_photo"] = bool(ortho)
        if source.roof_photo_max_m is not None and ortho is not None:
            man["stats"]["roof_plain_tall"] = n_plain_roof
        man["stats"]["procedural_capable"] = sum(1 for i in info if i.get("procedural"))
        man["stats"].update(source.extra_stats)
    man["stats"]["triangles"] = int(sum(a["triangles"] for a in assets))
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    n_assets = sum(1 for a in assets if a["kind"] in ("building", "building_3dbag", "roof"))
    print(f"{source.name}: {n_buildings} buildings, {n_parts} parts, {n_tris} triangles in {len(tiles)} tiles, "
          f"{n_assets} assets, {len(textures)} textures; styles {dict(styles_n)}; {n_party} party walls"
          + (f"; {n_plain_roof} tall buildings with a plain roof" if n_plain_roof else ""))
    return man["stats"]
