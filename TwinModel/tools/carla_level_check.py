"""Load a *baked* twin level by name in a running CARLA server (``client.load_world``), capture
cameras over the largest junctions, and run the same 20-vehicle Traffic Manager soak as
``carla_load_check.py`` (which uses the runtime OpenDriveGenerator mesh instead).

usage: python carla_level_check.py <build_dir> <name> --level <LevelName> [--out DIR]
       [--host localhost] [--port 4000] [--tm-port 8000]
(--out defaults to <build_dir>/carla_level; captures and carla_report.json are written there)
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carla  # noqa: E402
from twinmodel.model import TwinModel  # noqa: E402

FIXED_DT = 0.05


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def capture(world, bp_lib, transform: carla.Transform, path: Path, settle: int = 12,
            w: int = 1600, h: int = 900, fov: float = 70.0) -> None:
    bp = bp_lib.find("sensor.camera.rgb")
    bp.set_attribute("image_size_x", str(w))
    bp.set_attribute("image_size_y", str(h))
    bp.set_attribute("fov", str(fov))
    cam = world.spawn_actor(bp, transform)
    q: queue.Queue = queue.Queue()
    cam.listen(q.put)
    last = None
    try:
        for _ in range(settle):
            world.tick()
            try:
                while True:
                    last = q.get(timeout=2.0)
                    if q.empty():
                        break
            except queue.Empty:
                pass
        if last is None:
            log(f"  no frame received for {path.name}")
            return
        last.save_to_disk(str(path))
        log(f"  saved {path} (frame {last.frame})")
    finally:
        cam.stop()
        cam.destroy()


def ground_z(cmap, x: float, y: float, fallback: float = 0.0) -> float:
    wp = cmap.get_waypoint(carla.Location(x=x, y=y, z=fallback), project_to_road=True)
    return wp.transform.location.z if wp is not None else fallback


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("build_dir")
    ap.add_argument("name")
    ap.add_argument("--level", required=True, help="CARLA map name of the baked level (e.g. Eixample)")
    ap.add_argument("--out", default=None, help="output dir for captures/report (default: build_dir/carla_level)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--tm-port", type=int, default=8000)
    ap.add_argument("--vehicles", type=int, default=20)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--respawn", action="store_true", help="respawn vehicles lost at dead ends")
    args = ap.parse_args()
    src = Path(args.build_dir)
    out = Path(args.out) if args.out else src / "carla_level"
    out.mkdir(parents=True, exist_ok=True)
    model = TwinModel.load(src / f"{args.name}.twin")
    result: dict = {"level": args.level, "captures": {}, "notes": []}

    client = carla.Client(args.host, args.port)
    client.set_timeout(300.0)
    log(f"server {client.get_server_version()} client {client.get_client_version()}")

    avail = client.get_available_maps()
    result["available_maps"] = sorted(m.split("/")[-1] for m in avail)
    match = [m for m in avail if m.split("/")[-1].lower() == args.level.lower()]
    if not match:
        log(f"level {args.level} not in get_available_maps(): {result['available_maps']}")
        (out / "carla_report.json").write_text(json.dumps(result, indent=2))
        return 2
    t0 = time.perf_counter()
    world = client.load_world(match[0])
    result["load_seconds"] = round(time.perf_counter() - t0, 1)
    result["map_name"] = world.get_map().name
    log(f"load_world({match[0]}) done in {result['load_seconds']} s, map {world.get_map().name}")
    if len(world.get_map().to_opendrive()) < 1000:
        result["notes"].append("map has no OpenDRIVE content")

    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = FIXED_DT
    world.apply_settings(settings)
    for _ in range(5):
        world.tick()
    cmap = world.get_map()
    bp_lib = world.get_blueprint_library()

    t0 = time.perf_counter()
    wps = cmap.generate_waypoints(2.0)
    result["waypoints_2m"] = len(wps)
    result["driving_waypoints_2m"] = sum(1 for w in wps if w.lane_type == carla.LaneType.Driving)
    result["junction_waypoints_2m"] = sum(1 for w in wps if w.is_junction)
    result["roads"] = len({w.road_id for w in wps})
    result["junctions"] = len({w.junction_id for w in wps if w.is_junction})
    lms = cmap.get_all_landmarks()
    result["landmarks"] = len(lms)
    result["landmark_types"] = sorted({lm.type for lm in lms})
    result["topology_pairs"] = len(cmap.get_topology())
    result["spawn_points"] = len(cmap.get_spawn_points())
    zs = [w.transform.location.z for w in wps]
    result["waypoint_z_range"] = [round(min(zs), 2), round(max(zs), 2)]
    log(f"waypoints(2 m) {result['waypoints_2m']} (junction {result['junction_waypoints_2m']}), "
        f"landmarks {result['landmarks']} {result['landmark_types']}, topology {result['topology_pairs']}, "
        f"spawn points {result['spawn_points']}, z {result['waypoint_z_range']} "
        f"({time.perf_counter() - t0:.1f} s)")

    # --- junction top-downs ---------------------------------------------------------------
    js = sorted((j for j in model.junctions if j.polygon is not None), key=lambda j: -j.polygon.area)[:3]
    centres = {}
    for j in js:
        cx, cy = j.tags["centre"]
        gz = ground_z(cmap, cx, -cy, float(np.asarray(model.sample_z(cx, cy))))
        centres[j.id] = (cx, -cy, gz)
        tf = carla.Transform(carla.Location(x=cx, y=-cy, z=gz + 60.0), carla.Rotation(pitch=-90.0, yaw=0.0))
        p = out / f"carla_junction_{j.id}.png"
        log(f"capture junction {j.id} centre model ({cx:.1f},{cy:.1f}) carla z {gz:.2f}")
        capture(world, bp_lib, tf, p)
        result["captures"][f"junction_{j.id}"] = str(p)

    # --- avenue view (Passeig de Gracia) ----------------------------------------------------
    av = [r for r in model.roads if r.junction_id is None and "gr" in r.name.lower() and "passeig" in r.name.lower()]
    if not av:
        av = [r for r in model.roads if r.junction_id is None]
        result["notes"].append("no road named Passeig de Gracia; using the longest road for the avenue view")
    road = max(av, key=lambda r: r.length)
    c = np.asarray(road.reference_line.coords)
    i = len(c) // 2
    p0, p1 = c[max(i - 1, 0)], c[min(i + 1, len(c) - 1)]
    hdg = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))  # model heading CCW from +x
    # prefer looking toward the NE half-plane (SW->NE avenue)
    if math.cos(math.radians(hdg - 45.0)) < 0:
        hdg += 180.0
    mid = c[i]
    # camera slightly behind the mid point, 12 m up, pitched 35 deg down, along the avenue
    back = 25.0
    cx = mid[0] - back * math.cos(math.radians(hdg))
    cy = mid[1] - back * math.sin(math.radians(hdg))
    gz = ground_z(cmap, cx, -cy, float(mid[2]))
    av_tf = carla.Transform(carla.Location(x=cx, y=-cy, z=gz + 12.0),
                            carla.Rotation(pitch=-35.0, yaw=-hdg))
    log(f"avenue view along '{road.name}' ({road.id}) heading {hdg:.0f} deg from ({cx:.1f},{cy:.1f})")
    p = out / "carla_avenue.png"
    capture(world, bp_lib, av_tf, p)
    result["captures"]["avenue"] = str(p)
    result["avenue_road"] = {"id": road.id, "name": road.name, "heading_deg": round(hdg, 1)}

    # --- traffic manager soak ---------------------------------------------------------------
    tm = client.get_trafficmanager(args.tm_port)
    tm.set_synchronous_mode(True)
    tm.set_global_distance_to_leading_vehicle(2.5)
    tm.set_random_device_seed(7)
    random.seed(7)
    spawn_points = cmap.get_spawn_points()
    if len(spawn_points) < args.vehicles:
        result["notes"].append(f"only {len(spawn_points)} spawn points; sampling driving waypoints")
        cands = [w for w in wps if w.lane_type == carla.LaneType.Driving and not w.is_junction]
        random.shuffle(cands)
        for w in cands:
            tf = w.transform
            tf.location.z += 0.5
            spawn_points.append(tf)
            if len(spawn_points) >= 3 * args.vehicles:
                break
    random.shuffle(spawn_points)
    vbps = [b for b in bp_lib.filter("vehicle.*")
            if int(b.get_attribute("number_of_wheels")) == 4 and "carlacola" not in b.id
            and "firetruck" not in b.id and "ambulance" not in b.id and "sprinter" not in b.id]
    vehicles = []
    sensors = []
    collisions: list[dict] = []
    for tf in spawn_points:
        if len(vehicles) >= args.vehicles:
            break
        bp = random.choice(vbps)
        if bp.has_attribute("role_name"):
            bp.set_attribute("role_name", "autopilot")
        v = world.try_spawn_actor(bp, tf)
        if v is None:
            continue
        vehicles.append(v)
    world.tick()
    for v in vehicles:
        v.set_autopilot(True, args.tm_port)
        cbp = bp_lib.find("sensor.other.collision")
        s = world.spawn_actor(cbp, carla.Transform(), attach_to=v)

        def on_col(ev, vid=v.id):
            other = ev.other_actor
            loc = ev.transform.location
            imp = ev.normal_impulse
            collisions.append({"frame": ev.frame, "vehicle": vid,
                               "other": other.type_id if other is not None else None,
                               "other_id": other.id if other is not None else None,
                               "loc": [round(loc.x, 1), round(loc.y, 1), round(loc.z, 2)],
                               "impulse": round(math.sqrt(imp.x ** 2 + imp.y ** 2 + imp.z ** 2), 1)})
        s.listen(on_col)
        sensors.append(s)
    result["vehicles_spawned"] = len(vehicles)
    respawn_pool = list(spawn_points)

    def attach_collision(v):
        cbp = bp_lib.find("sensor.other.collision")
        s = world.spawn_actor(cbp, carla.Transform(), attach_to=v)

        def on_col(ev, vid=v.id):
            other = ev.other_actor
            loc = ev.transform.location
            imp = ev.normal_impulse
            collisions.append({"frame": ev.frame, "vehicle": vid,
                               "other": other.type_id if other is not None else None,
                               "other_id": other.id if other is not None else None,
                               "loc": [round(loc.x, 1), round(loc.y, 1), round(loc.z, 2)],
                               "impulse": round(math.sqrt(imp.x ** 2 + imp.y ** 2 + imp.z ** 2), 1)})
        s.listen(on_col)
        sensors.append(s)

    def respawn_one():
        random.shuffle(respawn_pool)
        for tf in respawn_pool:
            v = world.try_spawn_actor(random.choice(vbps), tf)
            if v is not None:
                return v
        return None
    respawned = 0
    result["spawn_points_used"] = min(len(spawn_points), 3 * args.vehicles)
    log(f"spawned {len(vehicles)} autopilot vehicles (TM port {args.tm_port}); ticking {args.frames} frames")

    # dead ends of the clipped lane graph (bbox edges), in CARLA coords
    dead_ends = []
    for r in model.roads:
        if r.junction_id is not None:
            continue
        c = np.asarray(r.reference_line.coords)
        if r.successor is None:
            dead_ends.append((r.id, "end", c[-1, 0], -c[-1, 1]))
        if r.predecessor is None:
            dead_ends.append((r.id, "start", c[0, 0], -c[0, 1]))
    jcentres = {j.id: (j.tags["centre"][0], -j.tags["centre"][1]) for j in model.junctions if "centre" in j.tags}

    def nearest_dead_end(loc):
        best = min(dead_ends, key=lambda d: math.hypot(d[2] - loc.x, d[3] - loc.y))
        return {"road": best[0], "contact": best[1], "distance": round(math.hypot(best[2] - loc.x, best[3] - loc.y), 1)}

    def nearest_junction(loc):
        jid, (jx, jy) = min(jcentres.items(), key=lambda kv: math.hypot(kv[1][0] - loc.x, kv[1][1] - loc.y))
        return {"junction": jid, "distance": round(math.hypot(jx - loc.x, jy - loc.y), 1)}

    z_floor = result["waypoint_z_range"][0] - 3.0
    start_pos = {v.id: v.get_location() for v in vehicles}
    path_len = {v.id: 0.0 for v in vehicles}
    last_pos = dict(start_pos)
    last_good = {v.id: v.get_location() for v in vehicles}
    lost: dict = {}
    in_junction = {v.id: None for v in vehicles}
    traversals = {v.id: 0 for v in vehicles}
    entries = {v.id: 0 for v in vehicles}
    junction_visits: dict = {}
    # stuck tracking: speed is sampled every 5 frames, so a sample is worth 5 frames
    stuck_run = {v.id: 0 for v in vehicles}          # frames in the current sub-0.5 m/s run
    stuck_longest = {v.id: 0 for v in vehicles}      # longest such run, in frames
    stuck_where: dict = {}                           # id -> location at the start of the longest run
    stuck_run_start = {v.id: None for v in vehicles}
    moving_hist = []
    t0 = time.perf_counter()
    for f in range(1, args.frames + 1):
        world.tick()
        if f % 5 == 0:
            n_mov = 0
            for v in vehicles:
                if v.id in lost:
                    continue
                if not v.is_alive:
                    lg = last_good[v.id]
                    lost[v.id] = {"frame": f, "reason": "destroyed", "last": [round(lg.x, 1), round(lg.y, 1), round(lg.z, 2)],
                                  "dead_end": nearest_dead_end(lg), "junction": nearest_junction(lg)}
                    log(f"  frame {f}: vehicle {v.id} destroyed; last seen ({lg.x:.1f},{lg.y:.1f},{lg.z:.1f}) "
                        f"dead end {lost[v.id]['dead_end']} junction {lost[v.id]['junction']}")
                    continue
                loc = v.get_location()
                if loc.z < z_floor:
                    lg = last_good[v.id]
                    lost[v.id] = {"frame": f, "reason": "fell", "z": round(loc.z, 1), "last": [round(lg.x, 1), round(lg.y, 1), round(lg.z, 2)],
                                  "dead_end": nearest_dead_end(lg), "junction": nearest_junction(lg)}
                    log(f"  frame {f}: vehicle {v.id} fell (z {loc.z:.1f}); last on road ({lg.x:.1f},{lg.y:.1f},{lg.z:.1f}) "
                        f"dead end {lost[v.id]['dead_end']} junction {lost[v.id]['junction']}")
                    continue
                last_good[v.id] = loc
                wp = cmap.get_waypoint(loc, project_to_road=True)
                jid = wp.junction_id if (wp is not None and wp.is_junction) else None
                prev = in_junction[v.id]
                if jid is not None and prev is None:
                    entries[v.id] += 1
                    junction_visits[jid] = junction_visits.get(jid, 0) + 1
                elif jid is None and prev is not None:
                    traversals[v.id] += 1
                in_junction[v.id] = jid
                vel5 = v.get_velocity()
                if math.hypot(vel5.x, vel5.y) < 0.5:
                    if stuck_run[v.id] == 0:
                        stuck_run_start[v.id] = loc
                    stuck_run[v.id] += 5
                    if stuck_run[v.id] > stuck_longest[v.id]:
                        stuck_longest[v.id] = stuck_run[v.id]
                        sl = stuck_run_start[v.id]
                        stuck_where[v.id] = [round(sl.x, 1), round(sl.y, 1), round(sl.z, 2)]
                else:
                    stuck_run[v.id] = 0
                if f % 25 == 0:
                    path_len[v.id] += loc.distance(last_pos[v.id])
                    last_pos[v.id] = loc
                    vel = v.get_velocity()
                    if math.hypot(vel.x, vel.y) > 0.5:
                        n_mov += 1
            if args.respawn:
                n_missing = sum(1 for v in vehicles if v.id in lost and not lost[v.id].get("replaced"))
                for v in vehicles:
                    if v.id in lost and not lost[v.id].get("replaced"):
                        nv = respawn_one()
                        lost[v.id]["replaced"] = True
                        if nv is None:
                            continue
                        nv.set_autopilot(True, args.tm_port)
                        attach_collision(nv)
                        vehicles.append(nv)
                        loc = nv.get_location()
                        start_pos[nv.id] = loc; path_len[nv.id] = 0.0; last_pos[nv.id] = loc
                        last_good[nv.id] = loc; in_junction[nv.id] = None
                        traversals[nv.id] = 0; entries[nv.id] = 0
                        stuck_run[nv.id] = 0; stuck_longest[nv.id] = 0
                        stuck_run_start[nv.id] = None
                        respawned += 1
            if f % 25 == 0:
                moving_hist.append((f, n_mov))
            if f % 100 == 0:
                log(f"  frame {f}: {n_mov}/{len(vehicles) - len(lost)} moving, {len(lost)} lost, "
                    f"{sum(traversals.values())} junction traversals, {len(collisions)} collision events, "
                    f"{(time.perf_counter() - t0) / f * 1000:.0f} ms/tick")
    result["tick_seconds"] = round(time.perf_counter() - t0, 1)
    result["lost"] = {str(k): v for k, v in lost.items()}
    result["lost_count"] = len(lost)
    result["respawned"] = respawned
    result["fleet_total"] = len(vehicles)
    result["lost_within_15m_of_dead_end"] = sum(1 for v in lost.values() if v["dead_end"]["distance"] <= 15.0)
    result["junction_entries"] = sum(entries.values())
    result["junction_traversals"] = sum(traversals.values())
    result["junction_visits_by_id"] = {str(k): v for k, v in junction_visits.items()}
    result["vehicles_that_traversed_a_junction"] = sum(1 for t in traversals.values() if t > 0)
    alive = [v for v in vehicles if v.is_alive]
    speeds = {}
    for v in alive:
        vel = v.get_velocity()
        speeds[v.id] = round(math.hypot(vel.x, vel.y), 2)
    # localise the collision events (nearest junction / dead end) for the report
    for cev in collisions:
        l = carla.Location(x=cev["loc"][0], y=cev["loc"][1], z=cev["loc"][2])
        cev["nearest_junction"] = nearest_junction(l)
        cev["nearest_dead_end"] = nearest_dead_end(l)
    result["vehicles_alive"] = len(alive)
    result["vehicles_moving_at_end"] = sum(1 for s in speeds.values() if s > 0.5)
    result["vehicles_travelled_gt_20m"] = sum(1 for v in vehicles if path_len[v.id] > 20.0)
    result["path_length_m"] = {str(k): round(v, 1) for k, v in path_len.items()}
    result["moving_history"] = moving_hist
    result["stuck_longest_frames"] = {str(k): v for k, v in stuck_longest.items()}
    result["vehicles_stuck_gt_200_frames"] = sum(1 for v in stuck_longest.values() if v > 200)
    result["stuck_max_frames"] = max(stuck_longest.values()) if stuck_longest else 0
    result["stuck_sites"] = [
        {"vehicle": str(k), "frames": stuck_longest[k], "loc": stuck_where[k],
         "nearest_junction": nearest_junction(carla.Location(x=stuck_where[k][0], y=stuck_where[k][1],
                                                             z=stuck_where[k][2]))}
        for k in sorted(stuck_longest, key=lambda i: -stuck_longest[i])[:5]
        if stuck_longest[k] > 0 and k in stuck_where]
    result["collision_events"] = len(collisions)
    result["collision_vehicles"] = len({c["vehicle"] for c in collisions})
    by_other: dict = {}
    for cev in collisions:
        by_other[cev["other"]] = by_other.get(cev["other"], 0) + 1
    result["collisions_by_other"] = by_other
    result["collisions_sample"] = collisions[:20]
    result["collisions_all"] = collisions
    log(f"lost {len(lost)} ({result['lost_within_15m_of_dead_end']} within 15 m of a dead end); junction entries "
        f"{result['junction_entries']}, traversals {result['junction_traversals']} by "
        f"{result['vehicles_that_traversed_a_junction']} vehicles; visits {junction_visits}")
    log(f"after {args.frames} frames: alive {len(alive)}/{len(vehicles)}, moving {result['vehicles_moving_at_end']}, "
        f"travelled >20 m {result['vehicles_travelled_gt_20m']}, collision events {len(collisions)} "
        f"({result['collision_vehicles']} vehicles) by other {by_other}; "
        f"stuck >200 frames {result['vehicles_stuck_gt_200_frames']} "
        f"(longest run {result['stuck_max_frames']} frames)")

    # --- collision hotspots (before teardown so the involved vehicles are still there) --------
    clusters: dict = {}
    for cev in collisions:
        key = (round(cev["loc"][0] / 10.0), round(cev["loc"][1] / 10.0))
        cl = clusters.setdefault(key, {"n": 0, "x": 0.0, "y": 0.0, "z": 0.0, "others": {}, "vehicles": set()})
        cl["n"] += 1; cl["x"] += cev["loc"][0]; cl["y"] += cev["loc"][1]; cl["z"] += cev["loc"][2]
        cl["others"][cev["other"]] = cl["others"].get(cev["other"], 0) + 1
        cl["vehicles"].add(cev["vehicle"])
    hot = sorted(clusters.values(), key=lambda c: -c["n"])[:3]
    result["collision_hotspots"] = []
    for k, cl in enumerate(hot):
        cx, cy, cz = cl["x"] / cl["n"], cl["y"] / cl["n"], cl["z"] / cl["n"]
        info = {"events": cl["n"], "centre": [round(cx, 1), round(cy, 1), round(cz, 2)], "others": cl["others"],
                "vehicles": sorted(cl["vehicles"]), "nearest_junction": nearest_junction(carla.Location(x=cx, y=cy, z=cz)),
                "model_xy": [round(cx, 1), round(-cy, 1)]}
        p = out / f"carla_collision_{k}.png"
        capture(world, bp_lib, carla.Transform(carla.Location(x=cx, y=cy, z=cz + 22.0), carla.Rotation(pitch=-90.0)), p, settle=8)
        info["capture"] = str(p)
        p2 = out / f"carla_collision_{k}_oblique.png"
        capture(world, bp_lib, carla.Transform(carla.Location(x=cx - 12.0, y=cy - 12.0, z=cz + 7.0), carla.Rotation(pitch=-28.0, yaw=45.0)), p2, settle=8)
        info["capture_oblique"] = str(p2)
        result["collision_hotspots"].append(info)
        log(f"  hotspot {k}: {cl['n']} events at carla ({cx:.1f},{cy:.1f}) others {cl['others']} vehicles {sorted(cl['vehicles'])}")

    # --- captures with traffic at frame N ----------------------------------------------------
    for jid, (cx, cy, gz) in centres.items():
        tf = carla.Transform(carla.Location(x=cx, y=cy, z=gz + 60.0), carla.Rotation(pitch=-90.0))
        p = out / f"carla_traffic_{jid}.png"
        capture(world, bp_lib, tf, p, settle=8)
        result["captures"][f"traffic_{jid}"] = str(p)
    # overview: whole bbox from ~300 m
    tf = carla.Transform(carla.Location(x=0.0, y=0.0, z=ground_z(cmap, 0.0, 0.0) + 330.0),
                         carla.Rotation(pitch=-90.0))
    p = out / "carla_traffic_overview.png"
    capture(world, bp_lib, tf, p, settle=8, fov=90.0)
    result["captures"]["traffic_overview"] = str(p)
    p = out / "carla_traffic_avenue.png"
    capture(world, bp_lib, av_tf, p, settle=8)
    result["captures"]["traffic_avenue"] = str(p)

    # --- teardown ----------------------------------------------------------------------------
    for s in sensors:
        try:
            s.stop()
            s.destroy()
        except Exception:
            pass
    for v in vehicles:
        try:
            v.set_autopilot(False, args.tm_port)
            v.destroy()
        except Exception:
            pass
    world.tick()
    settings.synchronous_mode = False
    settings.fixed_delta_seconds = None
    world.apply_settings(settings)
    tm.set_synchronous_mode(False)
    (out / "carla_report.json").write_text(json.dumps(result, indent=2))
    log(f"wrote {out / 'carla_report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
