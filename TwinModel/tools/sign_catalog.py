"""Traffic-sign catalog tooling, editor-free half.

The CARLA sign art is a set of 4096^2 atlases (``Carla/Static/Signs/DataAssetsTextures/SignsAtlases/
<STYLE>/T_<STYLE>_<Category>SignAtlas_NN``), 4 x 4 cells each, sampled by
``M_SignTextureAtlasSelector`` as ``uv = (TexCoord + (Index_X - 1, Index_Y - 1)) / Scale`` with
``Scale = 4``: **indices are 1-based, (1, 1) is the top-left cell**, blank cells are plain white.
One ``USignDataAsset`` (sign mesh + atlas + cell) describes one sign; this module builds and
checks the curated map that drives their generation (``ue/gen_sign_dataassets.py``).

Stages::

    sign_catalog.py sheets --png out/signs/png --out out/signs
        slice every atlas PNG (exported by the editor probe) into cells, flag the blank ones,
        guess each silhouette, write contact sheets (out/signs/sheets/*.png), per-cell crops
        (out/signs/cells/<atlas>/<x>_<y>.png) and the autofill map
        out/signs/sign_atlas_cells.autofill.yaml that a person completes.
    sign_catalog.py check --map ue/assets/sign_atlas_cells.yaml [--png out/signs/png]
        validate the curated map: every non-blank cell named, mesh known, no duplicate names,
        and (with --png) no drift between the curated blank flags and the detector.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw
from scipy import ndimage

GRID = 4
STYLES = ("VC", "MUTCD", "GB", "Miscellaneous")
CATEGORIES = ("Priority", "Mandatory", "Prohibitory", "Warning", "Information", "Guide", "Others", "SpeedLimit")
PLUG_SIGNS = "/CarlaDigitalTwinsTool/Carla/Static/Signs"
ATLAS_RE = re.compile(r"^T_(?:(?P<style>VC|MUTCD|GB)_)?(?P<cat>[A-Za-z]+)SignAtlas_(?P<n>\d+)$")

# silhouette classes -> the SignShapes mesh that carries them; rectangles are resolved to the
# SM_HorRect_NN / SM_VertRect_NN whose width/height ratio is closest (bounds from probe2.json)
SHAPE_MESH = {
    "circle": "SM_CircleShape",
    "triangle": "SM_DangerSignShape",
    "inv_triangle": "SM_InvertedTriangleShape",
    "octagon": "SM_OctogonalShape",
    "diamond": "SM_RomboidShape",
    "square": "SM_SquareShape",
}
RECT_PREFIX = {"hrect": "SM_HorRect_", "vrect": "SM_VertRect_"}


def load_mesh_aspects(probe2: Path | None) -> dict[str, float]:
    """mesh name -> width/height from the editor probe's bounds (x extent / z extent)."""
    if probe2 is None or not probe2.exists():
        return {}
    with open(probe2) as f:
        shapes = json.load(f)["shapes"]
    out = {}
    for path, info in shapes.items():
        ext = info.get("extent_cm")
        if ext and ext[2] > 0:
            out[path.split("/")[-1]] = ext[0] / ext[2]
    return out


def resolve_mesh(shape: str | None, aspect: float, mesh_aspects: dict[str, float]) -> str | None:
    if shape is None:
        return None
    if shape in SHAPE_MESH:
        return SHAPE_MESH[shape]
    prefix = RECT_PREFIX[shape]
    cands = {k: v for k, v in mesh_aspects.items() if k.startswith(prefix)}
    if not cands:
        return prefix + "??"
    return min(cands, key=lambda k: abs(cands[k] - aspect))
WHITE = 235          # a channel above this is "paper"
BLANK_FRACTION = 0.002


# ----------------------------------------------------------------------------- slicing
def cell_mask(cell: np.ndarray) -> np.ndarray:
    """Filled silhouette of the sign in a cell: everything not connected to the white border."""
    ink = (cell[..., :3] < WHITE).any(axis=2)
    filled = ndimage.binary_fill_holes(ink)
    # keep only the largest component (drops stray speckles)
    lab, n = ndimage.label(filled)  # type: ignore[misc]
    if n <= 1:
        return np.asarray(filled, dtype=bool)
    sizes = np.asarray(ndimage.sum(filled, lab, list(range(1, n + 1))))
    return np.asarray(lab == (int(np.argmax(sizes)) + 1), dtype=bool)


def guess_shape(mask: np.ndarray) -> dict:
    """Classify a filled silhouette by bbox aspect, fill ratio and corner / edge-midpoint hits."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return {"shape": None}
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    w, h = x1 - x0, y1 - y0
    sub = mask[y0:y1, x0:x1]
    fill = float(sub.mean())
    aspect = float(w) / float(h)
    k = 0.10  # probe inset (fraction of the bbox side)
    px = lambda f: min(w - 1, max(0, int(f * (w - 1))))
    py = lambda f: min(h - 1, max(0, int(f * (h - 1))))
    corners = [bool(sub[py(k), px(k)]), bool(sub[py(k), px(1 - k)]), bool(sub[py(1 - k), px(k)]), bool(sub[py(1 - k), px(1 - k)])]
    mids = [bool(sub[py(0.5), px(0.02)]), bool(sub[py(0.5), px(0.98)]), bool(sub[py(0.02), px(0.5)]), bool(sub[py(0.98), px(0.5)])]
    tl, tr, bl, br = corners
    if aspect > 1.25:
        shape = "hrect"
    elif aspect < 0.8:
        shape = "vrect"
    elif not any(corners) and fill < 0.62:
        shape = "diamond"
    elif (not tl and not tr) and (bl and br) and fill < 0.7:
        shape = "triangle"
    elif (tl and tr) and (not bl and not br) and fill < 0.7:
        shape = "inv_triangle"
    elif all(corners) and fill > 0.9:
        shape = "square"
    elif fill > 0.81:
        shape = "octagon" if not all(corners) else "square"
    else:
        shape = "circle"
    W, H = float(mask.shape[1]), float(mask.shape[0])
    return {"shape": shape, "fill": round(fill, 3), "aspect": round(aspect, 3),
            "bbox_frac": [round(x0 / W, 3), round(y0 / H, 3), round(w / W, 3), round(h / H, 3)],
            "corners": corners, "mids": mids}


def atlas_meta(png: Path, root: Path) -> dict | None:
    rel = png.relative_to(root)
    parts = rel.parts
    if parts[0] != "DataAssetsTextures" or parts[1] != "SignsAtlases":
        return None
    m = ATLAS_RE.match(png.stem)
    style = parts[2]
    cat = m.group("cat") if m else None
    if cat == "Miscellaneous":
        cat = "Others"
    return {"texture": "%s/%s" % (PLUG_SIGNS, rel.with_suffix("").as_posix()), "png": str(png), "style": style,
            "category": cat, "atlas": png.stem}


def stage_sheets(png_root: Path, out: Path, grid: int = GRID, probe2: Path | None = None) -> dict:
    sheets = out / "sheets"
    cells_dir = out / "cells"
    sheets.mkdir(parents=True, exist_ok=True)
    mesh_aspects = load_mesh_aspects(probe2)
    atlases = []
    for png in sorted(png_root.rglob("*.png")):
        meta = atlas_meta(png, png_root)
        if meta is None:
            continue
        im = Image.open(png).convert("RGBA")
        arr = np.asarray(im)
        n = arr.shape[0] // grid
        cells = []
        sheet = im.resize((1024, 1024))
        d = ImageDraw.Draw(sheet, "RGBA")
        c = 1024 // grid
        (cells_dir / meta["atlas"]).mkdir(parents=True, exist_ok=True)
        for y in range(grid):
            for x in range(grid):
                cell = arr[y * n:(y + 1) * n, x * n:(x + 1) * n]
                ink = float((cell[..., :3] < WHITE).any(axis=2).mean())
                blank = ink < BLANK_FRACTION
                row: dict = {"x": x + 1, "y": y + 1}
                if blank:
                    row["blank"] = True
                    d.rectangle([x * c, y * c, (x + 1) * c, (y + 1) * c], fill=(0, 0, 0, 110))
                    d.line([(x * c, y * c), ((x + 1) * c, (y + 1) * c)], fill=(255, 60, 60, 200), width=3)
                else:
                    g = guess_shape(cell_mask(cell))
                    mesh = resolve_mesh(g["shape"], g.get("aspect", 1.0), mesh_aspects)
                    row.update({"name": "", "description": "", "meaning": "", "xodr": "", "osm": "",
                                "shape": mesh, "shape_guessed": True, "silhouette": g["shape"], "ink": round(ink, 4),
                                "silhouette_stats": {k: g[k] for k in ("fill", "aspect", "bbox_frac") if k in g}})
                    Image.fromarray(cell).resize((256, 256)).save(cells_dir / meta["atlas"] / ("%d_%d.png" % (x + 1, y + 1)))
                    d.rectangle([x * c + 4, (y + 1) * c - 26, x * c + 200, (y + 1) * c - 4], fill=(0, 0, 0, 170))
                    d.text((x * c + 8, (y + 1) * c - 22), "%s %s" % (g["shape"] or "?", (mesh or "").replace("SM_", "")), fill=(120, 255, 120, 255))
                cells.append(row)
                d.rectangle([x * c + 4, y * c + 4, x * c + 64, y * c + 26], fill=(0, 0, 0, 200))
                d.text((x * c + 8, y * c + 8), "%d,%d" % (x + 1, y + 1), fill=(255, 255, 0, 255))
        for i in range(grid + 1):
            d.line([(i * c, 0), (i * c, 1024)], fill=(255, 0, 255, 255), width=2)
            d.line([(0, i * c), (1024, i * c)], fill=(255, 0, 255, 255), width=2)
        sheet_path = sheets / (meta["atlas"] + ".png")
        sheet.save(sheet_path)
        entry = {"texture": meta["texture"], "style": meta["style"], "category": meta["category"], "sheet": str(sheet_path), "cells": cells}
        atlases.append(entry)
        n_blank = sum(1 for r in cells if r.get("blank"))
        print("%-32s %-6s %-12s cells %2d  blank %2d" % (meta["atlas"], meta["style"], meta["category"], grid * grid - n_blank, n_blank))
    doc = {"version": 1, "grid": {"cols": grid, "rows": grid}, "index_base": 1, "origin": "top-left", "atlases": atlases}
    with open(out / "sign_atlas_cells.autofill.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
    return doc


# ----------------------------------------------------------------------------- validation
def load_map(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def check_map(doc: dict, shapes: set[str] | None = None, png_root: Path | None = None) -> list[str]:
    errs: list[str] = []
    grid = doc.get("grid", {"cols": GRID, "rows": GRID})
    names: dict[tuple[str, str], str] = {}
    seen_cells: set[tuple[str, int, int]] = set()
    for a in doc.get("atlases", []):
        style, cat, tex = a.get("style"), a.get("category"), a.get("texture")
        if style not in STYLES:
            errs.append("%s: unknown style %r" % (tex, style))
        if cat not in CATEGORIES:
            errs.append("%s: unknown category %r" % (tex, cat))
        for c in a.get("cells", []):
            x, y = c.get("x"), c.get("y")
            key = (tex, x, y)
            if key in seen_cells:
                errs.append("%s: cell (%s,%s) listed twice" % (tex, x, y))
            seen_cells.add(key)
            if not (1 <= x <= grid["cols"] and 1 <= y <= grid["rows"]):
                errs.append("%s: cell (%s,%s) outside the %dx%d grid" % (tex, x, y, grid["cols"], grid["rows"]))
            if c.get("blank"):
                continue
            name = c.get("name")
            if not name:
                errs.append("%s: cell (%s,%s) has no name" % (tex, x, y))
                continue
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
                errs.append("%s: cell (%s,%s) name %r is not an identifier" % (tex, x, y, name))
            if c.get("shape_guessed"):
                errs.append("%s: cell (%s,%s) %s still carries shape_guessed" % (tex, x, y, name))
            shape = c.get("shape")
            if not shape:
                errs.append("%s: cell (%s,%s) %s has no shape" % (tex, x, y, name))
            elif shapes is not None and shape not in shapes:
                errs.append("%s: cell (%s,%s) %s shape %r is not a SignShapes mesh" % (tex, x, y, name, shape))
            ccat = c.get("category", cat)
            if ccat not in CATEGORIES:
                errs.append("%s: cell (%s,%s) %s category %r unknown" % (tex, x, y, name, ccat))
            k = (style, name)
            if k in names:
                errs.append("%s: (%s) name %r also used by %s" % (tex, style, name, names[k]))
            names[k] = "%s(%s,%s)" % (tex, x, y)
    for u in doc.get("uniques", []):
        name, style = u.get("name"), u.get("style")
        if not name or not u.get("texture") or not u.get("shape"):
            errs.append("unique %r incomplete" % (name,))
            continue
        k = (style, name)
        if k in names:
            errs.append("unique %s/%s collides with %s" % (style, name, names[k]))
        names[k] = "unique " + u["texture"]
    if png_root is not None:
        det = {}
        for a in stage_sheets_detect_only(png_root):
            for c in a["cells"]:
                det[(a["texture"], c["x"], c["y"])] = bool(c.get("blank"))
        # a curator may blank a cell the detector kept (coloured backgrounds read as ink); the
        # reverse -- naming a cell the detector found empty -- is the drift worth failing on
        for a in doc.get("atlases", []):
            for c in a.get("cells", []):
                k = (a["texture"], c["x"], c["y"])
                if k in det and det[k] and not c.get("blank"):
                    errs.append("%s: cell (%s,%s) %s is named but the detector finds it blank" % (a["texture"], c["x"], c["y"], c.get("name")))
    return errs


def stage_sheets_detect_only(png_root: Path, grid: int = GRID):
    for png in sorted(png_root.rglob("*.png")):
        meta = atlas_meta(png, png_root)
        if meta is None:
            continue
        arr = np.asarray(Image.open(png).convert("RGBA"))
        n = arr.shape[0] // grid
        cells = []
        for y in range(grid):
            for x in range(grid):
                cell = arr[y * n:(y + 1) * n, x * n:(x + 1) * n]
                ink = float((cell[..., :3] < WHITE).any(axis=2).mean())
                cells.append({"x": x + 1, "y": y + 1, "blank": ink < BLANK_FRACTION})
        yield {"texture": meta["texture"], "cells": cells}


# ----------------------------------------------------------------------------- merge
# The one-off (non-atlas) textures under DataAssetsTextures/Textures/VC, with the plate that
# carries each; the "_sided" variants use the meshes pivoted on one edge (wall / pole mounted).
UNIQUES = [
    ("arrow_sign", "T_Arrow", "SM_ArrowShape", "Direction arrow plate"),
    ("uab_welcome", "T_BenvigutsALaAutonoma", "SM_SlightRectangleShape", "Benvinguts a l'Autonoma (UAB campus welcome)"),
    ("uab_welcome_sided", "T_BenvigutsALaAutonoma", "SM_SlightRectangleShapeSided", "Benvinguts a l'Autonoma, edge-pivoted plate"),
    ("campus_saludable", "T_CampusSaludable", "SM_VerticalRectangle", "Campus Saludable (UAB) banner"),
    ("ciencies_biociencies", "T_CienciesIBiociencies", "SM_VerticalRectangle", "Ciencies i Biociencies (UAB faculty) banner"),
    ("directions_double", "T_DoubleSign", "SM_DoubleSign", "Two-arm direction sign"),
    ("double_arrow", "t_DoubleArrow", "SM_SmallRectangle", "Double arrow plate"),
    ("long_thin_sign", "T_LongThinSign", "SM_LongThinShape", "Long thin direction plate"),
    ("roundabout_cerdanyola", "T_RoundAboutCerdanyola", "SM_SlightRectangleShape", "Roundabout directions (Cerdanyola)"),
    ("roundabout_cerdanyola_sided", "T_RoundAboutCerdanyola", "SM_SlightRectangleShapeSided", "Roundabout directions (Cerdanyola), edge-pivoted"),
    ("uab_directions_01", "T_UABDirections", "SM_LargeSlightRectangleShape", "UAB campus directions 1"),
    ("uab_directions_01_sided", "T_UABDirections", "SM_LargeSlightRectangleShapeSided", "UAB campus directions 1, edge-pivoted"),
    ("uab_directions_02", "T_UABDirections_02", "SM_LargeSlightRectangleShape", "UAB campus directions 2"),
    ("uab_directions_02_sided", "T_UABDirections_02", "SM_LargeSlightRectangleShapeSided", "UAB campus directions 2, edge-pivoted"),
]
CELL_KEYS = ("x", "y", "blank", "name", "description", "meaning", "xodr", "osm", "shape", "category")


def stage_merge(curated: Path, autofill: Path, out: Path) -> dict:
    """Combine the per-atlas curated files (in the autofill's atlas order) into the catalog map."""
    base = load_map(autofill)
    atlases = []
    missing = []
    for a in base["atlases"]:
        name = a["texture"].split("/")[-1]
        f = curated / (name + ".yaml")
        if not f.exists():
            missing.append(name)
            continue
        cur = load_map(f)
        cells = []
        for c in sorted(cur.get("cells", []), key=lambda c: (c["y"], c["x"])):
            row = {k: c[k] for k in CELL_KEYS if k in c and c[k] not in ("", None)}
            if row.get("blank"):
                row = {"x": row["x"], "y": row["y"], "blank": True}
            cells.append(row)
        atlases.append({"texture": a["texture"], "style": cur.get("style", a["style"]), "category": cur.get("category", a["category"]), "cells": cells})
    if missing:
        raise SystemExit("no curated file for: %s" % ", ".join(missing))
    uniques = [{"name": n, "texture": "%s/DataAssetsTextures/Textures/VC/%s" % (PLUG_SIGNS, t), "style": "VC", "category": "Guide",
                "shape": m, "description": d, "unique": True} for n, t, m, d in UNIQUES]
    doc = {"version": 1, "grid": base["grid"], "index_base": 1, "origin": "top-left", "atlases": atlases, "uniques": uniques}
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        f.write("# Curated traffic-sign catalog: one entry per atlas cell / one-off texture.\n"
                "# Built by tools/sign_catalog.py merge from out/signs/curated/*.yaml; hand-edit here afterwards.\n"
                "# Cells are 1-based (x = column, y = row, (1,1) top-left); see tools/sign_catalog.py.\n")
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=160)
    # the editor's embedded Python has no PyYAML: ship the same map as JSON for ue/gen_sign_dataassets.py
    with open(out.with_suffix(".json"), "w") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    n = sum(1 for a in atlases for c in a["cells"] if not c.get("blank"))
    print("merged %d atlases, %d signs + %d uniques -> %s" % (len(atlases), n, len(uniques), out))
    return doc


def write_map(doc: dict, out: Path) -> None:
    with open(out, "w") as f:
        f.write("# Curated traffic-sign catalog: one entry per atlas cell / one-off texture.\n"
                "# Built by tools/sign_catalog.py merge from out/signs/curated/*.yaml; hand-edit here afterwards.\n"
                "# Cells are 1-based (x = column, y = row, (1,1) top-left); see tools/sign_catalog.py.\n")
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=160)
    with open(out.with_suffix(".json"), "w") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)


def stage_apply_review(map_path: Path, review_dir: Path, out: Path | None = None) -> dict:
    """Fold the answers of the review page (tools/sign_review_page.py) into the catalog map.

    ``review_dir`` holds one JSON document per cell as the artifact database exports them
    (``<review_dir>/review/<AtlasShort>__<x>_<y>.json`` or flat ``<review_dir>/*.json``): fields
    atlas, x, y, verdict (ok | wrong | notsign), name, code, xodr, sub, note.
    """
    doc = load_map(map_path)
    by_atlas = {a["texture"].split("/")[-1]: a for a in doc["atlases"]}
    files = sorted(review_dir.rglob("*.json"))
    stats = {"docs": 0, "renamed": 0, "blanked": 0, "coded": 0, "xodr": 0, "unmatched": 0, "unanswered": 0}
    for f in files:
        with open(f) as fh:
            r = json.load(fh)
        if not isinstance(r, dict) or "atlas" not in r:
            continue
        stats["docs"] += 1
        atlas = by_atlas.get(r["atlas"])
        cell = next((c for c in atlas["cells"] if c["x"] == r["x"] and c["y"] == r["y"]), None) if atlas else None
        if cell is None:
            stats["unmatched"] += 1
            continue
        verdict = r.get("verdict") or ""
        if verdict == "notsign":
            cell.clear()
            cell.update({"x": r["x"], "y": r["y"], "blank": True})
            stats["blanked"] += 1
            continue
        if verdict == "wrong" and r.get("name"):
            new = re.sub(r"[^a-z0-9]+", "_", r["name"].lower()).strip("_")
            if new and new != cell.get("name"):
                cell["description"] = "%s (was: %s — %s)" % (r["name"].strip(), cell.get("name", ""), cell.get("description", ""))
                cell["name"] = new
                stats["renamed"] += 1
        if not verdict and not (r.get("code") or r.get("xodr")):
            stats["unanswered"] += 1
        code = (r.get("code") or "").strip()
        if code and code != cell.get("meaning"):
            cell["meaning"] = code
            stats["coded"] += 1
        xodr = (r.get("xodr") or "").strip()
        if xodr and xodr != "other":
            sub = (r.get("sub") or "").strip()
            val = "%s-%s" % (xodr, sub) if sub else xodr
            if val != str(cell.get("xodr", "")):
                cell["xodr"] = val
                stats["xodr"] += 1
        note = (r.get("note") or "").strip()
        if note:
            cell["description"] = (cell.get("description", "") + " [review: %s]" % note).strip()
        # keep key order stable for the diff
        ordered = {k: cell[k] for k in CELL_KEYS if k in cell and cell[k] not in ("", None)}
        cell.clear()
        cell.update(ordered)
    write_map(doc, out or map_path)
    print("applied review: %s -> %s" % (json.dumps(stats), out or map_path))
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="stage", required=True)
    r = sub.add_parser("apply-review", help="fold the review page's answers (exported JSON docs) into the map")
    r.add_argument("--map", required=True)
    r.add_argument("--review", required=True, help="directory with the exported review/*.json documents")
    r.add_argument("--out", default=None, help="write here instead of overwriting --map")
    m = sub.add_parser("merge")
    m.add_argument("--curated", required=True)
    m.add_argument("--autofill", required=True)
    m.add_argument("--out", required=True)
    s = sub.add_parser("sheets")
    s.add_argument("--png", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--grid", type=int, default=GRID)
    s.add_argument("--shapes", default=None, help="probe2.json with the SignShapes mesh bounds (rectangle resolution)")
    c = sub.add_parser("check")
    c.add_argument("--map", required=True)
    c.add_argument("--png", default=None, help="atlas PNG root; enables the blank-drift check")
    c.add_argument("--shapes", default=None, help="probe2.json (mesh inventory) to validate shape names against")
    args = ap.parse_args(argv)
    if args.stage == "sheets":
        stage_sheets(Path(args.png), Path(args.out), args.grid, Path(args.shapes) if args.shapes else None)
        return 0
    if args.stage == "merge":
        stage_merge(Path(args.curated), Path(args.autofill), Path(args.out))
        return 0
    if args.stage == "apply-review":
        stage_apply_review(Path(args.map), Path(args.review), Path(args.out) if args.out else None)
        return 0
    doc = load_map(Path(args.map))
    shapes = None
    if args.shapes:
        with open(args.shapes) as f:
            shapes = {k.split("/")[-1] for k in json.load(f)["shapes"]}
    errs = check_map(doc, shapes, Path(args.png) if args.png else None)
    for e in errs:
        print("ERROR", e)
    n = sum(1 for a in doc.get("atlases", []) for c in a.get("cells", []) if not c.get("blank")) + len(doc.get("uniques", []))
    print("%d signs, %d errors" % (n, len(errs)))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
