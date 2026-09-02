"""Capture top-down photo mosaics of a baked twin map from a *running* CARLA server, so the
region-review page can use the real render as its base layer instead of vectors.

    # level 0: one 20 cm/px mosaic per map, the zoomed-out base
    python tools/carla_topview.py --port 3000 --out out/review
    # level 1: one 6 cm/px image per 250 m material tile, the zoomed-in detail
    python tools/carla_topview.py --port 3000 --out out/review --detail

The map bounds come from the twin itself (the same ``region_review_page.extract_twin`` bounds the
page draws with) padded out to the 250 m material-tile grid, so every level and the vectors share
one model-frame rectangle.  For each frame:

  * the spectator is parked a few metres over the frame centre first -- it is the World Partition
    streaming source, so nothing is captured before that cell has streamed in;
  * a downward ``sensor.camera.rgb`` sits ``--alt`` metres above the ground with a narrow ``--fov``.
    Level 0 shoots one frame per 250 m tile from 300 m at 50 deg (280 m footprint); level 1 shoots
    ``--sub`` x ``--sub`` frames per tile from 100 m (93 m footprint for an 83 m sub-tile), which is
    where kerb texture, lane arrows, sign plates and signal heads actually resolve.
    Altitude is a ceiling, not a floor: World Partition swaps in HLOD proxies with distance, and
    above ~300 m the lane markings and crosswalks dissolve (measured: markings gone by 500 m, a
    hazy smear by 1000 m).  Ground features -- the ones the review is about -- do not lean at all;
    a 20 m building leans by alt/offset, 6.7 % at level 0 and 20 % at level 1, so tall roofs shift
    towards the frame edges;
  * frames are ticked until two consecutive ones are near-identical (streaming/LOD settled);
  * the exact central footprint is cropped with ``px_per_m = res / (2*alt*tan(fov/2))``;
  * finally the crops are levelled against each other (UE exposes every frame on its own content,
    which leaves a 3-5 % step at every seam) before they are pasted together.

Orientation.  A CARLA camera at ``pitch=-90`` has image-right = its right vector and image-up = its
pre-pitch forward vector.  At ``yaw=-90`` those are ``+X`` and ``-Y``, i.e. model east and model
north (``model_to_ue`` is ``(x, -y)``), which is exactly the page's north-up model frame.  ``--yaw``
exists to re-derive that empirically if a build ever disagrees.

Outputs (under ``--out``):

    topview_<Map>.png / .jpg / .json        level 0 mosaic, north up, + its model-frame sidecar
    topview_<Map>_detail.json               level 1 manifest: one entry per material tile
    detail_<Map>/tile_<i>_<j>.webp / .png   level 1 tiles, named by their material-tile index
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import queue
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

log = logging.getLogger("topview")

TILE = 250.0  # baker material tile size, model metres
VOID = 20     # luminance at or below this is the empty sky outside the map, not content


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


def content_fractions(mosaic: Path, meta: dict) -> dict[tuple[int, int], float]:
    """Fraction of each material tile that is *inside* the map, read off the level-0 mosaic.

    The maps are 600-800 m blobs on a 250 m grid, so a third of the grid cells are pure sky.  Level
    1 costs ``sub*sub`` frames and a whole embedded image per tile, so it is only worth shooting the
    cells that hold something.
    """
    nx, ny = meta["tiles"]
    tp = int(meta["tile_px"])
    lum = np.asarray(Image.open(mosaic).convert("L"))
    out = {}
    for j in range(ny):
        for i in range(nx):
            cell = lum[(ny - 1 - j) * tp:(ny - j) * tp, i * tp:(i + 1) * tp]
            out[(i, j)] = float((cell > VOID).mean()) if cell.size else 0.0
    return out


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


def exposure_gains(tiles: dict[Any, np.ndarray] | dict[Any, float], lo: float = 0.93,
                   hi: float = 1.07, min_cover: float = 0.35) -> dict[Any, float]:
    """Per-frame brightness gains that flatten the seams UE's eye adaptation leaves behind.

    Each frame is exposed on its own content, which puts a 3-5 % step across every seam.  Only
    frames that are mostly *inside* the map take part: a frame that is 90 % empty sky has a
    meaningless median and asking it to match the others would just brighten its few real pixels.
    The correction is clamped, so a genuinely dark or bright frame stays dark or bright.

    Values are either the RGB arrays themselves or ``(median, coverage)`` pairs already measured
    (level 1 keeps its crops on disk, so it measures as it captures).
    """
    meds: dict[Any, float] = {}
    for key, val in tiles.items():
        if isinstance(val, tuple):
            med, cover = val
        else:
            lum = val.mean(axis=2)
            content = lum > VOID
            cover = float(content.mean())
            med = float(np.median(lum[content])) if content.any() else 0.0
        if cover < min_cover:
            continue
        meds[key] = med
    if len(meds) < 2:
        return {k: 1.0 for k in tiles}
    target = float(np.median(list(meds.values())))
    out = {k: 1.0 for k in tiles}
    for key, med in meds.items():
        out[key] = min(hi, max(lo, target / med)) if med > 1e-3 else 1.0
    return out


def measure(arr: np.ndarray) -> tuple[float, float]:
    """(median luminance over the in-map pixels, in-map coverage) -- the input to `exposure_gains`."""
    lum = arr.mean(axis=2)
    content = lum > VOID
    cover = float(content.mean())
    return (float(np.median(lum[content])) if content.any() else 0.0, cover)


# -------------------------------------------------------------------------------------- capture

def _to_array(image) -> np.ndarray:
    """``carla.Image`` -> HxWx3 uint8 RGB."""
    buf = np.frombuffer(image.raw_data, dtype=np.uint8)
    return buf.reshape((image.height, image.width, 4))[:, :, 2::-1].copy()


class Shooter:
    """One downward camera, moved from pose to pose.  Spawning per frame costs a second each."""

    def __init__(self, world, carla, *, alt: float, fov: float, res: int, yaw: float,
                 settle_cap: int, settle_tol: float, warm_ticks: int, spectator_ticks: int):
        self.world, self.carla = world, carla
        self.alt, self.fov, self.res, self.yaw = alt, fov, res, yaw
        self.settle_cap, self.settle_tol = settle_cap, settle_tol
        self.warm_ticks, self.spectator_ticks = warm_ticks, spectator_ticks
        self.px_per_m = res / footprint(alt, fov)
        bp = world.get_blueprint_library().find("sensor.camera.rgb")
        bp.set_attribute("image_size_x", str(res))
        bp.set_attribute("image_size_y", str(res))
        bp.set_attribute("fov", str(fov))
        # DLSS is a temporal upsampler: a camera that teleports every frame smears without it off.
        for attr, val in (("enable_dlss", "false"), ("motion_blur_intensity", "0.0")):
            if bp.has_attribute(attr):
                bp.set_attribute(attr, val)
        self.sink: queue.Queue = queue.Queue()
        self.cam = world.spawn_actor(bp, carla.Transform(
            carla.Location(0, 0, alt), carla.Rotation(pitch=-90.0, yaw=yaw, roll=0.0)))
        self.cam.listen(self.sink.put)
        self.spectator = world.get_spectator()

    def _settle(self) -> np.ndarray | None:
        prev = last = None
        for n in range(self.settle_cap):
            self.world.tick()
            try:
                img = self.sink.get(timeout=20.0)
            except queue.Empty:
                continue
            while not self.sink.empty():
                img = self.sink.get_nowait()
            last = _to_array(img)
            small = last[::8, ::8].astype(np.int16)
            if prev is not None and n >= self.warm_ticks:
                if float(np.abs(small - prev).mean()) < self.settle_tol:
                    return last
            prev = small
        return last

    def frame(self, ux: float, uy: float, gz: float) -> np.ndarray | None:
        """Park the streaming source, move the camera, tick until settled, return the RGB frame."""
        carla = self.carla
        self.spectator.set_transform(carla.Transform(
            carla.Location(ux, uy, gz + 4.0), carla.Rotation(pitch=-30.0, yaw=0.0)))
        for _ in range(self.spectator_ticks):
            self.world.tick()
        self.cam.set_transform(carla.Transform(
            carla.Location(ux, uy, gz + self.alt),
            carla.Rotation(pitch=-90.0, yaw=self.yaw, roll=0.0)))
        while not self.sink.empty():
            self.sink.get_nowait()
        return self._settle()

    def crop(self, frame: np.ndarray, width_m: float, out_px: int) -> Image.Image:
        """Central ``width_m`` square of a frame, resampled to ``out_px``."""
        half = width_m * self.px_per_m / 2.0
        c = self.res / 2.0
        box = (int(round(c - half)), int(round(c - half)), int(round(c + half)), int(round(c + half)))
        return Image.fromarray(frame).crop(box).resize((out_px, out_px), Image.LANCZOS)

    def close(self) -> None:
        self.cam.stop()
        self.cam.destroy()


def _prepare_world(client, carla, name: str) -> Any:
    """Load ``name``, go synchronous, and put the sun straight overhead with no haze."""
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
    return world


def _release_world(world) -> None:
    """Hand the server back the way we found it: nothing ticks it once this process exits."""
    settings = world.get_settings()
    settings.synchronous_mode = False
    settings.fixed_delta_seconds = None
    world.apply_settings(settings)


def _ground(elev: Elevation, world, carla, cx: float, cy: float) -> float:
    """The twin's own DEM is what the baker built the level from, so it *is* the ground z.
    ``ground_projection`` is only a cross-check: it happily reports a 45 m building roof (measured
    on Sf_Soma -125,125), and trusting that would put the camera 45 m low and shrink the frame."""
    gz = elev.at(cx, cy)
    probe = world.ground_projection(carla.Location(cx, -cy, gz + 250.0), 600.0)
    if probe is not None and abs(probe.location.z - gz) <= 3.0:
        return probe.location.z
    return gz


def capture_level0(client, carla, *, name: str, twin_dir: Path, bounds: Sequence[float],
                   out_dir: Path, cm_per_px: float, alt: float, fov: float, res: int,
                   yaw: float, settle_cap: int, settle_tol: float, warm_ticks: int,
                   spectator_ticks: int, only: set[tuple[int, int]] | None = None,
                   jpeg_quality: int = 78, match_exposure: bool = True,
                   world=None) -> dict[str, Any]:
    """One frame per material tile, stitched into a single north-up mosaic for the whole map."""
    t_start = time.time()
    world = world or _prepare_world(client, carla, name)

    x0, y0, nx, ny = tile_grid(bounds)
    fp = footprint(alt, fov)
    if fp < TILE * 1.05:
        raise SystemExit("footprint %.1f m is not >= 5%% over the %.0f m tile: lower fov or raise alt"
                         % (fp, TILE))
    tile_px = int(round(TILE / cm_per_px))
    log.info("%s L0: %dx%d tiles, footprint %.1f m, crop -> tile %d px (%.0f cm/px)",
             name, nx, ny, fp, tile_px, 100 * TILE / tile_px)

    elev = Elevation(twin_dir / "elevation.npz")
    shooter = Shooter(world, carla, alt=alt, fov=fov, res=res, yaw=yaw, settle_cap=settle_cap,
                      settle_tol=settle_tol, warm_ticks=warm_ticks, spectator_ticks=spectator_ticks)
    crops: dict[tuple[int, int], np.ndarray] = {}
    try:
        for j in range(ny):
            for i in range(nx):
                if only is not None and (i, j) not in only:
                    continue
                cx, cy = x0 + (i + 0.5) * TILE, y0 + (j + 0.5) * TILE
                gz = _ground(elev, world, carla, cx, cy)
                frame = shooter.frame(cx, -cy, gz)
                if frame is None:
                    log.warning("%s L0 tile %d,%d: no frame", name, i, j)
                    continue
                crops[(i, j)] = np.asarray(shooter.crop(frame, TILE, tile_px), dtype=np.uint8)
                log.info("%s L0 tile %d,%d (model %.0f,%.0f z %.1f) %d/%d",
                         name, i, j, cx, cy, gz, len(crops), nx * ny)
    finally:
        shooter.close()

    gains = exposure_gains(crops) if match_exposure else {k: 1.0 for k in crops}
    spread = (max(gains.values()) - min(gains.values())) if gains else 0.0
    mosaic = Image.new("RGB", (nx * tile_px, ny * tile_px), (0, 0, 0))
    for (i, j), arr in crops.items():
        gain = gains.get((i, j), 1.0)
        if abs(gain - 1.0) > 1e-3:
            arr = np.clip(arr.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        mosaic.paste(Image.fromarray(arr), (i * tile_px, (ny - 1 - j) * tile_px))
    log.info("%s L0: exposure gains %.3f..%.3f", name,
             min(gains.values(), default=1.0), max(gains.values(), default=1.0))

    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"topview_{name}.png"
    jpg = out_dir / f"topview_{name}.jpg"
    mosaic.save(png)
    mosaic.save(jpg, quality=jpeg_quality, optimize=True, progressive=True)
    meta = {
        "map": name, "level": 0, "twin_dir": str(twin_dir),
        "bounds": [x0, y0, x0 + nx * TILE, y0 + ny * TILE],
        "bounds_model_data": [round(float(v), 2) for v in bounds],
        "size": [mosaic.width, mosaic.height],
        "px_per_m": round(tile_px / TILE, 4), "cm_per_px": round(100 * TILE / tile_px, 3),
        "tiles": [nx, ny], "tile_px": tile_px,
        "camera": {"alt": alt, "fov": fov, "res": res, "yaw": yaw,
                   "footprint_m": round(fp, 2), "px_per_m": round(shooter.px_per_m, 4)},
        "exposure_match": bool(match_exposure),
        "exposure_gain_spread": round(float(spread), 4),
        "jpeg_bytes": jpg.stat().st_size, "png_bytes": png.stat().st_size,
        "seconds": round(time.time() - t_start, 1),
    }
    (out_dir / f"topview_{name}.json").write_text(json.dumps(meta, indent=2))
    log.info("%s L0: %s  %dx%d px  jpeg %.2f MB  in %.0f s", name, jpg, mosaic.width, mosaic.height,
             meta["jpeg_bytes"] / 1e6, meta["seconds"])
    return meta


def capture_detail(client, carla, *, name: str, twin_dir: Path, bounds: Sequence[float],
                   out_dir: Path, cm_per_px: float, alt: float, fov: float, res: int, sub: int,
                   yaw: float, settle_cap: int, settle_tol: float, warm_ticks: int,
                   spectator_ticks: int, min_content: float, only: set[tuple[int, int]] | None = None,
                   webp_quality: int = 80, match_exposure: bool = True, keep_png: bool = True,
                   world=None) -> dict[str, Any]:
    """``sub`` x ``sub`` frames per material tile, stitched into one image *per tile*.

    Per tile, not per map: a 250 m tile at 6 cm/px is 4167 px square, which a browser decodes
    happily one at a time; the whole map at that resolution would be a 17000 px image and over a
    gigabyte decoded.  The crops go to a scratch directory as they are shot (a map's worth of them
    does not fit in memory) and the tiles are assembled from there once the exposure gains are known.
    """
    t_start = time.time()
    world = world or _prepare_world(client, carla, name)

    x0, y0, nx, ny = tile_grid(bounds)
    sub_m = TILE / sub
    fp = footprint(alt, fov)
    if fp < sub_m * 1.05:
        raise SystemExit("footprint %.1f m is not >= 5%% over the %.1f m sub-tile" % (fp, sub_m))
    sub_px = int(round(sub_m / cm_per_px))
    tile_px = sub_px * sub
    log.info("%s L1: %dx%d tiles x %dx%d sub, footprint %.1f m for %.1f m sub-tiles, "
             "tile %d px (%.1f cm/px)", name, nx, ny, sub, sub, fp, sub_m, tile_px,
             100 * TILE / tile_px)

    want = set(only) if only is not None else None
    if want is None:
        l0 = out_dir / f"topview_{name}.json"
        if l0.exists():
            frac = content_fractions(out_dir / f"topview_{name}.png", json.loads(l0.read_text()))
            want = {k for k, v in frac.items() if v > min_content}
            log.info("%s L1: %d of %d tiles hold >%.0f%% map, skipping the rest",
                     name, len(want), nx * ny, 100 * min_content)
        else:
            want = {(i, j) for j in range(ny) for i in range(nx)}

    elev = Elevation(twin_dir / "elevation.npz")
    scratch = out_dir / f".scratch_{name}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    shooter = Shooter(world, carla, alt=alt, fov=fov, res=res, yaw=yaw, settle_cap=settle_cap,
                      settle_tol=settle_tol, warm_ticks=warm_ticks, spectator_ticks=spectator_ticks)
    stats: dict[tuple[int, int, int, int], tuple[float, float]] = {}
    total = len(want) * sub * sub
    done = 0
    try:
        for (i, j) in sorted(want, key=lambda t: (t[1], t[0])):
            for sj in range(sub):
                for si in range(sub):
                    cx = x0 + i * TILE + (si + 0.5) * sub_m
                    cy = y0 + j * TILE + (sj + 0.5) * sub_m
                    gz = _ground(elev, world, carla, cx, cy)
                    frame = shooter.frame(cx, -cy, gz)
                    if frame is None:
                        log.warning("%s L1 %d,%d/%d,%d: no frame", name, i, j, si, sj)
                        continue
                    crop = shooter.crop(frame, sub_m, sub_px)
                    arr = np.asarray(crop, dtype=np.uint8)
                    stats[(i, j, si, sj)] = measure(arr)
                    crop.save(scratch / f"{i}_{j}_{si}_{sj}.png", compress_level=1)
                    done += 1
            log.info("%s L1 tile %d,%d done (%d/%d frames, %.0f s)",
                     name, i, j, done, total, time.time() - t_start)
    finally:
        shooter.close()

    gains = exposure_gains(stats) if match_exposure else {k: 1.0 for k in stats}
    log.info("%s L1: exposure gains %.3f..%.3f over %d frames", name,
             min(gains.values(), default=1.0), max(gains.values(), default=1.0), len(gains))

    tdir = out_dir / f"detail_{name}"
    tdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for (i, j) in sorted(want, key=lambda t: (t[1], t[0])):
        tile = Image.new("RGB", (tile_px, tile_px), (0, 0, 0))
        n = 0
        for sj in range(sub):
            for si in range(sub):
                p = scratch / f"{i}_{j}_{si}_{sj}.png"
                if not p.exists():
                    continue
                arr = np.asarray(Image.open(p), dtype=np.uint8)
                gain = gains.get((i, j, si, sj), 1.0)
                if abs(gain - 1.0) > 1e-3:
                    arr = np.clip(arr.astype(np.float32) * gain, 0, 255).astype(np.uint8)
                tile.paste(Image.fromarray(arr), (si * sub_px, (sub - 1 - sj) * sub_px))
                n += 1
        if not n:
            continue
        webp = tdir / f"tile_{i}_{j}.webp"
        tile.save(webp, format="WEBP", quality=webp_quality, method=4)
        if keep_png:
            tile.save(tdir / f"tile_{i}_{j}.png", compress_level=1)
        entries.append({
            "i": i, "j": j,
            "bounds": [x0 + i * TILE, y0 + j * TILE, x0 + (i + 1) * TILE, y0 + (j + 1) * TILE],
            "file": f"detail_{name}/{webp.name}", "bytes": webp.stat().st_size,
        })
        log.info("%s L1 tile %d,%d -> %s %.2f MB", name, i, j, webp.name,
                 webp.stat().st_size / 1e6)
    shutil.rmtree(scratch, ignore_errors=True)

    meta = {
        "map": name, "level": 1, "twin_dir": str(twin_dir),
        "grid": {"x0": x0, "y0": y0, "nx": nx, "ny": ny, "tile_m": TILE},
        "bounds": [x0, y0, x0 + nx * TILE, y0 + ny * TILE],
        "tile_px": tile_px, "sub": sub, "sub_px": sub_px,
        "px_per_m": round(tile_px / TILE, 4), "cm_per_px": round(100 * TILE / tile_px, 3),
        "min_content": min_content, "webp_quality": webp_quality,
        "camera": {"alt": alt, "fov": fov, "res": res, "yaw": yaw,
                   "footprint_m": round(fp, 2), "px_per_m": round(shooter.px_per_m, 4)},
        "tiles": entries,
        "webp_bytes": sum(e["bytes"] for e in entries),
        "seconds": round(time.time() - t_start, 1),
    }
    (out_dir / f"topview_{name}_detail.json").write_text(json.dumps(meta, indent=2))
    log.info("%s L1: %d tiles, %.2f MB webp total, %.1f cm/px, in %.0f s", name, len(entries),
             meta["webp_bytes"] / 1e6, meta["cm_per_px"], meta["seconds"])
    return meta


# ------------------------------------------------------------------------------------------ cli

DEFAULT_MAPS = [("Sf_Soma", "out/v8_soma/sf_soma.twin"),
                ("Sunnyvale", "out/v8_sunnyvale/sunnyvale.twin"),
                ("EixampleDemo", "out/v10_eixample/eixample.twin")]

# level -> (altitude m, fov deg, sub-tiles per axis, camera res px, ground resolution m/px)
LEVELS = {0: (300.0, 50.0, 1, 3072, 0.20), 1: (100.0, 50.0, 3, 2048, 0.06)}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default="out/review")
    ap.add_argument("--map", action="append", default=[], metavar="NAME[=TWINDIR]",
                    help="repeatable; default is the three baked twins")
    ap.add_argument("--detail", action="store_true",
                    help="capture the level-1 detail tiles instead of the level-0 mosaic")
    ap.add_argument("--cm-per-px", type=float, default=None, help="default: 20 (L0) / 6 (L1)")
    ap.add_argument("--alt", type=float, default=None, help="camera altitude over ground, m")
    ap.add_argument("--fov", type=float, default=None, help="horizontal fov, degrees")
    ap.add_argument("--res", type=int, default=None, help="camera image size, px (square)")
    ap.add_argument("--sub", type=int, default=None, help="level 1: frames per tile per axis")
    ap.add_argument("--min-content", type=float, default=0.10,
                    help="level 1: skip material tiles less than this fraction inside the map")
    ap.add_argument("--yaw", type=float, default=-90.0,
                    help="camera yaw; -90 puts model north up and model east right")
    ap.add_argument("--jpeg-quality", type=int, default=78)
    ap.add_argument("--webp-quality", type=int, default=80)
    ap.add_argument("--no-keep-png", action="store_true",
                    help="level 1: skip the lossless per-tile png (kept so the embeddable webp can "
                         "be re-encoded at another quality without re-flying the map)")
    ap.add_argument("--no-match-exposure", action="store_true",
                    help="keep each frame's own eye-adaptation exposure (leaves visible seams)")
    ap.add_argument("--settle-cap", type=int, default=150)
    ap.add_argument("--settle-tol", type=float, default=0.35)
    ap.add_argument("--warm-ticks", type=int, default=6)
    ap.add_argument("--spectator-ticks", type=int, default=25)
    ap.add_argument("--only-tile", default="", metavar="IX,IY[;IX,IY...]",
                    help="capture just these material tiles (probing)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    import carla  # noqa: PLC0415 -- only the CLI needs the wheel; the helpers above are pure

    level = 1 if args.detail else 0
    d_alt, d_fov, d_sub, d_res, d_cm = LEVELS[level]
    alt = args.alt if args.alt is not None else d_alt
    fov = args.fov if args.fov is not None else d_fov
    res = args.res if args.res is not None else d_res
    sub = args.sub if args.sub is not None else d_sub
    cm = args.cm_per_px if args.cm_per_px is not None else d_cm

    rr = _load_region_review()
    root = Path(args.root)
    if args.map:
        by_default = dict(DEFAULT_MAPS)
        specs = []
        for s in args.map:
            nm, _, rel = s.partition("=")
            specs.append((nm, rel or by_default.get(nm) or ""))
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

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir

    metas = []
    for nm, rel in specs:
        d = Path(rel)
        if not d.is_absolute():
            d = root / rel
        if not d.exists():
            log.warning("skipping %s: %s missing", nm, d)
            continue
        bounds = rr.extract_twin(d, nm, nm)["bounds"]
        world = _prepare_world(client, carla, nm)
        try:
            common = dict(name=nm, twin_dir=d, bounds=bounds, out_dir=out_dir, cm_per_px=cm,
                          alt=alt, fov=fov, res=res, yaw=args.yaw, settle_cap=args.settle_cap,
                          settle_tol=args.settle_tol, warm_ticks=args.warm_ticks,
                          spectator_ticks=args.spectator_ticks, only=only,
                          match_exposure=not args.no_match_exposure, world=world)
            if args.detail:
                metas.append(capture_detail(client, carla, sub=sub, min_content=args.min_content,
                                            webp_quality=args.webp_quality,
                                            keep_png=not args.no_keep_png, **common))
            else:
                metas.append(capture_level0(client, carla, jpeg_quality=args.jpeg_quality, **common))
        finally:
            _release_world(world)

    total = sum(m.get("webp_bytes", m.get("jpeg_bytes", 0)) for m in metas)
    log.info("done: level %d, %d map(s), %.2f MB embeddable in %.0f s",
             level, len(metas), total / 1e6, sum(m["seconds"] for m in metas))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
