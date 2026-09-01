"""Invariant checks between a TwinModel and its exported OpenDRIVE (DESIGN.md §Validation).

The xodr is parsed client-side with ``carla.Map(name, xodr_text)`` (no server).  CARLA's
coordinate frame is left-handed: it negates ``y`` when reading OpenDRIVE, so every waypoint
location is mapped back to model space with ``(x, -y, z)`` before any geometric test.

CLI: ``python -m twinmodel.validate <twin_dir> <xodr> [--out DIR] [--step 1.0]``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import shapely
from shapely.geometry import Point, mapping
from shapely.geometry.base import BaseGeometry

from scipy.spatial import cKDTree

from .model import TwinModel, lane_present_at, road_osm_layer
from .export.xodr import build_id_map, read_twin_ids, sample_reference

log = logging.getLogger("twinmodel.validate")

CONTAINMENT_TOL = 0.05  # metres of slack around surfaces (mesh/xodr are independent samples)
Z_TOL = 0.05
# A driving lane that stops with no ``next()`` this far inside the bbox is a dead end in the
# middle of the map: the traffic manager routes a vehicle onto it and then deletes the vehicle.
# Lanes that end at the bbox edge, and roads the lane graph marked as real cul-de-sacs
# (``dead_end_start`` / ``dead_end_end``, OSM degree-1 nodes) do not count.
TERMINAL_INSIDE_M = 30.0
# a road between two junctions shorter than this cannot carry a lane link (see
# profiles.JunctionRules.sliver_m)
SLIVER_M = 5.0
# grade separation (``grade_separation``): two driving waypoints on different OSM layers within
# CROSSING_RADIUS_M of each other in xy are a crossing; the upper one must clear the lower one
# by at least MIN_CLEARANCE_M (Caltrans/AASHTO minimum vertical clearance over a freeway is
# 16 ft 6 in = 5.03 m to the soffit; 4.5 m to the *deck surface* of the road above is the
# floor below which the twin is certainly wrong).
CROSSING_RADIUS_M = 3.0
MIN_CLEARANCE_M = 4.5


def _to_model_xyz(location) -> tuple[float, float, float]:
    return float(location.x), -float(location.y), float(location.z)


def _union(geoms: list[BaseGeometry]) -> Optional[BaseGeometry]:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    return shapely.union_all(geoms) if geoms else None


def _bbox_inside(model: TwinModel, margin: float) -> Optional[BaseGeometry]:
    """The requested bbox in model space, shrunk by ``margin``. ``None`` when the model has no
    bbox (a synthetic twin): every terminal lane then counts."""
    bbox = getattr(model, "bbox_wgs84", None)
    if not bbox:
        return None
    from .frame import LocalFrame
    frame = LocalFrame(model.origin_lat, model.origin_lon)
    south, west, north, east = (float(v) for v in bbox)
    lons = np.array([west, east, east, west])
    lats = np.array([south, south, north, north])
    x, y = frame.to_local(lons, lats)
    poly = shapely.Polygon(list(zip(np.atleast_1d(x), np.atleast_1d(y))))
    shrunk = poly.buffer(-margin)
    return shrunk if not shrunk.is_empty else None


def _junction_arm_lanes(model: TwinModel, junction) -> dict[str, set[int]]:
    """{road id: driving lane ids that travel INTO ``junction``} for every arm of it. Lanes with
    a negative id run along +s (they enter through the road's end), positive ones along -s.

    A road end the lane graph marked ``dead_end_<end>`` is skipped: OSM says no legal departure
    exists there (a cul-de-sac, or a junction every other arm of which is a one-way arriving —
    Jessie Street into Mint Street in SoMa). Those lanes are the same documented exception the
    ``terminal_lanes`` check makes, and a connection out of them would be an invented movement.
    """
    out: dict[str, set[int]] = {}
    for r in model.roads:
        if r.junction_id is not None:
            continue
        for link, sign, end in ((r.successor, -1, "end"), (r.predecessor, 1, "start")):
            if link is None or link.element != "junction" or link.id != junction.id:
                continue
            if r.tags.get(f"dead_end_{end}"):
                continue
            lanes = {l.id for l in r.lanes if l.type == "driving" and (l.id < 0) == (sign < 0)
                     and lane_present_at(l, r, end)}  # not an aux lane that tapered out earlier
            if lanes:
                out.setdefault(r.id, set()).update(lanes)
    return out


def _road_kind(r) -> str:
    """``street`` / ``aisle`` / ``driveway`` / ``junction`` for the reachability check."""
    if r.junction_id is not None:
        return "junction"
    if r.tags.get("driveway"):
        return "driveway"
    if r.tags.get("parking_aisle"):
        return "aisle"
    return "street"


def unreachable_lanes(model: TwinModel, cmap, road_of: dict[int, str], driving,
                      inside_poly: Optional[BaseGeometry]) -> dict[str, Any]:
    """Driving lanes no vehicle can reach from a street: no directed path through the xodr
    lane links (``carla.Map.get_topology``) from any lane of a street-class road (not a parking
    aisle, not a driveway, not a connecting road) to them.

    A vehicle may turn round at a dead end: a lane whose end has no successor flows into the
    opposite-direction lanes of its own road, so the outbound lane of a two-way cul-de-sac (or
    of an aisle that ends at its last stall) is reachable through the inbound one — only a lot
    nothing enters is reported. The unreachable lanes are grouped into connected components
    (one per lot, normally) with a reason each:

    - ``entrance_outside_bbox`` — a road of the component runs out of the bbox: its entrance is
      beyond the twin's scope;
    - ``exit_only`` — the component reaches a street through its one-way exit(s) but nothing
      leads in (an entrance mapped as a one-way *out*, or an entrance lost with a dropped way);
    - ``return_lane`` — every road of the component is reachable along its other lane: a
      two-way aisle whose far end only leads onto a one-way aisle has a return lane no legal
      movement enters (the lot itself is reachable; not a failure);
    - ``isolated`` — no link to the rest of the network at all.
    """
    roads_by_id = {r.id: r for r in model.roads}

    def kind_of(rid: str) -> str:
        r = roads_by_id.get(rid)
        return _road_kind(r) if r is not None else "unknown"

    nodes: set[tuple[str, int]] = {(road_of.get(wp.road_id, str(wp.road_id)), wp.lane_id) for wp in driving}
    out_edges: dict[tuple[str, int], set[tuple[str, int]]] = {n: set() for n in nodes}
    in_edges: dict[tuple[str, int], set[tuple[str, int]]] = {n: set() for n in nodes}
    for a, b in cmap.get_topology():
        ka = (road_of.get(a.road_id, str(a.road_id)), a.lane_id)
        kb = (road_of.get(b.road_id, str(b.road_id)), b.lane_id)
        if ka == kb:
            continue
        out_edges.setdefault(ka, set()).add(kb)
        in_edges.setdefault(kb, set()).add(ka)
        nodes.update((ka, kb))
    # turning round at a dead end: a lane end with no successor flows into the opposite lanes
    by_road: dict[str, set[int]] = {}
    for rid, lid in nodes:
        by_road.setdefault(rid, set()).add(lid)
    uturns: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for n in nodes:
        rid, lid = n
        if out_edges.get(n) or kind_of(rid) == "junction":
            continue
        uturns[n] = {(rid, o) for o in by_road[rid] if (o < 0) != (lid < 0)}

    seeds = [n for n in nodes if kind_of(n[0]) == "street"]
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        n = stack.pop()
        for m in out_edges.get(n, ()) | uturns.get(n, set()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    unreached = sorted(n for n in nodes if n not in seen)

    # components over the undirected link graph, restricted to the unreachable lanes
    comp: dict[tuple[str, int], int] = {}
    for n in unreached:
        if n in comp:
            continue
        cid = len({c for c in comp.values()})
        comp[n] = cid
        stack = [n]
        while stack:
            x = stack.pop()
            for y in out_edges.get(x, set()) | in_edges.get(x, set()) | uturns.get(x, set()):
                if y in comp or y in seen:
                    continue
                comp[y] = cid
                stack.append(y)
    groups: list[dict[str, Any]] = []
    for cid in sorted(set(comp.values())):
        members = [n for n, c in comp.items() if c == cid]
        rids = sorted({rid for rid, _ in members}, key=lambda s: (len(s), s))
        plain = [roads_by_id[r] for r in rids if r in roads_by_id and roads_by_id[r].junction_id is None]
        exits = any(m in seen for n in members for m in out_edges.get(n, ()))
        at_edge = False
        for r in plain:
            for link, end in ((r.predecessor, "start"), (r.successor, "end")):
                if link is not None or r.tags.get(f"dead_end_{end}"):
                    continue
                pt = r.reference_line.coords[0 if end == "start" else -1]
                if inside_poly is None or not inside_poly.contains(Point(pt[0], pt[1])):
                    at_edge = True
        return_lane = bool(plain) and all(
            any((r.id, l.id) in seen for l in r.lanes if l.type == "driving") for r in plain)
        reason = ("entrance_outside_bbox" if at_edge else "return_lane" if return_lane
                  else "exit_only" if exits else "isolated")
        xy = plain[0].reference_line.interpolate(0.5, normalized=True) if plain else None
        groups.append({
            "reason": reason, "lanes": len(members),
            "roads": [r for r in rids if r in roads_by_id and roads_by_id[r].junction_id is None],
            "kinds": sorted({kind_of(r) for r in rids}),
            "osm_way_ids": sorted({w for r in plain for w in (r.osm_way_ids or [])}),
            "x": float(xy.x) if xy is not None else None, "y": float(xy.y) if xy is not None else None,
        })
    by_kind: dict[str, int] = {}
    for rid, _ in unreached:
        by_kind[kind_of(rid)] = by_kind.get(kind_of(rid), 0) + 1
    return {
        "count": len(unreached), "by_kind": by_kind,
        "in_bbox_count": sum(g["lanes"] for g in groups if g["reason"] in ("exit_only", "isolated")),
        "return_lane_count": sum(g["lanes"] for g in groups if g["reason"] == "return_lane"),
        "groups": groups, "lanes": [{"road_id": r, "lane_id": l} for r, l in unreached[:200]],
        "pass": not any(g["reason"] in ("exit_only", "isolated") for g in groups),
    }


def _lane_last_waypoints(wps) -> list:
    """One waypoint per (road, lane): the last one in the lane's driving direction."""
    best: dict[tuple[int, int], Any] = {}
    for wp in wps:
        key = (wp.road_id, wp.lane_id)
        cur = best.get(key)
        if cur is None:
            best[key] = wp
        elif wp.lane_id < 0 and wp.s > cur.s:
            best[key] = wp
        elif wp.lane_id > 0 and wp.s < cur.s:
            best[key] = wp
    return list(best.values())


def validate(model: TwinModel, xodr_text: str, *, step: float = 1.0,
             out_dir: Optional[Path | str] = None, tol: float = CONTAINMENT_TOL) -> dict[str, Any]:
    """Run every check and return the report dict; write ``violations.geojson`` into ``out_dir``."""
    import carla  # local import: the wheel is heavy and only needed here

    report: dict[str, Any] = {
        "name": model.name, "step": step, "tolerance_m": tol,
        "topology": {"loaded": False}, "lane_in_drivable": None, "junction_containment": None,
        "z_error": None, "z_error_dem": None, "sidewalk_coverage": None, "lane_coverage": None,
        "landmarks": None, "grade_separation": None,
        "notes": [], "violations": [],
    }
    try:
        cmap = carla.Map(model.name or "twin", xodr_text)
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        report["topology"]["error"] = f"{type(exc).__name__}: {exc}"
        report["notes"].append("carla.Map failed to parse the xodr")
        return report

    ids = read_twin_ids(xodr_text) or build_id_map(model)
    road_of = ids.road_inv
    # OSM stacking level per model road; a connecting road inherits the level of the road it
    # comes from (a junction never spans a grade separation, see lanegraph 1b)
    plain_ids = {r.id for r in model.roads if r.junction_id is None}
    layer_of = {r.id: road_osm_layer(r) for r in model.roads if r.junction_id is None}
    junction_layer: dict[str, int] = {}
    for j in model.junctions:
        ls = [layer_of.get(c.incoming_road, 0) for c in j.connections]
        junction_layer[j.id] = max(set(ls), key=ls.count) if ls else 0
    for r in model.roads:
        if r.junction_id is not None:
            # one level per junction (its connecting roads share one plane, datum.py)
            layer_of[r.id] = junction_layer.get(r.junction_id, 0)
    wps = cmap.generate_waypoints(step)
    driving = [wp for wp in wps if wp.lane_type == carla.LaneType.Driving]
    topo = cmap.get_topology()
    report["topology"] = {
        "loaded": True,
        "waypoints": len(wps),
        "driving_waypoints": len(driving),
        "roads_in_xodr": len({wp.road_id for wp in wps}),
        "junctions_in_xodr": len({wp.junction_id for wp in wps if wp.is_junction}),
        "topology_pairs": len(topo),
        "roads_no_successor": sorted(r.id for r in model.roads
                                     if r.junction_id is None and r.successor is None),
        "roads_no_predecessor": sorted(r.id for r in model.roads
                                       if r.junction_id is None and r.predecessor is None),
        "dead_end_lanes": [],
    }
    for wp in _lane_last_waypoints(driving):
        if not wp.next(2.0):
            report["topology"]["dead_end_lanes"].append(
                {"road_id": road_of.get(wp.road_id, str(wp.road_id)), "lane_id": wp.lane_id})
    report["topology"]["dead_end_lane_count"] = len(report["topology"]["dead_end_lanes"])

    # lane coverage: every driving lane of every model road must have waypoints -------------
    seen: dict[str, set[int]] = {}
    for wp in driving:
        seen.setdefault(road_of.get(wp.road_id, str(wp.road_id)), set()).add(wp.lane_id)
    expected = {r.id: {l.id for l in r.lanes if l.type == "driving"} for r in model.roads}
    n_expected = sum(len(v) for v in expected.values())
    missing = [{"road_id": rid, "lane_id": lid} for rid, lanes in expected.items()
               for lid in sorted(lanes) if lid not in seen.get(rid, set())]
    report["lane_coverage"] = {
        "expected_lanes": n_expected, "missing": missing,
        "fraction": (1.0 - len(missing) / n_expected) if n_expected else None,
    }

    # landmarks --------------------------------------------------------------------------
    landmarks = cmap.get_all_landmarks()
    n_signals = sum(1 for s in model.signals if s.kind != "crosswalk")
    n_cross = sum(1 for s in model.signals if s.kind == "crosswalk")
    report["landmarks"] = {
        "count": len(landmarks), "expected_signals": n_signals,
        "types": sorted({lm.type for lm in landmarks}),
        "crosswalk_vertices": len(cmap.get_crosswalks()), "expected_crosswalks": n_cross,
    }

    # geometry in model space ------------------------------------------------------------
    if driving:
        xyz = np.array([_to_model_xyz(wp.transform.location) for wp in driving])
    else:
        xyz = np.zeros((0, 3))

    drivable = _union([s.geometry for s in model.surfaces_of("drivable")])
    violations: list[dict[str, Any]] = []
    if drivable is None:
        report["lane_in_drivable"] = None
        report["notes"].append("no drivable surfaces in the model (surfaces.py not run?) - "
                               "lane_in_drivable skipped")
    elif len(xyz):
        area = drivable.buffer(tol) if tol > 0 else drivable
        shapely.prepare(area)
        inside = shapely.contains_xy(area, xyz[:, 0], xyz[:, 1])
        report["lane_in_drivable"] = {
            "fraction": float(inside.mean()), "inside": int(inside.sum()),
            "outside": int((~inside).sum()), "total": int(len(inside)),
            "pass": bool(inside.mean() >= 0.98),
        }
        for i in np.flatnonzero(~inside):
            wp = driving[i]
            violations.append({
                "kind": "outside_drivable", "x": float(xyz[i, 0]), "y": float(xyz[i, 1]),
                "z": float(xyz[i, 2]), "road_id": road_of.get(wp.road_id, str(wp.road_id)),
                "lane_id": wp.lane_id, "s": float(wp.s),
                "distance": float(drivable.distance(Point(xyz[i, 0], xyz[i, 1]))),
            })
    else:
        report["lane_in_drivable"] = {"fraction": 0.0, "inside": 0, "outside": 0, "total": 0,
                                      "pass": False}
        report["notes"].append("no driving waypoints generated")

    # junction containment: connecting-road samples inside their junction polygon ---------
    polys = {j.id: j.polygon for j in model.junctions if j.polygon is not None}
    if not polys:
        report["notes"].append("no junction polygons in the model - junction_containment skipped")
    else:
        n_in = n_tot = 0
        # (a) CARLA waypoints on connecting roads
        for i, wp in enumerate(driving):
            rid = road_of.get(wp.road_id)
            if rid is None:
                continue
            try:
                jid = model.road(rid).junction_id
            except KeyError:
                continue
            if jid is None or jid not in polys:
                continue
            n_tot += 1
            if polys[jid].buffer(tol).covers(Point(xyz[i, 0], xyz[i, 1])):
                n_in += 1
            else:
                violations.append({"kind": "outside_junction", "x": float(xyz[i, 0]),
                                   "y": float(xyz[i, 1]), "z": float(xyz[i, 2]), "road_id": rid,
                                   "lane_id": wp.lane_id, "s": float(wp.s), "junction_id": jid,
                                   "distance": float(polys[jid].distance(Point(xyz[i, 0], xyz[i, 1])))})
        # (b) model-side: the fitted reference line of every connecting road
        m_in = m_tot = 0
        for r in model.roads:
            if r.junction_id is None or r.junction_id not in polys:
                continue
            pts = sample_reference(r, 1.0)
            cov = shapely.contains_xy(polys[r.junction_id].buffer(tol), pts[:, 0], pts[:, 1])
            m_in += int(cov.sum())
            m_tot += len(cov)
        report["junction_containment"] = {
            "fraction": (n_in / n_tot) if n_tot else None, "inside": n_in, "total": n_tot,
            "reference_line_fraction": (m_in / m_tot) if m_tot else None,
            "pass": bool(n_tot and n_in / n_tot >= 0.98),
        }

    # z error: waypoint z (xodr elevation profile) vs the surface z the mesh is built on
    # (road datum, see twinmodel.datum) -> measures xodr-vs-mesh consistency. The raw-DEM
    # comparison is kept as z_error_dem for information (how far the road sits off terrain).
    if len(xyz):
        datum = model.rebuild_datum()
        # grade separation: a waypoint on an overpass must be compared against the surface of
        # *its* layer, not against the road it flies over (they share the xy)
        wp_layer = np.array([layer_of.get(road_of.get(wp.road_id), 0) for wp in driving],
                            dtype=np.int64)
        zs = np.empty(len(xyz), dtype=np.float64)
        for lay in np.unique(wp_layer):
            sel = wp_layer == lay
            zs[sel] = np.asarray(model.sample_z(xyz[sel, 0], xyz[sel, 1], layer=int(lay)),
                                 dtype=np.float64)
        err = np.abs(xyz[:, 2] - zs)
        el_name = "none" if model.elevation is None else (model.elevation.source or "grid")
        report["z_error"] = {
            "p50": float(np.percentile(err, 50)), "p95": float(np.percentile(err, 95)),
            "max": float(err.max()), "pass": bool(np.percentile(err, 95) <= Z_TOL),
            "elevation": el_name,
            "surface_z": "road_datum" if datum is not None else ("dem" if model.elevation is not None else "0"),
        }
        if model.elevation is not None:
            zd = np.asarray(model.sample_dem_z(xyz[:, 0], xyz[:, 1]), dtype=np.float64)
            errd = np.abs(xyz[:, 2] - zd)
            report["z_error_dem"] = {
                "p50": float(np.percentile(errd, 50)), "p95": float(np.percentile(errd, 95)),
                "max": float(errd.max()), "elevation": el_name,
            }

    # grade separation: wherever two driving waypoints of different OSM layers share the same
    # xy, the upper one must clear the lower one by MIN_CLEARANCE_M ---------------------
    # Connecting roads are excluded: a junction sits on one plane, and one at a bridge abutment
    # legitimately has arms of two layers meeting there.
    plain_wp = np.array([(road_of.get(wp.road_id) or "") in plain_ids for wp in driving])
    # roads that meet: directly linked, or two arms of the same junction. A deck and its
    # approach share an abutment, where their z is continuous by construction.
    adjacent: set[tuple[str, str]] = set()
    for r in model.roads:
        for link in (r.predecessor, r.successor):
            if link is not None and link.element == "road":
                adjacent.add((r.id, link.id))
                adjacent.add((link.id, r.id))
    for j in model.junctions:
        arms = sorted({c.incoming_road for c in j.connections} |
                      {model.road(c.connecting_road).successor.id for c in j.connections
                       if model.road(c.connecting_road).successor is not None})
        for a in arms:
            for b in arms:
                adjacent.add((a, b))
    if len(xyz) and len(np.unique(wp_layer[plain_wp])) > 1:
        gaps: list[tuple[float, dict[str, Any]]] = []
        upper_layers = [int(l) for l in np.unique(wp_layer[plain_wp]) if l > int(wp_layer[plain_wp].min())]
        for lay in upper_layers:
            hi = (wp_layer == lay) & plain_wp
            lo = (wp_layer < lay) & plain_wp
            if not hi.any() or not lo.any():
                continue
            lo_xy = xyz[lo][:, :2]
            tree = cKDTree(lo_xy)
            d, k = tree.query(xyz[hi][:, :2], k=1)
            near = d <= CROSSING_RADIUS_M
            if not near.any():
                continue
            hi_idx = np.flatnonzero(hi)[near]
            lo_idx = np.flatnonzero(lo)[k[near]]
            dz = xyz[hi_idx, 2] - xyz[lo_idx, 2]
            for i, j, gap in zip(hi_idx, lo_idx, dz):
                a = road_of.get(driving[i].road_id)
                b = road_of.get(driving[j].road_id)
                if (a, b) in adjacent:
                    continue  # abutment: the deck and its approach meet there, z is continuous
                gaps.append((float(gap), {
                    "upper_road": road_of.get(driving[i].road_id, str(driving[i].road_id)),
                    "lower_road": road_of.get(driving[j].road_id, str(driving[j].road_id)),
                    "x": float(xyz[i, 0]), "y": float(xyz[i, 1]),
                }))
        if gaps:
            gaps.sort(key=lambda t: t[0])
            worst_gap, worst = gaps[0]
            report["grade_separation"] = {
                "crossing_waypoints": len(gaps),
                "min_z_gap_m": worst_gap,
                "p05_z_gap_m": float(np.percentile([g for g, _ in gaps], 5)),
                "min_required_m": MIN_CLEARANCE_M,
                "worst": worst,
                "pass": bool(worst_gap >= MIN_CLEARANCE_M),
            }
            if worst_gap < MIN_CLEARANCE_M:
                violations.append({"kind": "grade_separation", "x": worst["x"], "y": worst["y"],
                                   "z": worst_gap, "road_id": worst["upper_road"],
                                   "lower_road": worst["lower_road"]})

    # sidewalk coverage -----------------------------------------------------------------
    sidewalk = _union([s.geometry for s in model.surfaces_of("sidewalk")])
    if drivable is not None:
        report["sidewalk_coverage"] = {
            "drivable_area": float(drivable.area),
            "sidewalk_area": float(sidewalk.area) if sidewalk is not None else 0.0,
            "ratio": float(sidewalk.area / drivable.area) if sidewalk is not None and drivable.area else 0.0,
        }

    # terminal lanes inside the map -----------------------------------------------------
    # (the symptom the CARLA traffic-manager soak sees: a vehicle routed onto one of these is
    # deleted mid-map). A lane that stops at the bbox edge, or on a road whose OSM end node has
    # degree 1 (a real cul-de-sac: Jennifer Place, Scott Alley, ...), is not a defect.
    inside_poly = _bbox_inside(model, TERMINAL_INSIDE_M)
    roads_by_id = {r.id: r for r in model.roads}
    terminal: list[dict[str, Any]] = []
    n_culdesac = n_edge = 0
    for wp in (_lane_last_waypoints(driving) if inside_poly is not None else []):
        if wp.next(2.0):
            continue
        rid = road_of.get(wp.road_id, str(wp.road_id))
        r = roads_by_id.get(rid)
        end = "end" if wp.lane_id < 0 else "start"
        if r is not None and r.tags.get(f"dead_end_{end}"):
            n_culdesac += 1
            continue
        x, y, z = _to_model_xyz(wp.transform.location)
        if inside_poly is not None and not inside_poly.contains(Point(x, y)):
            n_edge += 1
            continue
        terminal.append({"road_id": rid, "lane_id": int(wp.lane_id), "x": float(x), "y": float(y),
                         "junction": bool(r is not None and r.junction_id is not None)})
        violations.append({"kind": "terminal_lane", "x": float(x), "y": float(y), "z": float(z),
                           "road_id": rid, "lane_id": int(wp.lane_id), "s": float(wp.s)})
    report["terminal_lanes"] = {
        "inside_m": TERMINAL_INSIDE_M, "count": len(terminal) if inside_poly is not None else None,
        "cul_de_sacs": n_culdesac, "at_bbox_edge": n_edge,
        "pass": not terminal, "lanes": terminal[:100],
    }
    if inside_poly is None:
        report["notes"].append("no bbox in the model - terminal_lanes skipped")

    # slivers: a road between two junctions too short to hold a lane link ------------------
    slivers = [{"road_id": r.id, "name": r.name, "length": round(float(r.length), 2),
                "predecessor": r.predecessor.id, "successor": r.successor.id}
               for r in model.roads
               if r.junction_id is None and r.length < SLIVER_M
               and r.predecessor is not None and r.predecessor.element == "junction"
               and r.successor is not None and r.successor.element == "junction"]
    report["junction_slivers"] = {"max_m": SLIVER_M, "count": len(slivers),
                                  "pass": not slivers, "roads": slivers}

    # every driving lane entering a junction must be the incoming lane of a connection ------
    unlinked: list[dict[str, Any]] = []
    for j in model.junctions:
        by_road: dict[str, set[int]] = {}
        for conn in j.connections:
            by_road.setdefault(conn.incoming_road, set()).update(ll.from_lane for ll in conn.lane_links)
        for rid, lanes in _junction_arm_lanes(model, j).items():
            missing = sorted(lanes - by_road.get(rid, set()))
            if missing:
                unlinked.append({"junction_id": j.id, "road_id": rid, "lane_ids": missing})
    report["junction_lane_links"] = {"unlinked_arms": len(unlinked), "pass": not unlinked,
                                    "arms": unlinked[:50]}

    # lanes no vehicle can reach from a street (a lot whose entrance is not in the twin) ------
    report["unreachable_lanes"] = unreachable_lanes(model, cmap, road_of, driving, inside_poly)
    for g in report["unreachable_lanes"]["groups"]:
        if g["reason"] in ("exit_only", "isolated") and g["x"] is not None:
            violations.append({"kind": "unreachable_lanes", "x": g["x"], "y": g["y"], "z": 0.0,
                               "road_id": g["roads"][0] if g["roads"] else "", "reason": g["reason"],
                               "lanes": g["lanes"]})
    # ramp continuity (taper-model gores, lanegraph 7k): a merging ramp's lane must run on
    # into a mainline driving lane through ``next()`` alone, without a junction; a diverging
    # ramp must be reached from the mainline's deceleration lane, through nothing but the
    # nose junction's stubs ----------------------------------------------------------------
    report["ramp_continuity"] = _ramp_continuity(model, driving, road_of, roads_by_id, violations)

    report["violations"] = violations
    report["violation_count"] = len(violations)
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        write_violations(violations, out / "violations.geojson")
    return report


RAMP_WALK_M = 60.0   # how far past the nose the continuity walk follows next()


def _ramp_continuity(model: TwinModel, driving: list, road_of: dict[int, str],
                     roads_by_id: dict[str, Any], violations: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk ``next(2.0)`` from the end of every taper-model ramp lane (merge) or from the end
    of the mainline lanes that feed a diverging ramp, and check that the other side is reached
    with no junction waypoint on the way (a diverge may pass its own nose junction)."""
    nose_conn: dict[str, str] = {}   # connecting road id -> junction id, for nose junctions only
    for j in model.junctions:
        if j.tags.get("gore_role") == "diverge_nose":
            for c in j.connections:
                nose_conn[c.connecting_road] = j.id
    last: dict[tuple[str, int], Any] = {}
    for wp in driving:
        rid = road_of.get(wp.road_id)
        if rid is None or wp.lane_id >= 0:
            continue
        cur = last.get((rid, wp.lane_id))
        if cur is None or wp.s > cur.s:
            last[(rid, wp.lane_id)] = wp
    checks: list[dict[str, Any]] = []
    for r in model.roads:
        kind = r.tags.get("gore_kind")
        if r.tags.get("gore_model") != "taper" or kind not in ("merge", "diverge"):
            continue
        main_id = r.tags.get("gore_mainline")
        main = roads_by_id.get(main_id)
        if main is None:
            continue
        if kind == "merge":
            starts = [wp for (rid, _lid), wp in last.items() if rid == r.id]
            target = main.id
        else:
            # the lanes feeding the ramp at the mainline's end, by their OpenDRIVE ids there
            # (an auxiliary lane that began mid-road is renumbered in the last lane section)
            from .export.xodr import _contact_lanes
            lanes_at_end, ids_at_end = _contact_lanes(main, "end")
            aux_ids = [ids_at_end[l.id] for l in lanes_at_end
                       if l.tags.get("aux") and l.tags.get("ramp") == r.id]
            feeder = sorted((ids_at_end[l.id] for l in lanes_at_end
                             if l.type == "driving" and l.id < 0), reverse=True)
            lane_ids = aux_ids or feeder[-1:]
            starts = [wp for (rid, lid), wp in last.items() if rid == main.id and lid in lane_ids]
            target = r.id
        for wp in starts:
            ok, via_junction, path = False, False, [road_of.get(wp.road_id, "?")]
            frontier, walked = [wp], 0.0
            while frontier and walked < RAMP_WALK_M:
                nxt = []
                for w in frontier:
                    for q in w.next(2.0):
                        qid = road_of.get(q.road_id, str(q.road_id))
                        if q.is_junction and qid not in nose_conn:
                            via_junction = True
                        if qid == target and q.lane_type == carla_driving():
                            ok = True
                        nxt.append(q)
                        if qid != path[-1]:
                            path.append(qid)
                if ok:
                    break
                frontier, walked = nxt[:8], walked + 2.0
            entry = {"ramp": r.id, "kind": kind, "from_road": road_of.get(wp.road_id), "from_lane": wp.lane_id,
                     "reached": ok, "via_junction": via_junction, "path": path[:8]}
            checks.append(entry)
            if not ok or via_junction:
                x, y, z = _to_model_xyz(wp.transform.location)
                violations.append({"kind": "ramp_continuity", "x": x, "y": y, "z": z,
                                   "road_id": road_of.get(wp.road_id), "lane_id": int(wp.lane_id),
                                   "s": float(wp.s), "ramp": r.id, "reached": ok,
                                   "via_junction": via_junction})
    failures = [c for c in checks if not c["reached"] or c["via_junction"]]
    return {"checked": len(checks), "failures": len(failures), "pass": not failures,
            "details": failures[:20]}


def carla_driving():
    import carla
    return carla.LaneType.Driving


def write_violations(violations: list[dict[str, Any]], path: Path | str) -> Path:
    feats = [{"type": "Feature", "geometry": mapping(Point(v["x"], v["y"])),
              "properties": {k: val for k, val in v.items() if k not in ("x", "y")}}
             for v in violations]
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "crs": "local-enu",
                                      "features": feats}))
    return Path(path)


def write_report(report: dict[str, Any], path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    slim = dict(report)
    slim["violations"] = slim.get("violations", [])[:200]  # keep report.json small
    p.write_text(json.dumps(slim, indent=2))
    return p


def summary(report: dict[str, Any]) -> str:
    t = report.get("topology", {})
    lines = [f"twin: {report.get('name')}",
             f"xodr loaded: {t.get('loaded')}" + (f"  ({t['error']})" if t.get("error") else "")]
    if t.get("loaded"):
        lines.append(f"waypoints: {t['waypoints']} (driving {t['driving_waypoints']}), roads "
                     f"{t['roads_in_xodr']}, junctions {t['junctions_in_xodr']}, topology pairs "
                     f"{t['topology_pairs']}, dead-end lanes {t['dead_end_lane_count']}, "
                     f"roads w/o successor {len(t['roads_no_successor'])}")
        lc = report.get("lane_coverage") or {}
        lines.append(f"lane coverage: {lc.get('fraction')} ({len(lc.get('missing', []))} missing)")
        tl = report.get("terminal_lanes") or {}
        if tl and tl.get("count") is not None:
            lines.append(f"terminal_lanes (>{tl['inside_m']:.0f} m inside the bbox): {tl['count']} "
                         f"[{tl['cul_de_sacs']} cul-de-sacs, {tl['at_bbox_edge']} at the edge] "
                         + ("PASS" if tl.get("pass") else "FAIL"))
        ur = report.get("unreachable_lanes") or {}
        if ur:
            outside = sum(g["lanes"] for g in ur["groups"] if g["reason"] == "entrance_outside_bbox")
            lines.append(f"unreachable_lanes (no path from a street): {ur['count']} "
                         f"[{ur['in_bbox_count']} with the entrance in the bbox, {outside} entered "
                         f"from outside it, {ur['return_lane_count']} return lanes; "
                         f"{len(ur['groups'])} group(s)] "
                         + ("PASS" if ur.get("pass") else "FAIL"))
        sl = report.get("junction_slivers") or {}
        if sl:
            lines.append(f"junction_slivers (< {sl['max_m']:.0f} m between two junctions): "
                         f"{sl['count']} " + ("PASS" if sl.get("pass") else "FAIL"))
        jl = report.get("junction_lane_links") or {}
        if jl:
            lines.append(f"junction_lane_links: {jl['unlinked_arms']} arm(s) with an unlinked lane "
                         + ("PASS" if jl.get("pass") else "FAIL"))
        rc = report.get("ramp_continuity") or {}
        if rc and rc.get("checked"):
            lines.append(f"ramp_continuity (taper gores): {rc['checked']} lane(s) walked, "
                         f"{rc['failures']} failure(s) " + ("PASS" if rc.get("pass") else "FAIL"))
        lm = report.get("landmarks") or {}
        lines.append(f"landmarks: {lm.get('count')} / {lm.get('expected_signals')} expected; "
                     f"crosswalk vertices {lm.get('crosswalk_vertices')}")
        ld = report.get("lane_in_drivable")
        lines.append("lane_in_drivable: " + ("null" if ld is None else
                     f"{ld['fraction']:.4f} ({ld['outside']} outside of {ld['total']}) "
                     f"{'PASS' if ld['pass'] else 'FAIL'}"))
        jc = report.get("junction_containment")
        lines.append("junction_containment: " + ("null" if jc is None else
                     f"{jc['fraction']} (ref-line {jc['reference_line_fraction']}) "
                     f"{'PASS' if jc['pass'] else 'FAIL'}"))
        ze = report.get("z_error")
        lines.append("z_error: " + ("null" if ze is None else
                     f"p50 {ze['p50']:.3f} p95 {ze['p95']:.3f} max {ze['max']:.3f} "
                     f"(surface z: {ze.get('surface_z', '?')}, elevation: {ze['elevation']}) "
                     f"{'PASS' if ze['pass'] else 'FAIL'}"))
        zd = report.get("z_error_dem")
        if zd is not None:
            lines.append(f"z_error_dem (info, vs raw DEM): p50 {zd['p50']:.3f} p95 {zd['p95']:.3f} "
                         f"max {zd['max']:.3f}")
        gs = report.get("grade_separation")
        if gs is not None:
            lines.append(f"grade_separation: min z gap {gs['min_z_gap_m']:.2f} m "
                         f"(p05 {gs['p05_z_gap_m']:.2f} m, {gs['crossing_waypoints']} crossing "
                         f"waypoints, need {gs['min_required_m']:.1f} m; worst "
                         f"{gs['worst']['upper_road']} over {gs['worst']['lower_road']}) "
                         f"{'PASS' if gs['pass'] else 'FAIL'}")
        sc = report.get("sidewalk_coverage")
        lines.append("sidewalk_coverage: " + ("null" if sc is None else
                     f"ratio {sc['ratio']:.3f} (sidewalk {sc['sidewalk_area']:.0f} m2 / "
                     f"drivable {sc['drivable_area']:.0f} m2)"))
    for n in report.get("notes", []):
        lines.append(f"note: {n}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m twinmodel.validate",
                                 description="Validate a TwinModel against its OpenDRIVE export")
    ap.add_argument("twin_dir", help="<name>.twin directory (TwinModel.save output)")
    ap.add_argument("xodr", help="OpenDRIVE file exported from the same model")
    ap.add_argument("--out", default=None, help="output dir for report.json/violations.geojson "
                                              "(default: twin_dir)")
    ap.add_argument("--step", type=float, default=1.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    model = TwinModel.load(args.twin_dir)
    xodr_text = Path(args.xodr).read_text()
    out = Path(args.out or args.twin_dir)
    report = validate(model, xodr_text, step=args.step, out_dir=out)
    write_report(report, out / "report.json")
    print(summary(report))
    print(f"report: {out / 'report.json'}")
    ok = report["topology"].get("loaded") and (report.get("lane_in_drivable") is None
                                               or report["lane_in_drivable"]["pass"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
