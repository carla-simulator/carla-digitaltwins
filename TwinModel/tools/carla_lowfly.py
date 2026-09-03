"""Fly a baked twin map at 5 m with a downward camera and stitch the frames into a 1 cm/px
orthomosaic, delivered as a tile pyramid the local review server streams.

    python tools/carla_lowfly.py --port 3000 --map Sunnyvale
    python tools/carla_lowfly.py --port 3000                # all three baked twins
    python tools/carla_lowfly.py --levels-only --map Sf_Soma  # rebuild the pyramid from the leaves

Why 5 m and a 90 deg lens: the probe (`.omc/captures/probe_*`) showed that the post-process depth
of field blurs anything shot through a narrow lens from higher up (30 m / 19 deg is soft, 100 m /
5.7 deg is mush), and switching post-processing off loses exposure altogether.  5 m at 90 deg is
pin sharp: 10 m footprint, 1024 px, 0.98 cm/px.  The cost is perspective: a 2 m pole 3 m off the
frame centre leans 2 m, so only the central 6.25 m of every frame is kept (STEP) and tall props
near a seam can show a break.  Ground features do not lean at all.

Per material tile (250 m, the baker's grid):

  * the spectator is parked once at the tile centre (World Partition streaming source);
  * ``--cams`` cameras fly in a row, 40 x 40 frame centres on the 6.25 m grid, ``--settle`` ticks
    per pose so the temporal AA and eye adaptation converge;
  * every frame is *orthorectified*: a depth camera rides with each RGB camera, and each 1 cm
    output pixel is projected back through the camera using the rendered surface height under
    it (the twin DEM only seeds the iteration).  That keeps sloping streets (Sf_Soma) from
    changing scale by 15 % across a frame and puts kerbs, roofs and car tops where they are,
    not where a 5 m perspective would lean them;
  * exposure: the UE5 camera exposes a frame that is one pale surface straight into clipping
    (median 249, 46 % of pixels at white on a Sunnyvale sidewalk; auto exposure has no room to
    compensate and this build exposes no exposure attributes).  Worse, the auto exposure lags
    the teleports: the same spot measured 155 / 141 / 148 depending on where the camera came
    from, which is the seam pattern no gain solve fully removes.  So for the flight the engine is
    switched to *manual* exposure (``r.EyeAdaptation.MethodOverride 3``: the same spot then
    measures 62.7 / 62.7 / 62.7) and the lens attenuation, the global exposure scalar, is set
    through the server's ``console_command`` RPC (``--lens-attenuation`` 0.44: asphalt median
    ~160, pale paving ~140, no clipping); both are restored when the flight ends;
  * the lens vignette (a 5-8 % darkening towards the frame corners that the eye never notices
    in one frame but that draws a grid on a mosaic) is measured as the mean of all the map's
    frames -- content averages out over hundreds of frames -- and divided out (flat-field);
  * the crops are pasted raw into a 25000 px memmap; the frames' overlap bands (the outer 3.75 m of
    each 10 m frame, which the neighbours also see) give the exposure ratio between neighbours,
    and a least-squares solve over those ratios -- anchored to the tiles already finished -- gives
    one gain per frame, which is what removes the eye-adaptation steps at the seams;
  * the memmap is then cut into 5 m / 500 px WebP leaf tiles (``z0/<i>_<j>.webp``) and deleted.

Once every tile is flown the coarser levels are built bottom-up (``z<k>`` tile = 5 * 2^k metres,
always 500 px) and ``manifest.json`` records the grid so the viewer can address any tile from model
metres: tile (z, i, j) covers ``[x0 + i*5*2^z, x0 + (i+1)*5*2^z) x [y0 + j*5*2^z, ...)`` with j
increasing north and image rows running north to south.
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
from scipy.ndimage import map_coordinates
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import lsqr

log = logging.getLogger("lowfly")

TILE = 250.0        # material tile, model metres
STEP = 6.25         # frame pitch = kept crop width; 40 per tile
FRAMES = int(round(TILE / STEP))
CM_PER_PX = 1.0
CROP_PX = int(round(STEP * 100 / CM_PER_PX))      # 625
TILE_PX = CROP_PX * FRAMES                         # 25000
LEAF_M = 5.0
LEAF_PX = int(round(LEAF_M * 100 / CM_PER_PX))     # 500
LEAVES = int(round(TILE / LEAF_M))                 # 50
VOID = 20
BAND_FRAC = 0.375   # overlap band of a 10 m frame at 6.25 m pitch = 3.75 m


LENS_ATTENUATION_DEFAULT = 0.78     # UE's r.EyeAdaptation.LensAttenuation default


def console_command(host: str, port: int, cmd: str, timeout: float = 10.0) -> bool:
    """``console_command`` RPC (CarlaServer.cpp -> PlayerController->ConsoleCommand).  The Python
    module does not expose it, so this speaks rpclib's msgpack-rpc wire format directly:
    request ``[0, id, method, [metadata, args...]]`` -> reply ``[1, id, error, result]``."""
    import socket  # noqa: PLC0415
    import msgpack  # noqa: PLC0415
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(msgpack.packb([0, 1, "console_command", [[False], cmd]], use_bin_type=True))
        unp = msgpack.Unpacker(raw=False)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                raise RuntimeError("console_command: connection closed")
            unp.feed(chunk)
            for msg in unp:
                ok = msg[2] is None
                log.info("console %r -> %s", cmd, "ok" if ok else msg[2])
                return ok
    finally:
        s.close()


def _load_topview():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("carla_topview", here / "carla_topview.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------- geometry

class Ortho:
    """Orthorectify a downward pinhole frame onto the 1 cm ground grid using the DEM."""

    def __init__(self, elev, alt: float, fov: float, res: int):
        self.elev, self.alt, self.res = elev, alt, res
        self.f = (res / 2.0) / math.tan(math.radians(fov) / 2.0)
        # output pixel centres relative to the frame centre, metres; row 0 = north
        off = (np.arange(CROP_PX) + 0.5) * (CM_PER_PX / 100.0) - STEP / 2.0
        self.dx = off[None, :].repeat(CROP_PX, axis=0)
        self.dy = (-off)[:, None].repeat(CROP_PX, axis=1)

    def ground(self, cx: float, cy: float) -> np.ndarray:
        """DEM height on the output grid (bilinear on the 2 m DEM), NaN -> median."""
        e = self.elev
        fx = (cx + self.dx - e.x0) / e.dx
        fy = (cy + self.dy - e.y0) / e.dy
        z = np.where(np.isfinite(e.z), e.z, e.median)
        return map_coordinates(z, [fy, fx], order=1, mode="nearest")

    def rectify(self, frame: np.ndarray, cx: float, cy: float, gz: float,
                depth: np.ndarray | None = None, iters: int = 3) -> np.ndarray:
        """``depth`` is the co-located depth camera's planar depth in metres (vertical distance for
        a nadir camera).  Starting from the DEM height, each output pixel is projected into the
        frame, the surface height read back there, and the projection repeated -- three rounds
        settle to the millimetre on anything that is not a vertical wall."""
        z = self.ground(cx, cy)
        cam_z = self.alt + gz
        u = v = None
        for _ in range(iters if depth is not None else 1):
            d = np.maximum(cam_z - z, 0.5)
            u = self.res / 2.0 + self.dx * self.f / d - 0.5
            v = self.res / 2.0 - self.dy * self.f / d - 0.5
            if depth is None:
                break
            seen = map_coordinates(depth, [v, u], order=1, mode="nearest")
            ok = (seen > 0.5) & (seen < 4.0 * self.alt)
            z = np.where(ok, cam_z - seen, z)
        out = np.empty((CROP_PX, CROP_PX, 3), dtype=np.uint8)
        for ch in range(3):
            out[..., ch] = np.clip(map_coordinates(frame[..., ch], [v, u], order=1, mode="nearest"),
                                   0, 255)
        return out


def band_medians(frame: np.ndarray, res: int) -> dict[str, float]:
    """Median luminance of the four overlap bands (L, R, T, B); NaN where mostly void."""
    lum = frame.mean(axis=2)
    b = int(round(res * BAND_FRAC))
    bands = {"L": lum[:, :b], "R": lum[:, res - b:], "T": lum[:b, :], "B": lum[res - b:, :]}
    out = {}
    for k, a in bands.items():
        m = a > VOID
        out[k] = float(np.median(a[m])) if m.mean() > 0.3 else float("nan")
    return out


def solve_gains(new: dict[tuple[int, int], dict[str, float]],
                fixed: dict[tuple[int, int], float],
                fixed_bands: dict[tuple[int, int], dict[str, float]],
                lo: float = 0.85, hi: float = 1.18, prior: float = 0.05) -> dict[tuple[int, int], float]:
    """One multiplicative gain per new frame so neighbours agree on their shared band.

    Frame (r, c) shares its R band with (r, c+1)'s L band and its T band with (r+1, c)'s B band.
    Unknowns are log gains of the ``new`` frames; frames in ``fixed`` keep theirs, which anchors
    the solve to the tiles already cut.  A weak prior towards 1 keeps islands bounded.
    """
    keys = sorted(new)
    idx = {k: n for n, k in enumerate(keys)}
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    rhs: list[float] = []
    allb = dict(fixed_bands)
    allb.update(new)

    def add(a, b, ma, mb):
        """log g_a - log g_b = log(mb / ma) so that g_a * ma == g_b * mb."""
        if not (np.isfinite(ma) and np.isfinite(mb)) or ma <= 1 or mb <= 1:
            return
        t = math.log(mb / ma)
        r = len(rhs)
        if a in idx and b in idx:
            rows.extend([r, r]); cols.extend([idx[a], idx[b]]); vals.extend([1.0, -1.0])
            rhs.append(t)
        elif a in idx:
            rows.append(r); cols.append(idx[a]); vals.append(1.0)
            rhs.append(t + math.log(fixed.get(b, 1.0)))
        elif b in idx:
            rows.append(r); cols.append(idx[b]); vals.append(-1.0)
            rhs.append(t - math.log(fixed.get(a, 1.0)))

    for (r, c) in keys:
        for nb, my, its in (((r, c + 1), "R", "L"), ((r, c - 1), "L", "R"),
                            ((r + 1, c), "T", "B"), ((r - 1, c), "B", "T")):
            if nb in allb:
                add((r, c), nb, new[(r, c)][my], allb[nb][its])
    if not keys:
        return {}
    n_pair = len(rhs)
    for k in keys:  # prior
        r = len(rhs)
        rows.append(r); cols.append(idx[k]); vals.append(prior); rhs.append(0.0)
    A = coo_matrix((vals, (rows, cols)), shape=(len(rhs), len(keys))).tocsr()
    b = np.asarray(rhs)
    # iteratively re-weighted: a band pair whose ratio is content (a black interior against a
    # pavement, 30 % and more) is not an exposure step (3-6 %), and must not drag its neighbours
    w = np.ones(len(rhs))
    w[:n_pair] = np.where(np.abs(b[:n_pair]) > math.log(1.25), 0.0, 1.0)
    x = np.zeros(len(keys))
    for _ in range(4):
        W = coo_matrix((w, (np.arange(len(w)), np.arange(len(w)))), shape=(len(w), len(w))).tocsr()
        x = lsqr(W @ A, w * b, atol=1e-8, btol=1e-8, iter_lim=3000)[0]
        res = np.abs(A @ x - b)[:n_pair]
        w[:n_pair] = np.where(np.abs(b[:n_pair]) > math.log(1.25), 0.0,
                              1.0 / np.maximum(1.0, res / 0.02))
    return {k: float(min(hi, max(lo, math.exp(x[idx[k]])))) for k in keys}


# ----------------------------------------------------------------------------------- capture

def _to_array(image) -> np.ndarray:
    buf = np.frombuffer(image.raw_data, dtype=np.uint8)
    return buf.reshape((image.height, image.width, 4))[:, :, 2::-1].copy()


def _to_depth(image) -> np.ndarray:
    """CARLA depth encoding (BGRA bytes): metres = (R + G*256 + B*65536) / (2^24 - 1) * 1000."""
    buf = np.frombuffer(image.raw_data, dtype=np.uint8).reshape((image.height, image.width, 4))
    code = buf[:, :, 2].astype(np.float64) + buf[:, :, 1].astype(np.float64) * 256.0 \
        + buf[:, :, 0].astype(np.float64) * 65536.0
    return (code / 16777215.0 * 1000.0).astype(np.float32)


def _drain(q: queue.Queue, last):
    try:
        img = q.get(timeout=20.0)
        while not q.empty():
            img = q.get_nowait()
        return img
    except queue.Empty:
        return last


class Rig:
    """``n`` downward cameras flown as a row; one tick renders all of them."""

    def __init__(self, world, carla, *, n: int, alt: float, fov: float, res: int, yaw: float,
                 settle: int, depth: bool = True):
        self.world, self.carla = world, carla
        self.n, self.alt, self.yaw, self.settle = n, alt, yaw, settle
        lib = world.get_blueprint_library()
        self.cams, self.sinks, self.dcams, self.dsinks = [], [], [], []
        kinds = [("sensor.camera.rgb", self.cams, self.sinks)]
        if depth:
            kinds.append(("sensor.camera.depth", self.dcams, self.dsinks))
        for _ in range(n):
            for bp_id, cams, sinks in kinds:
                bp = lib.find(bp_id)
                bp.set_attribute("image_size_x", str(res))
                bp.set_attribute("image_size_y", str(res))
                bp.set_attribute("fov", str(fov))
                for attr, val in (("enable_dlss", "false"), ("motion_blur_intensity", "0.0")):
                    if bp.has_attribute(attr):
                        bp.set_attribute(attr, val)
                q: queue.Queue = queue.Queue()
                cam = world.spawn_actor(bp, carla.Transform(
                    carla.Location(0, 0, 500.0), carla.Rotation(pitch=-90.0, yaw=yaw, roll=0.0)))
                cam.listen(q.put)
                cams.append(cam)
                sinks.append(q)
        self.spectator = world.get_spectator()

    def park(self, ux: float, uy: float, gz: float, ticks: int) -> None:
        self.spectator.set_transform(self.carla.Transform(
            self.carla.Location(ux, uy, gz + 4.0), self.carla.Rotation(pitch=-30.0, yaw=0.0)))
        for _ in range(ticks):
            self.world.tick()

    def shoot(self, poses: Sequence[tuple[float, float, float]]
              ) -> list[tuple[np.ndarray, np.ndarray | None] | None]:
        """``poses`` = (ue_x, ue_y, ground_z) per camera (fewer than n: the rest idle).
        Returns (rgb, depth_m | None) per pose, None where a camera produced nothing."""
        carla = self.carla
        for k in range(min(len(poses), self.n)):
            ux, uy, gz = poses[k]
            tf = carla.Transform(carla.Location(ux, uy, gz + self.alt),
                                 carla.Rotation(pitch=-90.0, yaw=self.yaw, roll=0.0))
            self.cams[k].set_transform(tf)
            if self.dcams:
                self.dcams[k].set_transform(tf)
        for q in self.sinks + self.dsinks:
            while not q.empty():
                q.get_nowait()
        last: list[Any] = [None] * self.n
        dlast: list[Any] = [None] * self.n
        for _ in range(self.settle):
            self.world.tick()
            for k, q in enumerate(self.sinks):
                last[k] = _drain(q, last[k])
            for k, q in enumerate(self.dsinks):
                dlast[k] = _drain(q, dlast[k])
        out = []
        for k in range(len(poses)):
            if last[k] is None:
                out.append(None)
            else:
                out.append((_to_array(last[k]), _to_depth(dlast[k]) if dlast[k] is not None else None))
        return out

    def close(self) -> None:
        for cam in self.cams + self.dcams:
            cam.stop()
            cam.destroy()


# ---------------------------------------------------------------------------------- flat field

class FlatField:
    """Mean of every rectified crop = the lens vignette (content averages out); its inverse,
    normalised to the frame centre and smoothed, is the per-pixel correction."""

    def __init__(self, path: Path):
        self.path = path
        self.sum = np.zeros((CROP_PX, CROP_PX), dtype=np.float64)
        self.n = 0
        if path.exists():
            d = np.load(path)
            self.sum, self.n = d["sum"], int(d["n"])

    def add(self, crop: np.ndarray) -> None:
        lum = crop.mean(axis=2)
        if (lum > VOID).mean() > 0.9:      # only frames fully inside the map
            self.sum += lum
            self.n += 1

    def save(self) -> None:
        np.savez(self.path, sum=self.sum, n=self.n)

    def correction(self, sigma: float = 30.0) -> np.ndarray:
        """(CROP_PX, CROP_PX) multiplier that flattens the vignette while keeping the crop's mean
        brightness as the camera exposed it (normalising to the centre instead lifted the whole
        mosaic 6 % towards clipping), clamped to [0.85, 1.25]."""
        if self.n < 50:
            return np.ones((CROP_PX, CROP_PX), dtype=np.float32)
        from scipy.ndimage import gaussian_filter  # noqa: PLC0415
        mean = gaussian_filter(self.sum / self.n, sigma)
        corr = float(mean.mean()) / np.maximum(mean, 1.0)
        return np.clip(corr, 0.85, 1.25).astype(np.float32)


# ------------------------------------------------------------------------------------ pyramid

def cut_leaves(mm: np.ndarray, gains: np.ndarray, present: np.ndarray, out: Path, ti: int, tj: int,
               quality: int, flat: np.ndarray | None = None) -> int:
    """Cut a tile memmap (rows north->south) into 50x50 leaf webps, applying per-frame gains and
    the flat-field correction.

    ``gains``/``present`` are (FRAMES, FRAMES) arrays indexed [row(south->north), col].
    """
    z0 = out / "z0"
    z0.mkdir(parents=True, exist_ok=True)
    n = 0
    for lj in range(LEAVES):           # leaf row, south -> north
        y_px0 = (LEAVES - 1 - lj) * LEAF_PX  # memmap row of the leaf's north edge
        fr_hi = FRAMES - 1 - (y_px0 // CROP_PX)                    # frame row at the north edge
        fr_lo = FRAMES - 1 - ((y_px0 + LEAF_PX - 1) // CROP_PX)    # frame row at the south edge
        for li in range(LEAVES):
            x_px0 = li * LEAF_PX
            fc_lo, fc_hi = x_px0 // CROP_PX, (x_px0 + LEAF_PX - 1) // CROP_PX
            if not present[fr_lo:fr_hi + 1, fc_lo:fc_hi + 1].any():
                continue
            win = np.asarray(mm[y_px0:y_px0 + LEAF_PX, x_px0:x_px0 + LEAF_PX], dtype=np.float32)
            if win.max() <= VOID:
                continue
            # per-pixel gain map from the (at most 2x2) frames under this leaf
            g = np.ones((LEAF_PX, LEAF_PX, 1), dtype=np.float32)
            for fr in range(fr_lo, fr_hi + 1):
                fy0 = (FRAMES - 1 - fr) * CROP_PX          # memmap row of this frame's north edge
                r0 = max(fy0 - y_px0, 0)
                r1 = min(fy0 + CROP_PX - y_px0, LEAF_PX)
                for fc in range(fc_lo, fc_hi + 1):
                    fx0 = fc * CROP_PX
                    c0 = max(fx0 - x_px0, 0)
                    c1 = min(fx0 + CROP_PX - x_px0, LEAF_PX)
                    g[r0:r1, c0:c1, 0] = gains[fr, fc]
                    if flat is not None:
                        # the same pixels in frame-local coordinates
                        g[r0:r1, c0:c1, 0] *= flat[y_px0 + r0 - fy0:y_px0 + r1 - fy0,
                                                   x_px0 + c0 - fx0:x_px0 + c1 - fx0]
            arr = np.clip(win * g, 0, 255).astype(np.uint8)
            gi, gj = ti * LEAVES + li, tj * LEAVES + lj
            Image.fromarray(arr).save(z0 / f"{gi}_{gj}.webp", format="WEBP", quality=quality,
                                      method=4)
            n += 1
    return n


def build_levels(out: Path, nx_leaf: int, ny_leaf: int, quality: int) -> int:
    """Coarser levels from the leaves: 2x2 children -> one 500 px parent, until one tile spans the map."""
    levels = 0
    n_i, n_j = nx_leaf, ny_leaf
    while n_i > 1 or n_j > 1:
        z = levels + 1
        src = out / f"z{levels}"
        dst = out / f"z{z}"
        dst.mkdir(exist_ok=True)
        pn_i, pn_j = (n_i + 1) // 2, (n_j + 1) // 2
        made = 0
        for pj in range(pn_j):
            for pi in range(pn_i):
                kids = []
                for dj in range(2):
                    for di in range(2):
                        p = src / f"{2 * pi + di}_{2 * pj + dj}.webp"
                        kids.append((di, dj, p if p.exists() else None))
                if not any(p for _, _, p in kids):
                    continue
                big = Image.new("RGB", (2 * LEAF_PX, 2 * LEAF_PX), (0, 0, 0))
                for di, dj, p in kids:
                    if p is None:
                        continue
                    # child dj=1 is the *northern* one -> top half of the parent image
                    big.paste(Image.open(p).convert("RGB"), (di * LEAF_PX, (1 - dj) * LEAF_PX))
                big.resize((LEAF_PX, LEAF_PX), Image.BOX).save(
                    dst / f"{pi}_{pj}.webp", format="WEBP", quality=quality, method=4)
                made += 1
        log.info("level z%d: %d tiles", z, made)
        levels = z
        n_i, n_j = pn_i, pn_j
    return levels


# ------------------------------------------------------------------------------------- driver

def fly_map(client, carla, tv, *, name: str, twin_dir: Path, bounds: Sequence[float], out: Path,
            alt: float, fov: float, res: int, yaw: float, cams: int, settle: int,
            spectator_ticks: int, min_content: float, quality: int,
            only: set[tuple[int, int]] | None, l0_dir: Path, world=None,
            depth: bool = True, lens_attenuation: float = 0.0,
            exposure: str = "auto") -> dict[str, Any]:
    t_start = time.time()
    out.mkdir(parents=True, exist_ok=True)
    x0, y0, nx, ny = tv.tile_grid(bounds)
    fp = tv.footprint(alt, fov)
    if fp < STEP * 1.5:
        raise SystemExit("footprint %.2f m leaves no overlap around the %.2f m crop" % (fp, STEP))
    log.info("%s: grid %dx%d tiles from (%.0f,%.0f); %d frames/tile; footprint %.2f m, crop %.2f m, "
             "%.2f cm/px, rig of %d cams x %d settle ticks",
             name, nx, ny, x0, y0, FRAMES * FRAMES, fp, STEP, 100.0 / (res / fp), cams, settle)

    # which material tiles and which frame cells hold map (from the 20 cm level-0 mosaic)
    l0_json = l0_dir / f"topview_{name}.json"
    cell_content = None
    want = {(i, j) for j in range(ny) for i in range(nx)}
    if l0_json.exists():
        meta0 = json.loads(l0_json.read_text())
        frac = tv.content_fractions(l0_dir / f"topview_{name}.png", meta0)
        want = {k for k, v in frac.items() if v > min_content}
        lum = np.asarray(Image.open(l0_dir / f"topview_{name}.png").convert("L"))
        ppm = meta0["px_per_m"]
        bx0, by0, _, by1 = meta0["bounds"]

        def cell_content(cx: float, cy: float) -> float:
            px0 = int((cx - STEP / 2 - bx0) * ppm); px1 = int((cx + STEP / 2 - bx0) * ppm)
            py0 = int((by1 - (cy + STEP / 2)) * ppm); py1 = int((by1 - (cy - STEP / 2)) * ppm)
            cell = lum[max(py0, 0):max(py1, 0), max(px0, 0):max(px1, 0)]
            return float((cell > VOID).mean()) if cell.size else 0.0
        log.info("%s: %d of %d tiles hold >%.0f%% map", name, len(want), nx * ny, 100 * min_content)
    if only is not None:
        want &= only

    done_file = out / "tiles_done.json"
    state = json.loads(done_file.read_text()) if done_file.exists() else {"tiles": [], "gains": {}, "bands": {}}
    done_tiles = {tuple(t) for t in state["tiles"]}
    fixed: dict[tuple[int, int], float] = {tuple(map(int, k.split(","))): v for k, v in state["gains"].items()}
    fixed_bands: dict[tuple[int, int], dict[str, float]] = {
        tuple(map(int, k.split(","))): v for k, v in state["bands"].items()}

    world = world or tv._prepare_world(client, carla, name)
    elev = tv.Elevation(twin_dir / "elevation.npz")
    ortho = Ortho(elev, alt, fov, res)
    rig = Rig(world, carla, n=cams, alt=alt, fov=fov, res=res, yaw=yaw, settle=settle, depth=depth)
    flat = FlatField(out / "flat.npz")
    scratch = out / ".tile.mm"
    frames_total = 0
    try:
        for (ti, tj) in sorted(want, key=lambda t: (t[1], t[0])):
            if (ti, tj) in done_tiles:
                log.info("%s tile %d,%d already cut, skipping", name, ti, tj)
                continue
            t_tile = time.time()
            tx0, ty0 = x0 + ti * TILE, y0 + tj * TILE
            gz_c = tv._ground(elev, world, carla, tx0 + TILE / 2, ty0 + TILE / 2)
            rig.park(tx0 + TILE / 2, -(ty0 + TILE / 2), gz_c, spectator_ticks)

            mm = np.memmap(scratch, dtype=np.uint8, mode="w+", shape=(TILE_PX, TILE_PX, 3))
            mm[:] = 0
            present = np.zeros((FRAMES, FRAMES), dtype=bool)
            bands: dict[tuple[int, int], dict[str, float]] = {}
            n_frames = 0
            for fr in range(FRAMES):
                cy = ty0 + (fr + 0.5) * STEP
                cols = [fc for fc in range(FRAMES)
                        if cell_content is None or cell_content(tx0 + (fc + 0.5) * STEP, cy) > 0.02]
                for g0 in range(0, len(cols), cams):
                    group = cols[g0:g0 + cams]
                    poses, meta = [], []
                    for fc in group:
                        cx = tx0 + (fc + 0.5) * STEP
                        gz = elev.at(cx, cy)
                        poses.append((cx, -cy, gz))
                        meta.append((fc, cx, gz))
                    frames = rig.shoot(poses)
                    for (fc, cx, gz), got in zip(meta, frames):
                        if got is None:
                            log.warning("%s tile %d,%d frame %d,%d: no image", name, ti, tj, fr, fc)
                            continue
                        frame, dep = got
                        crop = ortho.rectify(frame, cx, cy, gz, dep)
                        flat.add(crop)
                        r0 = (FRAMES - 1 - fr) * CROP_PX
                        mm[r0:r0 + CROP_PX, fc * CROP_PX:(fc + 1) * CROP_PX] = crop
                        present[fr, fc] = True
                        key = (tj * FRAMES + fr, ti * FRAMES + fc)
                        bands[key] = band_medians(frame, res)
                        n_frames += 1
                if fr % 8 == 7:
                    log.info("%s tile %d,%d row %d/%d, %d frames, %.0f s", name, ti, tj, fr + 1,
                             FRAMES, n_frames, time.time() - t_tile)
            mm.flush()

            gains = solve_gains(bands, fixed, fixed_bands)
            garr = np.ones((FRAMES, FRAMES), dtype=np.float32)
            for (gr, gc), g in gains.items():
                garr[gr - tj * FRAMES, gc - ti * FRAMES] = g
            flat.save()
            n_leaves = cut_leaves(mm, garr, present, out, ti, tj, quality, flat.correction())
            del mm
            fixed.update(gains)
            fixed_bands.update(bands)
            done_tiles.add((ti, tj))
            state = {"tiles": sorted(done_tiles),
                     "gains": {f"{k[0]},{k[1]}": v for k, v in fixed.items()},
                     "bands": {f"{k[0]},{k[1]}": v for k, v in fixed_bands.items()}}
            done_file.write_text(json.dumps(state))
            frames_total += n_frames
            gv = list(gains.values())
            log.info("%s tile %d,%d: %d frames -> %d leaves, gains %.3f..%.3f, %.0f s (%.2f s/frame)",
                     name, ti, tj, n_frames, n_leaves, min(gv, default=1), max(gv, default=1),
                     time.time() - t_tile, (time.time() - t_tile) / max(n_frames, 1))
    finally:
        rig.close()
        if scratch.exists():
            scratch.unlink()

    levels = build_levels(out, nx * LEAVES, ny * LEAVES, quality)
    n_leaf = sum(1 for _ in (out / "z0").glob("*.webp"))
    size = sum(p.stat().st_size for z in out.glob("z*") for p in z.glob("*.webp"))
    manifest = {
        "map": name, "twin_dir": str(twin_dir),
        "bounds": [x0, y0, x0 + nx * TILE, y0 + ny * TILE],
        "bounds_model_data": [round(float(v), 2) for v in bounds],
        "x0": x0, "y0": y0, "material_tiles": [nx, ny], "tile_m": TILE,
        "leaf_m": LEAF_M, "leaf_px": LEAF_PX, "leaves": [nx * LEAVES, ny * LEAVES],
        "levels": levels + 1, "cm_per_px": CM_PER_PX,
        "camera": {"alt": alt, "fov": fov, "res": res, "yaw": yaw, "footprint_m": round(fp, 2),
                   "step_m": STEP, "cams": cams, "settle": settle,
                   "ortho": "depth camera" if depth else "twin DEM",
                   "lens_attenuation": lens_attenuation or LENS_ATTENUATION_DEFAULT,
                   "exposure": exposure,
                   "flat_field_frames": flat.n},
        "webp_quality": quality, "leaf_tiles": n_leaf, "bytes": size,
        "frames": frames_total, "seconds": round(time.time() - t_start, 1),
        "tile_url": "z{z}/{i}_{j}.webp",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("%s: %d leaves, %d levels, %.0f MB, %d frames in %.0f s", name, n_leaf, levels + 1,
             size / 1e6, frames_total, manifest["seconds"])
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default="out/lowfly")
    ap.add_argument("--l0", default="out/review", help="where the level-0 mosaics live (content mask)")
    ap.add_argument("--map", action="append", default=[], metavar="NAME[=TWINDIR]")
    ap.add_argument("--alt", type=float, default=5.0)
    ap.add_argument("--fov", type=float, default=90.0)
    ap.add_argument("--res", type=int, default=1024)
    ap.add_argument("--yaw", type=float, default=-90.0)
    ap.add_argument("--cams", type=int, default=8)
    ap.add_argument("--settle", type=int, default=5)
    ap.add_argument("--spectator-ticks", type=int, default=25)
    ap.add_argument("--min-content", type=float, default=0.03)
    ap.add_argument("--webp-quality", type=int, default=82)
    ap.add_argument("--only-tile", default="", metavar="IX,IY[;IX,IY...]")
    ap.add_argument("--levels-only", action="store_true", help="rebuild z1.. from the z0 leaves")
    ap.add_argument("--fresh", action="store_true", help="ignore tiles_done.json and refly")
    ap.add_argument("--no-depth", action="store_true", help="orthorectify on the DEM only")
    ap.add_argument("--lens-attenuation", type=float, default=0.44,
                    help="r.EyeAdaptation.LensAttenuation during the flight (engine default 0.78 "
                         "clips pale surfaces; 0 = leave the engine alone)")
    ap.add_argument("--exposure", choices=("manual", "auto"), default="manual",
                    help="manual = r.EyeAdaptation.MethodOverride 3 for the flight: identical "
                         "exposure on every frame (auto lags the teleports by up to 10 %%)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")

    tv = _load_topview()
    rr = tv._load_region_review()
    root = Path(args.root)
    specs = []
    if args.map:
        by_default = dict(tv.DEFAULT_MAPS)
        for s in args.map:
            nm, _, rel = s.partition("=")
            specs.append((nm, rel or by_default.get(nm) or ""))
    else:
        specs = list(tv.DEFAULT_MAPS)
    only = None
    if args.only_tile:
        only = {tuple(int(v) for v in part.split(",")) for part in args.only_tile.split(";")}
    out_root = Path(args.out) if Path(args.out).is_absolute() else root / args.out
    l0_dir = Path(args.l0) if Path(args.l0).is_absolute() else root / args.l0

    if args.levels_only:
        for nm, rel in specs:
            out = out_root / nm
            man = json.loads((out / "manifest.json").read_text())
            levels = build_levels(out, *man["leaves"], args.webp_quality)
            man["levels"] = levels + 1
            man["bytes"] = sum(p.stat().st_size for z in out.glob("z*") for p in z.glob("*.webp"))
            (out / "manifest.json").write_text(json.dumps(man, indent=2))
        return 0

    import carla  # noqa: PLC0415
    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    if args.lens_attenuation > 0:
        console_command(args.host, args.port,
                        "r.EyeAdaptation.LensAttenuation %g" % args.lens_attenuation)
    if args.exposure == "manual":
        console_command(args.host, args.port, "r.EyeAdaptation.MethodOverride 3")
    try:
        _fly_all(args, specs, only, out_root, l0_dir, client, carla, tv, rr)
    finally:
        if args.exposure == "manual":
            console_command(args.host, args.port, "r.EyeAdaptation.MethodOverride -1")
        if args.lens_attenuation > 0:
            console_command(args.host, args.port,
                            "r.EyeAdaptation.LensAttenuation %g" % LENS_ATTENUATION_DEFAULT)
    return 0


def _fly_all(args, specs, only, out_root, l0_dir, client, carla, tv, rr) -> None:
    root = Path(args.root)
    for nm, rel in specs:
        d = Path(rel) if Path(rel).is_absolute() else root / rel
        if not d.exists():
            log.warning("skipping %s: %s missing", nm, d)
            continue
        out = out_root / nm
        if args.fresh and out.exists():
            shutil.rmtree(out)
        bounds = rr.extract_twin(d, nm, nm)["bounds"]
        world = tv._prepare_world(client, carla, nm)
        try:
            fly_map(client, carla, tv, name=nm, twin_dir=d, bounds=bounds, out=out, alt=args.alt,
                    fov=args.fov, res=args.res, yaw=args.yaw, cams=args.cams, settle=args.settle,
                    spectator_ticks=args.spectator_ticks, min_content=args.min_content,
                    quality=args.webp_quality, only=only, l0_dir=l0_dir, world=world,
                    depth=not args.no_depth, lens_attenuation=args.lens_attenuation,
                    exposure=args.exposure)
        finally:
            tv._release_world(world)


if __name__ == "__main__":
    raise SystemExit(main())
