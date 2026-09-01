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

from .model import TwinModel
from .export.xodr import build_id_map, read_twin_ids, sample_reference

log = logging.getLogger("twinmodel.validate")

CONTAINMENT_TOL = 0.05  # metres of slack around surfaces (mesh/xodr are independent samples)
Z_TOL = 0.05


def _to_model_xyz(location) -> tuple[float, float, float]:
    return float(location.x), -float(location.y), float(location.z)


def _union(geoms: list[BaseGeometry]) -> Optional[BaseGeometry]:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    return shapely.union_all(geoms) if geoms else None


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
        "landmarks": None,
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
        zs = np.asarray(model.sample_z(xyz[:, 0], xyz[:, 1]), dtype=np.float64)
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

    # sidewalk coverage -----------------------------------------------------------------
    sidewalk = _union([s.geometry for s in model.surfaces_of("sidewalk")])
    if drivable is not None:
        report["sidewalk_coverage"] = {
            "drivable_area": float(drivable.area),
            "sidewalk_area": float(sidewalk.area) if sidewalk is not None else 0.0,
            "ratio": float(sidewalk.area / drivable.area) if sidewalk is not None and drivable.area else 0.0,
        }

    report["violations"] = violations
    report["violation_count"] = len(violations)
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        write_violations(violations, out / "violations.geojson")
    return report


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
