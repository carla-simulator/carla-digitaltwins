"""Read buildings from open CityGML city models (the NYC 3D Building Model, PLATEAU, ...).

Big city models come as zips of CityGML files of hundreds of MB each. ``members`` reads such a zip with HTTP
range requests, so only the CityGML files a twin needs are downloaded (into ~/.cache/twinmodel/citygml/),
never the textures or the rest of the city: a file is chosen by the envelope in its header, or by a caller's
filter (PLATEAU's 1 km mesh codes). ``buildings`` streams a file (ElementTree iterparse, memory stays flat)
and yields each Building that has a vertex inside a bbox, with its thematic surfaces (RoofSurface,
WallSurface, GroundSurface, ...) of the highest LoD, else the faces of its LoD 2 / LoD 1 solid, and its
generic attributes.

Coordinates stay in the file's coordinate system; ``crs_of`` names it for pyproj (axis order and the height
unit included): geographic JGD2011 (PLATEAU, EPSG:6697, latitude first), EPSG:2263 (New York Long Island,
US survey feet, heights in feet too), or anything pyproj knows by its EPSG code.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

CACHE = Path(os.environ.get("TWINMODEL_CITYGML_CACHE", Path.home() / ".cache" / "twinmodel" / "citygml"))
UA = "twinmodel (carla-digitaltwins) citygml"
US_FT = 1200.0 / 3937.0


class HttpFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests, so zipfile reads only what it needs."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        self.size = int(urllib.request.urlopen(req, timeout=60).headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        req = urllib.request.Request(self.url, headers={"Range": "bytes=%d-%d" % (self.pos, self.pos + n - 1),
                                                        "User-Agent": UA})
        d = urllib.request.urlopen(req, timeout=600).read()
        b[:len(d)] = d
        self.pos += len(d)
        return len(d)


def _zip(source: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(HttpFile(source) if re.match(r"https?://", source) else source)


def _key(source: str) -> str:
    return re.sub(r"\W", "_", source.rstrip("/").split("/")[-1])


def envelope(head: bytes):
    """(srsName, (xmin, ymin, xmax, ymax)) of a CityGML file from the gml:Envelope in its first bytes."""
    srs = re.search(rb'srsName="([^"]+)"', head)
    lo = re.search(rb"<gml:lowerCorner>([^<]+)</gml:lowerCorner>", head)
    hi = re.search(rb"<gml:upperCorner>([^<]+)</gml:upperCorner>", head)
    if not (lo and hi):
        return (srs.group(1).decode() if srs else None), None
    a, b = [float(v) for v in lo.group(1).split()[:2]], [float(v) for v in hi.group(1).split()[:2]]
    return (srs.group(1).decode() if srs else None), (a[0], a[1], b[0], b[1])


def members(source: str, want: Optional[Callable[[str, Optional[str], Optional[tuple]], bool]] = None,
            cache_root: Path = CACHE) -> list[Path]:
    """Local paths of the CityGML files of a zip (URL or path) that ``want(name, srs, envelope)`` accepts
    (default: all), downloaded once into the cache. The member list and each file's header envelope are
    cached too (_index.json), so a rerun needs no network."""
    cache = cache_root / _key(source)
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "_index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    zf = None
    if "names" not in index:
        zf = _zip(source)
        index["names"] = zf.namelist()
    gml = [n for n in index["names"] if n.lower().endswith(".gml")]
    env = index.setdefault("envelopes", {})
    if want is not None:
        for n in gml:
            if n in env:
                continue
            zf = zf or _zip(source)
            with zf.open(n) as f:
                srs, box = envelope(f.read(8192))
            env[n] = [srs, box]
    index_path.write_text(json.dumps(index))
    chosen = [n for n in gml if want is None or want(n, *(env.get(n) or (None, None)))]
    out = []
    for n in chosen:
        local = cache / re.sub(r"[^\w.-]", "_", n)
        if not local.exists():
            zf = zf or _zip(source)
            print("citygml: downloading %s from %s" % (n, source.split("/")[-1]), flush=True)
            with zf.open(n) as src, open(str(local) + ".part", "wb") as dst:
                while True:
                    chunk = src.read(1 << 22)
                    if not chunk:
                        break
                    dst.write(chunk)
            os.replace(str(local) + ".part", local)
        out.append(local)
    return out


def crs_of(srs: Optional[str]):
    """(pyproj CRS string, latitude-first axis order, height scale to metres) for a GML srsName."""
    s = srs or ""
    if re.search(r"\b(6697|6668|4326|4979|4269|4612|4937)\b", s):
        code = re.search(r"\b(6697|6668|4326|4979|4269|4612|4937)\b", s).group(1)
        horiz = {"6697": "EPSG:6668", "4979": "EPSG:4326", "4937": "EPSG:4326"}.get(code, "EPSG:" + code)
        return horiz, True, 1.0
    if re.search(r"\b2263\b", s):
        return "EPSG:2263", False, US_FT
    m = re.search(r"EPSG[^\d]*(\d{4,5})", s)
    if m:
        return "EPSG:" + m.group(1), False, 1.0
    raise ValueError("unknown CityGML coordinate system %r" % srs)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rings(elem) -> list[np.ndarray]:
    """Polygons under elem as [exterior, *interiors] rings, each an (n, 3) array (closing point dropped)."""
    out = []
    for poly in elem.iter():
        if _local(poly.tag) != "Polygon":
            continue
        rings = []
        for part in poly:
            kind = _local(part.tag)
            if kind not in ("exterior", "interior"):
                continue
            for lr in part.iter():
                if _local(lr.tag) != "LinearRing":
                    continue
                vals = []
                for c in lr:
                    if _local(c.tag) in ("posList", "pos") and c.text:
                        vals.append(np.array(c.text.split(), dtype=np.float64))
                if not vals:
                    continue
                pts = np.concatenate(vals)
                pts = pts[:len(pts) // 3 * 3].reshape(-1, 3)
                if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
                    pts = pts[:-1]
                if len(pts) < 3:
                    continue
                if kind == "exterior":
                    rings.insert(0, pts)
                elif rings:
                    rings.append(pts)
        if rings:
            out.append(rings)
    return out


def building_surfaces(bld) -> list[tuple[str, list[np.ndarray]]]:
    """(kind, rings) of a building: thematic surfaces of the highest LoD, else the faces of its LoD 2 /
    LoD 1 solid or multi-surface (kind None: classified by the normal later)."""
    by_lod = {}
    for bounded in bld.iter():
        if _local(bounded.tag) != "boundedBy":
            continue
        for surf in bounded:
            kind = _local(surf.tag)
            for geom in surf:
                m = re.match(r"lod(\d)", _local(geom.tag))
                if m:
                    by_lod.setdefault(int(m.group(1)), []).extend((kind, r) for r in _rings(geom))
    if by_lod:
        return by_lod[max(by_lod)]
    for lod in ("lod2Solid", "lod2MultiSurface", "lod1Solid", "lod1MultiSurface"):
        out = [(None, r) for g in bld.iter() if _local(g.tag) == lod for r in _rings(g)]
        if out:
            return out
    return []


def attributes(bld) -> dict:
    """Generic attributes (gen:stringAttribute/intAttribute/doubleAttribute and a few bldg:* fields) of a
    Building, its own only (not those repeated on its surfaces)."""
    out = {}
    for c in bld:
        tag = _local(c.tag)
        if tag.endswith("Attribute") and c.get("name"):
            v = next((x.text for x in c if _local(x.tag) == "value"), None)
            out[c.get("name")] = v
        elif tag in ("yearOfConstruction", "measuredHeight", "storeysAboveGround", "usage", "class", "name"):
            out[tag] = c.text
    return out


def buildings(path: Path, inside: Callable[[np.ndarray], np.ndarray]) -> Iterator[dict]:
    """Stream the Buildings of a CityGML file that have a vertex where ``inside`` (an (n, 3) array of file
    coordinates -> bool array) holds: {"id", "attrs", "surfaces": [(kind, rings)]}."""
    ctx = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(ctx)
    for ev, el in ctx:
        if ev != "end":
            continue
        tag = _local(el.tag)
        if tag == "cityObjectMember":
            root.clear()
            continue
        if tag != "Building":
            continue
        surfaces = building_surfaces(el)
        if surfaces:
            first = surfaces[0][1][0]
            hit = inside(first[:1]).any() or any(inside(r[0]).any() for _, r in surfaces)
            if hit:
                yield {"id": el.get("{http://www.opengis.net/gml}id"), "attrs": attributes(el),
                       "surfaces": surfaces}
        el.clear()
