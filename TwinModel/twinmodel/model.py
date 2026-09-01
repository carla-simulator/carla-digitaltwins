"""Twin Model — the shared contract between ingest, surfaces, exporters and validation.

All geometry is in *model space*: local ENU metres (x east, y north, z up), origin at the
bbox centre, see ``twinmodel.frame.LocalFrame``. Headings are radians CCW from +x.

Rules: add fields (with defaults), never rename. Keep this module dependency-light
(shapely + numpy only) so every worker can import it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, mapping, shape
from shapely.geometry.base import BaseGeometry

log = logging.getLogger("twinmodel.model")

SCHEMA_VERSION = "0.1"

LaneType = Literal["driving", "sidewalk", "shoulder", "parking", "biking", "median", "none"]
Direction = Literal["forward", "backward"]
SurfaceKind = Literal["drivable", "sidewalk", "island", "crossing", "median", "parking", "ground"]
SurfaceSource = Literal["osm_tags", "area_highway", "imagery"]
SignalKind = Literal["traffic_light", "stop", "yield", "speed_limit", "crosswalk"]
MarkingKind = Literal["solid", "broken"]
MarkingColor = Literal["white", "yellow"]
ContactPoint = Literal["start", "end"]
LinkElement = Literal["road", "junction"]


# --------------------------------------------------------------------------- lane graph

@dataclass
class Marking:
    """Lane marking along a lane's *outer* edge (or a free-standing line when road_id is None)."""
    kind: MarkingKind = "solid"
    color: MarkingColor = "white"
    width: float = 0.12
    geometry: Optional[LineString] = None  # model space; None when attached to a lane edge


@dataclass
class Lane:
    """One lane of a Road. ``id`` follows OpenDRIVE: >0 left of the reference line,
    <0 right of it (ordered outward), 0 is the centre lane and is implicit — never stored."""
    id: int
    type: LaneType = "driving"
    width: float = 3.25
    direction: Direction = "forward"  # forward == along the reference line (id < 0 in RHT)
    marking: Optional[Marking] = None  # marking on the outer edge of this lane
    speed_limit: Optional[float] = None  # m/s
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoadLink:
    element: LinkElement
    id: str
    contact: Optional[ContactPoint] = None  # only for element == "road"


@dataclass
class Road:
    id: str
    reference_line: LineString  # model space, 3D (z may be 0.0)
    lanes: list[Lane] = field(default_factory=list)
    name: str = ""
    highway: str = ""  # OSM highway=* class
    osm_way_ids: list[int] = field(default_factory=list)
    junction_id: Optional[str] = None  # set for connecting roads inside a junction
    predecessor: Optional[RoadLink] = None
    successor: Optional[RoadLink] = None
    center_marking: Optional[Marking] = None  # marking on the reference line (lane 0 edge)
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def length(self) -> float:
        return float(self.reference_line.length)

    def lanes_left(self) -> list[Lane]:
        return sorted((l for l in self.lanes if l.id > 0), key=lambda l: l.id)

    def lanes_right(self) -> list[Lane]:
        return sorted((l for l in self.lanes if l.id < 0), key=lambda l: -l.id)

    def width_left(self, types: tuple[str, ...] = ("driving", "parking", "biking", "shoulder")) -> float:
        return sum(l.width for l in self.lanes if l.id > 0 and l.type in types)

    def width_right(self, types: tuple[str, ...] = ("driving", "parking", "biking", "shoulder")) -> float:
        return sum(l.width for l in self.lanes if l.id < 0 and l.type in types)


@dataclass
class LaneLink:
    from_lane: int  # lane id on the incoming road
    to_lane: int    # lane id on the connecting road


@dataclass
class Connection:
    id: str
    incoming_road: str
    connecting_road: str
    contact_point: ContactPoint = "start"  # contact point on the connecting road
    lane_links: list[LaneLink] = field(default_factory=list)


@dataclass
class Junction:
    id: str
    polygon: Optional[Polygon] = None  # model space; filled by surfaces.py if None
    connections: list[Connection] = field(default_factory=list)
    osm_node_ids: list[int] = field(default_factory=list)
    name: str = ""
    osm_way_ids: list[int] = field(default_factory=list)  # OSM ways swallowed by the node cluster
    tags: dict[str, Any] = field(default_factory=dict)  # e.g. centre, hull_wkt, area_wkt (lanegraph)


@dataclass
class Signal:
    id: str
    kind: SignalKind
    road_id: str
    s: float
    t: float
    position: Point  # model space, absolute (convenience; must agree with road/s/t)
    heading: float = 0.0
    value: Optional[float] = None  # speed limit in m/s for kind == speed_limit
    controller_id: Optional[str] = None  # traffic lights: one controller per junction
    orientation: Literal["+", "-"] = "+"
    osm_node_id: Optional[int] = None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class Controller:
    id: str
    junction_id: str
    signal_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- surfaces / objects

@dataclass
class Surface:
    id: str
    kind: SurfaceKind
    geometry: Polygon | MultiPolygon  # model space
    z_offset: float = 0.0  # above road datum (sidewalk/island 0.15)
    source: SurfaceSource = "osm_tags"
    confidence: float = 1.0
    road_ids: list[str] = field(default_factory=list)
    junction_id: Optional[str] = None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class CurbLine:
    id: str
    geometry: LineString
    height: float = 0.15
    low_side_kind: SurfaceKind = "drivable"
    high_side_kind: SurfaceKind = "sidewalk"


@dataclass
class Building:
    id: str
    footprint: Polygon | MultiPolygon
    height: Optional[float] = None  # metres
    levels: Optional[int] = None
    osm_id: Optional[int] = None
    tags: dict[str, Any] = field(default_factory=dict)

    def effective_height(self, level_height: float = 3.2, default_levels: int = 5) -> float:
        if self.height is not None:
            return self.height
        return (self.levels if self.levels is not None else default_levels) * level_height


@dataclass
class PointObject:
    """Trees, poles, street furniture."""
    id: str
    kind: str  # "tree" | "pole" | "bench" | ...
    position: Point
    osm_id: Optional[int] = None
    tags: dict[str, Any] = field(default_factory=dict)


class Elevation:
    """Regular grid of z over model space with bilinear sampling. ``z[j, i]`` at
    ``x = x0 + i*dx``, ``y = y0 + j*dy`` (dy may be positive; rows increase with y)."""

    def __init__(self, z: np.ndarray, x0: float, y0: float, dx: float, dy: float, source: str = ""):
        self.z = np.asarray(z, dtype=np.float64)
        self.x0, self.y0, self.dx, self.dy = float(x0), float(y0), float(dx), float(dy)
        self.source = source

    def sample(self, x, y):
        """Bilinear sample; accepts scalars or arrays; clamps to the grid edge."""
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        fi = np.clip((x - self.x0) / self.dx, 0, self.z.shape[1] - 1)
        fj = np.clip((y - self.y0) / self.dy, 0, self.z.shape[0] - 1)
        i0 = np.floor(fi).astype(int)
        j0 = np.floor(fj).astype(int)
        i1 = np.minimum(i0 + 1, self.z.shape[1] - 1)
        j1 = np.minimum(j0 + 1, self.z.shape[0] - 1)
        ti, tj = fi - i0, fj - j0
        z = (self.z[j0, i0] * (1 - ti) * (1 - tj) + self.z[j0, i1] * ti * (1 - tj)
             + self.z[j1, i0] * (1 - ti) * tj + self.z[j1, i1] * ti * tj)
        return z if z.shape else float(z)

    def to_npz(self, path: Path) -> None:
        np.savez_compressed(path, z=self.z, x0=self.x0, y0=self.y0, dx=self.dx, dy=self.dy,
                            source=self.source)

    @classmethod
    def from_npz(cls, path: Path) -> "Elevation":
        d = np.load(path, allow_pickle=False)
        return cls(d["z"], float(d["x0"]), float(d["y0"]), float(d["dx"]), float(d["dy"]),
                   str(d["source"]) if "source" in d else "")


# --------------------------------------------------------------------------- the model

@dataclass
class TwinModel:
    name: str
    origin_lat: float
    origin_lon: float
    bbox_wgs84: tuple[float, float, float, float]  # S, W, N, E
    roads: list[Road] = field(default_factory=list)
    junctions: list[Junction] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    controllers: list[Controller] = field(default_factory=list)
    surfaces: list[Surface] = field(default_factory=list)
    curbs: list[CurbLine] = field(default_factory=list)
    markings: list[Marking] = field(default_factory=list)  # free-standing markings only
    buildings: list[Building] = field(default_factory=list)
    objects: list[PointObject] = field(default_factory=list)
    elevation: Optional[Elevation] = None
    metadata: dict[str, Any] = field(default_factory=dict)  # sources, timings, refine stats ...
    # cached RoadDatum (twinmodel.datum) + fingerprint of the reference lines it was built from;
    # rebuilt lazily by sample_z() when a reference line object changed, or on rebuild_datum()
    _datum: Any = field(default=None, init=False, repr=False, compare=False)
    _datum_key: Any = field(default=None, init=False, repr=False, compare=False)

    # -- lookups
    def road(self, road_id: str) -> Road:
        for r in self.roads:
            if r.id == road_id:
                return r
        raise KeyError(road_id)

    def junction(self, junction_id: str) -> Junction:
        for j in self.junctions:
            if j.id == junction_id:
                return j
        raise KeyError(junction_id)

    def surfaces_of(self, kind: SurfaceKind) -> list[Surface]:
        return [s for s in self.surfaces if s.kind == kind]

    def _datum_fingerprint(self) -> tuple:
        # LineStrings are immutable: a changed z means a new object (cli.apply_elevation)
        return (self.elevation is not None, tuple(id(r.reference_line) for r in self.roads))

    def rebuild_datum(self, **kw):
        """(Re)build the cached ``RoadDatum`` from the current reference lines. Returns it, or
        None when no road carries a non-zero z (``sample_z`` then falls back to the DEM / 0)."""
        from .datum import RoadDatum, roads_have_z
        self._datum_key = self._datum_fingerprint()
        self._datum = RoadDatum(self.roads, self.elevation, **kw) if roads_have_z(self.roads) else None
        return self._datum

    def road_datum(self):
        """The cached ``RoadDatum`` (rebuilt when reference lines changed); None without road z."""
        if self._datum_key != self._datum_fingerprint():
            self.rebuild_datum()
        return self._datum

    def sample_z(self, x, y):
        """Surface z at xy: the road datum (reference-line z, see ``twinmodel.datum``) when the
        roads carry elevation, else the raw DEM, else 0."""
        datum = self.road_datum()
        if datum is not None:
            return datum.z(x, y)
        if self.elevation is None:
            return np.zeros_like(np.asarray(x, dtype=np.float64)) if np.ndim(x) else 0.0
        return self.elevation.sample(x, y)

    def sample_dem_z(self, x, y):
        """Raw DEM z at xy (0 without a DEM) — for information only, see ``validate.z_error_dem``."""
        if self.elevation is None:
            return np.zeros_like(np.asarray(x, dtype=np.float64)) if np.ndim(x) else 0.0
        return self.elevation.sample(x, y)

    @property
    def geo_reference(self) -> str:
        return (f"+proj=tmerc +lat_0={self.origin_lat} +lon_0={self.origin_lon} +k=1 "
                f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +geoidgrids=egm96_15.gtx +vunits=m +no_defs")

    # -- I/O ------------------------------------------------------------------
    def save(self, directory: Path | str) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "schema": SCHEMA_VERSION, "name": self.name, "crs": "local-enu",
            "origin_lat": self.origin_lat, "origin_lon": self.origin_lon,
            "bbox_wgs84": list(self.bbox_wgs84), "geo_reference": self.geo_reference,
            "metadata": self.metadata, "has_elevation": self.elevation is not None,
        }
        (d / "model.json").write_text(json.dumps(meta, indent=2))
        _write_layer(d / "roads.geojson", [_road_feature(r) for r in self.roads])
        _write_layer(d / "junctions.geojson", [_junction_feature(j) for j in self.junctions])
        _write_layer(d / "signals.geojson", [_feature(s.position, _props(s, drop=("position",)))
                                             for s in self.signals])
        _write_layer(d / "surfaces.geojson", [_feature(s.geometry, _props(s, drop=("geometry",)))
                                              for s in self.surfaces])
        _write_layer(d / "curbs.geojson", [_feature(c.geometry, _props(c, drop=("geometry",)))
                                           for c in self.curbs])
        _write_layer(d / "markings.geojson", [_feature(m.geometry, _props(m, drop=("geometry",)))
                                              for m in self.markings if m.geometry is not None])
        _write_layer(d / "buildings.geojson", [_feature(b.footprint, _props(b, drop=("footprint",)))
                                               for b in self.buildings])
        _write_layer(d / "objects.geojson", [_feature(o.position, _props(o, drop=("position",)))
                                             for o in self.objects])
        (d / "controllers.json").write_text(json.dumps([asdict(c) for c in self.controllers], indent=2))
        if self.elevation is not None:
            self.elevation.to_npz(d / "elevation.npz")
        log.info("saved twin model %s -> %s", self.name, d)
        return d

    @classmethod
    def load(cls, directory: Path | str) -> "TwinModel":
        d = Path(directory)
        meta = json.loads((d / "model.json").read_text())
        if meta.get("crs") != "local-enu":
            raise ValueError("twin model directories must be stored in local-enu model space")
        m = cls(name=meta["name"], origin_lat=meta["origin_lat"], origin_lon=meta["origin_lon"],
                bbox_wgs84=tuple(meta["bbox_wgs84"]), metadata=meta.get("metadata", {}))
        m.roads = [_road_from_feature(f) for f in _read_layer(d / "roads.geojson")]
        m.junctions = [_junction_from_feature(f) for f in _read_layer(d / "junctions.geojson")]
        m.signals = [Signal(position=shape(f["geometry"]), **f["properties"])
                     for f in _read_layer(d / "signals.geojson")]
        m.surfaces = [Surface(geometry=shape(f["geometry"]), **f["properties"])
                      for f in _read_layer(d / "surfaces.geojson")]
        m.curbs = [CurbLine(geometry=shape(f["geometry"]), **f["properties"])
                   for f in _read_layer(d / "curbs.geojson")]
        m.markings = [Marking(geometry=shape(f["geometry"]), **f["properties"])
                      for f in _read_layer(d / "markings.geojson")]
        m.buildings = [Building(footprint=shape(f["geometry"]), **f["properties"])
                       for f in _read_layer(d / "buildings.geojson")]
        m.objects = [PointObject(position=shape(f["geometry"]), **f["properties"])
                     for f in _read_layer(d / "objects.geojson")]
        cpath = d / "controllers.json"
        if cpath.exists():
            m.controllers = [Controller(**c) for c in json.loads(cpath.read_text())]
        epath = d / "elevation.npz"
        if epath.exists():
            m.elevation = Elevation.from_npz(epath)
        return m


# --------------------------------------------------------------------------- GeoJSON helpers

def _feature(geom: BaseGeometry, props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


def _props(obj, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    d = asdict(obj)
    for k in drop:
        d.pop(k, None)
    return d


def _write_layer(path: Path, features: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def _read_layer(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text())["features"]


def _marking_dict(m: Optional[Marking]) -> Optional[dict[str, Any]]:
    if m is None:
        return None
    return {"kind": m.kind, "color": m.color, "width": m.width}


def _marking_from(d: Optional[dict[str, Any]]) -> Optional[Marking]:
    return None if d is None else Marking(**d)


def _road_feature(r: Road) -> dict[str, Any]:
    props = {
        "id": r.id, "name": r.name, "highway": r.highway, "osm_way_ids": r.osm_way_ids,
        "junction_id": r.junction_id,
        "predecessor": asdict(r.predecessor) if r.predecessor else None,
        "successor": asdict(r.successor) if r.successor else None,
        "center_marking": _marking_dict(r.center_marking), "tags": r.tags,
        "lanes": [{"id": l.id, "type": l.type, "width": l.width, "direction": l.direction,
                   "marking": _marking_dict(l.marking), "speed_limit": l.speed_limit,
                   "tags": l.tags} for l in r.lanes],
    }
    return _feature(r.reference_line, props)


def _road_from_feature(f: dict[str, Any]) -> Road:
    p = f["properties"]
    return Road(
        id=p["id"], reference_line=shape(f["geometry"]),
        lanes=[Lane(id=l["id"], type=l["type"], width=l["width"], direction=l["direction"],
                    marking=_marking_from(l.get("marking")), speed_limit=l.get("speed_limit"),
                    tags=l.get("tags", {})) for l in p.get("lanes", [])],
        name=p.get("name", ""), highway=p.get("highway", ""), osm_way_ids=p.get("osm_way_ids", []),
        junction_id=p.get("junction_id"),
        predecessor=RoadLink(**p["predecessor"]) if p.get("predecessor") else None,
        successor=RoadLink(**p["successor"]) if p.get("successor") else None,
        center_marking=_marking_from(p.get("center_marking")), tags=p.get("tags", {}),
    )


def _junction_feature(j: Junction) -> dict[str, Any]:
    props = {"id": j.id, "name": j.name, "osm_node_ids": j.osm_node_ids,
             "osm_way_ids": j.osm_way_ids, "tags": j.tags,
             "connections": [asdict(c) for c in j.connections]}
    geom = j.polygon if j.polygon is not None else Point(0, 0)
    props["has_polygon"] = j.polygon is not None
    return _feature(geom, props)


def _junction_from_feature(f: dict[str, Any]) -> Junction:
    p = f["properties"]
    return Junction(
        id=p["id"], polygon=shape(f["geometry"]) if p.get("has_polygon") else None,
        connections=[Connection(id=c["id"], incoming_road=c["incoming_road"],
                                connecting_road=c["connecting_road"],
                                contact_point=c.get("contact_point", "start"),
                                lane_links=[LaneLink(**ll) for ll in c.get("lane_links", [])])
                     for c in p.get("connections", [])],
        osm_node_ids=p.get("osm_node_ids", []), name=p.get("name", ""),
        osm_way_ids=p.get("osm_way_ids", []), tags=p.get("tags", {}),
    )
