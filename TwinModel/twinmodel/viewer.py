"""Self-contained HTML viewer for a `twinmodel compare` output directory.

`write_viewer(build_dir, name)` reads `<build_dir>/compare/layers.json` + the rasters written by
`twinmodel.compare` and the twin model (`<build_dir>/<name>.twin`), embeds everything as data
URIs into `templates/compare_viewer.html`, and writes `<build_dir>/compare/viewer.html`.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

from shapely.geometry import shape

log = logging.getLogger("twinmodel.viewer")

TEMPLATE = Path(__file__).parent / "templates" / "compare_viewer.html"


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _round_coords(coords, nd: int = 2):
    return [[round(float(x), nd), round(float(y), nd)] for x, y, *_ in coords]


def vectors_from_twin(twin_dir: Path) -> dict:
    """Junction polygons (+centre/area), road reference lines and signals in model space."""
    out = {"junctions": [], "roads": [], "signals": []}
    jpath = twin_dir / "junctions.geojson"
    if jpath.exists():
        for f in json.loads(jpath.read_text())["features"]:
            p = f["properties"]
            if not p.get("has_polygon"):
                continue
            geom = shape(f["geometry"])
            polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
            rings = [_round_coords(pg.exterior.coords) for pg in polys]
            c = geom.centroid
            out["junctions"].append({"id": p["id"], "rings": rings, "centre": [round(c.x, 2), round(c.y, 2)],
                                     "area": round(float(geom.area), 1), "connections": len(p.get("connections", []))})
    rpath = twin_dir / "roads.geojson"
    if rpath.exists():
        for f in json.loads(rpath.read_text())["features"]:
            out["roads"].append(_round_coords(f["geometry"]["coordinates"]))
    spath = twin_dir / "signals.geojson"
    if spath.exists():
        for f in json.loads(spath.read_text())["features"]:
            x, y, *_ = f["geometry"]["coordinates"]
            out["signals"].append({"x": round(x, 2), "y": round(y, 2), "kind": f["properties"].get("kind", "")})
    return out


def write_viewer(build_dir: Path | str, name: str, out_html: Path | str | None = None,
                 title: str | None = None) -> Path:
    build_dir = Path(build_dir)
    cmp_dir = build_dir / "compare"
    layers = json.loads((cmp_dir / "layers.json").read_text())
    files = layers.get("files", {})

    def pick(key: str, *fallbacks: str) -> Path:
        cand = [files.get(key)] + list(fallbacks)
        for c in cand:
            if c and (cmp_dir / Path(c).name).exists():
                return cmp_dir / Path(c).name
        raise FileNotFoundError(f"compare raster for {key!r} not found in {cmp_dir}")

    imgs = {
        "osm": pick("osm_tiles", "osm_tiles.png"),
        "ortho": pick("ortho", "ortho.jpg", "ortho.png"),
        "mesh": pick("mesh_top", "mesh_top.png"),
        "diff": pick("diff", "diff.png"),
    }
    total = sum(p.stat().st_size for p in imgs.values())
    if total > 14_000_000:
        log.warning("embedded rasters total %.1f MB — the artifact limit is 16 MB", total / 1e6)

    vectors = vectors_from_twin(build_dir / f"{name}.twin")
    html = TEMPLATE.read_text()
    html = (html.replace("__TITLE__", title or f"{name.capitalize()} Twin Compare")
                .replace("__LAYERS_JSON__", json.dumps(layers))
                .replace("__VECTORS_JSON__", json.dumps(vectors))
                .replace("__IMG_OSM__", _data_uri(imgs["osm"]))
                .replace("__IMG_ORTHO__", _data_uri(imgs["ortho"]))
                .replace("__IMG_MESH__", _data_uri(imgs["mesh"]))
                .replace("__IMG_DIFF__", _data_uri(imgs["diff"])))
    out = Path(out_html) if out_html else cmp_dir / "viewer.html"
    out.write_text(html)
    log.info("wrote %s (%.1f MB, %d junctions, %d roads, %d signals)", out, out.stat().st_size / 1e6,
             len(vectors["junctions"]), len(vectors["roads"]), len(vectors["signals"]))
    return out


if __name__ == "__main__":  # python -m twinmodel.viewer BUILD_DIR NAME [OUT]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    write_viewer(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
