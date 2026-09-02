"""Offline audit of the traffic-light representation of an OpenDRIVE file (lxml only).

    python tools/xodr_tl_check.py out/v10_eixample/eixample.xodr [--json report.json] [--quiet]

Reports, per signalised junction: how many ``<controller>`` refs it carries, how many signals
each controller holds, and per traffic-light ``<signal>`` how many ``<validity>`` children it
has and whether those lanes are drivable and on the travel side its ``orientation`` implies.

Why each check exists (LibCarla, ``carla-ue58-dtupgrade/LibCarla/source/carla/``):

* ``road/MapBuilder.cpp:870`` ``SolveControllerAndJuntionReferences`` is the *only* thing that
  fills ``Signal::GetControllers()``; it walks ``<junction><controller>`` refs. A root-level
  ``<controller>`` that no junction references leaves all its signals uncontrolled, and
  ``ATrafficLightManager::RegisterLightComponentFromOpenDRIVE`` then drops them into a
  throwaway group. -> C_ORPHAN / C_NOJUNCTION.
* ``road/MapBuilder.cpp:992`` ``GenerateDefaultValiditiesForSignalReferences`` synthesises a
  validity for any reference that has none -- and it maps ``orientation="+"`` to lanes
  ``[1, max]``, i.e. the *oncoming* side under right-hand traffic. On a twin approach road
  (sidewalk at +1, driving at -1..-n) that yields no ``Driving`` lane at all, so
  ``UTrafficLightComponent::InitializeSign`` builds zero trigger boxes and the light stops
  nobody. -> V_MISSING is therefore a correctness violation, not a granularity nicety.
* ``road/MapBuilder.cpp:1059`` ``RemoveZeroLaneValiditySignalReferences`` deletes any reference
  whose validities are all ``from == 0 && to == 0``. -> V_ZERO.
* ``TrafficLightComponent.cpp:63`` skips lanes that are not ``LaneType::Driving``. -> V_TYPE.
* OpenDRIVE right-hand traffic: vehicles travelling in +s use the negative (right) lanes, so a
  signal facing them carries ``orientation="+"`` and must validate negative lanes. -> V_SIDE.
* ``JunctionParser.cpp:71`` reads only ``id`` from ``<junction><controller>``; ``sequence`` is
  discarded and ``ATrafficLightGroup::NextController`` round-robins in *insertion* order. So
  the emitted order must already be the intended stage order. -> C_SEQ.

Exit 1 if any violation is found (a baseline twin xodr is expected to fail V_MISSING).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from lxml import etree

TL_TYPE = "1000001"
# extra dynamic signal types this project may introduce (arrow / pedestrian heads)
TL_TYPES_EXTRA = ("1000002",)


def _lane_types_at(road_el, s: float) -> dict[int, str]:
    """lane id -> type for the laneSection covering ``s`` (the last one starting at or before)."""
    lanes = road_el.find("lanes")
    if lanes is None:
        return {}
    secs = sorted(lanes.findall("laneSection"), key=lambda e: float(e.get("s", 0.0)))
    if not secs:
        return {}
    sec = secs[0]
    for c in secs:
        if float(c.get("s", 0.0)) <= s + 1e-6:
            sec = c
    out: dict[int, str] = {}
    for side in ("left", "center", "right"):
        el = sec.find(side)
        if el is None:
            continue
        for ln in el.findall("lane"):
            out[int(ln.get("id"))] = ln.get("type", "none")
    return out


def _validities(sig_el) -> list[tuple[int, int]]:
    return [(int(v.get("fromLane")), int(v.get("toLane"))) for v in sig_el.findall("validity")]


def check(xodr_path: str | Path) -> dict:
    root = etree.parse(str(xodr_path)).getroot()

    roads = {r.get("id"): r for r in root.iter("road")}
    # traffic-light signals, keyed by signal id (ids are unique across the file by construction)
    signals: dict[str, dict] = {}
    for rid, r in roads.items():
        sigs = r.find("signals")
        if sigs is None:
            continue
        for s in sigs.findall("signal"):
            stype = s.get("type", "")
            if stype != TL_TYPE and stype not in TL_TYPES_EXTRA:
                continue
            signals[s.get("id")] = {
                "id": s.get("id"), "type": stype, "road_id": rid, "s": float(s.get("s", 0.0)),
                "t": float(s.get("t", 0.0)), "orientation": s.get("orientation", "+"),
                "name": s.get("name", ""), "validities": _validities(s),
            }

    # root controllers, in document order
    controllers: dict[str, dict] = {}
    ctl_order: list[str] = []
    for c in root.findall("controller"):
        cid = c.get("id")
        controllers[cid] = {"id": cid, "sequence": c.get("sequence"),
                            "signal_ids": [x.get("signalId") for x in c.findall("control")]}
        ctl_order.append(cid)

    # junction -> controller refs, in emission order (== the runtime's stage order)
    junc_ctl: dict[str, list[str]] = {}
    for j in root.iter("junction"):
        junc_ctl[j.get("id")] = [c.get("id") for c in j.findall("controller")]
    ctl_junction: dict[str, str] = {}
    for jid, cids in junc_ctl.items():
        for cid in cids:
            ctl_junction.setdefault(cid, jid)

    sig_ctl: dict[str, list[str]] = defaultdict(list)
    for cid, c in controllers.items():
        for sid in c["signal_ids"]:
            sig_ctl[sid].append(cid)

    violations: list[dict] = []

    def bad(code: str, **kw):
        violations.append({"code": code, **kw})

    # ---- controller structure
    for cid in ctl_order:
        if cid not in ctl_junction:
            bad("C_NOJUNCTION", controller=cid,
                detail="root <controller> not referenced by any <junction><controller>; "
                       "Signal::GetControllers() stays empty (MapBuilder.cpp:870)")
    for jid, cids in junc_ctl.items():
        for cid in cids:
            if cid not in controllers:
                bad("C_ORPHAN", junction=jid, controller=cid,
                    detail="<junction><controller> ref with no root <controller>")
        seqs = [controllers[c]["sequence"] for c in cids if c in controllers]
        nums = []
        for s in seqs:
            try:
                nums.append(int(s))
            except (TypeError, ValueError):
                nums.append(None)
        if len(nums) > 1 and all(n is not None for n in nums) and nums != sorted(nums):
            bad("C_SEQ", junction=jid, controllers=cids, sequences=seqs,
                detail="emission order != sequence order; the runtime round-robins in "
                       "emission order (JunctionParser.cpp:71 drops sequence)")

    # ---- per signal
    for sid, sig in sorted(signals.items()):
        cids = sig_ctl.get(sid, [])
        if not cids:
            bad("S_NOCTL", signal=sid, detail="traffic light in no <controller>")
        elif len(cids) > 1:
            bad("S_MULTICTL", signal=sid, controllers=cids,
                detail="RegisterLightComponentFromOpenDRIVE takes *begin() of a std::set, so "
                       "the effective controller is lexicographically arbitrary")
        if not sig["validities"]:
            bad("V_MISSING", signal=sid, road=sig["road_id"], orientation=sig["orientation"],
                detail="no <validity>; CARLA synthesises one for the wrong travel side "
                       "(MapBuilder.cpp:992) -> zero trigger boxes on a twin approach")
            continue
        road_el = roads.get(sig["road_id"])
        ltypes = _lane_types_at(road_el, sig["s"]) if road_el is not None else {}
        want_negative = sig["orientation"] == "+"
        for (a, b) in sig["validities"]:
            if a == 0 and b == 0:
                bad("V_ZERO", signal=sid, detail="fromLane=0 toLane=0 is dropped by "
                                                 "RemoveZeroLaneValiditySignalReferences")
                continue
            lo, hi = (a, b) if a <= b else (b, a)
            for lane in range(lo, hi + 1):
                if lane == 0:
                    continue
                lt = ltypes.get(lane)
                if lt is None:
                    bad("V_NOLANE", signal=sid, road=sig["road_id"], lane=lane,
                        detail="validity names a lane the road does not have at s")
                    continue
                if lt != "driving":
                    bad("V_TYPE", signal=sid, road=sig["road_id"], lane=lane, lane_type=lt,
                        detail="InitializeSign skips non-Driving lanes -> no trigger box")
                if sig["orientation"] in ("+", "-"):
                    on_own_side = (lane < 0) if want_negative else (lane > 0)
                    if not on_own_side:
                        bad("V_SIDE", signal=sid, road=sig["road_id"], lane=lane,
                            orientation=sig["orientation"],
                            detail="RHT: orientation '+' validates the negative (right) lanes")

    # ---- summary
    per_junction = {}
    for jid, cids in sorted(junc_ctl.items(), key=lambda kv: int(kv[0])):
        sig_ids = [s for c in cids for s in controllers.get(c, {}).get("signal_ids", [])]
        if not sig_ids and not cids:
            continue
        per_junction[jid] = {
            "n_controllers": len(cids),
            "controllers": [{"id": c, "sequence": controllers.get(c, {}).get("sequence"),
                             "n_signals": len(controllers.get(c, {}).get("signal_ids", [])),
                             "signals": controllers.get(c, {}).get("signal_ids", [])}
                            for c in cids],
            "n_signals": len(sig_ids),
        }
    n_val = sum(len(s["validities"]) for s in signals.values())
    report = {
        "xodr": str(xodr_path),
        "n_traffic_light_signals": len(signals),
        "n_validity_elements": n_val,
        "n_signals_without_validity": sum(1 for s in signals.values() if not s["validities"]),
        "n_root_controllers": len(controllers),
        "n_signalised_junctions": len(per_junction),
        "controllers_per_junction": {j: v["n_controllers"] for j, v in per_junction.items()},
        "junctions": per_junction,
        "violations": violations,
        "violation_counts": {c: sum(1 for v in violations if v["code"] == c)
                             for c in sorted({v["code"] for v in violations})},
        "signals": signals,
    }
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xodr")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--max-print", type=int, default=20)
    a = ap.parse_args(argv)

    rep = check(a.xodr)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1))
    if not a.quiet:
        print(f"{rep['xodr']}")
        print(f"  traffic-light signals : {rep['n_traffic_light_signals']}")
        print(f"  <validity> elements   : {rep['n_validity_elements']} "
              f"({rep['n_signals_without_validity']} signals with none)")
        print(f"  root <controller>     : {rep['n_root_controllers']}")
        print(f"  signalised junctions  : {rep['n_signalised_junctions']}")
        cpj = rep["controllers_per_junction"]
        if cpj:
            hist: dict[int, int] = defaultdict(int)
            for n in cpj.values():
                hist[n] += 1
            print("  controllers/junction  : " +
                  ", ".join(f"{n}x{k}" for k, n in sorted(hist.items())))
        for code, n in rep["violation_counts"].items():
            print(f"  VIOLATION {code:<12} {n}")
        for v in rep["violations"][:a.max_print]:
            print(f"    {v['code']}: " + " ".join(f"{k}={v[k]}" for k in v if k != "detail"))
        if len(rep["violations"]) > a.max_print:
            print(f"    ... {len(rep['violations']) - a.max_print} more")
    return 1 if rep["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
