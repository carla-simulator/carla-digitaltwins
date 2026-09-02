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
  5. buildings           ``--buildings procedural``: instead of the manifest's extruded facade
                         slabs, every footprint is fed to carla-digitaltwins' BP_BuildingGen
                         (modular Nanite facade atoms as ISM instances + a generated roof cap),
                         see place_buildings(). Needs the CarlaDigitalTwinsTool content mounted.
  6. report              <manifest dir>/bake_report.json

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
    # Grass for the ground slab. Town15's own ground meshes use MI_VertexPaintGround01
    # (M_VertexPaintCB), but that is a vertex-colour layer blend whose unpainted base layer is
    # brown dirt -- on a baked slab with default (white) vertex colours it reads as a flat tan
    # plane (verified in out/look_eixample/iter1). MI_LargeLandscape_Grass (T_Ground_Grass_*,
    # the grass the big UE5-native landscapes use) samples plain UVs and works on a static mesh.
    "ground": "/Game/Carla/Static/GenericMaterials/Ground/MI_LargeLandscape_Grass.MI_LargeLandscape_Grass",
    "building": "/Game/Carla/Static/GenericMaterials/Facade/MI_Facade01.MI_Facade01",
}

# Scalar overrides applied on our MICs. The bake's UVs are metric planar (1 uv unit = 1 m)
# while the CARLA masters are tuned for meshes whose UVs are roughly UE-cm sized, so tiling
# ("Scale"/"Tiling") parameters need ~100x the parent's value for the same texel density:
# MI_LargeLandscape_Grass ships Scale X/Y 0.005 (one tile per ~2 m of cm-UV), so metric UVs
# need 0.5. Values are either a float (global parameter) or (float, layer_index) for
# M_VertexPaintCB-style material-layer parameters (Bottom=0, MidLower=1, MidUpper=2, Top=3),
# which need association=LAYER_PARAMETER to resolve.
MATERIAL_SCALARS = {
    "ground": {"Scale X": 0.5, "Scale Y": 0.5},    # grass tile every ~2 m, like the landscapes
    "grass": {"Scale X": 0.5, "Scale Y": 0.5},
    # the road master's base albedo layers ship Base Scale 0.05/0.06 (cm UVs); x100 for the
    # metric bake so the asphalt grain reads at Town15 density instead of pale stretched patches
    "road": {"Base Scale": 5.0, "Base Scale 2": 6.0},
    # facades/curbs keep their shipped tiling: with metric UVs the facade textures already
    # repeat about every metre, which reads fine (scaling them down just made walls featureless
    # -- out/look_eixample/iter3)
}
# a few facade variants so neighbouring tiles do not all look the same. MI_Facade05/07 were
# dropped: on the big flat twin walls they render as near-black glossy slabs (dark curtain-wall
# textures with nothing to reflect -- out/look_eixample/iter4)
BUILDING_VARIANTS = ["MI_Facade01", "MI_Facade03", "MI_Brick01"]
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
            scalars = MATERIAL_SCALARS.get(key, {})
            if scalars:
                # MaterialEditingLibrary.set_material_instance_scalar_parameter_value refuses
                # names it cannot resolve on the parent (these masters route parameters
                # through material functions/layers), so write the override array directly --
                # the same thing the details panel serializes.
                entries = []
                for pname, val in scalars.items():
                    if isinstance(val, tuple):
                        value, layer = val
                        info = unreal.MaterialParameterInfo(
                            name=pname,
                            association=unreal.MaterialParameterAssociation.LAYER_PARAMETER,
                            index=int(layer))
                    else:
                        value = val
                        info = unreal.MaterialParameterInfo(name=pname)
                    e = unreal.ScalarParameterValue()
                    e.set_editor_property("parameter_info", info)
                    e.set_editor_property("parameter_value", float(value))
                    entries.append(e)
                mic.set_editor_property("scalar_parameter_values", entries)
                unreal.MaterialEditingLibrary.update_material_instance(mic)
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
    # Lumen: a twin tile is one huge merged Nanite mesh; with the default card budget (12)
    # and default distance-field resolution the Lumen surface cache barely covers it, so
    # shadow-side faces get no indirect light at all and render pitch black (the iter2/iter3
    # captures). More cards + a denser mesh distance field give the software-Lumen path
    # (r.Lumen.TraceMeshSDFs, see DefaultEngine.ini) something to work with.
    # (StaticMesh.MaxLumenMeshCards is not exposed to editor Python in UE 5.8; the distance
    # field bump below is what actually fixed the black faces.)
    if sms is not None:
        try:
            bsettings = sms.get_lod_build_settings(mesh, 0)
            bsettings.set_editor_property("distance_field_resolution_scale", 8.0)
            sms.set_lod_build_settings(mesh, 0, bsettings)
            info["distance_field_scale"] = 8.0
        except Exception as exc:
            warn("distance field scale: %s" % exc)
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


# --------------------------------------------------------------------------- Town10 look
# Recipe extracted from Town10HD_Opt (T3D export + component dump, 2026-09-01; see
# out/look_eixample/town10_dump/). Three pieces make Town10 read the way it does:
#   1. Content/Carla/Config/Weather/MapDefaults.json: an inline weather snapshot of what
#      Town10HD_Opt runs with (BP_CarlaWeather's per-town table: sun 10 deg at azimuth 170,
#      cloudiness 30, fog 0.0056). Maps WITHOUT an entry fall back to the weather blueprint's
#      hard-coded default (sun 45/220, fog 2.0 -- the washed-out look). NOTE: the game mode
#      must re-apply the JSON entry after the weather actor's deferred BeginPlay
#      (CarlaGameModeBase.cpp), otherwise the blueprint's table/fallback silently wins.
#   2. BP_Carla_Sky instance overrides: sun IndirectLightingIntensity 3, fog density 0.002 /
#      falloff 0.1 / start 75 / SkyAtmosphereAmbientContributionColorScale 0.14, and the
#      CameraParameters/PostProcessComponent grading block (6200K, sat .6, contrast 1.4,
#      gamma 1.2, exposure bias 1.2 in EV 10-12, AO .5/r200, sharpen 3, vignette .7).
#   3. There is no PostProcessVolume actor in Town10HD_Opt, and baked twins get none either
#      (see the note at the end of place(): an absorbed volume stacks on the sensors' own
#      camera defaults and blows sensor.camera.* out -- out/look_eixample/iter1).

TOWN10_WEATHER = {  # MapDefaults.json inline snapshot (camelCase = FJsonObjectConverter names)
    # What Town10HD_Opt actually runs with: BP_CarlaWeather's per-town "DefaultWeathers"
    # table entry (dumped from the class defaults, 2026-09-02) -- low sun at azimuth 170,
    # almost no fog, no extra scattering. (The 30 deg / az 320 / fog 2.0 snapshot used
    # before was never what the map renders: the table wins at runtime.)
    "cloudiness": 30, "precipitation": 0, "precipitationDeposits": 0, "windIntensity": 0.35,
    "sunAzimuthAngle": 170, "sunAltitudeAngle": 10, "fogDensity": 0.0056, "fogDistance": 0,
    "fogFalloff": 0.2, "wetness": 0, "scatteringIntensity": 0, "mieScatteringScale": 0,
    "rayleighScatteringScale": 0.033100001513957977, "dustStorm": 0,
}

# FPostProcessSettings fields Town10HD_Opt's sky claims (bOverride_* True), by exact
# UPROPERTY name; the setter below claims the matching bOverride_ flag for each.
TOWN10_PP = {
    "WhiteTemp": 6200.0, "WhiteTint": -0.15,
    "ColorSaturation": (0.6, 0.6, 0.6, 1.1),
    "ColorContrast": (1.4, 1.4, 1.4, 0.8),
    "ColorGamma": (1.2, 1.2, 1.2, 1.0),
    "ColorGammaHighlights": (0.5, 0.5, 0.5, 1.0),
    "SceneColorTint": (0.785339, 0.879092, 0.93125, 1.0),
    "SceneFringeIntensity": 0.15,
    "CameraShutterSpeed": 100.0, "CameraISO": 100.0,
    "AutoExposureBias": 1.2, "AutoExposureMinBrightness": 10.0,
    "AutoExposureMaxBrightness": 12.0, "AutoExposureSpeedUp": 3.0,
    "AutoExposureSpeedDown": 1.0, "AutoExposureApplyPhysicalCameraExposure": True,
    "LocalExposureHighlightContrastScale": 1.0, "LocalExposureShadowContrastScale": 0.89,
    "VignetteIntensity": 0.7, "Sharpen": 3.0,
    "AmbientOcclusionIntensity": 0.5, "AmbientOcclusionRadius": 200.0,
    "IndirectLightingColor": (1.0, 1.0, 1.0, 1.0), "IndirectLightingIntensity": 1.0,
    "DepthOfFieldFocalDistance": 250.0, "DepthOfFieldFstop": 8.0,
    "DepthOfFieldSensorWidth": 24.576,
    "LumenSceneLightingQuality": 1.0, "LumenSceneDetail": 1.0,
    "LumenSceneViewDistance": 20000.0, "LumenSceneLightingUpdateSpeed": 1.0,
    "LumenFinalGatherQuality": 1.0, "LumenFinalGatherLightingUpdateSpeed": 1.0,
    "LumenMaxTraceDistance": 50000.0, "LumenDiffuseColorBoost": 1.0,
    "LumenSkylightLeaking": 0.35, "LumenFullSkylightLeakingDistance": 1000.0,
    "LumenReflectionQuality": 1.0, "LumenFrontLayerTranslucencyReflections": True,
    "LumenMaxReflectionBounces": 3, "LumenSurfaceCacheResolution": 1.0,
    "ScreenSpaceReflectionIntensity": 100.0, "ScreenSpaceReflectionQuality": 100.0,
    "ScreenSpaceReflectionMaxRoughness": 0.6,
}


def _snake(name):
    out = ""
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper() or (i + 1 < len(name) and name[i + 1].islower())):
            out += "_"
        out += ch.lower()
    return out


VEC4_FIELDS = {"ColorSaturation", "ColorContrast", "ColorGamma", "ColorGammaHighlights"}
LINEAR_FIELDS = {"SceneColorTint", "IndirectLightingColor"}


def set_prop(obj, name, value):
    """set_editor_property tolerant of exact-UPROPERTY vs snake_case naming; returns success."""
    if isinstance(value, tuple):
        value = unreal.Vector4(*value) if name in VEC4_FIELDS else unreal.LinearColor(*value)
    for cand in (name, _snake(name)):
        try:
            obj.set_editor_property(cand, value)
            return True
        except Exception:
            continue
    return False


def town10_pp_settings():
    s = unreal.PostProcessSettings()
    for field, value in TOWN10_PP.items():
        ok_flag = set_prop(s, "bOverride_" + field, True) or set_prop(s, "override_" + _snake(field), True)
        ok_val = set_prop(s, field, value)
        if not (ok_flag and ok_val):
            warn("post process field %s: flag=%s value=%s" % (field, ok_flag, ok_val))
    # enums (best effort; project defaults are Lumen GI/reflections already)
    try:
        set_prop(s, "bOverride_AutoExposureMethod", True)
        set_prop(s, "AutoExposureMethod", unreal.AutoExposureMethod.AEM_HISTOGRAM)
    except Exception as exc:
        warn("exposure method: %s" % exc)
    return s


def apply_town10_sky(sky):
    """Push the Town10HD_Opt BP_Carla_Sky instance overrides onto a freshly spawned sky."""
    applied = {}
    comps = {
        "sun": (unreal.DirectionalLightComponent, {"intensity": 100000.0,
                                                   "indirect_lighting_intensity": 3.0}),
        "fog": (unreal.ExponentialHeightFogComponent, {
            "fog_density": 0.002, "fog_height_falloff": 0.1, "start_distance": 75.0,
            "sky_atmosphere_ambient_contribution_color_scale":
                unreal.LinearColor(0.140625, 0.140625, 0.140625, 1.0)}),
    }
    for key, (cls, props) in comps.items():
        for comp in sky.get_components_by_class(cls):
            if key == "sun" and "Moon" in comp.get_name():
                continue
            for pname, val in props.items():
                applied["%s.%s" % (key, pname)] = set_prop(comp, pname, val)
    # instance WeatherParameters (BP-added variable, matched by exact name) + CameraParameters
    try:
        wp = unreal.WeatherParameters()
        for pname, val in [("cloudiness", 30.0), ("wind_intensity", 0.35),
                           ("sun_azimuth_angle", 170.0), ("sun_altitude_angle", 10.0),
                           ("fog_density", 0.0056), ("fog_distance", 0.0), ("fog_falloff", 0.2),
                           ("scattering_intensity", 0.0), ("mie_scattering_scale", 0.0)]:
            set_prop(wp, pname, val)
        applied["WeatherParameters"] = set_prop(sky, "WeatherParameters", wp)
    except Exception as exc:
        warn("WeatherParameters: %s" % exc)
    try:
        cam = unreal.CameraParameters()
        for pname, val in [("shadow_contrast", 0.89), ("shutter_speed", 100.0),
                           ("aperture", 8.0), ("focal_distance", 250.0),
                           ("temperature", 6200.0), ("tint", -0.15),
                           ("scene_color_tint", unreal.LinearColor(0.785339, 0.879092, 0.93125, 1.0)),
                           ("global_saturation", 0.6), ("global_contrast", 1.4),
                           ("global_gamma", 1.2), ("vignette_intensity", 0.7)]:
            set_prop(cam, pname, val)
        applied["CameraParameters"] = set_prop(sky, "CameraParameters", cam)
    except Exception as exc:
        warn("CameraParameters: %s" % exc)
    # the same grading on the sky's own (unbound) PostProcessComponent -- what the -game
    # viewport blends; PPV_Town10Look (spawned by place()) is what sensors absorb
    for pc in sky.get_components_by_class(unreal.PostProcessComponent):
        applied["PostProcessComponent"] = set_prop(pc, "settings", town10_pp_settings())
    bad = [k for k, v in applied.items() if not v]
    if bad:
        warn("town10 sky overrides not applied: %s" % ", ".join(bad))
    log("town10 sky overrides applied (%d ok, %d failed)" %
        (sum(1 for v in applied.values() if v), len(bad)))
    return applied


def write_map_default_weather(name):
    """Give the baked map a MapDefaults.json entry (Town10HD_Opt's weather); without one
    ACarlaGameModeBase applies the all -1 sentinel and the sun sits on the horizon."""
    path = os.path.join(unreal.Paths.project_content_dir(), "Carla", "Config", "Weather",
                        "MapDefaults.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as exc:
            warn("MapDefaults.json unreadable (%s); leaving it alone" % exc)
            return False
    if data.get(name) == TOWN10_WEATHER:
        log("MapDefaults.json already has %s" % name)
        return True
    data[name] = dict(TOWN10_WEATHER)
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    log("MapDefaults.json: %s -> Town10 runtime weather (sun alt 10, az 170)" % name)
    return True


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
        if a["kind"] == "boundary":
            # collision-only wall around the terrain apron: invisible to every camera
            # (hidden actors are not rendered, so segmentation never sees it), still solid
            actor.set_actor_hidden_in_game(True)
            try:
                actor.set_editor_property("is_spatially_loaded", False)
            except Exception:
                pass
        placed.append(a["asset"])
    # spawn points
    n_sp = 0
    for sp in manifest["spawn_points"]:
        actor = spawn(world, unreal.VehicleSpawnPoint, unreal.Vector(sp["x"], sp["y"], sp["z"]),
                      unreal.Rotator(roll=0.0, pitch=0.0, yaw=sp["yaw"]), "SP_%s_%d" % (sp["road"], sp["lane"]),
                      "Twin/SpawnPoints")
        if actor is not None:
            # always loaded: ACarlaGameModeBase::StoreSpawnPoints iterates the world at
            # InitGame, before any WP streaming - a spatially loaded spawn point is invisible
            # and CARLA would fall back to xodr-topology spawn points
            try:
                actor.set_editor_property("is_spatially_loaded", False)
            except Exception:
                pass
            n_sp += 1
    # player start at the first spawn point (the spectator starts here)
    if manifest["spawn_points"]:
        sp = manifest["spawn_points"][0]
        ps = spawn(world, unreal.PlayerStart, unreal.Vector(sp["x"], sp["y"], sp["z"] + 200.0),
                   unreal.Rotator(roll=0.0, pitch=0.0, yaw=sp["yaw"]), "PlayerStart", "Twin")
        if ps is not None:
            try:
                ps.set_editor_property("is_spatially_loaded", False)
            except Exception:
                pass
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
            apply_town10_sky(sky)
    else:
        warn("sky blueprint %s not found; the game mode will spawn one at runtime" % SKY_BP)
    # Deliberately NO PostProcessVolume actor: Town10HD_Opt has none either. Its grading lives
    # on the sky's own unbound PostProcessComponent (replicated above), which the -game
    # viewport blends but sensors never see -- CARLA sensors only absorb an enabled unbound
    # *APostProcessVolume* at BeginPlay (SceneCaptureSensor.cpp). Baking one was tried
    # (out/look_eixample/iter1): the absorbed grading + exposure stacked on the camera
    # defaults and blew the sensor images out, making twins *diverge* from Town10's sensor
    # look instead of matching it. No volume = both viewport and sensors behave exactly as
    # they do on Town10HD_Opt.
    return placed, n_sp


# --------------------------------------------------------------------------- procedural buildings
# carla-digitaltwins' BP_BuildingGen, driven per footprint through its ProcessPoints function
# (its stock BuildingGeneration entry rejects every footprint -- CheckIfBuildingIsValid is
# broken -- so the baker calls the row placer directly). Sizing semantics decoded from the
# graph (T3D export, 2026-09-02):
#   * per contour segment N = floor(dist / atom width) modules of width dist/N, every atom
#     (walls and the corner on the last module) scaled X = slot / atom width, Z = 1;
#   * ``Plane`` and ``SlopeStyle`` are assumed to be 100x100 cm vertical unit wall quads
#     (pivot at the right end, extending along -X like the atoms): planes fill segments
#     shorter than one atom (scale dist/100 x band/100), the slope fills the leftover band
#     under the cornice (scale slot/100 x remainder/SlopeHeight) and the short-segment top
#     row. Feeding a real atom there produces the 5x-wide stretched slabs of trial 32.
#   * so heights are snapped to door + n*window + top (+1 cm) rows: no leftover band.
# Atom families: only kits with a consistent 500 cm module and all seven slots are used.
BGEN_BP = "/CarlaDigitalTwinsTool/Blueprints/BP_BuildingGen"
BGEN_PIECES = "/CarlaDigitalTwinsTool/Static/Building/Buildings/BuildingPieces"
PDA_CLASS = "/CarlaDigitalTwinsTool/Blueprints/PDA_ModuleWithGlass.PDA_ModuleWithGlass_C"
ATOM_ROOT = "/Game/Carla/Static/Building/TwinAtoms"  # PDAs + filler quads (ATagger: Building)
UNITWALL_OBJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "unitwall.obj")
FAMILIES = {
    # Eixample-scale apartment block: 500 x 350 cm window modules, 100 cm cornice
    "Skysc21": {
        "bottom": ["Btt/SM_Skysc21_Btt_Wall_A", "Btt/SM_Skysc21_Btt_Wall_B"],
        "bottom_corner": ["Btt/SM_Skysc21_Btt_Corner_A", "Btt/SM_Skysc21_Btt_Corner_B"],
        "door": ["Btt/SM_Skysc21_Btt_Door_A", "Btt/SM_Skysc21_Btt_Door_B"],
        "mid": ["Mid/SM_Skysc21_Mid_Wall_A", "Mid/SM_Skysc21_Mid_Wall_B", "Mid/SM_Skysc21_Mid_Wall_C"],
        "mid_corner": ["Mid/SM_Skysc21_Mid_Corner_A", "Mid/SM_Skysc21_Mid_Corner_B",
                       "Mid/SM_Skysc21_Mid_Corner_C"],
        # SM_Skysc21_Top_Wall1 ships with the VR-editor red placeholder material: excluded
        "top": ["Top/SM_Skysc21_Top_Wall"],
        "top_corner": ["Top/SM_Skysc21_Top_Corner"],
    },
    # glass office: 500 x 500 cm modules, full-storey top
    "Skysc19": {
        "bottom": ["Btt/SM_Skysc19_Btt_Wall"], "bottom_corner": ["Btt/SM_Skysc19_Btt_Corner"],
        "door": ["Btt/SM_Skysc19_Btt_Door"], "mid": ["Mid/SM_Skysc19_Mid_Wall"],
        "mid_corner": ["Mid/SM_Skysc19_Mid_Corner"], "top": ["Top/SM_Skysc19_Top_Wall"],
        "top_corner": ["Top/SM_Skysc19_Top_Corner"],
    },
}
DEFAULT_FAMILY = "Skysc21"
# The filler quad (short segments + slope band) is scaled non-uniformly, so whatever it
# carries gets stretched: an atom's baked atlas turns into streaks (out/look_demo/iter3/tl_0),
# a plain painted plaster reads as a party wall. Tiling materials only.
FILLER_MATERIAL = "/CarlaDigitalTwinsTool/Static/Building/01_ProceduralBuildings/FacadeMaterials/MI_Facade_Painted_Color_05"
CATEGORY_FAMILIES = {  # OSM building=* -> candidate families (picked by building id)
    "office": ["Skysc19", "Skysc21"], "commercial": ["Skysc19"], "public": ["Skysc19"],
    "civic": ["Skysc19"], "school": ["Skysc19"], "hotel": ["Skysc21", "Skysc19"],
}


def _bounds(mesh):
    bb = mesh.get_bounding_box()
    return bb.max - bb.min


def load_family(fam):
    """Load a family's meshes; returns dict slot -> [StaticMesh] plus the module dimensions."""
    spec = FAMILIES[fam]
    out = {}
    for slot, rels in spec.items():
        out[slot] = []
        for rel in rels:
            path = "%s/%s" % (BGEN_PIECES, rel)
            mesh = unreal.EditorAssetLibrary.load_asset(path)
            if mesh is None:
                warn("atom %s missing" % path)
                continue
            out[slot].append(mesh)
    for slot in ("bottom", "bottom_corner", "door", "mid", "mid_corner", "top", "top_corner"):
        if not out.get(slot):
            raise RuntimeError("family %s has no %s atom" % (fam, slot))
    sB, sD, sW, sT = (_bounds(out[s][0]) for s in ("bottom", "door", "mid", "top"))
    out["dims"] = {"bottom_bb_width": sB.x, "bottom_bb_height": sB.z,
                   "door_bb_width": sD.x, "door_bb_height": sD.z,
                   "window_bb_width": sW.x, "window_bb_height": sW.z,
                   "top_atom_bb_width": sT.x, "top_atom_bb_height": sT.z}
    return out


def snap_height_cm(h_cm, dims):
    """door + n*window + top (+1 cm so the slope band is a sliver, never a stretched row)."""
    n = max(1, int(round((h_cm - dims["door_bb_height"] - dims["top_atom_bb_height"])
                         / dims["window_bb_height"])))
    return dims["door_bb_height"] + n * dims["window_bb_height"] + dims["top_atom_bb_height"] + 1.0, n


def family_for(b):
    cands = CATEGORY_FAMILIES.get(b.get("category") or "", [DEFAULT_FAMILY])
    return cands[int(b.get("id", 0)) % len(cands)]


def ensure_unit_wall(fam, material):
    """100x100 cm two-sided vertical quad (x in [-100,0], z in [0,100]) carrying the family's
    facade material: the generator's Plane/Slope filler."""
    path = "%s/SM_TwinUnitWall_%s" % (ATOM_ROOT, fam)
    if asset_exists(path):
        quad = load_asset(path)
    else:
        unreal.EditorAssetLibrary.make_directory(ATOM_ROOT)
        t = unreal.AssetImportTask()
        t.set_editor_property("filename", UNITWALL_OBJ)
        t.set_editor_property("destination_path", ATOM_ROOT)
        t.set_editor_property("destination_name", "SM_TwinUnitWall_%s" % fam)
        t.set_editor_property("automated", True)
        t.set_editor_property("replace_existing", True)
        t.set_editor_property("save", False)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
        quad = None
        for ip in list(t.get_editor_property("imported_object_paths") or []):
            o = unreal.load_object(None, ip)
            if isinstance(o, unreal.StaticMesh):
                quad = o
        if quad is None and asset_exists(path):
            quad = load_asset(path)
        if quad is None:
            raise RuntimeError("unit wall import failed (%s)" % UNITWALL_OBJ)
    if material is not None:
        quad.set_material(0, material)
    sz = _bounds(quad)
    if abs(sz.x - 100.0) > 0.5 or abs(sz.z - 100.0) > 0.5:
        raise RuntimeError("unit wall %s is %.1f x %.1f, expected 100 x 100" % (path, sz.x, sz.z))
    unreal.EditorAssetLibrary.save_loaded_asset(quad)
    return quad


def ensure_pdas(fam, meshes):
    """The bottom-row pins of ProcessPoints are still PDA_ModuleWithGlass lists (the mid/top
    pins were migrated to plain StaticMesh lists): one data asset per bottom atom."""
    pda_cls = unreal.load_object(None, PDA_CLASS)
    if pda_cls is None:
        raise RuntimeError("PDA class %s not found" % PDA_CLASS)
    folder = "%s/%s" % (ATOM_ROOT, fam)
    unreal.EditorAssetLibrary.make_directory(folder)
    fac = unreal.DataAssetFactory()
    fac.set_editor_property("data_asset_class", pda_cls)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    out = {}
    for slot in ("bottom", "bottom_corner", "door"):
        out[slot] = []
        for mesh in meshes[slot]:
            name = "DA_%s_%s" % (slot, mesh.get_name()[3:])
            path = "%s/%s" % (folder, name)
            if asset_exists(path):
                da = load_asset(path)
            else:
                da = tools.create_asset(name, folder, None, fac)
                da.set_editor_property("Module", mesh)
                unreal.EditorAssetLibrary.save_loaded_asset(da)
            out[slot].append(da)
    return out


def _always_loaded(actor):
    try:
        actor.set_editor_property("is_spatially_loaded", False)
    except Exception:
        pass


def _ism_components(actor):
    return list(actor.get_components_by_class(unreal.InstancedStaticMeshComponent))


def _instance_count(actor):
    return sum(c.get_instance_count() for c in _ism_components(actor))


def place_buildings(world, manifest, name, mats, roof_material=None):
    """Modular facades for every manifest building through BP_BuildingGen.ProcessPoints.

    Per building: a host actor at the origin (the generator adds ISM components to it and
    places instances in world XY at z = 0), then every instance is lifted to the twin's
    base_z, and the roof cap (StreetMapComponent.GenerateTopOfBuilding, from the .osm the
    twin_buildings writer produced with the row-snapped heights) is spawned at base_z too.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import twin_buildings

    report = {"buildings": 0, "instances": 0, "roofs": 0, "families": {}, "skipped": 0,
              "hosts_over_limit": 0}
    blds = [b for b in manifest.get("buildings", ()) if b.get("rings_ue")]
    if not blds:
        warn("manifest has no buildings; nothing to generate")
        return report
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(
        ["/CarlaDigitalTwinsTool", ATOM_ROOT], True)
    bgen_bp = unreal.EditorAssetLibrary.load_asset(BGEN_BP)
    bgen_cls = unreal.load_object(None, BGEN_BP + ".BP_BuildingGen_C")
    if bgen_bp is None or bgen_cls is None:
        raise RuntimeError("BP_BuildingGen not mounted at %s (CarlaDigitalTwinsTool content)" % BGEN_BP)
    # the variables ProcessPoints reads from the object are not instance-editable in the
    # shipped BP; unlock them in memory (the BP package is excluded from the save below)
    for v in ("CurrentActor", "BatchSize", "BuildingLevelFloorFactor", "StreetMapActor", "MapName"):
        try:
            unreal.BlueprintEditorLibrary.set_blueprint_variable_instance_editable(bgen_bp, v, True)
        except Exception:
            pass
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(bgen_bp)
    except Exception as exc:
        warn("compile BP_BuildingGen: %s" % exc)

    # families, fillers, PDAs
    fams = {}
    for fam in sorted({family_for(b) for b in blds}):
        meshes = load_family(fam)
        if asset_exists(FILLER_MATERIAL):
            wall_mat = load_asset(FILLER_MATERIAL)
        else:
            wall_mat = meshes["mid"][0].get_material(0)
            warn("filler material %s missing; short segments will show %s stretched"
                 % (FILLER_MATERIAL, wall_mat.get_name() if wall_mat else None))
        meshes["quad"] = ensure_unit_wall(fam, wall_mat)
        log("family %s filler quad material: %s" % (fam, wall_mat.get_name() if wall_mat else None))
        meshes["pda"] = ensure_pdas(fam, meshes)
        fams[fam] = meshes
        log("family %s: %s" % (fam, {k: round(v, 1) for k, v in meshes["dims"].items()}))

    # one row-snapped .osm (the roof cap reads the way's height tag), one StreetMap import
    rings = []
    snapped = []
    for b in blds:
        fam = family_for(b)
        hs, n = snap_height_cm(float(b["height_m"]) * 100.0, fams[fam]["dims"])
        snapped.append((b, fam, hs, n))
    osm_manifest = {"buildings": [dict(b, height_m=hs / 100.0, levels=n) for b, fam, hs, n in snapped]}
    base = os.path.dirname(os.path.abspath(manifest["_path"]))
    osm_path = os.path.join(base, "buildings.osm")
    n_ways = twin_buildings.write_buildings_osm(osm_manifest, osm_path)
    import_dir = "%s/%s/Import" % (MAP_ROOT, name)
    t = unreal.AssetImportTask()
    t.set_editor_property("filename", osm_path)
    t.set_editor_property("destination_path", import_dir)
    t.set_editor_property("destination_name", "SM_%s_buildings" % name)
    t.set_editor_property("automated", True)
    t.set_editor_property("replace_existing", True)
    t.set_editor_property("save", False)
    t.set_editor_property("factory", unreal.StreetMapFactory())
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    paths = list(t.get_editor_property("imported_object_paths") or [])
    street_map = unreal.load_object(None, paths[0]) if paths else None
    if street_map is None:
        raise RuntimeError("StreetMap import of %s failed" % osm_path)
    sm_blds = list(street_map.get_editor_property("buildings"))
    log("buildings.osm: %d ways -> StreetMap %d buildings" % (n_ways, len(sm_blds)))
    # the importer keeps way order, but match rings by their first vertex to be safe
    first = {}
    for i, sb in enumerate(sm_blds):
        pts = list(sb.get_editor_property("building_points"))
        if pts:
            first.setdefault((round(pts[0].x), round(pts[0].y)), i)
    sma = spawn(world, unreal.StreetMapActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0),
                "TwinStreetMap", "Twin/Buildings")
    if sma is None:
        raise RuntimeError("could not spawn the StreetMap actor")
    smc = sma.get_editor_property("street_map_component")
    smc.set_editor_property("street_map", street_map)
    roof_dir = "Game/Carla/Static/Building/Twins/%s/Roofs" % name
    # idempotent: the roof generator saves a package per building under this folder and
    # aborts ("cannot be saved as it has only been partially loaded") if one already exists
    if unreal.EditorAssetLibrary.does_directory_exist("/" + roof_dir):
        unreal.EditorAssetLibrary.delete_directory("/" + roof_dir)
        log("cleared previous roof meshes under /%s" % roof_dir)
    if roof_material is None:
        roof_material = (mats.get("sidewalk") or mats.get("road") or [None])[0]

    t0 = time.time()
    outer = None  # new_object's default outer is the transient package
    for bi, (b, fam, hs, n) in enumerate(snapped):
        ring = b["rings_ue"][0]
        key = (round(ring[0][0]), round(ring[0][1]))
        si = first.get(key, bi if bi < len(sm_blds) else None)
        if si is None:
            report["skipped"] += 1
            continue
        sb = sm_blds[si]
        pts = list(sb.get_editor_property("building_points"))
        if len(pts) < 3:
            report["skipped"] += 1
            continue
        meshes = fams[fam]
        host = spawn(world, unreal.Actor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0),
                     "Bldg_%s" % b.get("id", bi), "Twin/Buildings")
        if host is None:
            report["skipped"] += 1
            continue
        # always loaded: World Partition streams only around the player/spectator, and
        # CARLA sensors are not streaming sources -- a camera two blocks away from the
        # spectator would otherwise look at an empty footprint (out/look_demo/iter1)
        _always_loaded(host)
        gen = unreal.new_object(bgen_cls, outer=outer) if outer is not None else unreal.new_object(bgen_cls)
        for k, v in (("CurrentActor", host), ("BatchSize", 100), ("BuildingLevelFloorFactor", 3.0),
                     ("StreetMapActor", sma), ("MapName", name)):
            try:
                gen.set_editor_property(k, v)
            except Exception as exc:
                if bi == 0:
                    warn("BP_BuildingGen.%s: %s" % (k, str(exc).splitlines()[0][:100]))
        kw = {"building_points": [unreal.Vector2D(p.x, p.y) for p in pts],
              "max_number_of_doors": 2, "height": hs, "building_levels": n,
              "slope_height": 100.0, "plane": meshes["quad"],
              "bottom_style": meshes["pda"]["bottom"],
              "bottom_corner_style": meshes["pda"]["bottom_corner"],
              "bottom_door_style": meshes["pda"]["door"],
              "mid_style": meshes["mid"], "mid_corner_style": meshes["mid_corner"],
              "top_style": meshes["top"], "top_corner_style": meshes["top_corner"],
              "slope_style": [meshes["quad"]]}
        kw.update(meshes["dims"])
        try:
            gen.call_method("ProcessPoints", (), kw)
        except Exception as exc:
            if outer is None and bi == 0:
                # a world-context node in the graph: retry with the world as outer
                warn("ProcessPoints with a transient generator failed (%s); using world outer"
                     % str(exc).splitlines()[0][:120])
                outer = world
                gen = unreal.new_object(bgen_cls, outer=outer)
                gen.set_editor_property("CurrentActor", host)
                gen.call_method("ProcessPoints", (), kw)
            else:
                warn("building %s ProcessPoints: %s" % (b.get("id"), str(exc).splitlines()[0][:150]))
                report["skipped"] += 1
                continue
        n_inst = _instance_count(host)
        if n_inst == 0 and bi == 0 and outer is None:
            warn("transient generator placed nothing; using world outer")
            outer = world
            gen = unreal.new_object(bgen_cls, outer=outer)
            gen.set_editor_property("CurrentActor", host)
            gen.call_method("ProcessPoints", (), kw)
            n_inst = _instance_count(host)
        # lift the building onto the twin's terrain
        dz = float(b.get("base_z_cm", 0.0))
        if dz:
            for c in _ism_components(host):
                cnt = c.get_instance_count()
                if not cnt:
                    continue
                tfs = []
                for i in range(cnt):
                    tf = c.get_instance_transform(i, True)
                    loc = tf.translation
                    tf.translation = unreal.Vector(loc.x, loc.y, loc.z + dz)
                    tfs.append(tf)
                c.batch_update_instances_transforms(0, tfs, True, True, True)
        for c in _ism_components(host):
            try:
                c.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
            except Exception:
                pass
        # roof cap
        try:
            roof = smc.call_method("GenerateTopOfBuilding", (),
                                   {"index": si, "map_name": roof_dir, "material_instance": roof_material})
        except Exception as exc:
            roof = None
            warn("building %s roof: %s" % (b.get("id"), str(exc).splitlines()[0][:150]))
        if roof is not None:
            try:
                ns = roof.get_editor_property("nanite_settings")
                ns.set_editor_property("enabled", True)
                roof.set_editor_property("nanite_settings", ns)
            except Exception:
                pass
            ra = spawn(world, unreal.StaticMeshActor, unreal.Vector(pts[0].x, pts[0].y, dz),
                       unreal.Rotator(0, 0, 0), "Roof_%s" % b.get("id", bi), "Twin/Buildings")
            if ra is not None:
                _always_loaded(ra)
                rc = ra.get_editor_property("static_mesh_component")
                rc.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
                if not rc.set_static_mesh(roof):
                    rc.set_editor_property("static_mesh", roof)
                report["roofs"] += 1
        report["buildings"] += 1
        report["instances"] += n_inst
        report["families"][fam] = report["families"].get(fam, 0) + 1
        if bi % 50 == 0:
            log("building %d/%d (%s, %d rows, %d instances so far)" %
                (bi + 1, len(snapped), fam, n, report["instances"]))
    # the StreetMap actor only served the roof generator (it would otherwise render its own
    # extruded footprints on top of the atoms)
    try:
        sma.destroy_actor()
    except Exception as exc:
        warn("destroy StreetMap actor: %s" % exc)
    report["seconds"] = round(time.time() - t0, 1)
    log("procedural buildings: %d buildings, %d instances, %d roofs, %d skipped in %.1f s %s" % (
        report["buildings"], report["instances"], report["roofs"], report["skipped"],
        report["seconds"], report["families"]))
    return report


def save_all(exclude_prefixes=("/CarlaDigitalTwinsTool",)):
    """Level + WP external actors + new content, but never the mounted plugin packages
    (the BP variable unlock above dirties BP_BuildingGen in memory)."""
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ok = les.save_current_level()
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
    try:
        dirty = list(unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages())
    except Exception as exc:
        warn("get_dirty_content_packages: %s; saving all dirty packages" % exc)
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        return ok
    keep = [p for p in dirty if not any(p.get_name().startswith(x) for x in exclude_prefixes)]
    skipped = len(dirty) - len(keep)
    if keep:
        unreal.EditorLoadingAndSavingUtils.save_packages(keep, True)
    log("saved %d content packages (%d plugin packages left unsaved)" % (len(keep), skipped))
    return ok


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
    ap.add_argument("--buildings", choices=("slabs", "procedural"), default="slabs",
                    help="slabs: the manifest's extruded facade meshes; procedural: "
                         "BP_BuildingGen modular facades from the footprints")
    ap.add_argument("--roof-material", default=None,
                    help="material path for the procedural roof caps (default: the twin's sidewalk MIC)")
    args = ap.parse_args(argv)

    t_start = time.time()
    with open(args.manifest) as f:
        manifest = json.load(f)
    manifest["_path"] = os.path.abspath(args.manifest)
    name = args.name
    if args.buildings == "procedural":
        n_all = len(manifest["assets"])
        manifest["assets"] = [a for a in manifest["assets"] if a["kind"] != "building"]
        log("procedural buildings: dropping %d facade slab assets" % (n_all - len(manifest["assets"])))
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
    if args.buildings == "procedural":
        roof_mat = load_asset(args.roof_material) if args.roof_material else None
        report["procedural_buildings"] = place_buildings(world, manifest, name, mats, roof_mat)
    xodr = copy_xodr(manifest, name)
    report["xodr"] = xodr
    report["map_default_weather"] = write_map_default_weather(name)

    ok = save_all()
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
