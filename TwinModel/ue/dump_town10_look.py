"""Dump Town10HD_Opt's look recipe (BP_Carla_Sky instance, lights, fog, PostProcessVolumes,
grass material instance parameters) so bake_level.py can replicate it on baked twin levels.

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript \
        -script="/abs/ue/dump_town10_look.py --out /abs/out/dir"

Writes <out>/town10hd_opt.t3d (full level export, every overridden property) and
<out>/look.json (curated component/material dump).
"""
import argparse
import json
import os
import sys
import traceback

import unreal

MAP_PATH = "/Game/Carla/Maps/Town10HD_Opt"
SKY_BP = "/Game/Carla/Blueprints/LevelDesign/BP_Carla_Sky.BP_Carla_Sky_C"
MATERIALS = [
    "/Game/Carla/Static/GenericMaterials/Ground/MI_VertexPaintGround01.MI_VertexPaintGround01",
    "/Game/Carla/Static/GenericMaterials/Ground/MI_LargeLandscape_Grass.MI_LargeLandscape_Grass",
    "/Game/Carla/Static/GenericMaterials/Ground/MI_Grass_Park.MI_Grass_Park",
    "/Game/Carla/Static/GenericMaterials/Ground/MI_Grass_Park_2.MI_Grass_Park_2",
    "/Game/Carla/Static/GenericMaterials/Ground/MI_Grass_Cutted_Yard.MI_Grass_Cutted_Yard",
    "/Game/Carla/Static/GenericMaterials/Roads/MI_RoadAsphalt_Town15.MI_RoadAsphalt_Town15",
]


def log(m):
    unreal.log("[dump_look] " + str(m))
    print("[dump_look] " + str(m), flush=True)


def dump_props(obj, names):
    out = {}
    for n in names:
        try:
            v = obj.get_editor_property(n)
        except Exception:
            continue
        try:
            json.dumps(v)
            out[n] = v
        except TypeError:
            out[n] = str(v)
    return out


LIGHT_PROPS = ["intensity", "light_color", "temperature", "use_temperature",
               "indirect_lighting_intensity", "volumetric_scattering_intensity",
               "shadow_bias", "shadow_slope_bias", "specular_scale",
               "dynamic_shadow_cascade_distance", "cascade_distribution_exponent",
               "light_source_angle", "atmosphere_sun_light", "cast_shadows",
               "forward_shading_priority"]
SKYLIGHT_PROPS = ["intensity", "light_color", "indirect_lighting_intensity",
                  "volumetric_scattering_intensity", "source_type", "cubemap_resolution",
                  "real_time_capture", "lower_hemisphere_is_solid_color", "occlusion_max_distance",
                  "contrast", "min_occlusion", "cast_shadows"]
FOG_PROPS = ["fog_density", "fog_height_falloff", "fog_inscattering_luminance",
             "fog_max_opacity", "start_distance", "second_fog_data",
             "directional_inscattering_exponent", "directional_inscattering_start_distance",
             "directional_inscattering_luminance", "volumetric_fog", "volumetric_fog_scattering_distribution",
             "volumetric_fog_albedo", "volumetric_fog_emissive", "volumetric_fog_extinction_scale",
             "volumetric_fog_distance", "sky_atmosphere_ambient_contribution_color_scale"]
ATMO_PROPS = ["rayleigh_scattering_scale", "rayleigh_exponential_distribution",
              "mie_scattering_scale", "mie_absorption_scale", "mie_anisotropy",
              "mie_exponential_distribution", "multi_scattering_factor",
              "aerial_pespective_view_distance_scale", "height_fog_contribution"]
CLOUD_PROPS = ["layer_bottom_altitude", "layer_height", "tracing_start_max_distance",
               "tracing_max_distance", "material"]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--map", default=MAP_PATH)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    log("loading map %s" % args.map)
    unreal.EditorLoadingAndSavingUtils.load_map(args.map)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    log("world: %s" % world.get_name())

    report = {"map": args.map, "sky": {}, "post_process_volumes": [], "materials": {},
              "directional_lights": [], "sky_lights": [], "fogs": []}

    # ---- full T3D export: every actor with every overridden property
    t3d = os.path.join(args.out, "town10hd_opt.t3d")
    try:
        task = unreal.AssetExportTask()
        task.set_editor_property("object", world)
        task.set_editor_property("filename", t3d)
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("selected", False)
        task.set_editor_property("replace_identical", True)
        task.set_editor_property("exporter", unreal.LevelExporterT3D())
        ok = unreal.Exporter.run_asset_export_task(task)
        log("T3D export -> %s (%s, %.1f MB)" % (t3d, ok, os.path.getsize(t3d) / 1e6 if os.path.exists(t3d) else -1))
    except Exception as exc:
        log("T3D export failed: %s" % exc)

    # ---- sky actor components
    sky_cls = unreal.load_class(None, SKY_BP)
    skies = unreal.GameplayStatics.get_all_actors_of_class(world, sky_cls) if sky_cls else []
    log("sky actors: %d" % len(skies))
    for sky in skies:
        entry = {"name": sky.get_name(), "label": sky.get_actor_label(), "components": {}}
        comp_map = {
            "directional_light": (unreal.DirectionalLightComponent, LIGHT_PROPS),
            "sky_light": (unreal.SkyLightComponent, SKYLIGHT_PROPS),
            "fog": (unreal.ExponentialHeightFogComponent, FOG_PROPS),
            "atmosphere": (unreal.SkyAtmosphereComponent, ATMO_PROPS),
            "clouds": (unreal.VolumetricCloudComponent, CLOUD_PROPS),
        }
        for key, (cls, props) in comp_map.items():
            comps = sky.get_components_by_class(cls)
            entry["components"][key] = [dict(dump_props(c, props), _name=c.get_name()) for c in comps]
        for pc in sky.get_components_by_class(unreal.PostProcessComponent):
            try:
                entry["components"].setdefault("post_process", []).append(
                    {"_name": pc.get_name(),
                     "settings": pc.get_editor_property("settings").export_text(),
                     "unbound": pc.get_editor_property("unbound"),
                     "priority": pc.get_editor_property("priority")})
            except Exception as exc:
                log("pp comp: %s" % exc)
        report["sky"][sky.get_actor_label()] = entry

    # ---- standalone lights / fog / PPVs
    for cls, key, props in [(unreal.DirectionalLight, "directional_lights", LIGHT_PROPS),
                            (unreal.SkyLight, "sky_lights", SKYLIGHT_PROPS),
                            (unreal.ExponentialHeightFog, "fogs", FOG_PROPS)]:
        for a in unreal.GameplayStatics.get_all_actors_of_class(world, cls):
            comp = a.get_components_by_class(getattr(unreal, cls.__name__ + "Component", unreal.ActorComponent))
            entry = {"label": a.get_actor_label(),
                     "rotation": str(a.get_actor_rotation())}
            if comp:
                entry["props"] = dump_props(comp[0], props)
            report[key].append(entry)
    for ppv in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PostProcessVolume):
        try:
            report["post_process_volumes"].append({
                "label": ppv.get_actor_label(),
                "enabled": ppv.get_editor_property("enabled"),
                "unbound": ppv.get_editor_property("unbound"),
                "priority": ppv.get_editor_property("priority"),
                "blend_weight": ppv.get_editor_property("blend_weight"),
                "settings": ppv.get_editor_property("settings").export_text()})
        except Exception as exc:
            log("ppv: %s" % exc)

    # ---- material instance overridden parameters
    for path in MATERIALS:
        if not unreal.EditorAssetLibrary.does_asset_exist(path):
            report["materials"][path] = None
            continue
        mi = unreal.EditorAssetLibrary.load_asset(path)
        m = {"parent": str(mi.get_editor_property("parent").get_path_name()) if mi.get_editor_property("parent") else None,
             "scalars": {}, "vectors": {}, "textures": {}}
        for sp in mi.get_editor_property("scalar_parameter_values"):
            m["scalars"][str(sp.get_editor_property("parameter_info").get_editor_property("name"))] = \
                sp.get_editor_property("parameter_value")
        for vp in mi.get_editor_property("vector_parameter_values"):
            m["vectors"][str(vp.get_editor_property("parameter_info").get_editor_property("name"))] = \
                str(vp.get_editor_property("parameter_value"))
        for tp in mi.get_editor_property("texture_parameter_values"):
            t = tp.get_editor_property("parameter_value")
            m["textures"][str(tp.get_editor_property("parameter_info").get_editor_property("name"))] = \
                t.get_path_name() if t else None
        report["materials"][path] = m

    out_json = os.path.join(args.out, "look.json")
    with open(out_json, "w") as f:
        json.dump(report, f, indent=1, default=str)
    log("look -> %s" % out_json)
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[dump_look] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[dump_look] exit %d" % rc, flush=True)
