"""Texture a twin's ground with the aerial photo, one texture per tile (like the 3DBAG roofs).

    python tools/ground_photo.py build/<name>              # after bake-export (and threedbag_buildings.py)
    python tools/ground_photo.py build/<name> --undo       # back to CARLA's ground materials

The ground assets (kinds ``ground`` and ``groundplane``: open lots, courtyards, gardens, block
interiors and the apron; roads, sidewalks, kerbs and markings keep CARLA's materials) get top-down
UVs into a per-tile texture T_<Name>_ground_<x>_<y>_d.jpg cut from the aerial photo, and the
manifest a ``textures`` entry per tile (bake_level.py makes one MI_<Name>_ground_<x>_<y> on
twin_materials.TEXTURED_PARENT for each, as for the roofs).

Photo: in the Netherlands PDOK Luchtfoto Actueel_orthoHR (8 cm, CC BY 4.0, WMS, no key) fetched per
tile at --px (2048 px per 250 m tile = 12 cm); elsewhere, or when PDOK fails, the twin pipeline's
cached ortho (data/ortho_<bbox>_*.tif, usually 25 cm). Standing objects (AHN 0.5 m DSM minus DTM
above 2 m: trees, shelters, vans; the scenery places the trees in 3D) are replaced by the ground
around them, and cast shadows are brightened (twinmodel.export.groundphoto). Fetches are cached
in <build>/ground_photo/. Fails soft: a tile whose photo cannot be had keeps CARLA's ground.

The PDOK sources are the same ones coupled-sim's Tools/aerial_photo.py uses for its city maps
(github.com/bazilinskyy/coupled-sim, "Real aerial photos on city maps").
"""
import argparse
import io
import json
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twinmodel.export import facades, groundphoto  # noqa: E402
from twinmodel.export.ue import gltf_to_model, read_glb, write_glb  # noqa: E402

KINDS = ("ground", "groundplane")
# surfaces that lie over the ground with their own CARLA material: the photo under them is never
# seen, so fills do not draw on it
COVER_KINDS = ("drivable", "parking", "crossing", "sidewalk", "island", "median", "verge", "curb")
PDOK_WMS = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
PDOK_LAYER = "Actueel_orthoHR"
PDOK_RES = 0.08
PDOK_CREDIT = "PDOK Luchtfoto, Beeldmateriaal Nederland (CC BY 4.0)"
AHN_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
AHN_CREDIT = "AHN via PDOK (CC0)"
UA = {"User-Agent": "twinmodel-ground-photo/0.1"}
NL_BOX = (50.7, 3.2, 53.6, 7.3)   # s, w, n, e: where PDOK and AHN are tried


def in_nl(bbox_swne) -> bool:
    s, w, n, e = bbox_swne
    return NL_BOX[0] <= s and n <= NL_BOX[2] and NL_BOX[1] <= w and e <= NL_BOX[3]


def model_crs(man):
    from pyproj import CRS
    proj = " ".join(t for t in man["geo_reference"].split() if not t.startswith(("+geoidgrids", "+vunits")))
    return CRS.from_proj4(proj)


def tile_transform(tile, tile_m, px):
    from rasterio.transform import from_origin
    return from_origin(tile[0] * tile_m, (tile[1] + 1) * tile_m, tile_m / px, tile_m / px)


def _warp(data: bytes, dst_shape, dst_transform, dst_crs, bands=3, nodata=None):
    """A GeoTIFF/JPEG response warped onto the tile grid: (bands, H, W) float32 (NaN = no data
    for single-band rasters)."""
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import Resampling, reproject
    with MemoryFile(data) as mem, mem.open() as src:
        arr = src.read()[:bands].astype(np.float32)
        tf, crs = src.transform, src.crs or "EPSG:28992"
        nd = src.nodata if nodata is None else nodata
    if nd is not None:
        arr[arr == nd] = np.nan
    arr[np.abs(arr) > 1e4] = np.nan   # AHN marks no data as ~3.4e38
    out = np.full((arr.shape[0],) + tuple(dst_shape), np.nan, dtype=np.float32)
    reproject(arr, out, src_transform=tf, src_crs=crs, dst_transform=dst_transform, dst_crs=dst_crs,
              resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
    return out


def _get(session, url, params, tries=4, expect="image/"):
    for k in range(tries):
        try:
            r = session.get(url, params=params, timeout=120, headers=UA)
            r.raise_for_status()
            if not r.headers.get("content-type", "").startswith(expect):
                raise RuntimeError("%s returned %s: %s" % (url, r.headers.get("content-type"), r.text[:200]))
            return r.content
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2.0 * (k + 1))


def _rd_bounds(crs, tile, tile_m, margin):
    from rasterio.warp import transform_bounds
    x0, y0 = tile[0] * tile_m, tile[1] * tile_m
    b = transform_bounds(crs, "EPSG:28992", x0, y0, x0 + tile_m, y0 + tile_m, densify_pts=21)
    return (b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin)


def fetch_pdok_tile(session, crs, tile, tile_m, px, max_px=2000):
    """The tile's photo (px x px x 3 uint8, row 0 north) from the PDOK 8 cm WMS (RD New JPEG
    requests of at most max_px, warped onto the tile grid)."""
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds
    import rasterio
    res = max(PDOK_RES, tile_m / px)
    x0, y0, x1, y1 = _rd_bounds(crs, tile, tile_m, 2.0)
    nx = int(np.ceil((x1 - x0) / res / max_px))
    ny = int(np.ceil((y1 - y0) / res / max_px))
    dst_tf = tile_transform(tile, tile_m, px)
    out = np.full((3, px, px), np.nan, dtype=np.float32)
    for i in range(nx):
        for j in range(ny):
            bx0, bx1 = x0 + i * (x1 - x0) / nx, x0 + (i + 1) * (x1 - x0) / nx
            by0, by1 = y0 + j * (y1 - y0) / ny, y0 + (j + 1) * (y1 - y0) / ny
            w, h = int(round((bx1 - bx0) / res)), int(round((by1 - by0) / res))
            jpg = _get(session, PDOK_WMS, dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap",
                                               LAYERS=PDOK_LAYER, STYLES="", CRS="EPSG:28992",
                                               BBOX="%.3f,%.3f,%.3f,%.3f" % (bx0, by0, bx1, by1),
                                               WIDTH=w, HEIGHT=h, FORMAT="image/jpeg"))
            from PIL import Image
            arr = np.moveaxis(np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB")), -1, 0)
            with MemoryFile() as mem:
                with mem.open(driver="GTiff", width=w, height=h, count=3, dtype=arr.dtype, crs="EPSG:28992",
                              transform=from_bounds(bx0, by0, bx1, by1, w, h)) as dst:
                    dst.write(arr)
                part = _warp(mem.read(), (px, px), dst_tf, crs, nodata=-1)
            hit = np.isfinite(part).all(axis=0)
            out[:, hit] = part[:, hit]
    if not np.isfinite(out).all(axis=0).any():
        raise RuntimeError("no pixels")
    img = np.moveaxis(out, 0, -1)
    holes = ~np.isfinite(img).all(axis=-1)
    if holes.any():
        img[holes] = np.nanmean(img[~holes], axis=0)
    sizes_same = len(np.unique(img[::64, ::64].reshape(-1, 3), axis=0)) <= 2
    if sizes_same:   # a blank answer (service moved or wants a key), as aerial_photo.py checks
        raise RuntimeError("blank image")
    return np.clip(img, 0, 255).astype(np.uint8)


def fetch_ndsm_tile(session, crs, tile, tile_m, shape):
    """Height above the terrain (AHN 0.5 m DSM minus DTM, metres, NaN unknown) on the tile grid."""
    x0, y0, x1, y1 = _rd_bounds(crs, tile, tile_m, 2.0)
    dst_tf = tile_transform(tile, tile_m, shape[0])
    layers = {}
    for cov in ("dsm_05m", "dtm_05m"):
        tif = _get(session, AHN_WCS, [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                                      ("coverageId", cov), ("format", "image/tiff"),
                                      ("subset", "x(%.2f,%.2f)" % (x0, x1)), ("subset", "y(%.2f,%.2f)" % (y0, y1))])
        layers[cov] = _warp(tif, shape, dst_tf, crs, bands=1)[0]
    return layers["dsm_05m"] - layers["dtm_05m"]


def building_raster(man, tile, tile_m, px):
    """Building footprints (the manifest's buildings, model frame) rasterised on the tile grid."""
    from rasterio.features import rasterize
    from shapely.geometry import Polygon, box
    x0, y0 = tile[0] * tile_m, tile[1] * tile_m
    cell = box(x0 - 5, y0 - 5, x0 + tile_m + 5, y0 + tile_m + 5)
    shapes = []
    for b in man.get("buildings", []):
        ring = b.get("raw_ring_ue") or (b.get("rings_ue") or [None])[0]
        if not ring or len(ring) < 3:
            continue
        p = Polygon([(x / 100.0, -y / 100.0) for x, y in ring])
        if p.is_valid and p.intersects(cell):
            shapes.append((p, 1))
    if not shapes:
        return np.zeros((px, px), bool)
    return rasterize(shapes, out_shape=(px, px), transform=tile_transform(tile, tile_m, px), fill=0,
                     dtype="uint8").astype(bool)


def cover_raster(man, out_dir: Path, tile, tile_m, px):
    """Where the tile's roads, sidewalks, kerbs and verges lie (their up-facing triangles)."""
    from rasterio.features import rasterize
    x0, y0 = tile[0] * tile_m, tile[1] * tile_m
    shapes = []
    for a in man["assets"]:
        if a["kind"] not in COVER_KINDS:
            continue
        b = a.get("bbox_ue") or {}
        if b and (b["max"][0] / 100 < x0 - 1 or b["min"][0] / 100 > x0 + tile_m + 1 or
                  -b["min"][1] / 100 < y0 - 1 or -b["max"][1] / 100 > y0 + tile_m + 1):
            continue
        try:
            g = read_glb(out_dir / a["file"])
        except Exception:  # noqa: BLE001
            continue
        p = gltf_to_model(g["positions"])
        f = g["faces"].astype(np.int64)
        tri = p[f]                                           # (m, 3, 3)
        nz = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, 2]
        for t in tri[np.abs(nz) > 1e-6, :, :2]:
            shapes.append(({"type": "Polygon", "coordinates": [[tuple(t[0]), tuple(t[1]), tuple(t[2]),
                                                                tuple(t[0])]]}, 1))
    if not shapes:
        return np.zeros((px, px), bool)
    return rasterize(shapes, out_shape=(px, px), transform=tile_transform(tile, tile_m, px), fill=0,
                     dtype="uint8").astype(bool)


def _cached_array(path: Path, fn):
    if path.exists():
        return np.load(path)["a"]
    a = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, a=a)
    return a


def _save_jpg(path: Path, img, quality=90):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path, quality=quality, optimize=True)


def _load_jpg(path: Path):
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"))


def ground_tiles(man, kinds=KINDS):
    """tile -> [asset entries] of the ground kinds."""
    out = defaultdict(list)
    for a in man["assets"]:
        if a["kind"] in kinds and a.get("tile") is not None:
            out[tuple(int(t) for t in a["tile"])].append(a)
    return dict(out)


def set_uvs(out_dir: Path, asset, uv_fn):
    """Rewrite one ground glb's UVs (positions, normals, faces kept) with uv_fn(model xyz)."""
    path = out_dir / asset["file"]
    g = read_glb(path)
    pos = gltf_to_model(g["positions"])
    nrm = gltf_to_model(g["normals"])
    write_glb(path, asset["asset"], pos, nrm, uv_fn(pos), g["faces"].astype(np.int64), g["material"])


def apply(man, out_dir: Path, textured: dict, tile_m: float, kinds=KINDS):
    """Point the ground assets of every tile in ``textured`` (tile -> texture entry) at their
    texture and give their glbs tile UVs; the other ground assets get metric UVs back."""
    name = man["name"]
    texs = {k: v for k, v in man.get("textures", {}).items() if not k.startswith("ground_")}
    n_tex = 0
    for tile, assets in sorted(ground_tiles(man, kinds).items()):
        entry = textured.get(tile)
        for a in assets:
            if entry is not None:
                key = "ground_%d_%d" % tile
                set_uvs(out_dir, a, lambda p, t=tile: groundphoto.tile_uv(p[:, :2], t, tile_m))
                a["texture"] = key
                texs[key] = entry
                n_tex += 1
            elif a.pop("texture", None) is not None:
                set_uvs(out_dir, a, lambda p: np.column_stack([p[:, 0], -p[:, 1]]))
    if texs:
        man["textures"] = dict(sorted(texs.items()))
    else:
        man.pop("textures", None)
    man.setdefault("stats", {})["ground_photo_assets"] = n_tex
    return n_tex


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build_dir")
    ap.add_argument("--px", type=int, default=2048, help="texture pixels per tile side")
    ap.add_argument("--source", choices=("auto", "pdok", "ortho"), default="auto",
                    help="auto: PDOK 8 cm in the Netherlands, else the twin's cached ortho")
    ap.add_argument("--ortho", default=None, help="ortho GeoTIFF in the model frame (default: the twin "
                    "pipeline's cached data/ortho_<bbox>_*.tif)")
    ap.add_argument("--no-objects", action="store_true", help="leave trees and other standing objects in the photo")
    ap.add_argument("--no-shadows", action="store_true", help="leave the photo's shadows as they are")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality of the textures")
    ap.add_argument("--undo", action="store_true", help="remove the ground textures (metric UVs back)")
    ap.add_argument("--only", default=None, help="only these tiles, e.g. 0_0,-1_2 (probing; the manifest "
                    "is not written)")
    ap.add_argument("--preview", action="store_true", help="also write ground_photo/preview.jpg (all tiles, small)")
    args = ap.parse_args(argv)

    build = Path(args.build_dir)
    out = build / "ue"
    man = json.loads((out / "manifest.json").read_text())
    name, tile_m = man["name"], float(man["tile_m"])
    tiles = ground_tiles(man)
    if args.undo:
        apply(man, out, {}, tile_m)
        man["stats"].pop("ground_photo", None)
        (out / "manifest.json").write_text(json.dumps(man, indent=1))
        print("ground photo removed from %d tiles" % len(tiles))
        return 0

    crs = model_crs(man)
    nl = in_nl(man["bbox_wgs84"])
    source = args.source if args.source != "auto" else ("pdok" if nl else "ortho")
    ortho = None

    def get_ortho():
        nonlocal ortho
        if ortho is None:
            from threedbag_buildings import find_ortho, load_ortho  # noqa: E402 (same folder)
            op = Path(args.ortho) if args.ortho else find_ortho(build, man["bbox_wgs84"])
            ortho = (load_ortho(op, man["geo_reference"]) if op and op.exists() else None) or False
            print("ortho: %s" % (op.name if ortho else "none"))
        return ortho or None

    import requests
    session = requests.Session()
    cache = build / "ground_photo"
    px, px_m = args.px, tile_m / args.px
    tex_dir = out / "textures"
    tex_dir.mkdir(exist_ok=True)
    facades.save_png(out / f"textures/T_{name}_flat_n.png", facades.flat_normal())
    facades.save_png(out / f"textures/T_{name}_ground_orm.png", facades.constant_orm(0.9))
    textured, per_tile, sources = {}, {}, defaultdict(int)
    ndsm_ok = nl and not args.no_objects
    t0 = time.time()
    todo = sorted(tiles)
    if args.only:
        want = {tuple(int(v) for v in t.split("_")) for t in args.only.split(",")}
        todo = [t for t in todo if t in want]
    for k, tile in enumerate(todo):
        tag = "%d_%d" % tile
        photo, src = None, None
        if source == "pdok":
            try:
                p = cache / f"pdok_{px}_{tag}.jpg"
                if not p.exists():
                    _save_jpg(p, fetch_pdok_tile(session, crs, tile, tile_m, px), quality=95)
                photo, src = _load_jpg(p), "pdok"
            except Exception as exc:  # noqa: BLE001
                print("tile %s: PDOK failed (%s); the cached ortho instead" % (tag, exc))
        if photo is None and get_ortho():
            photo = facades.ortho_tile(ortho[0], ortho[1], tile, tile_m, px)
            src = "ortho" if photo is not None else None
        if photo is None:
            print("tile %s: no photo; CARLA's ground stays" % tag)
            continue
        sources[src] += 1
        ndsm = None
        if ndsm_ok:
            try:
                ndsm = _cached_array(cache / f"ndsm_{tag}.npz",
                                     lambda: fetch_ndsm_tile(session, crs, tile, tile_m, (px // 4, px // 4)))
                ndsm = groundphoto.resample(ndsm, (px, px))
            except Exception as exc:  # noqa: BLE001
                print("tile %s: AHN failed (%s); standing objects stay in the photo" % (tag, exc))
        bld = building_raster(man, tile, tile_m, px)
        cov = cover_raster(man, out, tile, tile_m, px)
        img, st = groundphoto.process_tile(photo, px_m, ndsm, bld, cov, seed=zlib.crc32(tag.encode()),
                                           shadows=not args.no_shadows)
        rel = f"textures/T_{name}_ground_{tag}_d.jpg"
        _save_jpg(out / rel, img, quality=args.quality)
        textured[tile] = {"base_color": rel, "normal": f"textures/T_{name}_flat_n.png",
                          "orm": f"textures/T_{name}_ground_orm.png",
                          "source": PDOK_CREDIT if src == "pdok" else "twin ortho (%s)" % src}
        per_tile[tag] = {"source": src, "objects_filled": st.masked_frac, "shadow": st.shadow_frac,
                         "shadow_gain": list(st.shadow_gain)}
        print("tile %s (%d/%d): %s, objects %.1f %%, shadow %.1f %% x%s" % (
            tag, k + 1, len(tiles), src, 100 * st.masked_frac, 100 * st.shadow_frac, st.shadow_gain), flush=True)
    if args.only:
        print("probe only: %s" % json.dumps(per_tile))
        return 0
    n = apply(man, out, textured, tile_m)
    man["stats"]["ground_photo"] = {
        "tiles": len(textured), "px": px, "m_per_px": round(px_m, 4), "sources": dict(sources),
        "objects_filled_mean": round(float(np.mean([v["objects_filled"] for v in per_tile.values()] or [0])), 4),
        "shadow_mean": round(float(np.mean([v["shadow"] for v in per_tile.values()] or [0])), 4),
        "credit": [PDOK_CREDIT] + ([AHN_CREDIT] if ndsm_ok else [])}
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    (cache / "report.json").write_text(json.dumps({**man["stats"]["ground_photo"], "per_tile": per_tile}, indent=1))
    if args.preview and textured:
        from PIL import Image
        xs, ys = [t[0] for t in textured], [t[1] for t in textured]
        cpx = 256
        sheet = Image.new("RGB", ((max(xs) - min(xs) + 1) * cpx, (max(ys) - min(ys) + 1) * cpx), (0, 0, 0))
        for t, e in textured.items():
            im = Image.open(out / e["base_color"]).resize((cpx, cpx))
            sheet.paste(im, ((t[0] - min(xs)) * cpx, (max(ys) - t[1]) * cpx))
        sheet.save(cache / "preview.jpg", quality=85)
    mb = sum((out / e["base_color"]).stat().st_size for e in textured.values()) / 1e6
    print("ground photo: %d tiles, %d assets, %.0f MB of textures, %.0f s; sources %s" % (
        len(textured), n, mb, time.time() - t0, dict(sources)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
