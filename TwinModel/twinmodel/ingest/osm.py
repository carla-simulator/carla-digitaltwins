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
