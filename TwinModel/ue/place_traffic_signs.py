"""Place regional traffic signs (from the generated sign catalog) on a baked twin level, at its
OpenDRIVE stop / yield / speed-limit signals.

Editor Python (headless):

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript -script="/abs/ue/place_traffic_signs.py \\
        --name EixampleDemo --signals /abs/out/<twin>/ue/sign_signals.json \\
        --manifest /abs/ue/assets/sign_catalog_manifest.json --style VC"

``--signals`` is ``tools/xodr_signals.py <xodr> <json> --types 205 206 274`` (m, deg, CARLA
frame). For every signal a catalog entry of the requested style with the same OpenDRIVE
type / subtype is looked up in the generator's manifest and an ``AGeoTrafficSign`` (Carla
module: an ATrafficSignBase with a pole and a plate) is spawned at the signal, road level, yaw
+ 90 and 0.25 m forward exactly like ``ATrafficLightManager::SpawnSignals`` would spawn the
stock blueprint. The actor's TrafficSignState is set from the signal, so at runtime the manager
adopts it (``GetClosestTrafficSignActor``, 5 m) and attaches the USignComponent instead of
spawning ``BP_SpeedLimit30`` and friends on top of it. Speed limits carry their km/h value
(dedicated ETrafficSignState or the custom SpeedLimit state + SpeedLimitKmh), so every
value the xodr has is adopted; with ``--style auto`` US signals get MUTCD plates printed in
the nearest mph while the actor still reports the km/h limit.

Actors are labelled ``SIGN_<signal id>``; a re-run replaces them.
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

EAL = unreal.EditorAssetLibrary
DEFAULT_POLE = "/CarlaDigitalTwinsTool/Carla/Static/Pole/SM_Pole05"
# plate scale per mesh: the SignShapes meshes are not all drawn at road scale (the stop octagon is
# 41 cm across); scale to the usual European plate sizes
PLATE_SCALE = {"SM_OctogonalShape": 1.6, "SM_CircleShape": 0.75, "SM_InvertedTriangleShape": 0.85,
               "SM_DangerSignShape": 0.8, "SM_RomboidShape": 0.75}


KMH_PER_MPH = 1.60934
# regional style per ISO country of the OpenDRIVE signal (twin builds stamp it; stock maps say "OpenDRIVE")
STYLE_BY_COUNTRY = {"US": "MUTCD", "CN": "GB"}


def style_for(signal, requested):
    if requested != "auto":
        return requested
    return STYLE_BY_COUNTRY.get((signal.get("country") or "").upper(), "VC")


def pick(manifest, style, stype, subtype):
    """Best catalog entry for an OpenDRIVE signal: exact type + subtype, same style.

    The OpenDRIVE subtype of a speed limit is always km/h. MUTCD plates are printed in mph
    (their manifest subtype is the mph number), so for that style the plate nearest to
    kmh / 1.609 is chosen; the actor keeps the km/h value for the runtime."""
    best = None
    want_mph = None
    if stype == "274" and style == "MUTCD":
        try:
            want_mph = float(subtype) / KMH_PER_MPH
        except ValueError:
            return None
    for da_path, info in manifest.items():
        if info["style"] != style or info["xodr_type"] != stype:
            continue
        sub = info.get("xodr_subtype") or ""
        if want_mph is not None:
            try:
                mph = float(sub)
            except ValueError:
                continue
            score = (abs(mph - want_mph), len(info["name"]))
        elif stype == "274":
            if sub != subtype:
                continue
            score = (0.0, len(info["name"]))  # prefer the plain sign over variants (_2, _mirrored, school ...)
        else:
            if sub not in ("", "-1", subtype):
                continue
            score = (0.0, len(info["name"]))
        if best is None or score < best[0]:
            best = (score, da_path, info)
    if best is None:
        return None
    if want_mph is not None and best[0][0] > 5.0:
        return None  # no plate within 5 mph of the limit
    return best[1], best[2]


def ground_z(world, x, y, z_hint):
    """Ground height (cm) under (x, y) by a line trace; z_hint when nothing is hit."""
    start = unreal.Vector(x, y, z_hint + 20000.0)
    end = unreal.Vector(x, y, z_hint - 20000.0)
    hit = unreal.SystemLibrary.line_trace_single(world, start, end, unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [],
                                                 unreal.DrawDebugTrace.NONE, True)
    try:
        if hit and hit.to_tuple()[0]:
            return hit.to_tuple()[4].z  # impact point
    except Exception:
        pass
    return z_hint


# The plate meshes are thin along their Y axis (the printed face is a Y face); with the runtime
# yaw convention (signal yaw + 90) the print faces the oncoming traffic, i.e. the actor's +Y.
# Push the plate to that side of the pole so the pole does not cover the print (calibrated on
# EixampleDemo captures, out/look_demo/signs*).
PLATE_OFFSET_CM = (0.0, 9.0)


def place_one(world, pole, pole_h, mesh, mat, loc, yaw, plate_yaw, plate_z, label, name, style, signal=None, offset=PLATE_OFFSET_CM):
    rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=yaw)
    # EditorActorSubsystem.spawn_actor_from_class segfaults under -run=pythonscript (see bake_level.spawn)
    actor = spawn(world, unreal.GeoTrafficSign, loc, rot, label, "TrafficSigns")
    if actor is None:
        return None
    scale = PLATE_SCALE.get(mesh.get_name(), 1.0)
    actor.setup(pole, mesh, mat, unreal.Vector(offset[0], offset[1], plate_z), plate_yaw, scale)
    half_h = mesh.get_bounds().box_extent.z * scale
    want = plate_z + half_h + 8.0
    actor.pole.set_relative_scale3d(unreal.Vector(1.0, 1.0, min(1.0, want / pole_h) if pole_h > 0 else 1.0))
    actor.set_editor_property("sign_name", name)
    actor.set_editor_property("style", style)
    if signal is not None:
        actor.set_editor_property("signal_id", str(signal["id"]))
        # type / subtype + the runtime state; speed limits keep their km/h value whatever the plate prints
        actor.configure_for_signal(signal["type"], signal.get("subtype") or "")
    actor.set_actor_label(label)
    try:
        actor.set_editor_property("is_spatially_loaded", False)  # the runtime looks them up with GetAllActorsOfClass
    except Exception:
        pass
    return actor


def place_catalog_rows(world, manifest, pole, pole_h, args):
    """Every catalog entry on a pole, rows of --rows-of signs along +Y (UE) from (--catalog-at x y, CARLA m),
    rows stacked along +X every --row-pitch m; the plates face -X so a camera at x - 11 m sees a row head-on.
    Writes <signals dir>/catalog_rows.json with the camera pose per row (CARLA frame, m / deg)."""
    x0, y0 = args.catalog_at
    entries = sorted(manifest.items(), key=lambda kv: (kv[1]["style"], kv[1]["category"], kv[1]["name"]))
    rows = []
    for r0 in range(0, len(entries), args.rows_of):
        chunk = entries[r0:r0 + args.rows_of]
        ri = len(rows)
        x_ue = (x0 + ri * args.row_pitch) * 100.0
        names = []
        zs = []
        for i, (da_path, info) in enumerate(chunk):
            da = EAL.load_asset(da_path)
            mesh = da.get_editor_property("sign_mesh")
            mat = da.get_editor_property("material")
            y_ue = (y0 + i * args.sign_pitch) * 100.0  # the client frame is the engine frame (m vs cm)
            z = ground_z(world, x_ue, y_ue, args.catalog_z * 100.0)
            zs.append(z)
            a = place_one(world, pole, pole_h, mesh, mat, unreal.Vector(x_ue, y_ue, z), args.catalog_yaw, args.plate_yaw,
                          args.plate_height_m * 100.0, "SIGNCAT_%03d_%s_%s" % (r0 + i, info["style"], info["name"]), info["name"], info["style"])
            if a is not None:
                names.append(info["name"])
        n = len(chunk)
        span = (n - 1) * args.sign_pitch
        rows.append({"row": ri, "style": chunk[0][1]["style"], "category": chunk[0][1]["category"], "names": names,
                     "carla_x": x0 + ri * args.row_pitch, "carla_y0": y0, "carla_y1": y0 + span,
                     "ground_z": (sum(zs) / len(zs)) / 100.0 if zs else args.catalog_z,
                     "camera": {"x": x0 + ri * args.row_pitch - args.camera_dist, "y": y0 + span * 0.5,
                                "z": (sum(zs) / len(zs)) / 100.0 + args.plate_height_m, "yaw": 0.0, "pitch": 0.0}})
    out = os.path.join(os.path.dirname(os.path.abspath(args.signals)), "catalog_rows.json")
    with open(out, "w") as f:
        json.dump({"rows": rows, "sign_pitch": args.sign_pitch, "plate_height_m": args.plate_height_m}, f, indent=1)
    log("catalog rows: %d rows of %d at x=%.0f.. y=%.0f.. (CARLA m) -> %s" % (len(rows), args.rows_of, x0, y0, out))
    return len(rows)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="baked level name under /Game/Carla/Maps/Twins")
    ap.add_argument("--signals", required=True)
    ap.add_argument("--manifest", required=True, help="sign_catalog_manifest.json from gen_sign_dataassets.py")
    ap.add_argument("--catalog-at", nargs=2, type=float, default=None, metavar=("X", "Y"),
                    help="also lay the whole catalog out in rows from this CARLA (m) point (review museum)")
    ap.add_argument("--catalog-z", type=float, default=30.0, help="fallback ground height (m) for the museum")
    ap.add_argument("--catalog-yaw", type=float, default=90.0, help="actor yaw of the museum signs (90: prints face -X, toward the camera; calibrated)")
    ap.add_argument("--rows-of", type=int, default=12)
    ap.add_argument("--row-pitch", type=float, default=14.0, help="m between rows; keep it above --camera-dist so the previous row is behind the camera")
    ap.add_argument("--sign-pitch", type=float, default=1.5)
    ap.add_argument("--camera-dist", type=float, default=11.0)
    ap.add_argument("--remove-catalog", action="store_true", help="only remove a previously placed museum (SIGNCAT_*) and save")
    ap.add_argument("--plate-offset", nargs=2, type=float, default=list(PLATE_OFFSET_CM), metavar=("DX", "DY"), help="plate offset from the pole axis in actor space (cm)")
    ap.add_argument("--style", default="auto", choices=["auto", "VC", "MUTCD", "GB"],
                    help="regional style; auto = by the signal's country (US: MUTCD mph plates, CN: GB, else VC)")
    ap.add_argument("--pole", default=DEFAULT_POLE)
    ap.add_argument("--map-root", default=MAP_ROOT)
    ap.add_argument("--yaw-offset", type=float, default=90.0, help="added to the signal yaw (CARLA's convention: +90)")
    ap.add_argument("--plate-yaw", type=float, default=0.0, help="extra yaw of the plate on the pole")
    ap.add_argument("--forward-m", type=float, default=0.25)
    ap.add_argument("--plate-height-m", type=float, default=2.3, help="height of the plate centre above the road")
    ap.add_argument("--types", nargs="*", default=["205", "206", "274"])
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    t0 = time.time()
    with open(args.signals) as f:
        signals = [s for s in json.load(f) if s.get("type") in args.types]
    with open(args.manifest) as f:
        manifest = json.load(f)["assets"]
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(["/CarlaDigitalTwinsTool"], True)
    pole = EAL.load_asset(args.pole)
    if pole is None:
        raise RuntimeError("pole mesh missing: " + args.pole)
    pole_h = (pole.get_bounds().origin.z + pole.get_bounds().box_extent.z)

    map_path = "%s/%s/%s" % (args.map_root, args.name, args.name)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(map_path):
        raise RuntimeError("cannot load level " + map_path)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if args.remove_catalog:
        stale = [a for a in sub.get_all_level_actors() if a.get_actor_label().startswith("SIGNCAT_")]
        for a in stale:
            a.destroy_actor()
        ok = save_all()
        log("removed %d museum signs, saved=%s" % (len(stale), ok))
        return 0
    stale = [a for a in sub.get_all_level_actors() if a.get_actor_label().startswith("SIGN_") or a.get_actor_label().startswith("SIGNCAT_")]
    for a in stale:
        a.destroy_actor()
    if stale:
        log("removed %d previously baked signs" % len(stale))

    report = {"placed": 0, "unmatched": {}, "failed": [], "by_sign": {}}
    if args.catalog_at:
        report["catalog_rows"] = place_catalog_rows(world, manifest, pole, pole_h, args)
    for s in signals:
        key = "%s-%s" % (s["type"], s.get("subtype") or "")
        style = style_for(s, args.style)
        hit = pick(manifest, style, s["type"], s.get("subtype") or "")
        if hit is None:
            report["unmatched"][key] = report["unmatched"].get(key, 0) + 1
            continue
        da_path, info = hit
        da = EAL.load_asset(da_path)
        mesh = da.get_editor_property("sign_mesh")
        mat = da.get_editor_property("material")
        if mesh is None:
            report["failed"].append(s["id"])
            continue
        yaw = float(s["yaw"]) + args.yaw_offset
        rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=yaw)
        fwd = rot.get_forward_vector()
        # the client-library dump is already in the engine frame (m); same recipe as place_traffic_lights
        loc = unreal.Vector(s["x"] * 100.0 + fwd.x * args.forward_m * 100.0,
                            s["y"] * 100.0 + fwd.y * args.forward_m * 100.0,
                            (s["z"] - float(s.get("z_offset") or 0.0)) * 100.0)
        actor = place_one(world, pole, pole_h, mesh, mat, loc, yaw, args.plate_yaw, args.plate_height_m * 100.0,
                          "SIGN_%s" % s["id"], info["name"], style, signal=s)
        if actor is None:
            report["failed"].append(s["id"])
            continue
        report["placed"] += 1
        report["by_sign"][info["name"]] = report["by_sign"].get(info["name"], 0) + 1
        if report["placed"] == 1:
            log("first sign %s (%s) at (%.0f, %.0f, %.0f) yaw %.0f" % (info["name"], mesh.get_name(), loc.x, loc.y, loc.z, yaw))

    ok = True
    if not args.no_save:
        ok = save_all()
    report["level_saved"] = bool(ok)
    report["seconds"] = round(time.time() - t0, 1)
    log("placed %d / %d signs (%s), unmatched %s, failed %d, saved=%s, %.0f s" % (
        report["placed"], len(signals), report["by_sign"], report["unmatched"], len(report["failed"]), ok, report["seconds"]))
    rep = args.report or os.path.join(os.path.dirname(os.path.abspath(args.signals)), "traffic_signs_report.json")
    with open(rep, "w") as f:
        json.dump(report, f, indent=1)
    return 0 if report["placed"] else 1


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[place_traffic_signs] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[place_traffic_signs] exit %d" % rc, flush=True)
