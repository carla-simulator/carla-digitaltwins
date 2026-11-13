"""Replace a twin's extruded OSM buildings with 3DBAG LoD 2.2 buildings (the Netherlands).

    python tools/threedbag_buildings.py build/<name>             # after `twinmodel bake-export`

3DBAG (3dbag.nl, CC BY 4.0) models every building in the Netherlands from the BAG footprints and AHN lidar,
with roof shapes (LoD 2.2). This tool reads the bake export's manifest, fetches the 3DBAG buildings in its bbox
from the 3DBAG OGC API (CityJSON), triangulates their roof and wall surfaces, converts them from RD New + NAP
into the twin's local frame (NAP heights, as the AHN elevation the twin was built on), writes one glb per tile
in the export's format, and swaps them in for the `building` assets of the manifest. The OSM building meshes
are kept in meshes/osm_buildings/.
"""
import argparse
import json
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import mapbox_earcut as earcut
import numpy as np
import requests
from pyproj import Transformer

from twinmodel.export.ue import _tile_index, write_glb

API = "https://api.3dbag.nl/collections/pand/items"
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build_dir")
    ap.add_argument("--lod", default="2.2")
    args = ap.parse_args()
    out = Path(args.build_dir) / "ue"
    man = json.loads((out / "manifest.json").read_text())
    name, tile_m = man["name"], float(man["tile_m"])
    proj = " ".join(t for t in man["geo_reference"].split() if not t.startswith(("+geoidgrids", "+vunits")))
    to_model = Transformer.from_crs("EPSG:28992", proj, always_xy=True)
    to_rd = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
    s, w, n, e = man["bbox_wgs84"]
    xs, ys = zip(*(to_rd.transform(lon, lat) for lat, lon in ((s, w), (s, e), (n, w), (n, e))))
    feats = fetch((min(xs), min(ys), max(xs), max(ys)))

    tiles = defaultdict(lambda: {"pos": [], "nrm": [], "uv": [], "faces": [], "n": 0})
    n_buildings = n_parts = n_tris = 0
    for f in feats:
        verts_rd = np.asarray(f["vertices"])
        mx, my = to_model.transform(verts_rd[:, 0], verts_rd[:, 1])
        verts = np.column_stack([mx, my, verts_rd[:, 2]])
        parts = [o for o in f["CityObjects"].values() if o.get("type") == "BuildingPart"]
        used = False
        for part in parts:
            geoms = {g.get("lod"): g for g in part.get("geometry", [])}
            geom = geoms.get(args.lod) or geoms.get("2.2") or geoms.get("1.2")
            if geom is None:
                continue
            cx, cy = float(np.mean(verts[:, 0])), float(np.mean(verts[:, 1]))
            t = tiles[_tile_index(cx, cy, tile_m)]
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
                base = t["n"]
                t["pos"].append(v)
                t["nrm"].append(np.repeat(normal[None, :], len(v), axis=0))
                # metric planar UV in the surface's plane: horizontal for roofs, (along, up) for walls
                if abs(normal[2]) > 0.7:
                    t["uv"].append(v[:, :2])
                else:
                    along = np.array([-normal[1], normal[0], 0.0]) / max(1e-9, math.hypot(normal[0], normal[1]))
                    t["uv"].append(np.column_stack([v @ along, v[:, 2]]))
                t["faces"].append(tri + base)
                t["n"] += len(v)
                n_tris += len(tri)
            n_parts += 1
            used = True
        n_buildings += int(used)

    mesh_dir = out / "meshes"
    old = [a for a in man["assets"] if a["kind"] == "building"]
    template = old[0] if old else {"material": "building", "semantic": "Building", "layer": 0}
    backup = mesh_dir / "osm_buildings"
    backup.mkdir(exist_ok=True)
    for a in old:
        src = out / a["file"]
        if src.exists():
            shutil.move(str(src), str(backup / src.name))
    assets = [a for a in man["assets"] if a["kind"] != "building"]
    for (tx, ty), t in sorted(tiles.items()):
        if not t["faces"]:
            continue
        pos = np.vstack(t["pos"]); nrm = np.vstack(t["nrm"]); uv = np.vstack(t["uv"]); faces = np.vstack(t["faces"])
        asset = f"{name}_L0_building_{tx}_{ty}".replace("-", "m")
        bbox = write_glb(mesh_dir / f"{asset}.glb", asset, pos, nrm, uv, faces, template.get("material", "building"))
        assets.append({"file": f"meshes/{asset}.glb", "asset": asset, "layer": template.get("layer", 0),
                       "kind": "building", "material": template.get("material", "building"),
                       "semantic": template.get("semantic", "Building"), "tile": [tx, ty],
                       "vertices": int(len(pos)), "triangles": int(len(faces)), "bbox_ue": bbox,
                       "source": "3DBAG LoD %s (CC BY 4.0)" % args.lod})
    man["assets"] = assets
    man["stats"]["buildings_osm_replaced"] = man["stats"].get("buildings", 0)
    man["stats"]["buildings"] = n_buildings
    man["stats"]["building_source"] = f"3DBAG {args.lod} via api.3dbag.nl ({n_parts} parts, {n_tris} triangles)"
    man["stats"]["triangles"] = int(sum(a["triangles"] for a in assets))
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    print(f"3dbag: {n_buildings} buildings, {n_parts} parts, {n_tris} triangles in {len(tiles)} tiles; "
          f"{len(old)} OSM building meshes moved to {backup}")


if __name__ == "__main__":
    sys.exit(main())
