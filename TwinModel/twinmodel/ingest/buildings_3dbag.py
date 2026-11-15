"""3DBAG buildings (the Netherlands) for twinmodel.export.realbuildings.

3DBAG (3dbag.nl, CC BY 4.0) models every building in the Netherlands from the BAG footprints and AHN lidar,
with roof shapes (LoD 2.2). The buildings in the twin's bbox come from the 3DBAG OGC API (CityJSON) in RD
New + NAP and are converted into the twin's local frame (NAP heights, as the AHN elevation the twin was
built on). The use of each building's units (BAG verblijfsobjecten from the PDOK BAG OGC API, CC0: only the
point, use and floor area, no addresses) feeds the facade styles. Fetches are cached in <build>/3dbag/.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np

from ..export.realbuildings import RealBuilding, Source, cache_json, get_json, model_proj

API = "https://api.3dbag.nl/collections/pand/items"
BAG_API = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections/verblijfsobject/items"
COVERAGE = (50.70, 3.30, 53.60, 7.30)   # (s, w, n, e) of the Netherlands
UA = "twinmodel-3dbag/0.1"


def _fetch_pages(bbox_rd, limit=50):
    """One bbox, following the API's next links (it returns at most 50 features a page)."""
    import requests
    url, params, features, matched = API, {"bbox": ",".join(f"{v:.2f}" for v in bbox_rd), "limit": limit}, [], None
    while url:
        r = requests.get(url, params=params, timeout=120, headers={"User-Agent": UA})
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
                    page = get_json(url, params, user_agent=UA)
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


def load(build: Path, man: dict, looks: bool = True, lod: str = "2.2", uses: bool = True, **_):
    """(RealBuilding list, Source) for the bake export's bbox."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    proj = model_proj(man)
    to_model = Transformer.from_crs("EPSG:28992", proj, always_xy=True)
    wgs_to_model = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    to_rd = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
    s, w, n, e = man["bbox_wgs84"]
    xs, ys = zip(*(to_rd.transform(lon, lat) for lat, lon in ((s, w), (s, e), (n, w), (n, e))))
    bbox_rd = (min(xs), min(ys), max(xs), max(ys))
    cache = build / "3dbag"
    feats = cache_json(cache / ("features_%.0f_%.0f_%.0f_%.0f.json.gz" % bbox_rd), lambda: fetch(bbox_rd))
    print(f"3dbag: {len(feats)} buildings")
    units = []
    if looks and uses:
        try:
            units = cache_json(cache / ("bag_units_%.5f_%.5f_%.5f_%.5f.json.gz" % (s, w, n, e)),
                               lambda: fetch_uses((s, w, n, e)))
        except Exception as exc:  # noqa: BLE001 - styles fall back to year + OSM category
            print(f"bag: uses unavailable ({exc}); styles from construction year and OSM only")

    out = []
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
        surfaces, parts = [], 0
        for part in (o for o in f["CityObjects"].values() if o.get("type") == "BuildingPart"):
            geoms = {g.get("lod"): g for g in part.get("geometry", [])}
            geom = geoms.get(lod) or geoms.get("2.2") or geoms.get("1.2")
            if geom is None:
                continue
            parts += 1
            for rings_idx, kind in surfaces_of(geom):
                rings = [verts[np.asarray(r, dtype=np.int64)] for r in rings_idx if len(r) >= 3]
                if rings:
                    surfaces.append((rings, kind))
        h = eaves_height(attrs) or 0.0
        ground = attrs.get("b3_h_maaiveld")
        roof = attrs.get("b3_h_dak_max") or None   # 0 = unknown
        out.append(RealBuilding(
            id=f["id"], surfaces=surfaces, poly=poly, ground=None if ground is None else float(ground),
            height=h, roof_z=None if roof is None else float(roof), year=attrs.get("oorspronkelijkbouwjaar"),
            levels=attrs.get("b3_bouwlagen") or 0, roof_type=attrs.get("b3_dak_type"), parts=max(1, parts),
            centre=(float(np.mean(verts[:, 0])), float(np.mean(verts[:, 1]))), region="nl"))

    # BAG units -> buildings (point in footprint), floor area per use
    if units:
        tree = STRtree([b.poly if b.poly is not None else Polygon() for b in out])
        ux, uy = wgs_to_model.transform([u[0] for u in units], [u[1] for u in units])
        pts = shapely.points(np.column_stack([ux, uy]))
        pi, bi = tree.query(pts, predicate="within")
        for p_, b_ in zip(pi, bi):
            u = units[p_]
            for use in u[2] or ["onbekend"]:
                d = out[b_].uses
                d[use] = d.get(use, 0) + max(1, float(u[3] or 0)) / max(1, len(u[2]))
    src = Source(name="3dbag", label=f"3DBAG {lod} via api.3dbag.nl",
                 asset_source="3DBAG LoD %s (CC BY 4.0)" % lod,
                 extra_stats={"bag_units": len(units)} if looks else {})
    return out, src
