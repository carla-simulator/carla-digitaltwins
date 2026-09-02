"""Generate the traffic-sign catalog assets from the curated cell map.

Editor Python (headless, needs the CarlaDigitalTwinsTool content mounted and CarlaTools' native
``USignDataAsset``):

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript -script="/abs/ue/gen_sign_dataassets.py \\
        --map /abs/ue/assets/sign_atlas_cells.yaml [--style VC] [--dry-run] [--verify-only]"

For every non-blank cell (and every one-off texture) of the map it creates, under
``--root`` (default ``/CarlaDigitalTwinsTool/Carla/Blueprints/LevelDesign/Signs/Catalog``):

* ``<Style>/<Category>/DA_<name>[_<code>]`` -- a ``USignDataAsset`` with the plate mesh, the atlas
  texture, the 1-based cell, the regional style / category, the convention code, the OpenDRIVE
  signal type it renders and the OSM tags that select it;
* ``<Style>/<Category>/Materials/MI_<name>[_<code>]`` -- a constant instance of
  ``MI_SignTextureAtlasSelector`` with ``Diffuse`` / ``Index_X`` / ``Index_Y`` baked in (``Scale``
  1 for one-off textures), referenced from the DataAsset's ``Material`` so runtime actors can use
  the plate without a dynamic material.

Idempotent: existing assets are updated in place (created / updated / unchanged is logged per
asset). ``--dry-run`` prints the plan and touches nothing. A manifest (asset path -> cell) is
written next to the map (``--manifest``). ``--verify-only`` reloads every asset the map
describes and audits its fields instead of writing.

The generated assets land in the mounted plugin folder (a hardlink clone of the
carla-digitaltwins content checkout); sync them back to the checkout afterwards, e.g.
``cp -al <Plugins/CarlaDigitalTwinsTool/Content/.../Catalog> <carla-digitaltwins/Content/...>``.
"""
import argparse
import json
import os
import re
import sys
import time

import unreal

PLUG = "/CarlaDigitalTwinsTool/Carla"
DEFAULT_ROOT = PLUG + "/Blueprints/LevelDesign/Signs/Catalog"
SHAPES = PLUG + "/Static/Signs/SignShapes"
ATLAS_MI = PLUG + "/Static/Signs/Materials/Atlas/MI_SignTextureAtlasSelector"
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
# the Python API drops the leading E of C++ enum names (ESignStyle -> unreal.SignStyle)
STYLE_ENUM = {"VC": unreal.SignStyle.VC, "MUTCD": unreal.SignStyle.MUTCD, "GB": unreal.SignStyle.GB,
              "Miscellaneous": unreal.SignStyle.NONE}
CATEGORY_ENUM = {"Priority": unreal.SignCategory.PRIORITY, "Mandatory": unreal.SignCategory.MANDATORY,
                 "Prohibitory": unreal.SignCategory.PROHIBITORY, "Warning": unreal.SignCategory.WARNING,
                 "Information": unreal.SignCategory.INFORMATION, "Guide": unreal.SignCategory.GUIDE,
                 "Others": unreal.SignCategory.OTHERS, "SpeedLimit": unreal.SignCategory.SPEED_LIMIT}


def load_map(path):
    """The curated map; YAML when PyYAML is importable, else the JSON twin merge writes next to it."""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    try:
        import yaml  # the editor's embedded Python usually lacks it
    except ImportError:
        alt = os.path.splitext(path)[0] + ".json"
        if not os.path.exists(alt):
            raise RuntimeError("no PyYAML in this Python and no %s next to the map" % alt)
        with open(alt) as f:
            return json.load(f)
    with open(path) as f:
        return yaml.safe_load(f)


def log(msg):
    unreal.log("[gen_signs] %s" % msg)


def warn(msg):
    unreal.log_warning("[gen_signs] %s" % msg)


def asset_name(sign):
    code = re.sub(r"[^A-Za-z0-9]+", "_", sign.get("meaning") or "").strip("_")
    return "DA_%s%s" % (sign["name"], "_" + code if code else "")


def split_xodr(x):
    """'274-30' -> ('274', '30'); '206' -> ('206', '')."""
    if not x:
        return "", ""
    parts = str(x).split("-", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def iter_signs(doc, style_filter=None):
    """Flatten the map: one dict per generated sign."""
    for a in doc.get("atlases", []):
        for c in a.get("cells", []):
            if c.get("blank"):
                continue
            s = dict(c)
            s["texture"] = a["texture"]
            s["style"] = a["style"]
            s.setdefault("category", a["category"])
            s["unique"] = False
            if style_filter and s["style"] != style_filter:
                continue
            yield s
    for u in doc.get("uniques", []):
        s = dict(u)
        s["x"], s["y"] = 1, 1
        s["unique"] = True
        if style_filter and s["style"] != style_filter:
            continue
        yield s


_MESHES = None


def mesh_path(name):
    """SignShapes mesh by name (the rectangles sit in HorShapes/ and VertShapes/ subfolders)."""
    global _MESHES
    if _MESHES is None:
        _MESHES = {}
        for x in EAL.list_assets(SHAPES, recursive=True, include_folder=False):
            p = x.split(".")[0]
            _MESHES[p.split("/")[-1]] = p
    return _MESHES.get(name, "%s/%s" % (SHAPES, name))


def plan_for(sign, root):
    folder = "%s/%s/%s" % (root, sign["style"], sign["category"])
    name = asset_name(sign)
    xt, xs = split_xodr(sign.get("xodr"))
    osm = sign.get("osm") or ""
    return {
        "da": "%s/%s" % (folder, name),
        "mi": "%s/Materials/MI_%s" % (folder, name[3:]),
        "mesh": mesh_path(sign["shape"]),
        "texture": sign["texture"],
        "x": int(sign["x"]), "y": int(sign["y"]), "unique": bool(sign["unique"]),
        "style": sign["style"], "category": sign["category"], "name": sign["name"],
        "description": sign.get("description") or "", "meaning": sign.get("meaning") or "",
        "xodr_type": xt, "xodr_subtype": xs,
        "osm_tags": [t.strip() for t in osm.split(";") if t.strip()],
    }


def load_or_none(path):
    if not EAL.does_asset_exist(path):
        return None
    return EAL.load_asset(path)


def make_or_load(path, cls, factory):
    a = load_or_none(path)
    if a is not None:
        return a, False
    folder, name = path.rsplit("/", 1)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    a = tools.create_asset(name, folder, cls, factory)
    if a is None:
        raise RuntimeError("create_asset failed for " + path)
    return a, True


def set_if_changed(obj, prop, value):
    cur = obj.get_editor_property(prop)
    same = (cur == value)
    if isinstance(value, list):
        same = list(cur) == value
    if same:
        return False
    obj.set_editor_property(prop, value)
    return True


def ensure_material(p, parent_mi, tex, dry):
    changed = False
    mi = load_or_none(p["mi"])
    created = mi is None
    if dry:
        return "create" if created else "check"
    if created:
        mi, _ = make_or_load(p["mi"], unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        changed = True
    if mi.get_editor_property("parent") != parent_mi:
        mi.set_editor_property("parent", parent_mi)
        changed = True
    want = {"Index_X": float(p["x"]), "Index_Y": float(p["y"]), "Scale": 1.0 if p["unique"] else 4.0}
    for k, v in want.items():
        if abs(MEL.get_material_instance_scalar_parameter_value(mi, k) - v) > 1e-6:
            MEL.set_material_instance_scalar_parameter_value(mi, k, v)
            changed = True
    if MEL.get_material_instance_texture_parameter_value(mi, "Diffuse") != tex:
        MEL.set_material_instance_texture_parameter_value(mi, "Diffuse", tex)
        changed = True
    if changed:
        MEL.update_material_instance(mi)
        EAL.save_loaded_asset(mi)
    return "created" if created else ("updated" if changed else "unchanged")


def ensure_dataasset(p, mesh, tex, mi, dry):
    da = load_or_none(p["da"])
    created = da is None
    if dry:
        return "create" if created else "check"
    if created:
        da, _ = make_or_load(p["da"], unreal.SignDataAsset, unreal.DataAssetFactory())
    changed = created
    changed |= set_if_changed(da, "sign_mesh", mesh)
    changed |= set_if_changed(da, "diffuse", tex)
    changed |= set_if_changed(da, "material", mi)
    changed |= set_if_changed(da, "id_x", p["x"])
    changed |= set_if_changed(da, "id_y", p["y"])
    changed |= set_if_changed(da, "unique", p["unique"])
    changed |= set_if_changed(da, "style", STYLE_ENUM[p["style"]])
    changed |= set_if_changed(da, "category", CATEGORY_ENUM[p["category"]])
    changed |= set_if_changed(da, "sign_name", p["name"])
    changed |= set_if_changed(da, "description", p["description"])
    changed |= set_if_changed(da, "meaning", p["meaning"])
    changed |= set_if_changed(da, "xodr_type", p["xodr_type"])
    changed |= set_if_changed(da, "xodr_subtype", p["xodr_subtype"])
    changed |= set_if_changed(da, "osm_tags", p["osm_tags"])
    if changed:
        EAL.save_loaded_asset(da)
    return "created" if created else ("updated" if changed else "unchanged")


def verify(plans):
    """Reload every planned asset and audit its fields; returns the list of problems."""
    problems = []
    seen_cells = {}
    for p in plans:
        da = load_or_none(p["da"])
        if da is None:
            problems.append("%s missing" % p["da"])
            continue
        if not isinstance(da, unreal.SignDataAsset):
            problems.append("%s is a %s" % (p["da"], da.get_class().get_name()))
        mesh = da.get_editor_property("sign_mesh")
        tex = da.get_editor_property("diffuse")
        mat = da.get_editor_property("material")
        if mesh is None or not isinstance(mesh, unreal.StaticMesh):
            problems.append("%s: no plate mesh" % p["da"])
        elif mesh.get_path_name().split(".")[0] != p["mesh"]:
            problems.append("%s: mesh %s != %s" % (p["da"], mesh.get_path_name(), p["mesh"]))
        if tex is None:
            problems.append("%s: no diffuse" % p["da"])
        elif tex.get_path_name().split(".")[0] != p["texture"]:
            problems.append("%s: texture %s != %s" % (p["da"], tex.get_path_name(), p["texture"]))
        if mat is None:
            problems.append("%s: no material" % p["da"])
        else:
            for k, v in (("Index_X", float(p["x"])), ("Index_Y", float(p["y"])), ("Scale", 1.0 if p["unique"] else 4.0)):
                got = MEL.get_material_instance_scalar_parameter_value(mat, k)
                if abs(got - v) > 1e-6:
                    problems.append("%s: material %s=%s != %s" % (p["da"], k, got, v))
            if MEL.get_material_instance_texture_parameter_value(mat, "Diffuse") != tex:
                problems.append("%s: material diffuse differs from the DataAsset's" % p["da"])
        x, y = da.get_editor_property("id_x"), da.get_editor_property("id_y")
        if not (1 <= x <= 4 and 1 <= y <= 4) or x != p["x"] or y != p["y"]:
            problems.append("%s: cell (%s,%s) != (%s,%s)" % (p["da"], x, y, p["x"], p["y"]))
        if bool(da.get_editor_property("unique")) != p["unique"]:
            problems.append("%s: unique flag" % p["da"])
        if da.get_editor_property("sign_name") != p["name"]:
            problems.append("%s: sign_name %r" % (p["da"], da.get_editor_property("sign_name")))
        key = (p["texture"], x, y, p["unique"] and p["mesh"])
        if key in seen_cells and not p["unique"]:
            problems.append("%s and %s share cell (%s,%s) of %s" % (p["da"], seen_cells[key], x, y, p["texture"]))
        seen_cells[key] = p["da"]
    return problems


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--style", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--manifest", default=None, help="default: <map dir>/sign_catalog_manifest.json")
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)
    t0 = time.time()
    doc = load_map(args.map)
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(["/CarlaDigitalTwinsTool"], True)
    plans = [plan_for(s, args.root) for s in iter_signs(doc, args.style)]
    log("%d signs in the map (%s)" % (len(plans), args.style or "all styles"))

    if args.verify_only:
        problems = verify(plans)
        for pr in problems:
            warn(pr)
        log("verify: %d assets, %d problems" % (len(plans), len(problems)))
        if args.report:
            with open(args.report, "w") as f:
                json.dump({"assets": len(plans), "problems": problems}, f, indent=1)
        return 1 if problems else 0

    parent_mi = EAL.load_asset(ATLAS_MI)
    if parent_mi is None:
        raise RuntimeError("atlas material instance missing: " + ATLAS_MI)
    counts = {}
    manifest = {}
    for i, p in enumerate(plans):
        mesh = EAL.load_asset(p["mesh"])
        tex = EAL.load_asset(p["texture"])
        if mesh is None or tex is None:
            warn("%s: missing %s" % (p["da"], "mesh " + p["mesh"] if mesh is None else "texture " + p["texture"]))
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        if args.dry_run:
            log("plan %s  mesh=%s tex=%s cell=(%d,%d) unique=%s xodr=%s/%s osm=%s" % (
                p["da"], p["mesh"].split("/")[-1], p["texture"].split("/")[-1], p["x"], p["y"], p["unique"],
                p["xodr_type"], p["xodr_subtype"], p["osm_tags"]))
            continue
        try:
            mi_state = ensure_material(p, parent_mi, tex, False)
            mi = EAL.load_asset(p["mi"])
            da_state = ensure_dataasset(p, mesh, tex, mi, False)
        except Exception as exc:  # keep going, report at the end
            warn("%s: %s" % (p["da"], exc))
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[da_state] = counts.get(da_state, 0) + 1
        counts["mi_" + mi_state] = counts.get("mi_" + mi_state, 0) + 1
        manifest[p["da"]] = {k: p[k] for k in ("mi", "mesh", "texture", "x", "y", "unique", "style", "category", "name",
                                                "meaning", "xodr_type", "xodr_subtype", "osm_tags")}
        if (i + 1) % 50 == 0:
            log("  %d / %d" % (i + 1, len(plans)))
    if args.dry_run:
        log("dry run, nothing written")
        return 0
    manifest_path = args.manifest or os.path.join(os.path.dirname(os.path.abspath(args.map)), "sign_catalog_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump({"root": args.root, "assets": manifest}, f, indent=1, sort_keys=True)
    problems = verify(plans)
    for pr in problems:
        warn(pr)
    log("done in %.0f s: %s; verify %d problems; manifest %s" % (time.time() - t0, counts, len(problems), manifest_path))
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"counts": counts, "problems": problems, "manifest": manifest_path, "seconds": time.time() - t0}, f, indent=1)
    return 1 if problems or counts.get("failed") else 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        import traceback
        unreal.log_error("[gen_signs] FAILED\n" + traceback.format_exc())
        rc = 2
    if rc:
        unreal.log_error("[gen_signs] exit %d" % rc)
