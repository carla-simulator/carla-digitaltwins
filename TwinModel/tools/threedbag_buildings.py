"""Replace a twin's extruded OSM buildings with 3DBAG LoD 2.2 buildings (the Netherlands).

    python tools/threedbag_buildings.py build/<name>             # after `twinmodel bake-export`

3DBAG (3dbag.nl, CC BY 4.0) models every building in the Netherlands from the BAG footprints and AHN lidar,
with roof shapes (LoD 2.2). This tool reads the bake export's manifest, fetches the 3DBAG buildings in its bbox
from the 3DBAG OGC API (CityJSON), triangulates their roof and wall surfaces, converts them from RD New + NAP
into the twin's local frame (NAP heights, as the AHN elevation the twin was built on), writes one glb per tile
in the export's format, and swaps them in for the `building` assets of the manifest. The OSM building meshes
are kept in meshes/osm_buildings/.

Looks (default; --plain for one material): every building gets a facade style from its BAG construction year,
the use of its units (BAG verblijfsobjecten from the PDOK BAG OGC API, CC0) and its size: walls are written per
style with UVs that fit whole bays and storeys, party walls get a plain wall, buildings with shops a shopfront
ground floor; the synthetic facade textures go to ue/textures/ (twinmodel.export.facades). Roofs are textured with
the twin's aerial photo (PDOK Luchtfoto 25 cm, CC BY 4.0), one texture per tile. The manifest's `buildings`
become the 3DBAG footprints with real ground, eaves and ridge heights (the OSM ones move to `buildings_osm`), so
`bake_level.py --buildings procedural` builds on the real footprints and heights. Fetches are cached in
<build>/3dbag/.
"""
import argparse
import gzip
import json
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import mapbox_earcut as earcut
import numpy as np
import requests
import shapely
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from twinmodel.export import facades
from twinmodel.export.ue import _tile_index, write_glb

API = "https://api.3dbag.nl/collections/pand/items"
BAG_API = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections/verblijfsobject/items"
SKIP_SURFACES = {"GroundSurface"}


def _fetch_pages(bbox_rd, limit=50):
    """One bbox, following the API's next links (it returns at most 50 features a page)."""
    url, params, features, matched = API, {"bbox": ",".join(f"{v:.2f}" for v in bbox_rd), "limit": limit}, [], None
    while url:
        r = requests.get(url, params=params, timeout=120, headers={"User-Agent": "twinmodel-3dbag/0.1"})
        r.raise_for_status()
        page = r.json()
        matched = page.get("numberMatched", matched)
        tf = page["metadata"]["transform"]
        scale, translate = np.array(tf["scale"]), np.array(tf["translate"])
        for f in page["features"]:
            f["vertices"] = (np.asarray(f["vertices"], dtype=np.float64) * scale + translate).tolist()
            features.append(f)
        url = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
        params = None
    return features, matched


def fetch(bbox_rd, tile_m=400.0):
    """All 3DBAG features (CityJSONFeature with real coordinates) in an RD New bbox, fetched in tiles of
    tile_m. The API repeats buildings across pages (its numberMatched counts each about twice) and a building
    on a tile edge comes in both tiles, so every building is kept once, by id."""
    x0, y0, x1, y1 = bbox_rd
    nx, ny = max(1, math.ceil((x1 - x0) / tile_m)), max(1, math.ceil((y1 - y0) / tile_m))
    seen, out = set(), []
    for i in range(nx):
        for j in range(ny):
            tb = (x0 + i * (x1 - x0) / nx, y0 + j * (y1 - y0) / ny,
                  x0 + (i + 1) * (x1 - x0) / nx, y0 + (j + 1) * (y1 - y0) / ny)
            feats, matched = _fetch_pages(tb)
            for f in feats:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    out.append(f)
            print(f"3dbag: tile {i + 1},{j + 1} of {nx}x{ny}: {len(out)} buildings so far", flush=True)
    return out


def newell(pts):
    n = np.zeros(3)
    for a, b in zip(pts, np.roll(pts, -1, axis=0)):
        n += [(a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])]
    norm = np.linalg.norm(n)
    return n / norm if norm > 1e-12 else None


def triangulate(rings):
    """3D polygon (outer ring + holes, each an (n, 3) array) -> (vertices, triangles, normal)."""
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
    # keep the winding of the outer ring (CityJSON: counter-clockwise seen from outside)
    a, b, c = verts[tri[:, 0]], verts[tri[:, 1]], verts[tri[:, 2]]
    tn = np.cross(b - a, c - a)
    flip = (tn @ normal) < 0
    tri[flip] = tri[flip][:, ::-1]
    return verts, tri, normal


def surfaces_of(geom):
    """Yield (rings as vertex-index lists, semantic type) for a Solid or MultiSurface geometry."""
    sem = geom.get("semantics") or {}
    types = [s.get("type") for s in sem.get("surfaces", [])]
    values = sem.get("values")
    if geom["type"] == "Solid":
        for si, shell in enumerate(geom["boundaries"]):
            for fi, surface in enumerate(shell):
                v = values[si][fi] if values else None
                yield surface, (types[v] if v is not None else None)
    elif geom["type"] in ("MultiSurface", "CompositeSurface"):
        for fi, surface in enumerate(geom["boundaries"]):
            v = values[fi] if values else None
            yield surface, (types[v] if v is not None else None)




# --------------------------------------------------------------------------- caches and BAG uses

def _cache_json(path: Path, fetch_fn):
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


def _get_json(url, params, tries=4):
    """GET with retries (PDOK answers the odd page with a 500)."""
    import time
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=120, headers={"User-Agent": "twinmodel-3dbag/0.1"})
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2.0 * (k + 1))


def fetch_uses(bbox_wgs84, grid=3):
    """BAG verblijfsobjecten (units) in a (s, w, n, e) bbox from the PDOK BAG OGC API (CC0), in
    grid x grid tiles (a long cursor chain fails more often): only the point, use(s) and floor area
    are kept (no addresses). A tile that keeps failing is left out with a log line."""
    s, w, n, e = bbox_wgs84
    out, seen = [], set()
    for i in range(grid):
        for j in range(grid):
            tw, te = w + i * (e - w) / grid, w + (i + 1) * (e - w) / grid
            ts, tn = s + j * (n - s) / grid, s + (j + 1) * (n - s) / grid
            url = BAG_API
            params = {"f": "json", "limit": 1000, "bbox": f"{tw:.6f},{ts:.6f},{te:.6f},{tn:.6f}"}
            try:
                while url:
                    page = _get_json(url, params)
                    for f in page.get("features", []):
                        g, p = f.get("geometry") or {}, f.get("properties") or {}
                        uid = p.get("identificatie")
                        if g.get("type") != "Point" or uid in seen:
                            continue
                        seen.add(uid)
                        uses = p.get("gebruiksdoel") or ""
                        uses = uses if isinstance(uses, list) else \
                            [u.strip() for u in str(uses).split(",") if u.strip()]
                        out.append([g["coordinates"][0], g["coordinates"][1], uses, p.get("oppervlakte") or 0])
                    url = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
                    params = None
            except Exception as exc:  # noqa: BLE001
                print(f"bag: tile {i + 1},{j + 1} failed ({exc}); its units are left out")
            print(f"bag: tile {i + 1},{j + 1} of {grid}x{grid}: {len(out)} units so far", flush=True)
    return out


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
    from pyproj import CRS
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


# --------------------------------------------------------------------------- main

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


def footprint_of(obj, verts):
    """The LoD 0 footprint ring (model xy) of a 3DBAG Building, or None."""
    for g in obj.get("geometry", []):
        if str(g.get("lod")) == "0" and g.get("boundaries"):
            b = g["boundaries"]
            ring = b[0][0] if g["type"] == "MultiSurface" else b[0]
            if len(ring) >= 3:
                return verts[np.asarray(ring, dtype=np.int64)][:, :2]
    return None


def eaves_height(attrs) -> Optional[float]:
    g = attrs.get("b3_h_maaiveld")
    if g is None:
        return None
    top = attrs.get("b3_h_dak_min") if attrs.get("b3_dak_type") == "slanted" else attrs.get("b3_h_dak_70p")
    if top is None:
        top = attrs.get("b3_h_dak_50p") or attrs.get("b3_h_dak_max")
    return None if top is None else max(0.0, float(top) - float(g))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build_dir")
    ap.add_argument("--lod", default="2.2")
    ap.add_argument("--plain", action="store_true",
                    help="one material for every building (no facade styles, no roof photo)")
    ap.add_argument("--ortho", default=None, help="ortho GeoTIFF in the model frame (default: the "
                    "twin pipeline's cached data/ortho_<bbox>_*.tif)")
    ap.add_argument("--no-uses", action="store_true", help="do not fetch BAG uses (styles from year and OSM)")
    ap.add_argument("--texture-px", type=int, default=facades.TEX_PX)
    ap.add_argument("--roof-px", type=int, default=1024, help="aerial photo texture per tile")
    ap.add_argument("--procedural-min-height", type=float, default=facades.PROCEDURAL_MIN_HEIGHT_M,
                    help="buildings at least this high to the eaves (m) may take bake_level.py's "
                         "--buildings procedural facades: their walls below the eaves are written apart")
    args = ap.parse_args()
    build = Path(args.build_dir)
    out = build / "ue"
    man = json.loads((out / "manifest.json").read_text())
    name, tile_m = man["name"], float(man["tile_m"])
    proj = " ".join(t for t in man["geo_reference"].split() if not t.startswith(("+geoidgrids", "+vunits")))
    to_model = Transformer.from_crs("EPSG:28992", proj, always_xy=True)
    wgs_to_model = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    to_rd = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
    s, w, n, e = man["bbox_wgs84"]
    xs, ys = zip(*(to_rd.transform(lon, lat) for lat, lon in ((s, w), (s, e), (n, w), (n, e))))
    bbox_rd = (min(xs), min(ys), max(xs), max(ys))
    cache = build / "3dbag"
    feats = _cache_json(cache / ("features_%.0f_%.0f_%.0f_%.0f.json.gz" % bbox_rd), lambda: fetch(bbox_rd))
    print(f"3dbag: {len(feats)} buildings")

    looks = not args.plain
    units = []
    if looks and not args.no_uses:
        try:
            units = _cache_json(cache / ("bag_units_%.5f_%.5f_%.5f_%.5f.json.gz" % (s, w, n, e)),
                                lambda: fetch_uses((s, w, n, e)))
        except Exception as exc:  # noqa: BLE001 - styles fall back to year + OSM category
            print(f"bag: uses unavailable ({exc}); styles from construction year and OSM only")
    ortho = None
    if looks:
        op = Path(args.ortho) if args.ortho else find_ortho(build, man["bbox_wgs84"])
        if op and op.exists():
            ortho = load_ortho(op, proj)
            if ortho:
                print(f"ortho: {op.name} {ortho[0].shape[1]}x{ortho[0].shape[0]} px")
        else:
            print("ortho: none found; roofs get a plain roof material")

    # ---- per building: footprint, attributes, style
    already_3dbag = str(man["stats"].get("building_source", "")).startswith("3DBAG")
    osm_blds = man.get("buildings_osm")
    if osm_blds is None:   # the bake export's OSM footprints (also after a run of the --plain tool)
        osm_blds = [b for b in man.get("buildings", []) if not str(b.get("id", "")).startswith("NL.IMBAG")]
    osm_polys, osm_cat = [], []
    for b in osm_blds or ():
        ring = b.get("raw_ring_ue") or (b.get("rings_ue") or [None])[0]
        if ring and len(ring) >= 3:
            poly = Polygon([(x / 100.0, -y / 100.0) for x, y in ring])
            if poly.is_valid and poly.area > 0:
                osm_polys.append(poly)
                osm_cat.append(b.get("category"))
    osm_tree = STRtree(osm_polys) if osm_polys else None

    infos = []
    for f in feats:
        verts_rd = np.asarray(f["vertices"])
        mx, my = to_model.transform(verts_rd[:, 0], verts_rd[:, 1])
        verts = np.column_stack([mx, my, verts_rd[:, 2]])
        bobj = next((o for o in f["CityObjects"].values() if o.get("type") == "Building"), None)
        attrs = (bobj or {}).get("attributes", {})
        ring = footprint_of(bobj, verts) if bobj else None
        poly = Polygon(ring) if ring is not None else None
        if poly is not None and not poly.is_valid:
            poly = poly.buffer(0)
            if poly.geom_type != "Polygon":
                poly = max(poly.geoms, key=lambda g: g.area) if hasattr(poly, "geoms") else None
        infos.append({"f": f, "verts": verts, "attrs": attrs, "poly": poly, "id": f["id"]})

    polys = [i["poly"] if i["poly"] is not None else Polygon() for i in infos]
    tree = STRtree(polys)
    # BAG units -> buildings (point in footprint)
    for i in infos:
        i["uses"] = {}
    if units:
        ux, uy = wgs_to_model.transform([u[0] for u in units], [u[1] for u in units])
        pts = shapely.points(np.column_stack([ux, uy]))
        pi, bi = tree.query(pts, predicate="within")
        for p_, b_ in zip(pi, bi):
            u = units[p_]
            for use in u[2] or ["onbekend"]:
                d = infos[b_]["uses"]
                d[use] = d.get(use, 0) + max(1, float(u[3] or 0)) / max(1, len(u[2]))
    styles_n = defaultdict(int)
    for i in infos:
        a, poly = i["attrs"], i["poly"]
        h = eaves_height(a) or 0.0
        cat = None
        if osm_tree is not None and poly is not None and not poly.is_empty:
            hit = osm_tree.query(poly.representative_point(), predicate="within")
            cat = osm_cat[hit[0]] if len(hit) else None
        bi_ = facades.BuildingInfo(id=i["id"], year=a.get("oorspronkelijkbouwjaar"), uses=i["uses"],
                                   height_m=h, area_m2=poly.area if poly is not None else 0.0, osm_category=cat)
        style, shop = facades.classify(bi_) if looks else ("plain", False)
        i.update(style=style, shop=shop, height=h, osm_category=cat,
                 ground=a.get("b3_h_maaiveld"), year=bi_.year)
        styles_n[style] += 1

    # ---- meshes
    tiles = defaultdict(lambda: defaultdict(_Builder))   # tile -> material key -> builder
    used_keys = set()
    n_buildings = n_parts = n_tris = n_party = 0
    boundaries = [p.exterior if p is not None and not p.is_empty else None for p in polys]
    for k, i in enumerate(infos):
        f, verts, a = i["f"], i["verts"], i["attrs"]
        parts = [o for o in f["CityObjects"].values() if o.get("type") == "BuildingPart"]
        cx, cy = float(np.mean(verts[:, 0])), float(np.mean(verts[:, 1]))
        tile = _tile_index(cx, cy, tile_m)
        ground = float(i["ground"]) if i["ground"] is not None else float(verts[:, 2].min())
        st = facades.STYLES.get(i["style"]) if looks else None
        if st is not None:
            var = facades.variant_of(st.name, i["id"])
            low_h = facades.SHOP_FLOOR_M if i["shop"] else 0.0
            n_st, storey_h = facades.storeys_for(i["height"] - low_h, st.storey_m)
            blank = facades.STYLES[facades.BLANK_OF.get(st.name, "blank_brick")]
            blank_key = facades.material_key(blank.name, facades.variant_of(blank.name, i["id"]) if
                                             blank.name != st.name else var)
            shop_st = facades.STYLES["shopfront"]
            shop_key = facades.material_key("shopfront", facades.variant_of("shopfront", i["id"]))
            key = facades.material_key(st.name, var)
            eaves_z = ground + i["height"]
            i["procedural"] = facades.procedural_ok(st.name, i["height"], args.procedural_min_height)
            # neighbours for the party-wall test
            near = []
            if i["poly"] is not None and not i["poly"].is_empty:
                near = [boundaries[j] for j in tree.query(i["poly"].buffer(facades.PARTY_TOL_M))
                        if j != k and boundaries[j] is not None]
        used = False
        for part in parts:
            geoms = {g.get("lod"): g for g in part.get("geometry", [])}
            geom = geoms.get(args.lod) or geoms.get("2.2") or geoms.get("1.2")
            if geom is None:
                continue
            for rings_idx, kind in surfaces_of(geom):
                if kind in SKIP_SURFACES:
                    continue
                rings = [verts[np.asarray(r, dtype=np.int64)] for r in rings_idx if len(r) >= 3]
                if not rings:
                    continue
                res = triangulate(rings)
                if res is None:
                    continue
                v, tri, normal = res
                n_tris += len(tri)
                tb = tiles[tile]

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
                    rkey = "roof_%d_%d" % tile if ortho else "roof"
                    tb[(rkey, False)].add(v, normal, facades.roof_uv(v, tile, tile_m), tri)
                    used_keys.add(rkey)
                    continue
                # walls
                along = facades.wall_frame(normal)
                width = float(np.ptp(rings[0] @ along))
                foot = rings[0][rings[0][:, 2] <= rings[0][:, 2].min() + 0.5]
                mid = shapely.Point(float(foot[:, 0].mean()), float(foot[:, 1].mean()))
                party = any(b.distance(mid) < facades.PARTY_TOL_M for b in near)
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
            n_parts += 1
            used = True
        n_buildings += int(used)

    # ---- textures
    tex_dir = out / "textures"
    textures = {}
    if looks:
        tex_dir.mkdir(exist_ok=True)
        for key in sorted(k for k in used_keys if k.startswith("facade_")):
            sname, var = key[len("facade_"):].rsplit("_", 1)
            alb, nrm_, orm = facades.synth_facade(facades.STYLES[sname], int(var), px=args.texture_px)
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
                img = facades.ortho_tile(ortho[0], ortho[1], (tx, ty), tile_m, args.roof_px)
                if img is None:
                    continue
                rel = f"textures/T_{name}_{key}_d.png"
                facades.save_png(out / rel, img)
                textures[key] = {"base_color": rel, "normal": f"textures/T_{name}_flat_n.png",
                                 "orm": f"textures/T_{name}_roof_orm.png", "source": "PDOK Luchtfoto (CC BY 4.0)"}

    # ---- glb files and the manifest
    mesh_dir = out / "meshes"
    old = [a for a in man["assets"] if a["kind"] in ("building", "building_3dbag", "roof")]
    template = next((a for a in old), None) or {"material": "building", "semantic": "Building", "layer": 0}
    if not already_3dbag:
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
        for (key, low), b in sorted(mats.items()):
            if not b.faces:
                continue
            pos, nrm, uv, faces = b.arrays()
            is_roof = key.startswith("roof")
            if key == "building":
                asset = f"{name}_L0_building_{tx}_{ty}"
            else:
                asset = f"{name}_L0_{'roof' if is_roof else 'building'}_{tx}_{ty}" + \
                        ("" if is_roof else "_" + key[len("facade_"):] + ("_p" if low else ""))
            asset = asset.replace("-", "m")
            bbox = write_glb(mesh_dir / f"{asset}.glb", asset, pos, nrm, uv, faces, key)
            # kind 'building': what --buildings procedural replaces; 'building_3dbag': walls it keeps
            kind = "roof" if is_roof else ("building" if low else "building_3dbag")
            a = {"file": f"meshes/{asset}.glb", "asset": asset, "layer": template.get("layer", 0),
                 "kind": kind, "material": key,
                 "semantic": "Building", "tile": [tx, ty],
                 "vertices": int(len(pos)), "triangles": int(len(faces)), "bbox_ue": bbox,
                 "source": "3DBAG LoD %s (CC BY 4.0)" % args.lod}
            if key in textures:
                a["texture"] = key
            assets.append(a)
    man["assets"] = assets
    if textures:
        man["textures"] = textures
    else:
        man.pop("textures", None)
    if not already_3dbag:
        man["stats"]["buildings_osm_replaced"] = man["stats"].get("buildings", 0)
    if looks and "buildings_osm" not in man:
        man["buildings_osm"] = osm_blds
    if looks:
        man["buildings"] = []
        for i in infos:
            poly = i["poly"]
            if poly is None or poly.is_empty or i["ground"] is None:
                continue
            ring = np.asarray(poly.exterior.coords)[:-1]
            rgb = facades.mean_colour(ortho[0], ortho[1], ring) if ortho else None
            a = i["attrs"]
            gen_ring = ring
            if i.get("procedural"):
                # the generator fills every segment shorter than one 5 m module with a plain quad:
                # drop the footprint's small jogs (and zero-length edges) first
                simple = poly.simplify(facades.PROCEDURAL_SIMPLIFY_M, preserve_topology=True)
                if simple.geom_type == "Polygon" and simple.is_valid and len(simple.exterior.coords) >= 4:
                    gen_ring = np.asarray(simple.exterior.coords)[:-1]
            man["buildings"].append({
                "id": i["id"], "rings_ue": [[[round(x * 100, 1), round(-y * 100, 1)] for x, y in gen_ring]],
                "raw_ring_ue": [[round(x * 100, 1), round(-y * 100, 1)] for x, y in ring],
                "base_z_cm": round(float(i["ground"]) * 100, 1),
                "eaves_z_cm": round((float(i["ground"]) + i["height"]) * 100, 1),
                "roof_z_cm": round(float(a.get("b3_h_dak_max") or (i["ground"] + i["height"])) * 100, 1),
                "height_m": round(i["height"], 2), "levels": a.get("b3_bouwlagen") or 0,
                "category": i["osm_category"] or facades.dominant_use(i["uses"]) or "yes",
                "year": i["year"], "uses": {k: round(v) for k, v in i["uses"].items()},
                "roof_type": a.get("b3_dak_type"), "style": i["style"], "shopfront": bool(i["shop"]),
                "roof_rgb": list(rgb) if rgb else None, "procedural": bool(i.get("procedural")),
                "family": facades.procedural_family(i["style"], i["height"])})
    man["stats"]["buildings"] = n_buildings
    man["stats"]["building_source"] = f"3DBAG {args.lod} via api.3dbag.nl ({n_parts} parts, {n_tris} triangles)"
    if looks:
        man["stats"]["facade_styles"] = dict(sorted(styles_n.items()))
        man["stats"]["party_walls"] = n_party
        man["stats"]["bag_units"] = len(units)
        man["stats"]["roof_photo"] = bool(ortho)
        man["stats"]["procedural_capable"] = sum(1 for i in infos if i.get("procedural"))
    man["stats"]["triangles"] = int(sum(a["triangles"] for a in assets))
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    n_assets = sum(1 for a in assets if a["kind"] in ("building", "building_3dbag", "roof"))
    print(f"3dbag: {n_buildings} buildings, {n_parts} parts, {n_tris} triangles in {len(tiles)} tiles, "
          f"{n_assets} assets, {len(textures)} textures; styles {dict(styles_n)}; {n_party} party walls")


if __name__ == "__main__":
    sys.exit(main())
