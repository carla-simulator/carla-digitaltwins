"""Compile immutable editor annotations into local geometry and explicit semantics.

No network or building: this module can also run against an existing model for review.
Geometry and logic diagnostics are separate. A compiler report is not a routing guarantee.
"""

from __future__ import annotations
import copy
import hashlib
import json
import math
from collections import Counter

from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union
from . import corrections
from .frame import LocalFrame
from .model import road_osm_layer

CROSSINGS = {"crosswalk", "bike_crosswalk"}
SPACE_SURFACES = {
    "driving": "drivable",
    "bus": "drivable",
    "taxi": "drivable",
    "biking": "drivable",
    "parking": "parking",
    "sidewalk": "sidewalk",
    "shoulder": "drivable",
    "median": "median",
    "verge": "verge",
}


def fit_boundary(points, tolerance=0.10, closed=False, pinned=()):
    """Quadratic B-spline subdivision, bounded against the input polyline.

    Keep endpoints, explicit anchors and turns >=45 degrees. On failure return the
    original boundary, never repair a shape with buffer(0). Output is deterministic.
    """
    pts = [tuple(p[:2]) for p in points]
    if not pts:
        return []
    if closed and pts[0] == pts[-1]:
        pts.pop()
    if len(pts) < 3 or tolerance <= 0:
        return pts + [pts[0]] if closed else pts
    anchors = set(pinned)
    if not closed:
        anchors.update((0, len(pts) - 1))
    for i in range(len(pts)):
        if not closed and i in (0, len(pts) - 1):
            continue
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        u = (b[0] - a[0], b[1] - a[1])
        v = (c[0] - b[0], c[1] - b[1])
        n = math.hypot(*u) * math.hypot(*v)
        if n < 1e-12 or (u[0] * v[0] + u[1] * v[1]) / n <= math.cos(math.pi / 4):
            anchors.add(i)
    original = LineString(pts + [pts[0]] if closed else pts)
    # Reduce corner cutting until the symmetric deviation meets the explicit budget.
    for ratio in (0.25, 0.125, 0.0625, 0.03125, 0.015625):
        out = []
        for i, b in enumerate(pts):
            if i in anchors:
                out.append(b)
                continue
            a, c = pts[i - 1], pts[(i + 1) % len(pts)]
            out.extend(
                (
                    (
                        b[0] * (1 - ratio) + a[0] * ratio,
                        b[1] * (1 - ratio) + a[1] * ratio,
                    ),
                    (
                        b[0] * (1 - ratio) + c[0] * ratio,
                        b[1] * (1 - ratio) + c[1] * ratio,
                    ),
                )
            )
        if closed:
            out.append(out[0])
        candidate = LineString(out)
        if candidate.is_simple and original.hausdorff_distance(candidate) <= tolerance:
            return out
    return list(original.coords)


def resolve_reviewed_map(model, ops, tolerance=0.10):
    """Return a JSON-serializable local-metre representation; mutate neither input."""
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 1:
        raise ValueError("tolerance must be 0..1 metres")
    frame = LocalFrame(model.origin_lat, model.origin_lon)
    project = frame._to_local().transform
    roads = {r.id: r for r in model.roads}
    surfaces = {s.id: s for s in model.surfaces}
    signals = {s.id: s for s in model.signals}
    report = {
        "schema": "reviewed-map/1",
        "crs": "local-enu",
        "origin": [model.origin_lat, model.origin_lon],
        "source_sha256": hashlib.sha256(
            json.dumps(ops, sort_keys=True).encode()
        ).hexdigest(),
        "tolerance_m": tolerance,
        "features": [],
        "diagnostics": [],
    }

    def issue(code, ids, message, stage="geometry", severity="error"):
        report["diagnostics"].append(
            dict(code=code, ids=ids, message=message, stage=stage, severity=severity)
        )

    invalid = corrections.validate(ops)
    if invalid:
        for message in invalid:
            issue("invalid_annotation", [], message)
        report.update(geometry_ready=False, logic_ready=False)
        return report
    active = [
        o
        for o in ops
        if not o.get("disabled")
        and o.get("op") in ("space.set", "control.set", "junction.polygon")
    ]
    ids = [o.get("id") for o in active]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        issue(
            "invalid_identity",
            [],
            "Every reviewed object needs a unique persistent ID.",
        )
    shared = Counter()
    for o in active:
        for edge in ("left", "right"):
            for p in set(map(tuple, o.get(edge, []))):
                shared[p] += 1
    geometries = {}
    for o in active:
        oid = o.get("id", "")
        deleted = bool(o.get("deleted"))
        kind = o.get("kind", "junction")
        target = copy.deepcopy(o.get("target") or {})
        if not isinstance(target, dict):
            issue("invalid_target", [oid], "target must be an object.", "logic")
            target = {}
        if not target.get("road_id") and o.get("provenance", {}).get("road_id"):
            target = {**o["provenance"], **target}
        source_id = o.get("provenance", {}).get("source_id") or o.get(
            "provenance", {}
        ).get("signal_id")
        source_surface = surfaces.get(source_id)
        source_signal = signals.get(source_id)
        if source_surface and source_surface.tags.get("signal_id"):
            source_signal = signals.get(source_surface.tags["signal_id"])
        if source_signal and not target.get("lane_ids") and "lane_id" not in target:
            target["lane_ids"] = [
                lid
                for lo, hi in source_signal.validities
                for lid in range(lo, hi + 1)
                if lid
            ]
        if (
            not target.get("controller_id")
            and source_signal
            and source_signal.controller_id
        ):
            target["controller_id"] = source_signal.controller_id
        if not target.get("road_id"):
            if source_signal:
                target["road_id"] = source_signal.road_id
            elif source_surface and len(source_surface.road_ids) == 1:
                target["road_id"] = source_surface.road_ids[0]
        rid = target.get("road_id")
        road = roads.get(rid)
        if rid and not road:
            issue("missing_road", [oid], f"Road {rid} does not exist.", "logic")
        if road:
            expected = set(o.get("provenance", {}).get("osm_way_ids", []))
            if expected and not expected.intersection(road.osm_way_ids):
                issue(
                    "stale_road_reference",
                    [oid],
                    f"Road {rid} no longer matches the source OSM ways.",
                    "logic",
                )
            lane_ids = target.get("lane_ids", []) or (
                [target["lane_id"]] if "lane_id" in target else []
            )
            if not isinstance(lane_ids, list) or any(
                not isinstance(i, int)
                or isinstance(i, bool)
                or i not in {l.id for l in road.lanes}
                for i in lane_ids
            ):
                issue(
                    "missing_lane", [oid], f"Invalid lane assignment on {rid}.", "logic"
                )
                target.pop("lane_id", None)
                target["lane_ids"] = []
        local = lambda pts: [project(p[0], p[1]) for p in pts]
        boundaries = {}
        if o["op"] == "space.set":
            for edge in ("left", "right"):
                anchors = [i for i, p in enumerate(o[edge]) if shared[tuple(p)] > 1]
                boundaries[edge] = fit_boundary(
                    local(o[edge]), tolerance, pinned=anchors
                )
            raw = Polygon(local(o["left"]) + local(o["right"])[::-1])
            geom = Polygon(boundaries["left"] + boundaries["right"][::-1])
        elif o["op"] == "junction.polygon":
            # Junction boundaries are consumed before surface generation. Keep their
            # existing exact polygon until that stage shares the same fitting contract.
            raw = Polygon(local(o["polygon"]))
            geom = raw
            issue(
                "junction_topology_unchanged",
                [oid],
                "The polygon changes the junction surface, not its lane connections.",
                "logic",
                "warning",
            )
        else:
            raw = transform(project, shape(o["geometry"]))
            if raw.geom_type == "Polygon":
                geom = Polygon(
                    fit_boundary(list(raw.exterior.coords), tolerance, closed=True),
                    [list(r.coords) for r in raw.interiors],
                )
            elif raw.geom_type == "LineString":
                geom = LineString(fit_boundary(list(raw.coords), tolerance))
            else:
                geom = raw
        if not raw.is_valid or raw.is_empty:
            issue("invalid_geometry", [oid], "Invalid source geometry.")
            geom = raw
        elif (
            not geom.is_valid
            or geom.is_empty
            or (
                raw.geom_type == "Polygon"
                and raw.boundary.hausdorff_distance(geom.boundary) > tolerance + 1e-8
            )
        ):
            geom = raw
            boundaries = {k: local(o[k]) for k in boundaries}
            issue(
                "smoothing_fallback",
                [oid],
                "Smoothing violated polygon constraints; original geometry retained.",
                severity="warning",
            )
        if raw.geom_type == "Polygon":
            if raw.interiors:
                issue(
                    "unsupported_holes",
                    [oid],
                    "OpenDRIVE object outlines with holes are not implemented.",
                    "logic",
                )
            pts = list(raw.exterior.coords)
            tiny = sum(math.dist(a, b) < 0.01 for a, b in zip(pts, pts[1:]))
            if tiny:
                issue(
                    "tiny_edges",
                    [oid],
                    f"{tiny} edges are shorter than 1 cm; retained for review.",
                    severity="warning",
                )
        if not deleted and kind != "junction" and not rid:
            issue(
                "needs_road_binding",
                [oid],
                "Assign a reference road; no nearest-road behavior is inferred.",
                "logic",
            )
        if (
            not deleted
            and kind == "stop_line"
            and not (target.get("lane_ids") or target.get("lane_id"))
        ):
            issue(
                "needs_affected_lanes",
                [oid],
                "Assign the lanes that stop at this line.",
                "logic",
            )
        if (
            not deleted
            and kind in CROSSINGS
            and not (
                isinstance(o.get("relationships", {}), dict)
                and o.get("relationships", {}).get("connects_to")
            )
        ):
            issue(
                "needs_crossing_connections",
                [oid],
                "Crossing endpoints need pedestrian/cycle-space connections.",
                "logic",
            )
        lane_fit = None
        needs_lane = (
            kind
            in {
                "driving",
                "bus",
                "taxi",
                "biking",
                "sidewalk",
                "shoulder",
                "median",
                "verge",
            }
            or kind == "parking"
            and bool(o.get("replaces"))
        )
        if deleted and o["op"] == "space.set" and o.get("replaces"):
            issue(
                "needs_lane_removal",
                [oid],
                "Removing an existing lane/space requires lane-section and connection updates; not yet implemented.",
                "logic",
            )
        if not deleted and o["op"] == "space.set" and needs_lane:
            lane = (
                next((l for l in road.lanes if l.id == target.get("lane_id")), None)
                if road
                else None
            )
            if lane:
                lane_fit = fit_straight_lane(road, lane, boundaries, tolerance)
            if lane and lane.type != ("driving" if kind in {"bus", "taxi"} else kind):
                lane_fit = None
            if not lane_fit:
                issue(
                    "needs_lane_fitting",
                    [oid],
                    "Needs a complete, unambiguous lane fit: assign the lane and its extent; curved/partial-road fitting is not yet supported.",
                    "logic",
                )
        if (
            o.get("replaces")
            and o["op"] == "space.set"
            and not o.get("source_geometry")
        ):
            issue(
                "needs_source_coverage",
                [oid],
                "Legacy section replacement has no frozen source footprint; recapture its coverage before generation.",
            )
        if o.get("source_geometry"):
            try:
                source_geom = transform(project, shape(o["source_geometry"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                source_geom = Polygon()
            if (
                source_geom.geom_type != "Polygon"
                or not source_geom.is_valid
                or source_geom.is_empty
            ):
                issue(
                    "invalid_source_coverage",
                    [oid],
                    "Replacement source footprint must be a valid polygon.",
                )
        else:
            source_geom = source_surface.geometry if source_surface else None
        if (
            o["op"] == "space.set"
            and kind not in CROSSINGS
            and (
                deleted
                or source_geom is not None
                and source_geom.is_valid
                and geom.is_valid
                and source_geom.difference(geom).area > 1e-8
            )
        ):
            if o.get("replacement_kind") not in {
                "ground",
                "drivable",
                "sidewalk",
                "parking",
            }:
                issue(
                    "needs_replacement_fill",
                    [oid],
                    "Choose the surface that fills the old footprint when this space moves or is deleted.",
                )
        if kind in {"traffic_light", "traffic_sign", "stop_line"} and not deleted:
            affected = target.get("lane_ids") or (
                [target["lane_id"]] if "lane_id" in target else []
            )
            if not affected:
                issue(
                    "needs_affected_lanes",
                    [oid],
                    "Assign the affected lanes; orientation alone is insufficient.",
                    "logic",
                )
            if kind == "traffic_light" and target.get("controller_id") not in {
                c.id for c in model.controllers
            }:
                issue(
                    "needs_signal_controller",
                    [oid],
                    "Assign an existing signal controller/stage.",
                    "logic",
                )
            if kind == "traffic_sign" and o["type"] not in {
                "stop",
                "yield",
                "speed_limit",
                "priority_road",
            }:
                issue(
                    "unsupported_signal_behavior",
                    [oid],
                    "This sign can be placed visually but has no implemented CARLA signal binding.",
                    "logic",
                )
            if (
                road
                and affected
                and len({l.direction for l in road.lanes if l.id in affected}) > 1
            ):
                issue(
                    "ambiguous_control_direction",
                    [oid],
                    "Use separate controls for opposite travel directions.",
                    "logic",
                )
        relationships = copy.deepcopy(o.get("relationships", {}))
        if not isinstance(relationships, dict):
            issue(
                "invalid_relationships",
                [oid],
                "relationships must be an object.",
                "logic",
            )
            relationships = {}
        for key in ("connects_to", "yields_to", "controlled_by"):
            refs = relationships.get(key, [])
            if not isinstance(refs, list) or any(
                not isinstance(ref, str)
                or ref not in ids
                or next(x for x in active if x.get("id") == ref).get("deleted")
                for ref in refs
            ):
                issue(
                    "dangling_relationship",
                    [oid],
                    f"{key} must reference existing reviewed object IDs.",
                    "logic",
                )
        if not deleted and kind in CROSSINGS:
            refs = relationships.get("connects_to", [])
            expected_kind = "sidewalk" if kind == "crosswalk" else "biking"
            if (
                isinstance(refs, list)
                and refs
                and (
                    len(set(str(r) for r in refs)) < 2
                    or any(
                        next(
                            (x.get("kind") for x in active if x.get("id") == ref), None
                        )
                        != expected_kind
                        for ref in refs
                    )
                )
            ):
                issue(
                    "invalid_crossing_connections",
                    [oid],
                    f"Connect the crossing to at least two {expected_kind} spaces.",
                    "logic",
                )
            issue(
                "crossing_behavior_metadata",
                [oid],
                "Connections are exported as semantic metadata; CARLA pedestrian/cyclist routing is not generated.",
                "logic",
                "warning",
            )
        props = {
            "id": oid,
            "op": o["op"],
            "kind": kind,
            "deleted": deleted,
            "target": target,
            "relationships": relationships,
            "source_id": source_id,
            "source_signal_id": source_signal.id if source_signal else None,
            "source_geometry": mapping(source_geom)
            if source_geom is not None
            else None,
            "lane_fit": lane_fit,
            "boundaries": boundaries,
            "raw_geometry": mapping(raw),
            "annotation": copy.deepcopy(o),
            "layer": road_osm_layer(road) if road else 0,
            "allowed_users": {
                "bus": ["bus"],
                "taxi": ["taxi"],
                "biking": ["bicycle"],
                "bike_crosswalk": ["bicycle"],
                "crosswalk": ["pedestrian"],
            }.get(kind, []),
        }
        report["features"].append(
            {"type": "Feature", "geometry": mapping(geom), "properties": props}
        )
        if not deleted and o["op"] == "space.set" and kind not in CROSSINGS:
            geometries[oid] = (geom, props["layer"])
    items = list(geometries.items())
    for i, (aid, (a, alayer)) in enumerate(items):
        for bid, (b, blayer) in items[i + 1 :]:
            if a.is_valid and b.is_valid and alayer == blayer:
                overlap = a.intersection(b).area
                if overlap > 0.05:
                    issue(
                        "space_overlap",
                        [aid, bid],
                        f"Space footprints overlap by {overlap:.3f} square metres.",
                    )
    report["geometry_ready"] = not any(
        d["stage"] == "geometry" and d["severity"] == "error"
        for d in report["diagnostics"]
    )
    report["logic_ready"] = not any(
        d["stage"] == "logic" and d["severity"] == "error"
        for d in report["diagnostics"]
    )
    return report


def apply_reviewed_geometry(model, report):
    """Apply a conflict-free resolved patch to model surfaces; preserve overlay semantics.

    Ground ownership is changed only inside explicit replacement/new footprints. Markings
    overlay pavement; they do not cut holes in it. This function is idempotent per input hash.
    """
    if not report["geometry_ready"]:
        raise ValueError(
            "Reviewed geometry has unresolved errors; inspect diagnostics."
        )
    application_key = f"{report['source_sha256']}:{report['tolerance_m']}"
    if model.metadata.get("reviewed_applied") == application_key:
        return
    if model.metadata.get("reviewed_applied"):
        raise ValueError("Apply changed annotations to a fresh generated model.")
    from .model import Surface, Marking, PointObject, CurbLine
    from . import profiles

    P = profiles.get()
    surfaces = copy.deepcopy(model.surfaces)
    curbs = copy.deepcopy(model.curbs)
    markings = copy.deepcopy(model.markings)
    objects = copy.deepcopy(model.objects)
    for feature in report["features"]:
        p = feature["properties"]
        g = shape(feature["geometry"])
        kind = p["kind"]
        o = p["annotation"]
        rid = p["target"].get("road_id")
        if kind == "junction":
            continue  # applied before surface generation by corrections.apply_junctions
        if p["source_id"]:
            surfaces = [s for s in surfaces if s.id != p["source_id"]]
        if p["op"] == "space.set" and kind not in CROSSINGS:
            old = shape(p["source_geometry"]) if p["source_geometry"] else Polygon()
            coverage = old if p["deleted"] else unary_union([old, g])
            for s in surfaces:
                if (
                    s.kind
                    in {
                        "drivable",
                        "sidewalk",
                        "parking",
                        "median",
                        "verge",
                        "island",
                        "ground",
                    }
                    and int(s.tags.get("layer", 0)) == p["layer"]
                ):
                    s.geometry = s.geometry.difference(coverage)
            # Never leave the generated curb through the newly reviewed surface.
            for c in curbs:
                if (c.layer or 0) == p["layer"]:
                    c.geometry = c.geometry.difference(coverage)
            for marking in markings:
                if marking.geometry is not None and (marking.layer or 0) == p["layer"]:
                    # A shared center/boundary marking belongs to the neighboring lane
                    # too. Remove interiors, keeping unchanged shared boundary lines.
                    marking.geometry = marking.geometry.difference(
                        coverage.buffer(-1e-6)
                    )
            if p["deleted"]:
                surfaces.append(
                    Surface(
                        "review:" + p["id"],
                        o["replacement_kind"],
                        coverage,
                        z_offset=P.sidewalk.z
                        if o["replacement_kind"] == "sidewalk"
                        else 0,
                        tags={"layer": p["layer"]},
                    )
                )
                continue
            remainder = old.difference(g)
            if remainder.area > 1e-8:
                surfaces.append(
                    Surface(
                        "review:remainder:" + p["id"],
                        o["replacement_kind"],
                        remainder,
                        z_offset=P.sidewalk.z
                        if o["replacement_kind"] == "sidewalk"
                        else 0,
                        tags={"layer": p["layer"]},
                    )
                )
            height = (
                P.sidewalk.z
                if kind in {"sidewalk", "median"}
                else P.sidewalk.verge_z
                if kind == "verge"
                else 0.0
            )
            surfaces.append(
                Surface(
                    "review:" + p["id"],
                    SPACE_SURFACES[kind],
                    g,
                    z_offset=height,
                    road_ids=[rid] if rid else [],
                    tags={"layer": p["layer"], "review_id": p["id"]},
                )
            )
            if p.get("lane_fit"):
                lane = next(
                    l for l in model.road(rid).lanes if l.id == p["lane_fit"]["lane_id"]
                )
                if lane.marking:
                    marking = copy.deepcopy(lane.marking)
                    marking.geometry = LineString(
                        p["boundaries"]["left" if lane.id > 0 else "right"]
                    )
                    marking.layer = p["layer"]
                    markings.append(marking)
        elif not p["deleted"] and kind in CROSSINGS:
            area = (
                g
                if g.geom_type == "Polygon"
                else g.buffer(o["width_m"] / 2, cap_style=2)
            )
            surfaces.append(
                Surface(
                    "review:" + p["id"],
                    "crossing",
                    area,
                    z_offset=P.crossing.z,
                    road_ids=[rid] if rid else [],
                    tags={
                        "layer": p["layer"],
                        "review_id": p["id"],
                        "crossing_kind": kind,
                    },
                )
            )
        elif not p["deleted"] and kind == "stop_line":
            markings.append(
                Marking(kind="solid", width=o["width_m"], geometry=g, layer=p["layer"])
            )
        elif not p["deleted"] and g.geom_type == "Point":
            objects.append(
                PointObject(
                    "review:" + p["id"],
                    kind,
                    g,
                    tags={
                        "review_id": p["id"],
                        "type": o["type"],
                        "bearing": o["bearing"],
                    },
                )
            )
    model.surfaces = [s for s in surfaces if not s.geometry.is_empty]
    # Recreate curb edges where a reviewed raised surface meets lower pavement.
    for raised in model.surfaces:
        if (
            not raised.id.startswith("review:")
            or raised.z_offset <= 0
            or raised.kind == "crossing"
        ):
            continue
        for low in model.surfaces:
            if low.kind not in {"drivable", "parking", "ground"} or int(
                low.tags.get("layer", 0)
            ) != int(raised.tags.get("layer", 0)):
                continue
            border = raised.geometry.boundary.intersection(low.geometry.boundary)
            parts = (
                [border]
                if border.geom_type == "LineString"
                else list(getattr(border, "geoms", []))
            )
            for i, part in enumerate(parts):
                if part.geom_type == "LineString" and part.length > 1e-6:
                    curbs.append(
                        CurbLine(
                            f"{raised.id}:{low.id}:{i}",
                            part,
                            raised.z_offset - low.z_offset,
                            low.kind,
                            raised.kind,
                            int(raised.tags.get("layer", 0)),
                        )
                    )
    # Difference can split a curb; keep the model's LineString contract.
    model.curbs = []
    for c in curbs:
        parts = (
            [c.geometry]
            if c.geometry.geom_type == "LineString"
            else list(getattr(c.geometry, "geoms", []))
        )
        for i, g in enumerate(parts):
            if g.geom_type == "LineString" and not g.is_empty:
                item = copy.deepcopy(c)
                item.geometry = g
                item.id = f"{c.id}:review:{i}"
                model.curbs.append(item)
    model.markings = []
    for marking in markings:
        if marking.geometry is None:
            continue
        parts = (
            [marking.geometry]
            if marking.geometry.geom_type == "LineString"
            else list(getattr(marking.geometry, "geoms", []))
        )
        for part in parts:
            if part.geom_type == "LineString" and not part.is_empty:
                item = copy.deepcopy(marking)
                item.geometry = part
                model.markings.append(item)
    model.objects = objects
    model.metadata["reviewed_map"] = copy.deepcopy(report)
    model.metadata["reviewed_applied"] = application_key


def write_reviewed_objects(parent, model, road, geoms, sub):
    """Write exact reviewed polygons in object-local coordinates, with explicit lane validity.

    Bike crossings stay roadMark objects, not pedestrian crosswalk triggers. Relationships
    survive in userData for consumers that implement the additional behavior.
    """
    report = model.metadata.get("reviewed_map", {})
    if not report.get("geometry_ready"):
        return
    blocked = {
        oid
        for d in report.get("diagnostics", [])
        if d["severity"] == "error"
        for oid in d["ids"]
    }
    for f in report.get("features", []):
        p = f["properties"]
        o = p["annotation"]
        kind = p["kind"]
        if (
            p["deleted"]
            or p["id"] in blocked
            or p["target"].get("road_id") != road.id
            or kind not in CROSSINGS | {"parking", "stop_line"}
        ):
            continue
        g = shape(f["geometry"])
        g = g if g.geom_type == "Polygon" else g.buffer(o["width_m"] / 2, cap_style=2)
        # The object anchor is projected onto the *exported* spline, not the raw OSM line.
        center = g.centroid
        _, s, t, heading = project_reference(center, geoms)
        segment = next((g for g in geoms if g.s <= s <= g.s + g.length), geoms[-1])
        x0, y0 = segment.point_at(max(0, min(1, (s - segment.s) / segment.length)))
        anchor = Point(x0 - t * math.sin(heading), y0 + t * math.cos(heading))
        obj = sub(
            parent,
            "object",
            id="review:" + p["id"],
            name=o.get("label") or o.get("name") or kind,
            type="crosswalk"
            if kind == "crosswalk"
            else "parkingSpace"
            if kind == "parking"
            else "roadMark",
            s=s,
            t=t,
            zOffset=0,
            orientation="none",
            hdg=-heading,
            pitch=0,
            roll=0,
        )
        outline = sub(obj, "outline")
        for x, y in g.exterior.coords:
            sub(outline, "cornerLocal", u=x - anchor.x, v=y - anchor.y, z=0)
        for lid in p["target"].get("lane_ids", []) or (
            [p["target"]["lane_id"]] if "lane_id" in p["target"] else []
        ):
            # Convert model IDs through the same lane-section mapping as ordinary objects.
            from .export.xodr import road_sections, section_ids

            sections = road_sections(road)
            section = next(
                (section for section in sections if section[0] <= s < section[1]),
                sections[-1],
            )
            mapped = section_ids(section[2])[lid]
            sub(obj, "validity", fromLane=mapped, toLane=mapped)
        data = sub(
            obj,
            "userData",
            code="twin:review",
            value=json.dumps(
                {
                    "id": p["id"],
                    "kind": kind,
                    "relationships": p["relationships"],
                    "allowed_users": p["allowed_users"],
                },
                sort_keys=True,
            ),
        )


def fit_straight_lane(road, lane, boundaries, tolerance):
    """Fit a complete lane on a straight reference; reject partial/ambiguous associations."""
    ref = LineString([(p[0], p[1]) for p in road.reference_line.coords])
    a, b = Point(ref.coords[0]), Point(ref.coords[-1])
    axis = LineString([a, b])
    if axis.length < 1 or ref.hausdorff_distance(axis) > 0.001:
        return None
    dx = (b.x - a.x) / axis.length
    dy = (b.y - a.y) / axis.length
    projected = {}
    for side in ("left", "right"):
        samples = [
            ((x - a.x) * dx + (y - a.y) * dy, -(x - a.x) * dy + (y - a.y) * dx)
            for x, y in boundaries[side]
        ]
        if samples[0][0] > samples[-1][0]:
            samples.reverse()
        if any(q[0] - p[0] <= 1e-6 for p, q in zip(samples, samples[1:])):
            return None
        if (
            abs(samples[0][0]) > tolerance
            or abs(samples[-1][0] - axis.length) > tolerance
        ):
            return None
        projected[side] = samples

    def at(samples, s):
        for p, q in zip(samples, samples[1:]):
            if s <= q[0]:
                return p[1] + (q[1] - p[1]) * max(0, min(1, (s - p[0]) / (q[0] - p[0])))
        return samples[-1][1]

    stations = sorted(
        {
            0.0,
            axis.length,
            *[
                max(0, min(axis.length, s))
                for samples in projected.values()
                for s, t in samples
            ],
        }
    )
    inner_width = sum(
        l.width for l in road.lanes if l.id * lane.id > 0 and abs(l.id) < abs(lane.id)
    )
    expected = inner_width * (1 if lane.id > 0 else -1)
    widths = []
    for s in stations:
        left, right = at(projected["left"], s), at(projected["right"], s)
        if (
            left - right < 0.05
            or abs((right if lane.id > 0 else left) - expected) > tolerance
        ):
            return None
        widths.append([s, left - right])
    # Moving an interior lane's outer boundary would also move unreviewed outer lanes.
    if any(abs(w - lane.width) > tolerance for s, w in widths) and any(
        l.id * lane.id > 0 and abs(l.id) > abs(lane.id) for l in road.lanes
    ):
        return None
    return {"road_id": road.id, "lane_id": lane.id, "widths": widths}


def apply_reviewed_logic(model, report):
    """Apply only explicit, validated bindings. Unresolved logic never reaches OpenDRIVE."""
    if not report["logic_ready"]:
        raise ValueError("Reviewed logic has unresolved errors; inspect diagnostics.")
    for f in report["features"]:
        p = f["properties"]
        if p["deleted"]:
            continue
        fit = p.get("lane_fit")
        if fit:
            road = model.road(fit["road_id"])
            lane = next(l for l in road.lanes if l.id == fit["lane_id"])
            lane.tags["reviewed_widths"] = copy.deepcopy(fit["widths"])
            lane.type = "driving" if p["kind"] in {"bus", "taxi"} else p["kind"]
            lane.tags["reviewed_access"] = p["allowed_users"]
            lane.width = fit["widths"][0][1]
    from .model import Signal
    from .export.xodr import road_geometry

    replaced = {
        f["properties"]["source_signal_id"]
        for f in report["features"]
        if f["properties"]["source_signal_id"]
    }
    replaced.update("review:" + f["properties"]["id"] for f in report["features"])
    model.signals = [sig for sig in model.signals if sig.id not in replaced]
    for f in report["features"]:
        p = f["properties"]
        o = p["annotation"]
        kind = p["kind"]
        if p["deleted"] or kind not in {"stop_line", "traffic_light", "traffic_sign"}:
            continue
        road = model.road(p["target"]["road_id"])
        g = shape(f["geometry"])
        position = g if g.geom_type == "Point" else g.centroid
        geoms, _ = road_geometry(road)
        _, station, lateral, tangent = project_reference(position, geoms)
        lane_ids = p["target"].get("lane_ids") or [p["target"]["lane_id"]]
        forward = next(l.direction for l in road.lanes if l.id in lane_ids) == "forward"
        heading = (
            math.radians(90 - o["bearing"])
            if "bearing" in o
            else tangent + (0 if forward else math.pi)
        )
        signal_kind = (
            o["type"]
            if kind in {"stop_line", "traffic_sign"}
            else "traffic_light_ped"
            if o["type"] == "pedestrian"
            else "traffic_light_arrow"
            if o["type"] != "vehicle"
            else "traffic_light"
        )
        sig = Signal(
            "review:" + p["id"],
            signal_kind,
            road.id,
            station,
            lateral,
            position,
            heading=heading,
            value=o.get("value", 0) / 3.6 if o.get("type") == "speed_limit" else None,
            controller_id=p["target"].get("controller_id"),
            orientation="+" if forward else "-",
            validities=[(lid, lid) for lid in lane_ids],
            h_offset=heading - tangent,
            tags={"review_id": p["id"], "relationships": p["relationships"]},
        )
        model.signals.append(sig)
    current = {sig.id for sig in model.signals}
    for controller in model.controllers:
        controller.signal_ids = [sid for sid in controller.signal_ids if sid in current]
        for sig in model.signals:
            if (
                sig.controller_id == controller.id
                and sig.id not in controller.signal_ids
            ):
                controller.signal_ids.append(sig.id)
    model.metadata["reviewed_map"] = copy.deepcopy(report)


def project_reference(center, geoms):
    """Project onto the exported reference, with <=5 cm sampling intervals."""
    best = None
    for segment in geoms:
        n = max(8, int(math.ceil(segment.length / 0.05)))
        ref = LineString([segment.point_at(i / n) for i in range(n + 1)])
        along = ref.project(center)
        foot = ref.interpolate(along)
        distance = center.distance(foot)
        if best is None or distance < best[0]:
            before = ref.interpolate(max(0, along - 0.01))
            after = ref.interpolate(min(ref.length, along + 0.01))
            heading = math.atan2(after.y - before.y, after.x - before.x)
            lateral = -(center.x - foot.x) * math.sin(heading) + (
                center.y - foot.y
            ) * math.cos(heading)
            best = (
                distance,
                segment.s + along / ref.length * segment.length,
                lateral,
                heading,
            )
    return best
