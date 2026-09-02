"""Audit the traffic lights of a *running* CARLA server (one client, synchronous).

    python tools/tl_audit.py --port 3000 --out /tmp/tl_base.json [--frames 900]

Never loads a world: it attaches to whatever is running (stop every other client first --
``ATrafficLightGroup`` only advances while somebody ticks, and two tickers desynchronise the
audit). Reports, as JSON and as a short console summary:

  * ``traffic.traffic_light`` actor count, and per actor ``get_opendrive_id()``,
    ``len(get_stop_waypoints())``, ``len(get_affected_lane_waypoints())`` with each waypoint's
    (road_id, lane_id, is_junction), ``len(get_light_boxes())``,
    ``[a.id for a in get_group_traffic_lights()]``, ``get_pole_index()`` and the matching
    ``carla.Landmark`` (orientation, validities, road, s);

Read ``n_stop_waypoints``, not ``n_light_boxes``, to ask "does this light stop anybody":
``get_stop_waypoints`` sweeps the actor's **trigger volume** (client/TrafficLight.cpp:115), so
it is 0 exactly when ``UTrafficLightComponent::InitializeSign`` built no box. ``get_light_boxes``
does *not* return trigger volumes -- it is
``UBoundingBoxCalculator::GetTrafficLightBoundingBox``, the bounding boxes of the mesh
components carrying the ``TrafficLight`` semantic tag -- and a baked digital-twin rig carries
no such tag, so it is 0 whether or not the light works.

``lanes_on_oncoming_side`` counts affected lane waypoints that sit on the far side of the
travel direction the landmark's orientation implies (right-hand traffic: '+' -> negative
lanes). Every one of them is a lane the light claims to govern but no vehicle drives on.
  * group -> junction map, derived from the affected waypoints;
  * a ``--frames``-tick state trace: per-actor transitions, and per group the distinct sets of
    simultaneously-green members ("stages") with how many ticks each was observed.

It asserts nothing -- it is the measuring stick the gates read.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carla  # noqa: E402

FIXED_DT = 0.05
STATE_NAME = {carla.TrafficLightState.Red: "Red", carla.TrafficLightState.Yellow: "Yellow",
              carla.TrafficLightState.Green: "Green", carla.TrafficLightState.Off: "Off",
              carla.TrafficLightState.Unknown: "Unknown"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _state(tl) -> str:
    return STATE_NAME.get(tl.get_state(), str(tl.get_state()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--out", default=None, help="report JSON path")
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--keep-sync", action="store_true",
                    help="leave the server in synchronous mode when done")
    args = ap.parse_args()

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = client.get_world()
    cmap = world.get_map()
    log(f"world {cmap.name}")

    prev = world.get_settings()
    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = FIXED_DT
    world.apply_settings(settings)
    world.tick()

    landmarks = {}
    for lm in cmap.get_all_landmarks():
        landmarks[str(lm.id)] = {
            "type": lm.type, "road_id": lm.road_id, "s": lm.s, "t": lm.t,
            "orientation": str(lm.orientation),
            "validities": [list(v) for v in lm.get_lane_validities()],
        }

    tls = sorted(world.get_actors().filter("traffic.traffic_light"), key=lambda a: a.id)
    log(f"{len(tls)} traffic.traffic_light actors")

    lights = []
    group_of: dict[int, int] = {}
    for tl in tls:
        try:
            odid = str(tl.get_opendrive_id())
        except Exception:
            odid = ""
        try:
            boxes = tl.get_light_boxes()
        except Exception:
            boxes = []
        try:
            wps = tl.get_affected_lane_waypoints()
        except Exception:
            wps = []
        try:
            grp = sorted(a.id for a in tl.get_group_traffic_lights())
        except Exception:
            grp = [tl.id]
        gkey = min(grp) if grp else tl.id
        group_of[tl.id] = gkey
        lanes = [{"road_id": w.road_id, "lane_id": w.lane_id, "s": round(w.s, 2),
                  "junction": w.is_junction} for w in wps]
        lights.append({
            "actor_id": tl.id, "opendrive_id": odid, "type_id": tl.type_id,
            "n_light_boxes": len(boxes), "n_affected_lane_waypoints": len(wps),
            "affected_lanes": lanes,
            "n_stop_waypoints": len(tl.get_stop_waypoints()),
            "pole_index": tl.get_pole_index(),
            "group": grp, "group_key": gkey,
            "location": [round(tl.get_location().x, 2), round(tl.get_location().y, 2),
                         round(tl.get_location().z, 2)],
            "landmark": landmarks.get(odid),
            "green_time": tl.get_green_time(), "yellow_time": tl.get_yellow_time(),
            "red_time": tl.get_red_time(),
        })

    groups: dict[int, list[int]] = defaultdict(list)
    for l in lights:
        groups[l["group_key"]].append(l["actor_id"])
    # junction of a group: the junction ids reached by its members' affected waypoints
    group_junctions: dict[int, list[int]] = {}
    for gk, members in groups.items():
        js = set()
        for l in lights:
            if l["actor_id"] not in members:
                continue
            for w in l["affected_lanes"]:
                wp = cmap.get_waypoint_xodr(w["road_id"], w["lane_id"], w["s"])
                nxt = wp.next(12.0) if wp is not None else []
                for n in nxt:
                    if n.is_junction and n.get_junction() is not None:
                        js.add(n.get_junction().id)
        group_junctions[gk] = sorted(js)

    # ---- state trace
    log(f"ticking {args.frames} frames ({args.frames * FIXED_DT:.0f} s sim)")
    by_id = {tl.id: tl for tl in tls}
    last: dict[int, str] = {i: _state(tl) for i, tl in by_id.items()}
    transitions: dict[int, list] = {i: [[0, last[i]]] for i in by_id}
    n_trans = Counter()
    green_sets: dict[int, Counter] = {gk: Counter() for gk in groups}
    state_ticks: dict[int, Counter] = {i: Counter() for i in by_id}
    t0 = time.time()
    for f in range(1, args.frames + 1):
        world.tick()
        cur = {i: _state(tl) for i, tl in by_id.items()}
        for i, s in cur.items():
            state_ticks[i][s] += 1
            if s != last[i]:
                transitions[i].append([f, s])
                n_trans[i] += 1
                last[i] = s
        for gk, members in groups.items():
            key = tuple(sorted(by_id[m].get_opendrive_id() for m in members if cur[m] == "Green"))
            green_sets[gk][key] += 1
    log(f"done in {time.time() - t0:.0f} s wall")

    zero_box = [l["opendrive_id"] for l in lights if l["n_light_boxes"] == 0]
    no_trigger = [l["opendrive_id"] for l in lights if l["n_stop_waypoints"] == 0]
    oncoming = []
    for l in lights:
        o = (l.get("landmark") or {}).get("orientation", "")
        if o not in ("Positive", "Negative"):
            continue
        want_negative = o == "Positive"
        for w in l["affected_lanes"]:
            if (w["lane_id"] < 0) != want_negative:
                oncoming.append({"signal": l["opendrive_id"], "road_id": w["road_id"],
                                 "lane_id": w["lane_id"], "orientation": o})
    report = {
        "map": cmap.name,
        "port": args.port,
        "frames": args.frames,
        "n_lights": len(lights),
        "n_lights_no_trigger_volume": len(no_trigger),
        "lights_no_trigger_volume": sorted(no_trigger),
        "n_affected_lane_waypoints": sum(l["n_affected_lane_waypoints"] for l in lights),
        "n_lanes_on_oncoming_side": len(oncoming),
        "lanes_on_oncoming_side": oncoming,
        "n_lights_zero_boxes": len(zero_box),
        "lights_zero_boxes": sorted(zero_box),
        "n_duplicate_opendrive_ids": len(lights) - len({l["opendrive_id"] for l in lights}),
        "duplicate_opendrive_ids": sorted(
            k for k, v in Counter(l["opendrive_id"] for l in lights).items() if v > 1),
        "n_groups": len(groups),
        "groups": {str(gk): {"members": sorted(m),
                             "opendrive_ids": sorted(by_id[a].get_opendrive_id() for a in m),
                             "junctions": group_junctions.get(gk, []),
                             "green_sets": [{"green": list(k), "ticks": n}
                                            for k, n in green_sets[gk].most_common()],
                             "n_distinct_green_sets": len(
                                 [k for k in green_sets[gk] if k]),
                             }
                   for gk, m in sorted(groups.items())},
        "lights": lights,
        "transitions": {str(i): t for i, t in transitions.items()},
        "state_ticks": {str(i): dict(c) for i, c in state_ticks.items()},
    }

    if not args.keep_sync:
        world.apply_settings(prev)

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
        log(f"wrote {args.out}")

    print(f"  lights                       : {report['n_lights']}")
    print(f"  with NO trigger volume       : {report['n_lights_no_trigger_volume']}")
    print(f"  affected lanes on oncoming   : {report['n_lanes_on_oncoming_side']} "
          f"/ {report['n_affected_lane_waypoints']}")
    print(f"  duplicate opendrive ids      : {report['n_duplicate_opendrive_ids']}")
    print(f"  groups                       : {report['n_groups']}")
    sw_hist = Counter(l["n_stop_waypoints"] for l in lights)
    print("  stop wps / light             : " +
          ", ".join(f"{n}x{k}" for k, n in sorted(sw_hist.items())))
    wp_hist = Counter(l["n_affected_lane_waypoints"] for l in lights)
    print("  affected lane wps / light    : " +
          ", ".join(f"{n}x{k}" for k, n in sorted(wp_hist.items())))
    stage_hist = Counter(g["n_distinct_green_sets"] for g in report["groups"].values())
    print("  distinct green sets / group  : " +
          ", ".join(f"{n}x{k}" for k, n in sorted(stage_hist.items())))
    allgreen = sum(1 for g in report["groups"].values()
                   if any(len(gs["green"]) == len(g["members"]) and gs["green"]
                          for gs in g["green_sets"]))
    print(f"  groups all-green at once     : {allgreen} / {report['n_groups']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
