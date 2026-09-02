"""Does the Traffic Manager actually obey the twin's signals? A deterministic soak.

    python tools/tm_signal_soak.py --port 3000 --vehicles 20 --seed 7 --frames 900 \
        --out /tmp/soak.json

Attaches to a *running* server (one client, synchronous, ``fixed_delta_seconds`` 0.05 -- the
Traffic Manager needs sync mode, and ``ATrafficLightGroup`` only advances while somebody
ticks), spawns ``--vehicles`` autopilot vehicles from the map's spawn points with a fixed seed,
ticks ``--frames`` and measures, per tick and per vehicle:

* **red-light entries** -- the tick a vehicle crosses from a non-junction waypoint onto a
  junction one, with the state of the light governing it at that moment
  (``carla.Vehicle.get_traffic_light_state()``, which the ALSM fills from the *actor's* trigger
  boxes, i.e. from the ``<validity>`` the exporter writes). Entering on Red is a violation;
  entering on Green is the proof the plan lets somebody through.
* **stop-sign compliance** -- for every ``traffic.stop`` actor, the minimum speed each vehicle
  reached inside ``--stop-radius`` of it. A vehicle that got below ``--stop-speed`` there
  stopped; one that did not, rolled it.

Cleans up its vehicles and restores the previous world settings.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carla  # noqa: E402

FIXED_DT = 0.05
STATE_NAME = {carla.TrafficLightState.Red: "Red", carla.TrafficLightState.Yellow: "Yellow",
              carla.TrafficLightState.Green: "Green", carla.TrafficLightState.Off: "Off",
              carla.TrafficLightState.Unknown: "Unknown"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--tm-port", type=int, default=8000)
    ap.add_argument("--vehicles", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--stop-radius", type=float, default=3.0,
                    help="distance from a stop / give-way sign's trigger volume (its stop line) "
                         "within which a vehicle is counted as having met the sign")
    ap.add_argument("--stop-speed", type=float, default=0.3, help="m/s counted as stopped")
    ap.add_argument("--grace-ticks", type=int, default=20,
                    help="a red entry counts as running the light only if it was already red "
                         "this many ticks earlier (1 s at 0.05 s/tick)")
    ap.add_argument("--landmark-m", type=float, default=0.5,
                    help="a regulatory actor must sit this close to its 205/206 landmark")
    ap.add_argument("--out", default=None)
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
    tm = client.get_trafficmanager(args.tm_port)
    tm.set_synchronous_mode(True)
    tm.set_random_device_seed(args.seed)
    world.tick()

    rng = random.Random(args.seed)
    spawns = cmap.get_spawn_points()
    rng.shuffle(spawns)
    bps = [b for b in world.get_blueprint_library().filter("vehicle.*")
           if int(b.get_attribute("number_of_wheels")) == 4]
    bps.sort(key=lambda b: b.id)
    batch = []
    for i, sp in enumerate(spawns[:args.vehicles]):
        bp = bps[i % len(bps)]
        batch.append(carla.command.SpawnActor(bp, sp)
                     .then(carla.command.SetAutopilot(carla.command.FutureActor, True, args.tm_port)))
    ids = [r.actor_id for r in client.apply_batch_sync(batch, True) if not r.error]
    world.tick()
    vehicles = [a for a in world.get_actors(ids)]
    log(f"{len(vehicles)} vehicles on autopilot (seed {args.seed})")

    # regulatory signs of the unsignalised junctions, and the landmarks they must sit on
    stops = list(world.get_actors().filter("traffic.stop"))
    yields = list(world.get_actors().filter("traffic.yield"))
    # measure against the sign's *trigger volume* (the stop line on the carriageway), not the
    # pole: ATrafficSignBase spawns at the OpenDRIVE signal, which stands 3-5 m off the lane
    # centre outside the kerb, and no vehicle ever passes within a car length of it.
    def _stop_line(a):
        loc = a.get_location()
        try:
            tv = a.trigger_volume.location
            f = a.get_transform()
            return f.transform(carla.Location(tv.x, tv.y, tv.z))
        except Exception:
            return loc
    stop_pos = [(s.id, _stop_line(s)) for s in stops + yields]
    lm_pos = {}
    for lm in cmap.get_all_landmarks():
        if lm.type in ("205", "206"):
            lm_pos[str(lm.id)] = (lm.type, lm.transform.location)
    matched, unmatched = [], []
    for a in stops + yields:
        loc = a.get_location()
        best, bd = None, 1e9
        for sid, (ty, l) in lm_pos.items():
            d = ((loc.x - l.x) ** 2 + (loc.y - l.y) ** 2) ** 0.5
            if d < bd:
                best, bd = (sid, ty), d
        (matched if bd <= args.landmark_m else unmatched).append(
            {"actor": a.id, "type_id": a.type_id, "landmark": best[0] if best else None,
             "landmark_type": best[1] if best else None, "distance_m": round(bd, 3)})
    log(f"{len(stops)} traffic.stop + {len(yields)} traffic.yield actors; "
        f"{len(lm_pos)} 205/206 landmarks; {len(matched)} within {args.landmark_m} m")

    in_junction = {v.id: False for v in vehicles}
    # the light state each vehicle saw --grace-ticks ago: a car already past the stop line when
    # the light turns amber -> red is *inside* the junction on red without having run anything,
    # so a violation is an entry whose light was red a second earlier as well
    hist: dict[int, list] = {v.id: [] for v in vehicles}
    entries = []                      # {"vehicle", "frame", "state", "junction"}
    stop_min_speed: dict[tuple, float] = {}
    stop_seen: dict[tuple, int] = defaultdict(int)
    t0 = time.time()
    for f in range(1, args.frames + 1):
        world.tick()
        for v in vehicles:
            if not v.is_alive:
                continue
            loc = v.get_location()
            vel = v.get_velocity()
            speed = (vel.x ** 2 + vel.y ** 2 + vel.z ** 2) ** 0.5
            wp = cmap.get_waypoint(loc, project_to_road=True)
            now_junction = bool(wp and wp.is_junction)
            st = STATE_NAME.get(v.get_traffic_light_state(), "Unknown")
            h = hist[v.id]
            h.append(st)
            if len(h) > args.grace_ticks + 1:
                del h[0]
            if now_junction and not in_junction[v.id]:
                entries.append({"vehicle": v.id, "frame": f, "state": st, "state_before": h[0],
                                "junction": wp.get_junction().id if wp.get_junction() else -1})
            in_junction[v.id] = now_junction
            for sid, sloc in stop_pos:
                if loc.distance(sloc) <= args.stop_radius:
                    key = (v.id, sid)
                    stop_seen[key] += 1
                    stop_min_speed[key] = min(stop_min_speed.get(key, 1e9), speed)
    log(f"done in {time.time() - t0:.0f} s wall")

    by_state = defaultdict(int)
    for e in entries:
        by_state[e["state"]] += 1
    stopped = {k: v for k, v in stop_min_speed.items() if v < args.stop_speed}
    ran_red = [e for e in entries if e["state"] == "Red" and e["state_before"] == "Red"]
    report = {
        "map": cmap.name, "frames": args.frames, "seed": args.seed,
        "n_vehicles": len(vehicles),
        "n_junction_entries": len(entries),
        "junction_entries_by_light_state": dict(by_state),
        "n_red_entries": by_state.get("Red", 0),
        "n_ran_a_red": len(ran_red),
        "red_entries": [e for e in entries if e["state"] == "Red"][:50],
        "n_traffic_stop_actors": len(stops),
        "n_traffic_yield_actors": len(yields),
        "n_regulatory_landmarks": len(lm_pos),
        "n_regulatory_actors_on_a_landmark": len(matched),
        "regulatory_actors": matched + unmatched,
        "n_stop_encounters": len(stop_min_speed),
        "n_stop_encounters_with_a_full_stop": len(stopped),
        "stop_encounters": [{"vehicle": k[0], "stop": k[1], "min_speed": round(v, 3),
                             "ticks_within_radius": stop_seen[k]}
                            for k, v in sorted(stop_min_speed.items())][:80],
    }

    client.apply_batch_sync([carla.command.DestroyActor(v) for v in vehicles], True)
    world.tick()
    tm.set_synchronous_mode(False)
    world.apply_settings(prev)

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
        log(f"wrote {args.out}")
    print(f"  vehicles                     : {report['n_vehicles']}")
    print(f"  junction entries             : {report['n_junction_entries']} "
          + ", ".join(f"{n} on {k}" for k, n in sorted(by_state.items())))
    print(f"  entries on RED               : {report['n_red_entries']} "
          f"({report['n_ran_a_red']} already red {args.grace_ticks} ticks earlier = ran it)")
    print(f"  traffic.stop actors          : {report['n_traffic_stop_actors']}")
    print(f"  traffic.yield actors         : {report['n_traffic_yield_actors']}")
    print(f"  205/206 landmarks            : {report['n_regulatory_landmarks']}, "
          f"{report['n_regulatory_actors_on_a_landmark']} actors within {args.landmark_m} m")
    print(f"  stop encounters (<= {args.stop_radius:.1f} m)   : {report['n_stop_encounters']}, "
          f"{report['n_stop_encounters_with_a_full_stop']} with a full stop "
          f"(< {args.stop_speed} m/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
