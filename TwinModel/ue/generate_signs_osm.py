"""Run the CarlaTools sign generator (ASignGenerationController) on a baked twin level from an
OSM file: import the OSM as a UStreetMap with the twin's datum as projection origin, stand a
catalog sign (AGeoTrafficSign) at every OSM sign node, push it out of the carriageway with the
level's OpenDRIVE and face it to the oncoming traffic.

Editor Python (headless):

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript -script="/abs/ue/generate_signs_osm.py \\
        --name EixampleDemo --osm /abs/out/v10_eixample/ue/eixample_full.osm --origin 41.3925 2.166 \\
        --style VC [--save] [--report /abs/report.json]"

``--osm`` is OSM XML (``tools/overpass_to_osm.py`` turns twinmodel's cached Overpass JSON into
one); ``--origin`` is the twin's ``origin`` (lat, lon) from the UE manifest. Without ``--save``
nothing is written (the imported StreetMap asset and the signs stay in memory only), which is
the mode for checking a level.
"""
import argparse
import json
import os
import sys
import time
import traceback

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_level import MAP_ROOT, log, save_all, spawn  # noqa: E402

CATALOG = "/CarlaDigitalTwinsTool/Carla/Blueprints/LevelDesign/Signs/Catalog"
POLES = "/CarlaDigitalTwinsTool/Carla/Blueprints/LevelDesign/Signs/DataAssets_Pole"


def import_osm(osm_path, dest_dir, dest_name, origin):
    unreal.StreetMapFactory.set_lat_lon_origin(unreal.Vector2D(origin[0], origin[1]))
    t = unreal.AssetImportTask()
    t.set_editor_property("filename", osm_path)
    t.set_editor_property("destination_path", dest_dir)
    t.set_editor_property("destination_name", dest_name)
    t.set_editor_property("automated", True)
    t.set_editor_property("replace_existing", True)
    t.set_editor_property("save", False)
    t.set_editor_property("factory", unreal.StreetMapFactory())
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    paths = list(t.get_editor_property("imported_object_paths") or [])
    sm = unreal.load_object(None, paths[0]) if paths else None
    if sm is None:
        raise RuntimeError("StreetMap import of %s failed" % osm_path)
    return sm


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="baked level name under /Game/Carla/Maps/Twins")
    ap.add_argument("--osm", required=True)
    ap.add_argument("--origin", nargs=2, type=float, required=True, metavar=("LAT", "LON"))
    ap.add_argument("--style", default="VC", choices=["VC", "MUTCD", "GB"])
    ap.add_argument("--catalog", default=CATALOG)
    ap.add_argument("--poles", default=POLES)
    ap.add_argument("--xodr", default=None, help="override the level's OpenDRIVE file")
    ap.add_argument("--no-displace", action="store_true")
    ap.add_argument("--save", action="store_true", help="save the level (and the imported StreetMap asset)")
    ap.add_argument("--map-root", default=MAP_ROOT)
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    t0 = time.time()
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(["/CarlaDigitalTwinsTool"], True)
    map_path = "%s/%s/%s" % (args.map_root, args.name, args.name)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(map_path):
        raise RuntimeError("cannot load level " + map_path)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()

    sm = import_osm(os.path.abspath(args.osm), "%s/%s/Import" % (args.map_root, args.name), "SM_%s_osm" % args.name, args.origin)
    signs = list(sm.get_editor_property("signs"))
    log("StreetMap %s: %d roads, %d buildings, %d sign nodes" % (
        sm.get_name(), len(sm.get_editor_property("roads")), len(sm.get_editor_property("buildings")), len(signs)))
    tag_hist = {}
    for s in signs:
        props = dict(s.get_editor_property("properties"))
        key = ",".join("%s=%s" % (k, v) for k, v in sorted(props.items()) if k in ("highway", "traffic_sign", "maxspeed", "crossing"))
        tag_hist[key] = tag_hist.get(key, 0) + 1

    ctrl = spawn(world, unreal.SignGenerationController, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "SignGenerationController", "Tools")
    if ctrl is None:
        raise RuntimeError("cannot spawn ASignGenerationController")
    # let the controller find the asset the way the editor tool would; fall back to the handle
    lookup_ok = ctrl.resolve_inputs()
    found = ctrl.get_editor_property("street_map_data")
    log("resolve_inputs -> %s, StreetMap %s, xodr %s" % (lookup_ok, found.get_name() if found else None, ctrl.get_editor_property("xodr_path")))
    if found is None:
        ctrl.set_editor_property("street_map_data", sm)
    if args.xodr:
        ctrl.set_editor_property("xodr_path", os.path.abspath(args.xodr))
    if args.no_displace:
        ctrl.set_editor_property("displace_signs_to_edge", False)
    ctrl.sign_generation_by_path(args.catalog, args.poles, getattr(unreal.SignStyle, args.style))

    generated = list(ctrl.get_editor_property("generated_signs"))
    rows = []
    for a in generated:
        if a is None:
            continue
        plate = a.get_editor_property("plate")
        loc = a.get_actor_location()
        rows.append({"label": a.get_actor_label(), "sign": a.get_editor_property("sign_name"),
                     "state": str(a.get_editor_property("traffic_sign_state")), "kmh": a.get_speed_limit_kmh(),
                     "xodr": "%s-%s" % (a.get_editor_property("xodr_type"), a.get_editor_property("xodr_subtype")),
                     "plate_mesh": plate.static_mesh.get_name() if plate and plate.static_mesh else None,
                     "material": plate.get_material(0).get_name() if plate and plate.static_mesh and plate.get_material(0) else None,
                     "x": round(loc.x / 100.0, 2), "y": round(loc.y / 100.0, 2), "z": round(loc.z / 100.0, 2),
                     "yaw": round(a.get_actor_rotation().yaw, 1)})
    report = {"level": args.name, "osm": args.osm, "sign_nodes": len(signs), "sign_node_tags": tag_hist,
              "matched": ctrl.get_editor_property("last_matched_count"), "spawned": ctrl.get_editor_property("last_spawned_count"),
              "standing": len(rows), "with_plate": sum(1 for r in rows if r["plate_mesh"]),
              "with_material": sum(1 for r in rows if r["material"]),
              "by_sign": {}, "signs": rows, "lookup_found_streetmap": found is not None, "seconds": round(time.time() - t0, 1)}
    for r in rows:
        report["by_sign"][r["sign"]] = report["by_sign"].get(r["sign"], 0) + 1
    log("sign nodes %d -> matched %d, standing %d (%d with plate, %d with material) %s" % (
        len(signs), report["matched"], len(rows), report["with_plate"], report["with_material"], report["by_sign"]))
    if args.save:
        report["level_saved"] = bool(save_all())
    rep = args.report or os.path.join(os.path.dirname(os.path.abspath(args.osm)), "signs_osm_report.json")
    with open(rep, "w") as f:
        json.dump(report, f, indent=1)
    return 0 if rows else 1


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[generate_signs_osm] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[generate_signs_osm] exit %d" % rc, flush=True)
