"""Repaint an already baked twin level with the current material pools -- without rebaking it.

Editor Python, run headless:

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript \\
        -script="/abs/ue/repaint_materials.py --name EixampleDemo \\
                 --manifest /abs/out/v10_eixample/ue/manifest.json"

Why a separate pass. A bake recreates the level, which would destroy everything other tooling
put in it (EixampleDemo carries 125 traffic-light rig actors and 75 SIGN_* actors placed by
place_traffic_lights.py / place_traffic_signs.py, plus the procedural building hosts). The
material of a twin surface lives on the *static mesh asset*, not on the StaticMeshActor, so a
repaint never has to open the level at all:

  1. rebuild MI_<Name>_<key>_<i> for every member of every pool under
     /Game/Carla/Maps/Twins/<Name>/Materials/  (bake_level.make_material_instances);
  2. for every twin static mesh under /Game/Carla/Static/<Semantic>/Twins/<Name>/ set slot 0
     (and any further slot) to the pool member the deterministic hash picks for its
     (key, tile) -- exactly what a rebake would have assigned;
  3. the procedural roof caps under /Game/Carla/Static/Building/Twins/<Name>/Roofs get a
     sidewalk-pool member each;
  4. the pre-pool single MICs (MI_<Name>_<key>, no index) are deleted once nothing references
     them any more.

Meshes not listed in the manifest (leftovers from an earlier bake of the same level) are
repainted too, from the kind encoded in their asset name, so no reference to a legacy MIC
survives. Idempotent: re-running assigns the same variants.
"""
import argparse
import json
import os
import re
import sys
import time
import traceback

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_materials  # noqa: E402
from bake_level import (MAP_ROOT, MESH_ROOT, asset_exists, asset_key, load_asset, log,  # noqa: E402
                        make_material_instances, pick_variant, warn)

EAL = unreal.EditorAssetLibrary
# <twin>_L<layer>_<kind>_<i>_<j>, tile indices with 'm' for minus (bake_level asset naming)
ASSET_RE = re.compile(r"^(?P<twin>.+?)_L(?P<layer>\d+)_(?P<kind>.+)_(?P<i>m?\d+)_(?P<j>m?\d+)$")
SEMANTICS = ("Road", "SideWalk", "RoadLine", "Terrain", "Building", "Static")


def _tile(tok):
    return -int(tok[1:]) if tok.startswith("m") else int(tok)


def asset_from_name(asset_name):
    """Reconstruct the manifest-shaped record (kind + tile) of a twin mesh from its name."""
    m = ASSET_RE.match(asset_name)
    if not m:
        return None
    kind = m.group("kind")
    if kind not in twin_materials.KIND_KEY:
        return None
    return {"asset": asset_name, "kind": kind, "material": twin_materials.KIND_KEY[kind],
            "tile": [_tile(m.group("i")), _tile(m.group("j"))]}


def mesh_folders(name, mesh_root_fmt):
    out = []
    for sem in SEMANTICS:
        d = mesh_root_fmt.format(semantic=sem, name=name)
        if EAL.does_directory_exist(d):
            out.append(d)
    return out


def static_meshes_in(folder, recursive=False):
    out = []
    for p in EAL.list_assets(folder, recursive=recursive, include_folder=False):
        path = p.split(".")[0]
        obj = EAL.load_asset(path)
        if isinstance(obj, unreal.StaticMesh):
            out.append((path, obj))
    return out


def repaint_mesh(mesh, mic):
    n = len(mesh.get_editor_property("static_materials"))
    for slot in range(max(n, 1)):
        mesh.set_material(slot, mic)


def delete_legacy_mics(name, mat_dir, keys):
    """The pre-pool MI_<Name>_<key> assets, once nothing references them."""
    out = {"deleted": [], "kept": {}}
    for key in sorted(keys):
        path = "%s/MI_%s_%s" % (mat_dir, name, key)
        if not asset_exists(path):
            continue
        try:
            refs = list(EAL.find_package_referencers_for_asset(path, True))
        except Exception as exc:
            warn("referencers of %s: %s" % (path, exc))
            refs = ["<unknown>"]
        if refs:
            out["kept"][path] = [str(r) for r in refs][:10]
            warn("%s still referenced by %d package(s); left in place" % (path, len(refs)))
            continue
        if EAL.delete_asset(path):
            out["deleted"].append(path)
            log("deleted legacy %s" % path)
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="baked level / asset name, e.g. EixampleDemo")
    ap.add_argument("--manifest", default=None,
                    help="the bake manifest (optional: without it every mesh is read from its name)")
    ap.add_argument("--map-root", default=MAP_ROOT)
    ap.add_argument("--mesh-root", default=MESH_ROOT)
    ap.add_argument("--report", default=None)
    ap.add_argument("--keep-legacy", action="store_true",
                    help="do not delete the pre-pool MI_<Name>_<key> instances")
    args = ap.parse_args(argv)

    t0 = time.time()
    name = args.name
    mat_dir = "%s/%s/Materials" % (args.map_root, name)
    report = {"name": name, "materials": {}, "assets": {}, "variants": {}, "counts": {},
              "unmatched": [], "manifest": args.manifest}

    by_asset = {}
    if args.manifest:
        with open(args.manifest) as f:
            manifest = json.load(f)
        by_asset = {a["asset"]: a for a in manifest["assets"]}
        log("manifest %s: %d assets" % (args.manifest, len(by_asset)))

    folders = mesh_folders(name, args.mesh_root)
    if not folders:
        raise RuntimeError("no twin mesh folder for %s under %s" % (name, args.mesh_root))
    log("mesh folders: %s" % ", ".join(folders))
    found = []
    for d in folders:
        found.extend(static_meshes_in(d))
    log("found %d twin static meshes" % len(found))

    records = {}
    for path, _mesh in found:
        an = path.rsplit("/", 1)[-1]
        a = by_asset.get(an) or asset_from_name(an)
        if a is None:
            report["unmatched"].append(path)
            warn("cannot tell the surface kind of %s; left untouched" % path)
            continue
        records[path] = a
    keys = {asset_key(a) for a in records.values()} | {"sidewalk"}   # sidewalk: the roof caps

    mats = make_material_instances(name, keys, mat_dir)
    report["materials"] = {k: [m.get_path_name() for m in v] for k, v in mats.items()}
    report["pool_sizes"] = {k: len(v) for k, v in mats.items()}

    n_saved = 0
    for path, a in sorted(records.items()):
        variants = mats.get(asset_key(a)) or mats.get("road")
        if not variants:
            warn("no pool for %s (%s)" % (path, asset_key(a)))
            continue
        mic, vi = pick_variant(name, a, variants)
        mesh = load_asset(path)
        repaint_mesh(mesh, mic)
        EAL.save_asset(path, only_if_is_dirty=False)
        n_saved += 1
        report["assets"][a["asset"]] = {"key": asset_key(a), "tile": a.get("tile"),
                                        "variant": vi, "material": mic.get_path_name()}
        report["variants"].setdefault(asset_key(a), {})[str(a.get("tile"))] = vi
        report["counts"][asset_key(a)] = report["counts"].get(asset_key(a), 0) + 1

    # procedural roof caps: one sidewalk-pool member per roof
    roof_dir = "/Game/Carla/Static/Building/Twins/%s/Roofs" % name
    n_roofs = 0
    if EAL.does_directory_exist(roof_dir):
        pool = mats.get("sidewalk") or mats.get("road") or []
        for path, mesh in static_meshes_in(roof_dir, recursive=True):
            if not pool:
                break
            i = twin_materials.building_variant_index(name, "roof", path.rsplit("/", 1)[-1], len(pool))
            repaint_mesh(mesh, pool[i])
            EAL.save_asset(path, only_if_is_dirty=False)
            n_roofs += 1
    report["roofs_repainted"] = n_roofs

    if not args.keep_legacy:
        report["legacy"] = delete_legacy_mics(name, mat_dir, sorted(keys))

    report["meshes_repainted"] = n_saved
    report["seconds"] = round(time.time() - t0, 1)
    log("repainted %d meshes + %d roof caps in %.1f s; pools %s" % (
        n_saved, n_roofs, report["seconds"], report["pool_sizes"]))
    for key in sorted(report["variants"]):
        log("  %-14s %s" % (key, json.dumps(report["variants"][key], sort_keys=True)))
    rep = args.report or (os.path.join(os.path.dirname(os.path.abspath(args.manifest)),
                                       "repaint_report.json") if args.manifest else None)
    if rep:
        with open(rep, "w") as f:
            json.dump(report, f, indent=1)
        log("report -> %s" % rep)
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[repaint_materials] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[repaint_materials] exit %d" % rc, flush=True)
