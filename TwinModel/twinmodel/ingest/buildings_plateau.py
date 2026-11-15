"""Japanese buildings for twinmodel.export.realbuildings: Project PLATEAU (MLIT) CityGML.

PLATEAU (https://www.mlit.go.jp/plateau/, PLATEAU terms of use: free use with attribution, compatible with
CC BY 4.0) models the buildings of Japanese cities in CityGML 2.0: LoD 2 roofs and walls in the city centres,
LoD 1 blocks with their measured height elsewhere, in JGD2011 geographic coordinates (EPSG:6697: latitude,
longitude, orthometric height in metres, as GSI's elevation). One zip per city or ward holds the buildings in
1 km tiles (the Japanese 3rd mesh); only the tiles that meet the twin's bbox are downloaded (range requests,
twinmodel.ingest.citygml; cached in ~/.cache/twinmodel/citygml/), only the buildings in the bbox kept
(<build>/plateau/). The building's use code (bldg:usage) maps to facades.classify's neutral uses; PLATEAU
rarely gives a construction year.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from ..export.realbuildings import RealBuilding, Source, cache_json, model_proj, newell, roof_heights
from . import citygml

# (s, w, n, e) -> CityGML zip of a ward or city (add more from https://www.geospatial.jp/ckan/dataset/plateau)
DATASETS = [
    ((35.640, 139.660, 35.695, 139.725),   # Shibuya-ku 2023
     "https://assets.cms.plateau.reearth.io/assets/e5/29b4f5-f0bf-433d-8f98-deb594bfbebd/"
     "13113_shibuya-ku_pref_2023_citygml_2_op.zip"),
]
COVERAGE = (20.0, 122.0, 46.0, 154.0)   # Japan; covers() checks the datasets
# bldg:usage (Building_usage.xml) -> facades uses; mixed homes with shops get a share of retail
USAGE = {"401": {"office": 1.0}, "402": {"retail": 1.0}, "403": {"hotel": 1.0}, "404": {"retail": 1.0},
         "411": {"residential": 1.0}, "412": {"residential": 1.0}, "413": {"residential": 0.7, "retail": 0.3},
         "414": {"residential": 0.8, "retail": 0.2}, "415": {"residential": 0.7, "industry": 0.3},
         "421": {"government": 1.0}, "422": {"education": 1.0}, "431": {"storage": 1.0}, "441": {"industry": 1.0},
         "452": {"utility": 1.0}}


def dataset_for(bbox):
    s, w, n, e = bbox
    return next((url for (cs, cw, cn, ce), url in DATASETS if cs <= s and n <= cn and cw <= w and e <= ce), None)


def covers(bbox) -> bool:
    return dataset_for(bbox) is not None


def mesh3(lat: float, lon: float) -> str:
    """Japanese standard grid code of the 3rd mesh (30" x 45", about 1 km) holding the point."""
    a = lat * 1.5
    p = int(a)
    b = lon - 100
    u = int(b)
    q = int((a - p) * 8)
    v = int((b - u) * 8)
    r = int(((a - p) * 8 - q) * 10)
    w = int(((b - u) * 8 - v) * 10)
    return "%d%d%d%d%d%d" % (p, u, q, v, r, w)


def meshes(bbox) -> set:
    """Every 3rd mesh code meeting the bbox."""
    s, w, n, e = bbox
    lat_step, lon_step = 30.0 / 3600.0, 45.0 / 3600.0
    out = set()
    for i in range(int(math.ceil((n - s) / lat_step)) + 2):
        for j in range(int(math.ceil((e - w) / lon_step)) + 2):
            out.add(mesh3(min(n, s + i * lat_step), min(e, w + j * lon_step)))
    return out


def read_citygml(bbox, source, pad_deg=0.0003):
    s, w, n, e = bbox
    want_meshes = meshes(bbox)

    def want(name, srs, env):
        m = re.search(r"/bldg/(\d{8})_bldg_", name)
        return bool(m and m.group(1) in want_meshes)

    def inside(p):   # (lat, lon, h)
        return (p[:, 0] >= s - pad_deg) & (p[:, 0] <= n + pad_deg) & (p[:, 1] >= w - pad_deg) & (p[:, 1] <= e + pad_deg)

    out = []
    for f in citygml.members(source, want):
        for b in citygml.buildings(f, inside):
            out.append({"id": b["id"], "attrs": b["attrs"],
                        "surfaces": [[k, [np.round(r, 8).ravel().tolist() for r in rings]]
                                     for k, rings in b["surfaces"]]})
        print("plateau: %s: %d buildings in the bbox so far" % (f.name, len(out)), flush=True)
    return out


def load(build: Path, man: dict, looks: bool = True, source: str = None, **_):
    """(RealBuilding list, Source) for the bake export's bbox."""
    from pyproj import Transformer
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    bbox = tuple(man["bbox_wgs84"])
    source = source or dataset_for(bbox)
    if source is None:
        raise ValueError("no PLATEAU dataset listed for %s (buildings_plateau.DATASETS)" % (bbox,))
    to_model = Transformer.from_crs("EPSG:6668", model_proj(man), always_xy=True)
    raw = cache_json(build / "plateau" / ("citygml_%.5f_%.5f_%.5f_%.5f.json.gz" % bbox),
                     lambda: read_citygml(bbox, source))
    print(f"plateau: {len(raw)} buildings")
    out = []
    for b in raw:
        surfaces = []
        for kind, rings in b["surfaces"]:
            rr = []
            for flat in rings:
                p = np.asarray(flat, dtype=np.float64).reshape(-1, 3)
                mx, my = to_model.transform(p[:, 1], p[:, 0])
                rr.append(np.column_stack([mx, my, p[:, 2]]))
            if kind is None:   # LoD 1 solid faces: by their normal
                nrm = newell(rr[0])
                if nrm is None:
                    continue
                kind = "RoofSurface" if nrm[2] > 0.7 else "GroundSurface" if nrm[2] < -0.7 else "WallSurface"
            surfaces.append((rr, kind))
        if not surfaces:
            continue
        allz = np.concatenate([r[:, 2] for rings, _ in surfaces for r in rings])
        ground = float(allz.min())
        bottoms = [rings[0] for rings, kind in surfaces if kind == "GroundSurface"]
        poly = None
        if bottoms:
            u = unary_union([p if p.is_valid else p.buffer(0) for p in (Polygon(r[:, :2]) for r in bottoms)])
            poly = max(u.geoms, key=lambda g: g.area) if hasattr(u, "geoms") else u
        if poly is None or poly.is_empty:
            from shapely.geometry import MultiPoint
            low = np.vstack([r[r[:, 2] <= ground + 0.5][:, :2] for rings, _ in surfaces for r in rings])
            hull = MultiPoint([tuple(p) for p in low]).convex_hull if len(low) >= 3 else None
            poly = hull if hull is not None and hull.geom_type == "Polygon" else None
        height, top = roof_heights(surfaces, ground)
        a = b["attrs"]
        try:
            mh = float(a.get("measuredHeight") or 0)
        except ValueError:
            mh = 0.0
        if top is None:
            height, top = max(mh, float(allz.max()) - ground), float(allz.max())
        try:
            year = int(a.get("yearOfConstruction") or 0) or None
        except ValueError:
            year = None
        try:
            storeys = int(a.get("storeysAboveGround") or 0)
        except ValueError:
            storeys = 0
        out.append(RealBuilding(id="JP." + str(b["id"]), surfaces=surfaces, poly=poly, ground=ground,
                                height=height, roof_z=top, year=year, uses=dict(USAGE.get(str(a.get("usage")), {})),
                                levels=storeys if 0 < storeys < 999 else 0, region="jp"))
    src = Source(name="plateau", label="PLATEAU CityGML (MLIT)",
                 asset_source="PLATEAU, MLIT Japan (PLATEAU terms of use, attribution)", roof_photo_max_m=25.0,
                 skirt=True)
    return out, src
