"""Junction-quality metrics for a ``twinmodel build`` output directory.

    python -m tools.junction_metrics out/us_sunnyvale sunnyvale us_suburban

Reports the numbers the divided-carriageway work is judged on: junction count, connecting-road
length distribution, the worst junction area / (widest arm street width)^2, and the validator's
``lane_in_drivable`` / ``junction_containment`` / z error from ``report.json``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from twinmodel import profiles
from twinmodel.model import TwinModel
from twinmodel.surfaces import carriageway_extent


def metrics(build_dir: str | Path, name: str, profile: str) -> dict:
    profiles.activate(profile)
    m = TwinModel.load(Path(build_dir) / f"{name}.twin")
    rep_path = Path(build_dir) / "report.json"
    rep = json.loads(rep_path.read_text()) if rep_path.exists() else {}
    conn = [r for r in m.roads if r.junction_id is not None]
    lens = sorted(r.reference_line.length for r in conn)
    roads = {r.id: r for r in m.roads}

    ratios = []
    for j in m.junctions:
        if j.polygon is None or j.polygon.is_empty:
            continue
        arms = set()
        for c in j.connections:
            arms.add(c.incoming_road)
            cr = roads.get(c.connecting_road)
            if cr is not None and cr.successor is not None:
                arms.add(cr.successor.id)
        ws = [sum(carriageway_extent(roads[a])) for a in arms if a in roads]
        wmax = max(ws) if ws else 0.0
        if wmax > 0:
            ratios.append((j.polygon.area / wmax ** 2, j.id, j.polygon.area, wmax))
    ratios.sort(reverse=True)

    longest = sorted(((r.reference_line.length, r.id, r.junction_id) for r in conn), reverse=True)
    return {
        "junctions": len(m.junctions),
        "roads": sum(1 for r in m.roads if r.junction_id is None),
        "connecting_roads": len(conn),
        "conn_len_max": round(max(lens), 1) if lens else 0.0,
        "conn_len_p95": round(float(np.percentile(lens, 95)), 1) if lens else 0.0,
        "conn_len_p50": round(float(np.percentile(lens, 50)), 1) if lens else 0.0,
        "conn_len_over_40m": sum(1 for l in lens if l > 40.0),
        "worst_area_ratio": [(round(a, 2), jid, round(ar), round(w, 1)) for a, jid, ar, w in ratios[:5]],
        "lane_in_drivable": (rep.get("lane_in_drivable") or {}).get("fraction"),
        "junction_containment": (rep.get("junction_containment") or {}).get("fraction"),
        "z_p95": (rep.get("z_error") or {}).get("p95"),
        "dead_end_lanes": (rep.get("topology") or {}).get("dead_end_lane_count"),
        "waypoints": (rep.get("topology") or {}).get("waypoints"),
        "junctions_in_xodr": (rep.get("topology") or {}).get("junctions_in_xodr"),
        "dual_carriageways": (m.metadata.get("lanegraph") or {}).get("dual_carriageways"),
        "dual_pairs": (m.metadata.get("lanegraph") or {}).get("dual_carriageway_pairs"),
        "dual_merges_suppressed": (m.metadata.get("lanegraph") or {}).get("dual_merges_suppressed"),
        "longest_conn": [(round(l, 1), rid, jid) for l, rid, jid in longest[:8]],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print(__doc__)
        return 2
    print(json.dumps(metrics(argv[0], argv[1], argv[2]), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
