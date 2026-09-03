#!/usr/bin/env python
"""Run Tile2Net (NYU, sidewalk / crosswalk / road from aerial) on the twin's own ortho.

    python tools/tile2net_run.py out/v10_eixample eixample                 # z19 tiles from the cached ICGC ortho
    python tools/tile2net_run.py out/v10_eixample eixample --zoom 20 --python /path/to/.venv-tile2net/bin/python

Tile2Net only downloads imagery for a handful of US regions, but it accepts a local slippy-tile
directory (``--input path/z/x/y.png``).  This script:

  1. cuts web-mercator XYZ tiles (256 px, ``--zoom``) covering the twin bbox from the cached
     pipeline ortho (``data/ortho_<bbox>_*.tif``, ICGC 10 cm by default) with the same
     reprojection as ``geo_overlay.OrthoTiler``  ->  ``<build_dir>/tile2net/tiles/<z>/<x>/<y>.png``
  2. runs ``tile2net generate`` + ``tile2net inference`` in the Tile2Net env (``--python``,
     default ``<CARLA_SOURCE>/.venv-tile2net/bin/python``; it needs Python <= 3.12)
  3. converts the resulting polygons (EPSG:4326) to model metres and writes
     ``<build_dir>/detect/tile2net_polygons.geojson`` with ``kind`` in {sidewalk, crosswalk, road}
     plus ``<build_dir>/detect/tile2net_network.geojson`` (the pedestrian network centrelines) when
     produced, so ``tools/geo_overlay.py`` shows them next to the twin.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from twinmodel.frame import LocalFrame                       # noqa: E402
from geo_overlay import OrthoTiler, find_input_ortho          # noqa: E402

log = logging.getLogger("tile2net_run")
DEFAULT_PY = ROOT.parent.parent / ".venv-tile2net" / "bin" / "python"


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def cut_tiles(tiler: OrthoTiler, bbox_swne: Sequence[float], zoom: int, out: Path) -> int:
    s_, w_, n_, e_ = bbox_swne
    x0, y0 = lonlat_to_tile(w_, n_, zoom)          # NW corner
    x1, y1 = lonlat_to_tile(e_, s_, zoom)          # SE corner
    n = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            png = tiler.tile_png(zoom, x, y)
            if png is None:
                continue
            p = out / str(zoom) / str(x) / f"{y}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            # tile2net wants opaque RGB tiles
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(png)).convert("RGBA")
            bg = Image.new("RGB", im.size, (0, 0, 0))
            bg.paste(im, mask=im.split()[3])
            bg.save(p, format="PNG", compress_level=3)
            n += 1
    log.info("cut %d tiles at z%d into %s (x %d..%d, y %d..%d)", n, zoom, out, x0, x1, y0, y1)
    return n


def run(build_dir: Path, name: str, *, zoom: int, python: Path, data_dir: Path, skip_inference: bool) -> Path:
    twin_dir = build_dir / f"{name}.twin"
    model = json.loads((twin_dir / "model.json").read_text())
    frame = LocalFrame(model["origin_lat"], model["origin_lon"])
    bbox = [float(v) for v in model["bbox_wgs84"]]
    work = build_dir / "tile2net"
    tiles = work / "tiles"
    work.mkdir(parents=True, exist_ok=True)

    ortho = find_input_ortho(data_dir, bbox)
    if ortho is None:
        raise SystemExit(f"no cached ortho for bbox in {data_dir}; run tools/ortho_detect.py once to fetch it")
    tiler = OrthoTiler(ortho)
    log.info("ortho %s (%s, %.2f m/px)", ortho.name, tiler.detail, tiler.res_m)
    t0 = time.time()
    n = cut_tiles(tiler, bbox, zoom, tiles)
    log.info("tiles ready in %.0fs", time.time() - t0)
    if n == 0:
        raise SystemExit("no tiles cut")

    if skip_inference:
        return work
    if not python.exists():
        raise SystemExit(f"tile2net python not found: {python}")
    loc = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    gen = [str(python), "-m", "tile2net", "generate", "-l", loc, "-n", name, "-o", str(work),
           "-i", str(tiles / "z" / "x" / "y.png"), "-z", str(zoom)]
    log.info("$ %s", " ".join(gen))
    res = subprocess.run(gen, capture_output=True, text=True)
    (work / "generate.log").write_text(res.stdout + "\n--- stderr ---\n" + res.stderr)
    if res.returncode != 0:
        log.error("tile2net generate failed (rc %d); see %s\n%s", res.returncode, work / "generate.log", res.stderr[-2000:])
        raise SystemExit(2)
    # generate prints the project structure as pretty JSON; inference wants the tiles "info" file from it
    info_path = None
    try:
        structure = json.loads(res.stdout[res.stdout.index("{"):])
        info_path = Path(structure["tiles"]["info"])
    except (ValueError, KeyError, json.JSONDecodeError):
        cands = sorted(work.rglob("*_info.json"))
        if not cands:
            log.error("no tiles info json found after generate; stdout:\n%s", res.stdout[-3000:])
            raise SystemExit(2)
        info_path = cands[0]
    if not info_path.is_absolute():
        info_path = Path.cwd() / info_path
    log.info("city_info %s", info_path)
    inf = [str(python), "-u", "-m", "tile2net", "inference", "--city_info", str(info_path), "--dump_percent", "0"]
    log.info("$ %s", " ".join(inf))
    t1 = time.time()
    # stream to a log file with stdin closed: with captured pipes the inference process hung indefinitely
    with (work / "inference.log").open("w") as lf:
        rc = subprocess.run(inf, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, text=True).returncode
    log.info("inference rc %d in %.0fs; log %s", rc, time.time() - t1, work / "inference.log")
    if rc != 0:
        log.error("tile2net inference failed; tail of log:\n%s", (work / "inference.log").read_text()[-3000:])
        raise SystemExit(3)
    convert_outputs(work, frame, build_dir / "detect")
    return work


def convert_outputs(work: Path, frame: LocalFrame, detect_dir: Path) -> None:
    """Tile2Net writes polygons/ and network/ (shapefile or geojson, EPSG:4326) -> model-metre GeoJSON."""
    import geopandas as gpd
    from shapely.ops import transform as shp_transform
    detect_dir.mkdir(parents=True, exist_ok=True)
    tf = frame._to_local()

    def to_model(geom):
        return shp_transform(lambda x, y, z=None: tf.transform(x, y), geom)
    for sub, out_name in (("polygons", "tile2net_polygons.geojson"), ("network", "tile2net_network.geojson")):
        files = sorted(list(work.rglob(f"{sub}/**/*.shp")) + list(work.rglob(f"{sub}/**/*.geojson")) + list(work.rglob(f"{sub}/**/*.parquet")))
        if not files:
            log.warning("no %s output under %s", sub, work)
            continue
        frames = []
        for f in files:
            try:
                frames.append(gpd.read_parquet(f) if f.suffix == ".parquet" else gpd.read_file(f))
            except Exception as exc:                          # noqa: BLE001
                log.warning("cannot read %s: %s", f, exc)
        if not frames:
            continue
        g = gpd.pd.concat(frames, ignore_index=True)
        if g.crs is not None and g.crs.to_epsg() != 4326:
            g = g.to_crs(4326)
        kind_col = next((c for c in ("f_type", "type", "class", "label") if c in g.columns), None)
        feats = []
        for _, row in g.iterrows():
            if row.geometry is None or row.geometry.is_empty:
                continue
            props = {"kind": str(row[kind_col]).lower() if kind_col else sub, "source": "tile2net"}
            feats.append({"type": "Feature", "properties": props, "geometry": to_model(row.geometry).__geo_interface__})
        (detect_dir / out_name).write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        counts = {}
        for f in feats:
            counts[f["properties"]["kind"]] = counts.get(f["properties"]["kind"], 0) + 1
        log.info("%s: %d features %s -> %s", sub, len(feats), counts, detect_dir / out_name)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("build_dir"); ap.add_argument("name")
    ap.add_argument("--zoom", type=int, default=19, help="slippy zoom for the tiles Tile2Net sees (19 ~ 0.23 m/px at 41°N, 20 ~ 0.11)")
    ap.add_argument("--python", default=str(DEFAULT_PY), help="python of the Tile2Net env (needs Python <= 3.12)")
    ap.add_argument("--data", default="data"); ap.add_argument("--tiles-only", action="store_true")
    ap.add_argument("--convert-only", action="store_true", help="only convert existing Tile2Net outputs")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(name)s %(message)s")
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    build_dir = Path(a.build_dir)
    if a.convert_only:
        model = json.loads((build_dir / f"{a.name}.twin" / "model.json").read_text())
        convert_outputs(build_dir / "tile2net", LocalFrame(model["origin_lat"], model["origin_lon"]), build_dir / "detect")
        return 0
    out = run(build_dir, a.name, zoom=a.zoom, python=Path(a.python), data_dir=Path(a.data), skip_inference=a.tiles_only)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
