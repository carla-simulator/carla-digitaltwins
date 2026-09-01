"""Overpass download (disk cached) and OSM element parsing.

``fetch_overpass(bbox_swne, cache_dir)`` returns the raw Overpass JSON (``{"elements": [...]}``),
``parse_osm(json)`` turns it into an :class:`OsmData` with plain dict/list containers so the
lane graph builder never has to touch the raw element list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("twinmodel.ingest.osm")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_USER_AGENT = "twinmodel/0.1 (carla-digitaltwins spike)"


# --------------------------------------------------------------------------- containers

@dataclass
class OsmNode:
    id: int
    lat: float
    lon: float
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OsmWay:
    id: int
    nodes: list[int]
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OsmMember:
    type: str  # node | way | relation
    ref: int
    role: str = ""


@dataclass
class OsmRelation:
    id: int
    members: list[OsmMember]
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OsmData:
    nodes: dict[int, OsmNode] = field(default_factory=dict)
    ways: list[OsmWay] = field(default_factory=list)
    relations: list[OsmRelation] = field(default_factory=list)
    bbox_swne: Optional[tuple[float, float, float, float]] = None

    # -- convenience lookups
    def way(self, way_id: int) -> OsmWay:
        for w in self.ways:
            if w.id == way_id:
                return w
        raise KeyError(way_id)

    def ways_with(self, key: str) -> list[OsmWay]:
        return [w for w in self.ways if key in w.tags]

    def relations_with(self, key: str, value: Optional[str] = None) -> list[OsmRelation]:
        return [r for r in self.relations
                if key in r.tags and (value is None or r.tags[key] == value)]

    def way_coords(self, way: OsmWay) -> list[tuple[float, float]]:
        """(lon, lat) pairs for a way, skipping node refs missing from the download."""
        out = []
        for nid in way.nodes:
            n = self.nodes.get(nid)
            if n is not None:
                out.append((n.lon, n.lat))
        return out


# --------------------------------------------------------------------------- query

def overpass_query(bbox_swne: tuple[float, float, float, float]) -> str:
    """Overpass QL for everything the twin needs inside the bbox.

    Highway ways and their nodes, the point features we turn into signals/objects,
    buildings (ways + multipolygon relations), ``area:highway`` ways and turn-restriction
    relations. ``out body; >; out skel qt;`` recurses down to every referenced node.
    """
    s, w, n, e = bbox_swne
    bb = f"{s},{w},{n},{e}"
    return f"""[out:json][timeout:180];
(
  way["highway"]({bb});
  node["highway"~"^(crossing|traffic_signals|stop|give_way)$"]({bb});
  node["traffic_sign"]({bb});
  node["natural"="tree"]({bb});
  way["building"]({bb});
  relation["building"]({bb});
  way["area:highway"]({bb});
  relation["type"="restriction"]({bb});
);
out body;
>;
out skel qt;
"""


def _cache_path(cache_dir: Path, bbox_swne: tuple[float, float, float, float]) -> Path:
    key = "_".join(f"{v:.5f}" for v in bbox_swne)
    digest = hashlib.sha1(overpass_query(bbox_swne).encode()).hexdigest()[:8]
    return cache_dir / f"overpass_{key}_{digest}.json"


def fetch_overpass(bbox_swne: tuple[float, float, float, float],
                   cache_dir: Path | str = "data",
                   url: str = OVERPASS_URL,
                   force: bool = False,
                   retries: int = 3) -> dict[str, Any]:
    """Download (or load from cache) the raw Overpass JSON for ``bbox_swne`` = (S, W, N, E)."""
    bbox_swne = tuple(float(v) for v in bbox_swne)  # type: ignore[assignment]
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, bbox_swne)
    if path.exists() and not force:
        log.info("overpass cache hit %s", path)
        return json.loads(path.read_text())

    query = overpass_query(bbox_swne)
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            log.info("overpass fetch bbox=%s attempt %d", bbox_swne, attempt + 1)
            resp = requests.post(url, data={"data": query}, timeout=240,
                                 headers={"User-Agent": _USER_AGENT})
            if resp.status_code in (429, 504):
                raise RuntimeError(f"overpass busy: HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            if "elements" not in data:
                raise RuntimeError(f"unexpected overpass payload: {list(data)[:5]}")
            data.setdefault("twinmodel", {})["bbox_swne"] = list(bbox_swne)
            path.write_text(json.dumps(data))
            log.info("overpass: %d elements cached -> %s", len(data["elements"]), path)
            return data
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last_err = exc
            log.warning("overpass attempt %d failed: %s", attempt + 1, exc)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"overpass fetch failed after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------- parse

def parse_osm(data: dict[str, Any]) -> OsmData:
    """Raw Overpass JSON -> :class:`OsmData`.

    Elements can appear twice (once from ``out body`` with tags, once from ``out skel``);
    the tagged occurrence wins.
    """
    osm = OsmData()
    bb = (data.get("twinmodel") or {}).get("bbox_swne")
    if bb:
        osm.bbox_swne = tuple(bb)  # type: ignore[assignment]
    ways: dict[int, OsmWay] = {}
    rels: dict[int, OsmRelation] = {}
    for el in data.get("elements", []):
        t = el.get("type")
        tags = el.get("tags", {}) or {}
        if t == "node":
            prev = osm.nodes.get(el["id"])
            if prev is None or (tags and not prev.tags):
                osm.nodes[el["id"]] = OsmNode(el["id"], float(el["lat"]), float(el["lon"]), dict(tags))
        elif t == "way":
            prev = ways.get(el["id"])
            if prev is None or (tags and not prev.tags):
                ways[el["id"]] = OsmWay(el["id"], list(el.get("nodes", [])), dict(tags))
        elif t == "relation":
            prev = rels.get(el["id"])
            if prev is None or (tags and not prev.tags):
                members = [OsmMember(m["type"], int(m["ref"]), m.get("role", ""))
                           for m in el.get("members", [])]
                rels[el["id"]] = OsmRelation(el["id"], members, dict(tags))
    osm.ways = list(ways.values())
    osm.relations = list(rels.values())
    log.info("parsed osm: %d nodes, %d ways, %d relations",
             len(osm.nodes), len(osm.ways), len(osm.relations))
    return osm


def load_fixture(path: Path | str) -> OsmData:
    return parse_osm(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- region helpers

COUNTRY_QUERY = ('[out:json][timeout:60];is_in({lat:.6f},{lon:.6f})->.a;'
                 'area.a["admin_level"="2"]["ISO3166-1"];out tags;')


def _country_cache_path(cache_dir: Path, lat: float, lon: float) -> Path:
    return cache_dir / f"country_{lat:.4f}_{lon:.4f}.json"


def country_for_bbox(bbox_swne: tuple[float, float, float, float],
                     cache_dir: Path | str = "data",
                     url: str = OVERPASS_URL,
                     retries: int = 2,
                     retry_sleep: float = 3.0) -> Optional[str]:
    """ISO 3166-1 alpha-2 code of the country containing the bbox centre, via Overpass
    ``is_in`` (``area["admin_level"="2"]["ISO3166-1"]``). Cached as
    ``<cache_dir>/country_<lat>_<lon>.json``; returns ``None`` (never raises) when Overpass is
    unreachable or the point is in no admin_level=2 area (open sea)."""
    s, w, n, e = (float(v) for v in bbox_swne)
    lat, lon = (s + n) / 2.0, (w + e) / 2.0
    cache_dir = Path(cache_dir)
    path = _country_cache_path(cache_dir, lat, lon)
    if path.exists():
        try:
            d = json.loads(path.read_text())
            log.info("country cache hit %s -> %s", path, d.get("iso2"))
            return d.get("iso2")
        except Exception as exc:  # noqa: BLE001
            log.warning("country cache %s unreadable (%s); re-querying", path, exc)

    query = COUNTRY_QUERY.format(lat=lat, lon=lon)
    for attempt in range(max(1, retries)):
        try:
            resp = requests.post(url, data={"data": query}, timeout=90,
                                 headers={"User-Agent": _USER_AGENT})
            if resp.status_code in (429, 504):
                raise RuntimeError(f"overpass busy: HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            iso2, name = None, None
            for el in data.get("elements", []):
                tags = el.get("tags", {}) or {}
                code = tags.get("ISO3166-1:alpha2") or tags.get("ISO3166-1")
                if code:
                    iso2, name = code.strip().upper()[:2], tags.get("name:en") or tags.get("name")
                    break
            if iso2 is None:
                log.warning("country: no admin_level=2 area contains (%.5f, %.5f)", lat, lon)
                return None
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"iso2": iso2, "name": name, "lat": lat, "lon": lon}))
            except Exception as exc:  # noqa: BLE001
                log.warning("country: could not write cache %s (%s)", path, exc)
            log.info("country for (%.5f, %.5f): %s (%s)", lat, lon, iso2, name)
            return iso2
        except Exception as exc:  # noqa: BLE001
            log.warning("country lookup attempt %d failed: %s", attempt + 1, exc)
            if attempt + 1 < retries and retry_sleep > 0:
                time.sleep(retry_sleep * (attempt + 1))
    log.warning("country: Overpass unreachable; region unknown")
    return None


def building_footprints(osm: OsmData, frame) -> list:
    """Shapely polygons (model space) of every ``building=*`` way and multipolygon relation
    outer ring in ``osm``; invalid rings are repaired with ``buffer(0)``; failures skipped."""
    from shapely.geometry import Polygon
    from shapely.validation import make_valid

    ways_by_id = {w.id: w for w in osm.ways}
    polys = []

    def add(way: OsmWay) -> None:
        coords = osm.way_coords(way)
        if len(coords) < 4 or coords[0] != coords[-1]:
            if len(coords) >= 3 and coords[0] != coords[-1]:
                coords = coords + [coords[0]]
            else:
                return
        lon, lat = zip(*coords)
        x, y = frame.to_local(lon, lat)
        try:
            p = Polygon(zip(x.tolist(), y.tolist()))
            if not p.is_valid:
                p = make_valid(p)
            if not p.is_empty:
                polys.append(p)
        except Exception:  # noqa: BLE001 - degenerate ring
            return

    for w in osm.ways:
        if "building" in w.tags and w.tags["building"] != "no":
            add(w)
    for r in osm.relations:
        if "building" not in r.tags or r.tags["building"] == "no":
            continue
        for m in r.members:
            if m.type == "way" and m.role in ("outer", ""):
                w = ways_by_id.get(m.ref)
                if w is not None and "building" not in w.tags:
                    add(w)
    return polys


def bbox_polygon(frame, bbox_swne: tuple[float, float, float, float]):
    """The WGS84 bbox as a model-space shapely polygon (4 corners projected)."""
    from shapely.geometry import Polygon
    s, w, n, e = bbox_swne
    lon = [w, e, e, w]
    lat = [s, s, n, n]
    x, y = frame.to_local(lon, lat)
    return Polygon(zip(x.tolist(), y.tolist()))


def building_coverage(osm: OsmData, frame, bbox_swne: tuple[float, float, float, float]) -> float:
    """Building footprint area / bbox area in model space (0..1), a quick shapely union of every
    building way / multipolygon outer ring clipped to the bbox. Used to pick urban vs suburban
    profiles for US bboxes (``profiles.choose_for_country``)."""
    from shapely.ops import unary_union
    clip = bbox_polygon(frame, bbox_swne)
    if clip.area <= 0:
        return 0.0
    polys = building_footprints(osm, frame)
    if not polys:
        return 0.0
    union = unary_union(polys)
    if not union.is_valid:
        union = union.buffer(0)
    cov = union.intersection(clip).area / clip.area
    log.info("building coverage: %d footprints, %.1f%% of the bbox", len(polys), 100 * cov)
    return float(min(max(cov, 0.0), 1.0))
