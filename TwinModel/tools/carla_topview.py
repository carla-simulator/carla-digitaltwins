"""Capture a top-down orthographic-ish photo mosaic of a baked twin map from a *running* CARLA
server, so the region-review page can use the real render as its base layer instead of vectors.

    python tools/carla_topview.py --map Sunnyvale --twin out/v8_sunnyvale/sunnyvale.twin \
        --port 3000 --out out/review

The map bounds come from the twin itself (the same ``region_review_page.extract_twin`` bounds the
page draws with) padded out to the 250 m material-tile grid, so the mosaic and the vectors share
one model-frame rectangle.  One tile is captured per grid cell:

  * the spectator is parked a few metres over the tile centre first -- it is the World Partition
    streaming source, so nothing is captured before the cell has streamed in;
  * a downward ``sensor.camera.rgb`` sits ``--alt`` metres above the tile's ground with a narrow
    ``--fov``: at 300 m / 50 deg the footprint is 280 m for a 250 m tile, so a 20 m building leans
    by 6.7 % of its offset from the tile centre instead of the ~35 % a 70 deg lens at 70 m would
    give.  Ground features -- the ones the review is about -- do not lean at all.  Going higher
    than ~300 m is counter-productive: World Partition swaps in HLOD proxies and the lane markings,
    crosswalks and curbs disappear into a blur (measured: markings gone by 500 m, everything a
    smear plus atmospheric haze by 1000 m);
  * frames are ticked until two consecutive ones are near-identical (streaming/LOD settled);
  * the exact central 250 m x 250 m is cropped with ``px_per_m = res / (2*alt*tan(fov/2))`` and
    resampled to the requested ground resolution;
  * finally the tiles are levelled against each other (UE exposes every tile on its own content,
    which leaves a 3-5 % step at every seam) before they are pasted into the mosaic.

Orientation.  A CARLA camera at ``pitch=-90`` has image-right = its right vector and image-up = its
pre-pitch forward vector.  At ``yaw=-90`` those are ``+X`` and ``-Y``, i.e. model east and model
north (``model_to_ue`` is ``(x, -y)``), which is exactly the page's north-up model frame.  ``--yaw``
and ``--flip``/``--transpose`` exist to re-derive that empirically if a build ever disagrees.

Outputs (under ``--out``):

    topview_<Map>.png    lossless mosaic, north up, model-frame rectangle
    topview_<Map>.jpg    the same image as JPEG, what the page embeds
    topview_<Map>.json   {"bounds": [xmin,ymin,xmax,ymax], "px_per_m", "size", ...}
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import queue
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

log = logging.getLogger("topview")

TILE = 250.0  # baker material tile size, model metres


def _load_region_review():
    """Import the sibling page generator (shared bounds + model->CARLA flip)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("region_review_page", here / "region_review_page.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------ geometry

def tile_grid(bounds: Sequence[float], tile: float = TILE) -> tuple[float, float, int, int]:
    """Model-frame bounds -> (x0, y0, nx, ny) of the enclosing ``tile`` grid (floor/ceil)."""
    x0 = math.floor(bounds[0] / tile) * tile
    y0 = math.floor(bounds[1] / tile) * tile
    x1 = math.ceil(bounds[2] / tile) * tile
    y1 = math.ceil(bounds[3] / tile) * tile
    return x0, y0, int(round((x1 - x0) / tile)), int(round((y1 - y0) / tile))


def footprint(alt: float, fov: float) -> float:
    """Ground width seen by a downward pinhole camera at ``alt`` metres with horizontal ``fov``."""
    return 2.0 * alt * math.tan(math.radians(fov) / 2.0)


class Elevation:
    """``elevation.npz`` sampler: bilinear ground z in model metres, NaN-safe."""

    def __init__(self, path: Path):
        d = np.load(path)
        self.z = np.asarray(d["z"], dtype=float)
        self.x0, self.y0 = float(d["x0"]), float(d["y0"])
        self.dx, self.dy = float(d["dx"]), float(d["dy"])
        finite = self.z[np.isfinite(self.z)]
        self.median = float(np.median(finite)) if finite.size else 0.0

    def at(self, x: float, y: float) -> float:
        ny, nx = self.z.shape
        fx = (x - self.x0) / self.dx
        fy = (y - self.y0) / self.dy
        i = min(max(int(math.floor(fx)), 0), nx - 2) if nx > 1 else 0
        j = min(max(int(math.floor(fy)), 0), ny - 2) if ny > 1 else 0
        tx = min(max(fx - i, 0.0), 1.0)
        ty = min(max(fy - j, 0.0), 1.0)
        c = [self.z[j, i], self.z[j, i + 1] if nx > 1 else self.z[j, i],
             self.z[j + 1, i] if ny > 1 else self.z[j, i],
             self.z[j + 1, i + 1] if nx > 1 and ny > 1 else self.z[j, i]]
        c = [self.median if not np.isfinite(v) else float(v) for v in c]
        return ((c[0] * (1 - tx) + c[1] * tx) * (1 - ty) + (c[2] * (1 - tx) + c[3] * tx) * ty)


# -------------------------------------------------------------------------------------- capture

VOID = 20  # luminance at or below this is the empty sky outside the map, not content


def exposure_gains(tiles: dict[tuple[int, int], np.ndarray], lo: float = 0.93, hi: float = 1.07,
                   min_cover: float = 0.35) -> dict[tuple[int, int], float]:
    """Per-tile brightness gains that flatten the seams UE's eye adaptation leaves behind.

    Each tile is exposed on its own content, which puts a 3-5 % step across every tile boundary.
    Only tiles that are mostly *inside* the map take part: a tile that is 90 % empty sky has a
    meaningless median and asking it to match the others would just brighten its few real pixels.
    The correction is clamped, so a genuinely dark or bright tile stays dark or bright.
    """
    meds: dict[tuple[int, int], float] = {}
    for key, arr in tiles.items():
        lum = arr.mean(axis=2)
        content = lum > VOID
        if content.mean() < min_cover:
            continue
        meds[key] = float(np.median(lum[content]))
    if len(meds) < 2:
        return {k: 1.0 for k in tiles}
    target = float(np.median(list(meds.values())))
    out = {k: 1.0 for k in tiles}
    for key, med in meds.items():
        out[key] = min(hi, max(lo, target / med)) if med > 1e-3 else 1.0
    return out


def _to_array(image) -> np.ndarray:
    """``carla.Image`` -> HxWx3 uint8 RGB."""
    buf = np.frombuffer(image.raw_data, dtype=np.uint8)
    return buf.reshape((image.height, image.width, 4))[:, :, 2::-1].copy()


def _settle(world, sink: "queue.Queue", cap: int, tol: float, warm: int) -> np.ndarray | None:
    """Tick until two consecutive frames differ by less than ``tol`` mean absolute level."""
    prev = None
    last = None
    for n in range(cap):
        world.tick()
        try:
            img = sink.get(timeout=20.0)
        except queue.Empty:
            continue
        while not sink.empty():
            img = sink.get_nowait()
        last = _to_array(img)
        small = last[::8, ::8].astype(np.int16)
        if prev is not None and n >= warm:
            if float(np.abs(small - prev).mean()) < tol:
                return last
        prev = small
    return last


def capture_map(client, carla, *, name: str, twin_dir: Path, bounds: Sequence[float],
                out_dir: Path, cm_per_px: float, alt: float, fov: float, res: int,
                yaw: float, settle_cap: int, settle_tol: float, warm_ticks: int,
                spectator_ticks: int, only: set[tuple[int, int]] | None = None,
                jpeg_quality: int = 78, match_exposure: bool = True) -> dict[str, Any]:
    """Load ``name`` on the server, mosaic its tiles and write png/jpg/json into ``out_dir``."""
    t_start = time.time()
    target = next((m for m in client.get_available_maps() if m.rsplit("/", 1)[-1] == name), name)
    log.info("%s: loading %s", name, target)
    world = client.load_world(target)
    time.sleep(3.0)

    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = 0.05
    world.apply_settings(settings)

    w = carla.WeatherParameters(
        cloudiness=0.0, precipitation=0.0, precipitation_deposits=0.0, wind_intensity=0.0,
        sun_azimuth_angle=0.0, sun_altitude_angle=90.0, fog_density=0.0, fog_distance=0.0,
        wetness=0.0, fog_falloff=0.0)
    for attr in ("mie_scattering_scale", "rayleigh_scattering_scale", "scattering_intensity",
                 "dust_storm"):
        if hasattr(w, attr):
            setattr(w, attr, 0.0)
    world.set_weather(w)
    for _ in range(10):
        world.tick()

    x0, y0, nx, ny = tile_grid(bounds)
    fp = footprint(alt, fov)
    if fp < TILE * 1.05:
        raise SystemExit("footprint %.1f m is not >= 5%% over the %.0f m tile: lower fov or raise alt"
                         % (fp, TILE))
    px_per_m_cam = res / fp
    tile_px = int(round(TILE / cm_per_px))
    crop_px = int(round(TILE * px_per_m_cam))
    log.info("%s: %dx%d tiles, footprint %.1f m, camera %.3f px/m, crop %d px -> tile %d px",
             name, nx, ny, fp, px_per_m_cam, crop_px, tile_px)

    elev = Elevation(twin_dir / "elevation.npz")
    bp = world.get_blueprint_library().find("sensor.camera.rgb")
    bp.set_attribute("image_size_x", str(res))
    bp.set_attribute("image_size_y", str(res))
    bp.set_attribute("fov", str(fov))
    # DLSS is a temporal upsampler: a camera that teleports every tile smears without it off.
    for attr, val in (("enable_dlss", "false"), ("motion_blur_intensity", "0.0")):
        if bp.has_attribute(attr):
            bp.set_attribute(attr, val)

    spectator = world.get_spectator()
    crops: dict[tuple[int, int], np.ndarray] = {}
    sink: queue.Queue = queue.Queue()
    cam = world.spawn_actor(bp, carla.Transform(carla.Location(0, 0, alt),
                                                carla.Rotation(pitch=-90.0, yaw=yaw, roll=0.0)))
    cam.listen(sink.put)
    tiles_done = 0
    try:
        for j in range(ny):
            for i in range(nx):
                if only is not None and (i, j) not in only:
                    continue
                cx = x0 + (i + 0.5) * TILE
                cy = y0 + (j + 0.5) * TILE
                ux, uy = cx, -cy                       # model -> CARLA/UE metres
                # The twin's own DEM is what the baker built the level from, so it *is* the tile's
                # ground z.  ``ground_projection`` is only a cross-check: it happily reports a
                # 45 m building roof (measured on Sf_Soma -125,125), and trusting that would put
                # the camera 45 m low and shrink the whole tile by 15 %.
                gz = elev.at(cx, cy)
                probe = world.ground_projection(carla.Location(ux, uy, gz + 250.0), 600.0)
                dz = None if probe is None else probe.location.z - gz
                if dz is not None and abs(dz) <= 3.0:
                    gz = probe.location.z
                spectator.set_transform(carla.Transform(carla.Location(ux, uy, gz + 4.0),
                                                        carla.Rotation(pitch=-30.0, yaw=0.0)))
                for _ in range(spectator_ticks):
                    world.tick()
                cam.set_transform(carla.Transform(carla.Location(ux, uy, gz + alt),
                                                  carla.Rotation(pitch=-90.0, yaw=yaw, roll=0.0)))
                while not sink.empty():
                    sink.get_nowait()
                frame = _settle(world, sink, settle_cap, settle_tol, warm_ticks)
                if frame is None:
                    log.warning("%s tile %d,%d: no frame", name, i, j)
                    continue
                half = crop_px / 2.0
                c = res / 2.0
                box = (int(round(c - half)), int(round(c - half)),
                       int(round(c + half)), int(round(c + half)))
                crop = Image.fromarray(frame).crop(box).resize((tile_px, tile_px), Image.LANCZOS)
                crops[(i, j)] = np.asarray(crop, dtype=np.uint8)
                tiles_done += 1
                log.info("%s tile %d,%d (model %.0f,%.0f  carla %.0f,%.0f  z %.1f dem%+.1f) %d/%d",
                         name, i, j, cx, cy, ux, uy, gz, gz - elev.at(cx, cy), tiles_done, nx * ny)
    finally:
        cam.stop()
        cam.destroy()
        # hand the server back the way we found it: nothing ticks it once this process exits
        settings.synchronous_mode = False
        settings.fixed_delta_seconds = None
        world.apply_settings(settings)

    gains = exposure_gains(crops) if match_exposure else {k: 1.0 for k in crops}
    spread = (max(gains.values()) - min(gains.values())) if gains else 0.0
    mosaic = Image.new("RGB", (nx * tile_px, ny * tile_px), (0, 0, 0))
    for (i, j), arr in crops.items():
        gain = gains.get((i, j), 1.0)
        if abs(gain - 1.0) > 1e-3:
            arr = np.clip(arr.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        mosaic.paste(Image.fromarray(arr), (i * tile_px, (ny - 1 - j) * tile_px))
    log.info("%s: exposure gains %.3f..%.3f (spread %.1f %%)", name,
             min(gains.values(), default=1.0), max(gains.values(), default=1.0), 100 * spread)

    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"topview_{name}.png"
    jpg = out_dir / f"topview_{name}.jpg"
    mosaic.save(png)
    mosaic.save(jpg, quality=jpeg_quality, optimize=True, progressive=True)
    meta = {
        "map": name, "twin_dir": str(twin_dir),
        "bounds": [x0, y0, x0 + nx * TILE, y0 + ny * TILE],
        "bounds_model_data": [round(float(v), 2) for v in bounds],
        "size": [mosaic.width, mosaic.height],
        "px_per_m": round(1.0 / cm_per_px, 4), "cm_per_px": cm_per_px,
        "tiles": [nx, ny], "tile_px": tile_px,
        "camera": {"alt": alt, "fov": fov, "res": res, "yaw": yaw,
                   "footprint_m": round(fp, 2), "px_per_m": round(px_per_m_cam, 4)},
        "exposure_match": bool(match_exposure),
        "exposure_gain_spread": round(float(spread), 4),
        "jpeg_bytes": jpg.stat().st_size, "png_bytes": png.stat().st_size,
        "seconds": round(time.time() - t_start, 1),
    }
    (out_dir / f"topview_{name}.json").write_text(json.dumps(meta, indent=2))
    log.info("%s: %s  %dx%d px  jpeg %.2f MB  in %.0f s", name, jpg, mosaic.width, mosaic.height,
             meta["jpeg_bytes"] / 1e6, meta["seconds"])
    return meta


# ------------------------------------------------------------------------------------------ cli

DEFAULT_MAPS = [("Sf_Soma", "out/v8_soma/sf_soma.twin"),
                ("Sunnyvale", "out/v8_sunnyvale/sunnyvale.twin"),
                ("EixampleDemo", "out/v10_eixample/eixample.twin")]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default="out/review")
    ap.add_argument("--map", action="append", default=[], metavar="NAME[=TWINDIR]",
                    help="repeatable; default is the three baked twins")
    ap.add_argument("--cm-per-px", type=float, default=0.20)
    ap.add_argument("--alt", type=float, default=300.0, help="camera altitude over ground, m")
    ap.add_argument("--fov", type=float, default=50.0, help="horizontal fov, degrees")
    ap.add_argument("--res", type=int, default=3072, help="camera image size, px (square)")
    ap.add_argument("--yaw", type=float, default=-90.0,
                    help="camera yaw; -90 puts model north up and model east right")
    ap.add_argument("--jpeg-quality", type=int, default=78)
    ap.add_argument("--no-match-exposure", action="store_true",
                    help="keep each tile's own eye-adaptation exposure (leaves visible seams)")
    ap.add_argument("--settle-cap", type=int, default=150)
    ap.add_argument("--settle-tol", type=float, default=0.35)
    ap.add_argument("--warm-ticks", type=int, default=6)
    ap.add_argument("--spectator-ticks", type=int, default=25)
    ap.add_argument("--only-tile", default="", metavar="IX,IY[;IX,IY...]",
                    help="capture just these grid cells (probing)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    import carla  # noqa: PLC0415 -- only the CLI needs the wheel; the helpers above are pure

    rr = _load_region_review()
    root = Path(args.root)
    specs = []
    if args.map:
        by_default = dict(DEFAULT_MAPS)
        for s in args.map:
            name, _, rel = s.partition("=")
            specs.append((name, rel or by_default.get(name) or ""))
    else:
        specs = list(DEFAULT_MAPS)

    only = None
    if args.only_tile:
        only = set()
        for part in args.only_tile.split(";"):
            a, b = part.split(",")
            only.add((int(a), int(b)))

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)

    metas = []
    for name, rel in specs:
        d = Path(rel)
        if not d.is_absolute():
            d = root / rel
        if not d.exists():
            log.warning("skipping %s: %s missing", name, d)
            continue
        payload = rr.extract_twin(d, name, name)
        out_dir = Path(args.out)
        if not out_dir.is_absolute():
            out_dir = root / out_dir
        metas.append(capture_map(
            client, carla, name=name, twin_dir=d, bounds=payload["bounds"], out_dir=out_dir,
            cm_per_px=args.cm_per_px, alt=args.alt, fov=args.fov, res=args.res, yaw=args.yaw,
            settle_cap=args.settle_cap, settle_tol=args.settle_tol, warm_ticks=args.warm_ticks,
            spectator_ticks=args.spectator_ticks, only=only, jpeg_quality=args.jpeg_quality,
            match_exposure=not args.no_match_exposure))

    total = sum(m["jpeg_bytes"] for m in metas)
    log.info("done: %d map(s), %.2f MB of JPEG in %.0f s",
             len(metas), total / 1e6, sum(m["seconds"] for m in metas))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
