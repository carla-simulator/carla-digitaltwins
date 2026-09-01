"""Look-verification captures for a baked twin level (or a reference town).

Loads a map by name on a running CARLA server, then captures street-level and oblique RGB
shots at deterministic poses derived from the map's spawn points (same pose *rule* on every
map, so a twin and Town10HD_Opt are comparable):

    python look_check.py --map Eixample --out out/look_eixample/iter1 [--port 4000]
    python look_check.py --map Town10HD_Opt --out out/look_eixample/town10_ref

Poses (k = 3 by default): spawn points at index 0, N/3, 2N/3 of get_spawn_points();
per pose a street shot (1.7 m up, horizontal, vehicle heading) and an oblique
(25 m up, 25 m back, pitch -35). Synchronous mode, ticks between captures.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import time
from pathlib import Path

import carla

FIXED_DT = 0.05


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def capture(world, bp_lib, transform: carla.Transform, path: Path, settle: int = 15,
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
            log(f"  no frame for {path.name}")
            return
        last.save_to_disk(str(path))
        log(f"  saved {path}")
    finally:
        cam.stop()
        cam.destroy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--poses", type=int, default=3)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    client = carla.Client(args.host, args.port)
    client.set_timeout(300.0)
    log(f"server {client.get_server_version()}")
    world = client.get_world()
    if world.get_map().name.split("/")[-1].lower() != args.map.lower():
        t0 = time.perf_counter()
        world = client.load_world(args.map)
        log(f"load_world({args.map}) in {time.perf_counter() - t0:.0f} s")
    else:
        log(f"map {args.map} already loaded")

    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = FIXED_DT
    world.apply_settings(settings)
    for _ in range(10):
        world.tick()
    bp_lib = world.get_blueprint_library()
    sps = world.get_map().get_spawn_points()
    log(f"{len(sps)} spawn points")
    if not sps:
        log("no spawn points; aborting")
        return 2

    n = len(sps)
    picks = sorted({0, n // 3, (2 * n) // 3})
    meta = {}
    try:
        for i in picks:
            sp = sps[i]
            yaw = sp.rotation.yaw
            loc = sp.location
            street = carla.Transform(
                carla.Location(loc.x, loc.y, loc.z + 1.7), carla.Rotation(pitch=-2.0, yaw=yaw))
            back = 25.0
            obl = carla.Transform(
                carla.Location(loc.x - back * math.cos(math.radians(yaw)),
                               loc.y - back * math.sin(math.radians(yaw)), loc.z + 25.0),
                carla.Rotation(pitch=-35.0, yaw=yaw))
            for tag, tf in (("street", street), ("oblique", obl)):
                p = out / f"{args.map.lower()}_sp{i}_{tag}.png"
                capture(world, bp_lib, tf, p)
                meta[p.name] = {"spawn_index": i, "pose": tag,
                                "xyz": [loc.x, loc.y, loc.z], "yaw": yaw}
    finally:
        settings.synchronous_mode = False
        settings.fixed_delta_seconds = None
        world.apply_settings(settings)
    (out / "captures.json").write_text(json.dumps(meta, indent=1))
    log(f"done -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
