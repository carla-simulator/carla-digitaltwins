"""New York City buildings for twinmodel.export.realbuildings: the NYC 3D Building Model with PLUTO uses.

* Geometry: the NYC 3-D Building Model (NYC Office of Technology and Innovation, NYC Open Data terms of
  use: free, no key), CityGML LoD 2 (roof shapes from 2014 photogrammetry) in EPSG:2263 (feet, NAVD88
  heights in feet), one ~600 MB file per delivery area in DA_WISE_GML.zip. Only the delivery areas whose
  envelope meets the twin's bbox are downloaded (range requests, twinmodel.ingest.citygml; cached in
  ~/.cache/twinmodel/citygml/), and only the buildings in the bbox are kept (cached in <build>/nyc3d/).
  Heights become metres NAVD88, as the USGS 3DEP elevation the us profiles build the twin on.
* Looks: each building's BIN finds its tax lot in the NYC Building Footprints (OTI, NYC Open Data:
  BIN -> BBL, construction year), and the lot's floor areas by use, building class, year built and number
  of floors come from PLUTO (NYC Department of City Planning, NYC Open Data). Only those fields are
  requested (no owner names). The floor areas map to facades.classify's neutral uses (residential, office,
  retail, parking, storage, industry); a lot without areas gets its use from the building class letter.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from ..export.realbuildings import RealBuilding, Source, cache_json, get_json, model_proj, roof_heights
from . import citygml

DA_ZIP = "https://s-media.nyc.gov/agencies/oti/DA_WISE_GML.zip"
FOOTPRINTS = "https://data.cityofnewyork.us/resource/5zhs-2jue.json"
PLUTO = "https://data.cityofnewyork.us/resource/64uk-42ks.json"
COVERAGE = (40.49, -74.27, 40.92, -73.68)   # (s, w, n, e) of New York City
SQFT = 0.09290304
ROOF_PHOTO_MAX_M = 25.0   # the NYS orthos are not true orthos: taller buildings lean out of their footprint

# PLUTO floor area fields -> facades.classify uses
AREAS = {"resarea": "residential", "officearea": "office", "retailarea": "retail", "garagearea": "parking",
         "strgearea": "storage", "factryarea": "industry", "otherarea": "other"}
# building class (first letter, NYC DOF) -> use, for lots without floor areas and for their "other" area
CLASS_USE = {"A": "residential", "B": "residential", "C": "residential", "D": "residential",
             "R": "residential", "S": "residential", "L": "office", "O": "office", "K": "retail",
             "H": "hotel", "E": "storage", "F": "industry", "G": "parking", "I": "health", "N": "health",
             "W": "education", "J": "assembly", "M": "assembly", "P": "assembly", "Q": "assembly",
             "Y": "government", "U": "utility", "T": "utility"}


def _within(bbox, pad=0.0):
    s, w, n, e = bbox
    return s - pad, w - pad, n + pad, e + pad


def fetch_footprints(bbox):
    """{BIN: (BBL, construction year)} of the NYC Building Footprints in the bbox."""
    s, w, n, e = _within(bbox, 0.002)
    rows = get_json(FOOTPRINTS, {"$select": "bin,mappluto_bbl,base_bbl,construction_year",
                                 "$where": f"within_box(the_geom, {n}, {w}, {s}, {e})", "$limit": 50000})
    out = {}
    for r in rows:
        if r.get("bin"):
            bbl = r.get("mappluto_bbl") or r.get("base_bbl")
            out[str(r["bin"])] = [str(int(float(bbl))) if bbl else None,
                                  int(float(r["construction_year"])) if r.get("construction_year") else None]
    return out


def fetch_pluto(bbox):
    """{BBL: {bldgclass, yearbuilt, numfloors, *area}} of the PLUTO lots in the bbox."""
    s, w, n, e = _within(bbox, 0.003)
    fields = ["bbl", "bldgclass", "yearbuilt", "numfloors", *AREAS]
    rows = get_json(PLUTO, {"$select": ",".join(fields), "$limit": 50000,
                            "$where": f"latitude between {s} and {n} and longitude between {w} and {e}"})
    out = {}
    for r in rows:
        if r.get("bbl"):
            out[str(int(float(r["bbl"])))] = {k: r.get(k) for k in fields if k != "bbl"}
    return out


def uses_of(lot: dict) -> dict:
    """Floor area (m2) per facades use of a PLUTO lot."""
    cls_use = CLASS_USE.get((lot.get("bldgclass") or " ")[0])
    out = {}
    for f, use in AREAS.items():
        a = float(lot.get(f) or 0) * SQFT
        if a > 0:
            if use == "other":
                use = cls_use or "other"
            out[use] = out.get(use, 0.0) + a
    if not out and cls_use:
        out[cls_use] = 1.0
    return out


def read_citygml(bbox, proj, source=DA_ZIP, pad_m=30.0):
    """[{id, attrs, surfaces: [[kind, [ring as flat list]]]}] of the buildings in the bbox (file coords)."""
    from pyproj import Transformer
    to_file = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True)
    s, w, n, e = bbox
    xs, ys = to_file.transform([w, e, w, e], [s, s, n, n])
    pad = pad_m / citygml.US_FT
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad

    def want(name, srs, env):
        return env is not None and not (env[2] < x0 or env[0] > x1 or env[3] < y0 or env[1] > y1)

    def inside(p):
        return (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)

    files = citygml.members(source, want)
    print("nyc3d: delivery areas %s" % [re.sub(r".*_(DA\d+)_.*", r"\1", f.name) for f in files], flush=True)
    out = []
    for f in files:
        for b in citygml.buildings(f, inside):
            out.append({"id": b["id"], "attrs": b["attrs"],
                        "surfaces": [[k, [np.round(r, 3).ravel().tolist() for r in rings]]
                                     for k, rings in b["surfaces"]]})
        print("nyc3d: %s: %d buildings in the bbox so far" % (f.name, len(out)), flush=True)
    return out


def load(build: Path, man: dict, looks: bool = True, source: str = DA_ZIP, **_):
    """(RealBuilding list, Source) for the bake export's bbox."""
    from pyproj import Transformer
    from shapely.geometry import MultiPoint, Polygon
    from shapely.ops import unary_union

    proj = model_proj(man)
    s, w, n, e = bbox = tuple(man["bbox_wgs84"])
    to_model = Transformer.from_crs("EPSG:2263", proj, always_xy=True)
    cache = build / "nyc3d"
    tag = "%.5f_%.5f_%.5f_%.5f" % bbox
    raw = cache_json(cache / f"citygml_{tag}.json.gz", lambda: read_citygml(bbox, proj, source))
    print(f"nyc3d: {len(raw)} buildings")
    feet, lots = {}, {}
    if looks:
        try:
            feet = cache_json(cache / f"footprints_{tag}.json.gz", lambda: fetch_footprints(bbox))
            lots = cache_json(cache / f"pluto_{tag}.json.gz", lambda: fetch_pluto(bbox))
        except Exception as exc:  # noqa: BLE001 - styles from the height and OSM only
            print(f"nyc3d: footprints/PLUTO unavailable ({exc}); styles from height and OSM only")
    out, seen, n_lot = [], set(), 0
    for b in raw:
        surfaces = []
        for kind, rings in b["surfaces"]:
            rr = []
            for flat in rings:
                p = np.asarray(flat, dtype=np.float64).reshape(-1, 3)
                mx, my = to_model.transform(p[:, 0], p[:, 1])
                rr.append(np.column_stack([mx, my, p[:, 2] * citygml.US_FT]))
            surfaces.append((rr, kind))
        if not surfaces:
            continue
        allz = np.concatenate([r[:, 2] for rings, _ in surfaces for r in rings])
        grounds = [rings[0] for rings, kind in surfaces if kind == "GroundSurface"]
        ground = float(min(r[:, 2].min() for r in grounds)) if grounds else float(allz.min())
        poly = None
        if grounds:
            pp = [Polygon(r[:, :2]) for r in grounds if len(r) >= 3]
            pp = [p if p.is_valid else p.buffer(0) for p in pp]
            u = unary_union([p for p in pp if not p.is_empty])
            poly = max(u.geoms, key=lambda g: g.area) if hasattr(u, "geoms") else u
        if poly is None or poly.is_empty:
            low = np.vstack([r[r[:, 2] <= ground + 0.5][:, :2] for rings, _ in surfaces for r in rings])
            hull = MultiPoint([tuple(p) for p in low]).convex_hull if len(low) >= 3 else None
            poly = hull if hull is not None and hull.geom_type == "Polygon" else None
        height, top = roof_heights(surfaces, ground)
        if top is None:
            top = float(allz.max())
            height = max(0.0, top - ground)
        a = b["attrs"]
        bin_ = str(a.get("BIN") or "").strip()
        bid = f"NYC.BIN.{bin_}" if bin_ and not bin_.endswith("000000") else f"NYC.DOITT.{a.get('DOITT_ID') or b['id']}"
        if bid in seen:
            bid = f"NYC.DOITT.{a.get('DOITT_ID') or b['id']}"
        seen.add(bid)
        year, uses, levels = None, {}, 0
        fp = feet.get(bin_)
        if fp:
            bbl, year = fp
            lot = lots.get(bbl) if bbl else None
            if lot:
                n_lot += 1
                uses = uses_of(lot)
                yb = int(float(lot.get("yearbuilt") or 0))
                year = yb if yb > 0 else year
                levels = int(round(float(lot.get("numfloors") or 0)))
        out.append(RealBuilding(id=bid, surfaces=surfaces, poly=poly, ground=ground, height=height,
                                roof_z=top, year=year, uses=uses, levels=levels, region="us"))
    print(f"nyc3d: {n_lot} of {len(out)} buildings matched to a PLUTO lot")
    src = Source(name="nyc3d", label="NYC 3D Building Model LoD 2 (OTI)",
                 asset_source="NYC 3D Building Model, NYC OTI (NYC Open Data terms of use)",
                 roof_photo_max_m=ROOF_PHOTO_MAX_M, skirt=True,
                 extra_stats={"pluto_lots_matched": n_lot} if looks else {})
    return out, src
