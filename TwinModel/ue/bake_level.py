"""Bake a Twin Model export (``twinmodel bake-export`` -> manifest.json + .glb) into a CARLA
UE 5.8 World Partition level. Editor Python, run headless:

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript \\
        -script="/abs/path/ue/bake_level.py --manifest /abs/ue/manifest.json --name Eixample"

What it does (DESIGN.md §Baker):
  1. material instances  /Game/Carla/Maps/Twins/<Name>/Materials/MI_<Name>_<key>, parented to
                         CARLA's road / sidewalk / curb / marking / grass / facade masters
  2. static meshes       Interchange import of every .glb into
                         /Game/Carla/Static/<Semantic>/Twins/<Name>/  (the folder right under
                         /Game/Carla/Static/ is what ATagger reads: Road, SideWalk, RoadLine,
                         Building, Terrain -> static.road, static.sidewalk, ... at runtime),
                         Nanite on (full-resolution fallback so complex collision = the surface),
                         complex-as-simple collision, our material in slot 0
  3. level               /Game/Carla/Maps/Twins/<Name>/<Name>  (World Partition, one
                         StaticMeshActor per asset at the origin — the glb vertices are already in
                         UE cm/handedness — AVehicleSpawnPoint per manifest spawn point,
                         PlayerStart, BP_Carla_Sky)
  4. OpenDRIVE           <Content>/Carla/Maps/Twins/<Name>/OpenDrive/<Name>.xodr, which is where
                         UOpenDrive::GetXODR looks for a level saved under Maps/Twins/<Name>/
  5. report              <manifest dir>/bake_report.json

Idempotent: re-running replaces the meshes and recreates the level.
"""
import argparse
import json
import os
import shutil
import sys
import time
import traceback

import unreal

MAP_ROOT = "/Game/Carla/Maps/Twins"
MESH_ROOT = "/Game/Carla/Static/{semantic}/Twins/{name}"

# material key (manifest) -> CARLA material to instance (see OpenDriveGenerator.h defaults and
# the Town10HD lane markings; curbs/facades from GenericMaterials)
MATERIAL_PARENTS = {
    "road": "/Game/Carla/Static/GenericMaterials/Roads/MI_RoadAsphalt_Town15.MI_RoadAsphalt_Town15",
    "sidewalk": "/Game/Carla/Static/GenericMaterials/Sidewalk/MI_Sidewalk_Apartment.MI_Sidewalk_Apartment",
    "curb": "/Game/Carla/Static/GenericMaterials/Gutters_Curbs/Curb/MI_CurbDirty01.MI_CurbDirty01",
    "marking_white": "/Game/Carla/Static/GenericMaterials/Roads/MI_Road_Asphalt_B_LaneMarkingWhite.MI_Road_Asphalt_B_LaneMarkingWhite",
    "marking_yellow": "/Game/Carla/Static/GenericMaterials/Roads/MI_Road_Asphalt_B_LaneMarkingYellow.MI_Road_Asphalt_B_LaneMarkingYellow",
    "grass": "/Game/Carla/Static/GenericMaterials/Ground/MI_LargeLandscape_Grass.MI_LargeLandscape_Grass",
    "ground": "/Game/Carla/Static/GenericMaterials/Ground/MI_Grass_Park.MI_Grass_Park",
    "building": "/Game/Carla/Static/GenericMaterials/Facade/MI_Facade01.MI_Facade01",
}
# a few facade variants so neighbouring tiles do not all look the same
BUILDING_VARIANTS = ["MI_Facade01", "MI_Facade03", "MI_Facade05", "MI_Brick01", "MI_Facade07"]
SKY_BP = "/Game/Carla/Blueprints/LevelDesign/BP_Carla_Sky.BP_Carla_Sky_C"
NO_COLLISION_KINDS = {"marking_white", "marking_yellow"}


def log(msg):
    unreal.log("[bake_level] " + str(msg))
    print("[bake_level] " + str(msg), flush=True)


def warn(msg):
    unreal.log_warning("[bake_level] " + str(msg))
    print("[bake_level] WARNING " + str(msg), flush=True)


# --------------------------------------------------------------------------- assets

def asset_exists(path):
    return unreal.EditorAssetLibrary.does_asset_exist(path)


def load_asset(path):
    a = unreal.EditorAssetLibrary.load_asset(path)
    if a is None:
        raise RuntimeError("cannot load asset " + path)
    return a


def make_material_instances(name, materials, mat_dir):
    """MI_<Name>_<key> for every key, parented to the CARLA material. Returns key -> MIC."""
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    unreal.EditorAssetLibrary.make_directory(mat_dir)
    out = {}
    for key in sorted(materials):
        parent_path = MATERIAL_PARENTS.get(key)
        if parent_path is None:
            warn("no CARLA material for key %s; using road" % key)
            parent_path = MATERIAL_PARENTS["road"]
        variants = [parent_path]
        if key == "building":
            base = parent_path.rsplit("/", 1)[0]
            variants = ["%s/%s.%s" % (base if "Brick" not in v else base, v, v) for v in BUILDING_VARIANTS]
        for i, ppath in enumerate(variants):
            mi_name = "MI_%s_%s" % (name, key) if i == 0 else "MI_%s_%s_%d" % (name, key, i)
            mi_path = "%s/%s" % (mat_dir, mi_name)
            if not asset_exists(ppath):
                warn("parent material %s missing" % ppath)
                continue
            parent = load_asset(ppath)
            if asset_exists(mi_path):
                mic = load_asset(mi_path)
            else:
                mic = tools.create_asset(mi_name, mat_dir, unreal.MaterialInstanceConstant,
                                         unreal.MaterialInstanceConstantFactoryNew())
            unreal.MaterialEditingLibrary.set_material_instance_parent(mic, parent)
            unreal.EditorAssetLibrary.save_asset(mi_path, only_if_is_dirty=False)
            out.setdefault(key, []).append(mic)
    return out


def interchange_options():
    """Pipeline override: static meshes only, no materials/textures, Nanite on."""
    pipe = unreal.InterchangeGenericAssetsPipeline()
    mp = pipe.get_editor_property("mesh_pipeline")
    mp.set_editor_property("import_static_meshes", True)
    mp.set_editor_property("import_skeletal_meshes", False)
    mp.set_editor_property("build_nanite", True)
    try:
        mp.set_editor_property("nanite_triangle_threshold", 0)
    except Exception:
        pass
    try:
        mp.set_editor_property("collision", True)
        mp.set_editor_property("import_collision_according_to_mesh_name", False)
    except Exception:
        pass
    mat = pipe.get_editor_property("material_pipeline")
    mat.set_editor_property("import_materials", False)
    try:
        mat.get_editor_property("texture_pipeline").set_editor_property("import_textures", False)
    except Exception:
        pass
    common = pipe.get_editor_property("common_meshes_properties")
    try:
        common.set_editor_property("force_all_mesh_as_type", unreal.InterchangeForceMeshType.IFMT_STATIC_MESH)
    except Exception:
        pass
    try:
        common.set_editor_property("recompute_normals", False)
        common.set_editor_property("recompute_tangents", True)
    except Exception:
        pass
    override = unreal.InterchangePipelineStackOverride()
    override.add_pipeline(pipe)
    return override


def import_meshes(manifest, mesh_root_fmt, name, replace=True):
    """Import every glb; returns asset name -> object path."""
    base = os.path.dirname(os.path.abspath(manifest["_path"]))
    tasks = []
    dest_of = {}
    for a in manifest["assets"]:
        dest = mesh_root_fmt.format(semantic=a["semantic"], name=name)
        dest_of[a["asset"]] = dest
        obj_path = "%s/%s" % (dest, a["asset"])
        if not replace and asset_exists(obj_path):
            continue
        t = unreal.AssetImportTask()
        t.set_editor_property("filename", os.path.join(base, a["file"]))
        t.set_editor_property("destination_path", dest)
        t.set_editor_property("destination_name", a["asset"])
        t.set_editor_property("automated", True)
        t.set_editor_property("replace_existing", True)
        t.set_editor_property("save", False)
        t.set_editor_property("options", interchange_options())
        tasks.append(t)
    log("importing %d glb files" % len(tasks))
    t0 = time.time()
    if tasks:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)
    log("import done in %.1f s" % (time.time() - t0))
    out = {}
    for a in manifest["assets"]:
        obj_path = "%s/%s" % (dest_of[a["asset"]], a["asset"])
        if not asset_exists(obj_path):
            # Interchange may name the asset after the glTF mesh node; look for it
            found = [p for p in unreal.EditorAssetLibrary.list_assets(dest_of[a["asset"]], recursive=False)
                     if p.split("/")[-1].split(".")[0].startswith(a["asset"])]
            if found:
                obj_path = found[0].split(".")[0]
            else:
                warn("asset %s not found after import" % obj_path)
                continue
        out[a["asset"]] = obj_path
    return out


_SMS = [None, False]


def static_mesh_subsystem():
    """StaticMeshEditorSubsystem, or None in a commandlet where the StaticMeshEditor module
    is not loaded (then the deprecated ``nanite_settings`` property is written directly)."""
    if not _SMS[1]:
        _SMS[1] = True
        sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        if sub is None:
            try:
                unreal.load_module("StaticMeshEditor")
                sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
            except Exception as exc:
                warn("StaticMeshEditor module: %s" % exc)
        if sub is None:
            warn("StaticMeshEditorSubsystem unavailable; writing nanite_settings directly")
        _SMS[0] = sub
    return _SMS[0]


def finish_mesh(mesh, mic, kind, nanite=True):
    """Material slot 0, Nanite (full-res fallback), complex-as-simple collision."""
    info = {}
    # material
    n_mat = len(mesh.get_editor_property("static_materials"))
    for slot in range(max(n_mat, 1)):
        mesh.set_material(slot, mic)
    info["material"] = mic.get_path_name()
    # nanite (the Interchange pipeline already built it; here we pin the settings: full-res
    # fallback so the complex collision is the exact surface)
    sms = static_mesh_subsystem()
    ns = sms.get_nanite_settings(mesh) if sms else mesh.get_editor_property("nanite_settings")
    ns.set_editor_property("enabled", bool(nanite))
    try:
        ns.set_editor_property("fallback_target", unreal.NaniteFallbackTarget.PERCENT_TRIANGLES)
        ns.set_editor_property("fallback_percent_triangles", 1.0)
    except Exception as exc:
        warn("nanite fallback settings: %s" % exc)
    if sms:
        sms.set_nanite_settings(mesh, ns, True)
        info["nanite"] = bool(sms.get_nanite_settings(mesh).get_editor_property("enabled"))
    else:
        mesh.set_editor_property("nanite_settings", ns)
        info["nanite"] = bool(mesh.get_editor_property("nanite_settings").get_editor_property("enabled"))
    # collision
    bs = mesh.get_editor_property("body_setup")
    if bs is not None:
        if kind in NO_COLLISION_KINDS:
            bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_SIMPLE_AS_COMPLEX)
            try:
                bs.get_editor_property("agg_geom").set_editor_property("convex_elems", [])
                bs.get_editor_property("agg_geom").set_editor_property("box_elems", [])
            except Exception:
                pass
            info["collision"] = "none"
        else:
            bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
            info["collision"] = "complex_as_simple"
    try:
        info["triangles"] = int(mesh.get_num_triangles(0))
    except Exception:
        pass
    return info


# --------------------------------------------------------------------------- level

def new_level(map_path):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if asset_exists(map_path):
        log("deleting existing level %s" % map_path)
        unreal.EditorAssetLibrary.delete_asset(map_path)
    ok = les.new_level(map_path, True)  # partitioned world
    if not ok:
        raise RuntimeError("new_level failed for " + map_path)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    partitioned = None
    try:
        partitioned = unreal.WorldPartitionBlueprintLibrary.get_editor_world_bounds() is not None
    except Exception:
        pass
    log("created level %s (world %s, partitioned=%s)" % (map_path, world.get_name(), partitioned))
    return world


def spawn(world, cls, loc, rot, label=None, folder=None):
    """Spawn an actor straight into the editor world (UWorld::SpawnActor through CarlaTools'
    UOpenDriveToMap::SpawnActorWithCheckNoCollisions). The editor placement path
    (EditorActorSubsystem.spawn_actor_from_*) goes through the PlacementSubsystem, which is not
    initialised under -run=pythonscript and segfaults; GameplayStatics' deferred spawn is
    BlueprintInternalUseOnly and not exposed to Python."""
    tf = unreal.Transform(loc, rot, unreal.Vector(1.0, 1.0, 1.0))
    # CarlaTools' static helper: GEditor world -> UWorld::SpawnActor (bNoFail, AlwaysSpawn)
    actor = unreal.OpenDriveToMap.spawn_actor_with_check_no_collisions(cls, tf)
    if actor is None:
        return None
    if label:
        try:
            actor.set_actor_label(label)
        except Exception as exc:
            warn("label %s: %s" % (label, exc))
    if folder:
        try:
            actor.set_folder_path(folder)
        except Exception:
            pass
    return actor


def place(world, manifest, mesh_paths, name):
    placed = []
    for a in manifest["assets"]:
        path = mesh_paths.get(a["asset"])
        if path is None:
            continue
        mesh = load_asset(path)
        actor = spawn(world, unreal.StaticMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0),
                      "SM_" + a["asset"], "Twin/%s" % a["kind"])
        if actor is None:
            warn("could not place " + a["asset"])
            continue
        smc = actor.get_editor_property("static_mesh_component")
        smc.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
        if not smc.set_static_mesh(mesh):
            smc.set_editor_property("static_mesh", mesh)
        placed.append(a["asset"])
    # spawn points
    n_sp = 0
    for sp in manifest["spawn_points"]:
        actor = spawn(world, unreal.VehicleSpawnPoint, unreal.Vector(sp["x"], sp["y"], sp["z"]),
                      unreal.Rotator(0.0, sp["yaw"], 0.0), "SP_%s_%d" % (sp["road"], sp["lane"]),
                      "Twin/SpawnPoints")
        if actor is not None:
            n_sp += 1
    # player start at the first spawn point (the spectator starts here)
    if manifest["spawn_points"]:
        sp = manifest["spawn_points"][0]
        spawn(world, unreal.PlayerStart, unreal.Vector(sp["x"], sp["y"], sp["z"] + 200.0),
              unreal.Rotator(0.0, sp["yaw"], 0.0), "PlayerStart", "Twin")
    # sky (sun, atmosphere, fog, skylight): CarlaGameModeBase would spawn it on a WP map without
    # an ASkyBase, but placing it makes the level self-contained (and lit in the editor too)
    sky_cls = unreal.load_class(None, SKY_BP)
    if sky_cls is not None:
        sky = spawn(world, sky_cls, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "CarlaSky", "Twin")
        if sky is not None:
            try:
                sky.set_editor_property("is_spatially_loaded", False)
            except Exception:
                pass
    else:
        warn("sky blueprint %s not found; the game mode will spawn one at runtime" % SKY_BP)
    return placed, n_sp


def copy_xodr(manifest, name):
    src = manifest.get("xodr")
    if not src or not os.path.exists(src):
        warn("manifest has no xodr; the map will have no waypoints")
        return None
    content = unreal.Paths.project_content_dir()
    dst_dir = os.path.join(content, "Carla", "Maps", "Twins", name, "OpenDrive")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name + ".xodr")
    shutil.copyfile(src, dst)
    log("xodr -> %s (%.1f MB)" % (dst, os.path.getsize(dst) / 1e6))
    return dst


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


# --------------------------------------------------------------------------- main

def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--name", required=True, help="level / asset name (CamelCase, e.g. Eixample)")
    ap.add_argument("--map-root", default=MAP_ROOT)
    ap.add_argument("--mesh-root", default=MESH_ROOT, help="format with {semantic} and {name}")
    ap.add_argument("--no-nanite", action="store_true")
    ap.add_argument("--skip-import", action="store_true", help="reuse already imported meshes")
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    t_start = time.time()
    with open(args.manifest) as f:
        manifest = json.load(f)
    manifest["_path"] = os.path.abspath(args.manifest)
    name = args.name
    map_dir = "%s/%s" % (args.map_root, name)
    map_path = "%s/%s" % (map_dir, name)
    mat_dir = "%s/Materials" % map_dir
    report = {"name": name, "map": map_path, "manifest": manifest["_path"], "assets": {}, "warnings": []}

    log("bake %s: %d assets, %d spawn points, xodr %s" % (
        name, len(manifest["assets"]), len(manifest["spawn_points"]), manifest.get("xodr")))

    mats = make_material_instances(name, {a["material"] for a in manifest["assets"]}, mat_dir)
    report["materials"] = {k: [m.get_path_name() for m in v] for k, v in mats.items()}

    mesh_paths = import_meshes(manifest, args.mesh_root, name, replace=not args.skip_import)
    by_asset = {a["asset"]: a for a in manifest["assets"]}
    n_build = 0
    for asset_name, path in sorted(mesh_paths.items()):
        a = by_asset[asset_name]
        mesh = load_asset(path)
        variants = mats.get(a["material"]) or mats.get("road")
        if a["material"] == "building":
            mic = variants[n_build % len(variants)]
            n_build += 1
        else:
            mic = variants[0]
        info = finish_mesh(mesh, mic, a["kind"], nanite=not args.no_nanite)
        info["path"] = path
        report["assets"][asset_name] = info
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    log("finished %d meshes (nanite on: %d)" % (
        len(report["assets"]), sum(1 for i in report["assets"].values() if i.get("nanite"))))

    world = new_level(map_path)
    placed, n_sp = place(world, manifest, mesh_paths, name)
    report["actors_placed"] = len(placed)
    report["spawn_points_placed"] = n_sp
    xodr = copy_xodr(manifest, name)
    report["xodr"] = xodr

    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ok = les.save_current_level()
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    log("saved level: %s" % ok)
    report["level_saved"] = bool(ok)

    content = unreal.Paths.project_content_dir()
    sizes = {}
    for rel in ("Carla/Maps/Twins/%s" % name, "Carla/__ExternalActors__/Carla/Maps/Twins/%s" % name,
                "Carla/__ExternalObjects__/Carla/Maps/Twins/%s" % name):
        p = os.path.join(content, rel)
        if os.path.isdir(p):
            sizes[rel] = dir_size(p)
    for sem in sorted({a["semantic"] for a in manifest["assets"]}):
        rel = args.mesh_root.format(semantic=sem, name=name).replace("/Game/", "")
        p = os.path.join(content, rel)
        if os.path.isdir(p):
            sizes[rel] = dir_size(p)
    report["content_bytes"] = sizes
    report["content_total_mb"] = round(sum(sizes.values()) / 1e6, 1)
    report["seconds"] = round(time.time() - t_start, 1)
    rep = args.report or os.path.join(os.path.dirname(manifest["_path"]), "bake_report.json")
    with open(rep, "w") as f:
        json.dump(report, f, indent=1)
    log("report -> %s (%.1f MB of content, %.0f s)" % (rep, report["content_total_mb"], report["seconds"]))
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[bake_level] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[bake_level] exit %d" % rc, flush=True)
