"""Place geo-styled traffic-light rigs on a baked twin level, at the OpenDRIVE signals.

Editor Python (headless):

    UnrealEditor-Cmd <CarlaUnreal.uproject> -run=pythonscript -script="/abs/ue/place_traffic_lights.py \\
        --name EixampleDemo --signals /abs/tl_signals.json --rig /abs/ue/rigs/eu_pole.json"

``--rig`` also takes a *directory* of presets, in which case each signal gets one from
``pick_rig`` (by lane count, turn movements and whether the approach carries a crossing) or
from ``--rig-map`` ``{"<signal id>": "<rig name>"}``. ``--style eu`` (the default) keeps every
approach on ``--default-rig``; ``--style na`` lets the selector reach the North-American mast
arm / gantry / pedestrian presets in ``ue/rigs/``.

The rig is carla-digitaltwins' ``ATrafficLightActor`` (CarlaTools' "Traffic Light Tool"): a
pole / head / module description driven by the TrafficLights2025 DataTables, whose rows carry
a style (NorthAmerican / European / Asian). The tool's JSON preset format is used verbatim
(``ue/rigs/*.json``); ``BuildFromJSON`` resolves mesh names against the rows of the head's
style, so a European preset can only name European modules.

Per traffic-light signal (a JSON dump of ``carla.Map.get_all_landmarks()``: id, x, y, z in m,
yaw in deg -- the same transform the runtime would use):
  1. spawn an ATrafficLightActor at the signal (yaw + 90 and 0.25 m forward, exactly like
     UOpenDriveToMap::GenerateTrafficLights), build it from the preset, stamp SignalID /
     JunctionID / TrafficLightGroupID (controller from the xodr) and the phase durations;
  2. ``Bake(name, label)``: the rig is flattened into a plain actor ("BakedRoot" + static
     mesh components with the LED material instances) and its logic appended to
     ``Plugins/<name>/Content/Maps/OpenDrive/map_logic.json`` (the tool assumes a per-map
     plugin; ``CopyAssetToPlugin`` is an identity mapping on ue58-dev, so no assets move);
  3. the rig actor is destroyed, the baked actor made always-loaded.
Finally ``map_logic.json`` is moved next to the level's xodr
(``Content/Carla/Maps/Twins/<name>/OpenDrive/``): that file is the runtime switch --
``ATrafficLightManager::InitializeTrafficLights`` then skips its own BP_TLOpenDrive spawn and
``UMapLogicParser::ApplyLaneIdsFromMapLogic`` adopts each baked actor (within 50 cm of the
signal) into an ``ADigitalTwinsTrafficLight``, a real ATrafficLightBase the Traffic Manager
drives. Stop / yield / speed signs keep spawning from the xodr as before.
"""
import argparse
import json
import os
import shutil
import sys
import time
import traceback
import xml.etree.ElementTree as ET

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_level import MAP_ROOT, log, warn, spawn, save_all  # noqa: E402


def controllers_from_xodr(xodr_path):
    """signal id -> controller id.

    The twin writes one ``<controller>`` per signal *stage*, so a junction has several and a
    signal belongs to exactly one of them. If a signal ever appeared in two, the last writer
    would silently win here -- and CARLA would pick a third answer, since
    ``ATrafficLightManager::RegisterLightComponentFromOpenDRIVE`` takes ``*begin()`` of a
    ``std::set<std::string>``. Fail loudly instead.
    """
    out = {}
    if not xodr_path or not os.path.exists(xodr_path):
        return out
    root = ET.parse(xodr_path).getroot()
    for ctl in root.iter("controller"):
        for c in ctl.findall("control"):
            sid = c.get("signalId")
            if sid in out and out[sid] != ctl.get("id"):
                raise RuntimeError(
                    "signal %s is in two controllers (%s and %s); one stage per signal"
                    % (sid, out[sid], ctl.get("id")))
            out[sid] = ctl.get("id")
    return out


def junctions_from_xodr(xodr_path):
    """controller id -> junction id, from ``<junction id=J><controller id=C/></junction>``.

    This used to map ``road/@id -> road/@junction``, which is always "-1" for a traffic light:
    the signal sits on the *approach* road, and an approach road is by definition outside the
    junction. Every light in map_logic.json therefore had ``JunctionID: -1``. The controller
    ref is the real link -- and it is the same one CARLA follows
    (``ATrafficLightManager::RegisterLightComponentFromOpenDRIVE``: signal -> controller ->
    junction).
    """
    out = {}
    if not xodr_path or not os.path.exists(xodr_path):
        return out
    root = ET.parse(xodr_path).getroot()
    for j in root.iter("junction"):
        for c in j.findall("controller"):
            out[c.get("id")] = j.get("id")
    return out


def stages_per_junction(xodr_path):
    """junction id -> number of ``<controller>`` refs (= signal stages) it carries."""
    out = {}
    if not xodr_path or not os.path.exists(xodr_path):
        return out
    root = ET.parse(xodr_path).getroot()
    for j in root.iter("junction"):
        n = len(j.findall("controller"))
        if n:
            out[j.get("id")] = n
    return out


# Green time by how many stages the junction cycles through, so a 3- or 4-stage plan does not
# make a vehicle wait a minute at a red (cycle = stages x (green + amber + all-red)).
# Index = stage count, clamped; mirrors twinmodel.profiles.JunctionRules.signal_green_s.
STAGE_GREEN_S = [10.0, 10.0, 10.0, 8.0, 6.0]


def load_rigs(path):
    """``--rig`` as either one preset file or a directory of them -> {stem: abs path}."""
    if os.path.isdir(path):
        rigs = {os.path.splitext(f)[0]: os.path.join(path, f)
                for f in sorted(os.listdir(path)) if f.endswith(".json")}
        if not rigs:
            raise RuntimeError("no *.json rig presets in " + path)
        return rigs
    return {os.path.splitext(os.path.basename(path))[0]: path}


def pick_rig(sig, rigs, style, default):
    """Choose a rig preset for one traffic-light signal.

    ``tl_signals.json`` carries what the choice needs (tools/xodr_signals.py):
    ``n_driving_lanes`` (how wide the approach is), ``turns`` (which movements leave it) and
    ``has_crossing``. The North-American presets are only reachable with ``--style na``;
    ``--style eu`` keeps every approach on the European pole, which is what a European twin
    should look like whatever its lane count.
    """
    if style != "na":
        return rigs.get(default) or next(iter(rigs.values()))
    n = int(sig.get("n_driving_lanes") or 1)
    turns = set(sig.get("turns") or ())
    order = []
    if n >= 4 or (n >= 3 and "left" in turns):
        order.append("na_gantry_8head")     # wide approach, or one with its own left movement
    if n >= 2:
        order.append("na_mast_2head")       # mast arm reaching over the carriageway
    if sig.get("has_crossing"):
        order.append("na_pole_ped")         # kerbside pole with a pedestrian head
    order.append(default)
    for name in order:
        if name in rigs:
            return rigs[name]
    return next(iter(rigs.values()))


def find_actor_by_label(label):
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        try:
            if a.get_actor_label() == label:
                return a
        except Exception:
            continue
    return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="baked level name under /Game/Carla/Maps/Twins")
    ap.add_argument("--signals", required=True, help="JSON list of traffic-light landmarks (m, deg)")
    ap.add_argument("--rig", required=True,
                    help="Traffic Light Tool preset JSON, or a directory of them")
    ap.add_argument("--rig-map", default=None,
                    help='JSON {"<signal id>": "<rig name or path>"} overriding the selector')
    ap.add_argument("--style", default="eu", choices=("eu", "na"),
                    help="rig family the fallback selector may draw from (default: eu, which "
                         "always uses --default-rig)")
    ap.add_argument("--default-rig", default="eu_pole")
    ap.add_argument("--map-root", default=MAP_ROOT)
    ap.add_argument("--red", type=float, default=2.0,
                    help="all-red clearance at the end of each stage")
    ap.add_argument("--green", type=float, default=None,
                    help="green time (default: by the junction's stage count, STAGE_GREEN_S)")
    ap.add_argument("--amber", type=float, default=3.0)
    ap.add_argument("--yaw-offset", type=float, default=90.0,
                    help="added to the signal yaw (CARLA's convention: +90)")
    # 0, not 0.25: the transform in tl_signals.json is carla.Landmark.transform, which already
    # contains CARLA's own +0.25 m traffic-light nudge (MapBuilder.cpp:858). Adding it again
    # put the rig ~0.5 m from the road point and left only 25 cm of the 50 cm match radius
    # UMapLogicParser::ApplyLaneIdsFromMapLogic searches in.
    ap.add_argument("--forward-m", type=float, default=0.0)
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    t0 = time.time()
    with open(args.signals) as f:
        signals = [s for s in json.load(f) if s.get("type", "1000001") == "1000001"]
    map_path = "%s/%s/%s" % (args.map_root, args.name, args.name)
    content = unreal.Paths.project_content_dir()
    xodr_dir = os.path.join(content, "Carla", "Maps", "Twins", args.name, "OpenDrive")
    xodr = os.path.join(xodr_dir, args.name + ".xodr")
    ctl_of = controllers_from_xodr(xodr)
    junction_of_ctl = junctions_from_xodr(xodr)
    stages_of = stages_per_junction(xodr)
    rigs = load_rigs(args.rig)
    rig_map = {}
    if args.rig_map:
        with open(args.rig_map) as f:
            rig_map = json.load(f)
    log("%d traffic-light signals, %d controllers, %d rig preset(s) %s, style=%s%s" % (
        len(signals), len(set(ctl_of.values())), len(rigs), sorted(rigs), args.style,
        ", %d rig-map overrides" % len(rig_map) if rig_map else ""))

    # the tool appends map_logic.json under a per-map plugin folder; start clean
    plugin_logic_dir = os.path.join(unreal.Paths.project_plugins_dir(), args.name, "Content", "Maps", "OpenDrive")
    plugin_logic = os.path.join(plugin_logic_dir, "map_logic.json")
    if os.path.exists(plugin_logic):
        os.remove(plugin_logic)

    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(map_path):
        raise RuntimeError("cannot load level " + map_path)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    log("level %s loaded" % world.get_name())

    # previous run's baked lights (labels TL_<signal id>) go away first
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    stale = [a for a in sub.get_all_level_actors() if a.get_actor_label().startswith("TL_")]
    for a in stale:
        a.destroy_actor()
    if stale:
        log("removed %d previously baked lights" % len(stale))

    rig_cls = unreal.TrafficLightActor
    report = {"placed": 0, "failed": [], "labels": [], "rigs": {}, "junctions": {}, "timing": {}}
    for s in signals:
        label = "TL_%s" % s["id"]
        sid = str(s["id"])
        ctl = ctl_of.get(sid, "")
        junction = int(junction_of_ctl.get(ctl, "-1"))
        n_stages = stages_of.get(junction_of_ctl.get(ctl), 1)
        green = args.green if args.green is not None else \
            STAGE_GREEN_S[min(n_stages, len(STAGE_GREEN_S) - 1)]
        override = rig_map.get(sid)
        rig_path = (rigs.get(override, override) if override
                    else pick_rig(s, rigs, args.style, args.default_rig))
        report["rigs"][sid] = os.path.splitext(os.path.basename(rig_path))[0]
        report["junctions"][sid] = junction
        report["timing"][sid] = {"stages": n_stages, "green": green,
                                 "amber": args.amber, "red": args.red}
        yaw = float(s["yaw"]) + args.yaw_offset
        rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=yaw)
        fwd = rot.get_forward_vector()
        loc = unreal.Vector(s["x"] * 100.0 + fwd.x * args.forward_m * 100.0,
                            s["y"] * 100.0 + fwd.y * args.forward_m * 100.0,
                            s["z"] * 100.0)
        rig = spawn(world, rig_cls, loc, rot, label + "_rig", "TrafficLights")
        if rig is None:
            report["failed"].append(s["id"])
            warn("signal %s: spawn failed" % s["id"])
            continue
        try:
            rig.set_editor_property("json_file", unreal.FilePath(rig_path))
            rig.build_from_json()
            rig.set_editor_property("signal_id", sid)
            rig.set_editor_property("traffic_light_group_id", ctl)
            rig.set_editor_property("junction_id", junction)
            # ExportLogicToJSON writes these into map_logic.json, and MapLogicParser applies
            # them to the UTrafficLightController named by TrafficLightGroupID -- i.e. per
            # stage, not per junction.
            rig.set_editor_property("red_duration", args.red)
            rig.set_editor_property("green_duration", green)
            rig.set_editor_property("amber_duration", args.amber)
            n_mesh = len(rig.get_components_by_class(unreal.StaticMeshComponent))
            rig.bake(args.name, label)
        except Exception as exc:
            report["failed"].append(s["id"])
            warn("signal %s: %s" % (s["id"], str(exc).splitlines()[0][:160]))
            rig.destroy_actor()
            continue
        rig.destroy_actor()
        baked = find_actor_by_label(label)
        if baked is None:
            report["failed"].append(s["id"])
            warn("signal %s: baked actor %s not found" % (s["id"], label))
            continue
        try:
            baked.set_editor_property("is_spatially_loaded", False)
        except Exception:
            pass
        report["placed"] += 1
        report["labels"].append(label)
        if report["placed"] == 1:
            log("first rig: %d mesh components, baked as %s at (%.0f, %.0f, %.0f)" % (n_mesh, label, loc.x, loc.y, loc.z))

    # move the logic file next to the xodr (what FindPathToXODRFile + InitializeTrafficLights read)
    dst = os.path.join(xodr_dir, "map_logic.json")
    if os.path.exists(plugin_logic):
        os.makedirs(xodr_dir, exist_ok=True)
        shutil.move(plugin_logic, dst)
        with open(dst) as f:
            n_logic = len(json.load(f).get("TrafficLights", []))
        log("map_logic.json -> %s (%d entries)" % (dst, n_logic))
        report["map_logic"] = dst
        report["map_logic_entries"] = n_logic
        # drop the empty per-map plugin folder the tool created
        try:
            top = os.path.join(unreal.Paths.project_plugins_dir(), args.name)
            for d in (plugin_logic_dir, os.path.dirname(plugin_logic_dir), os.path.dirname(os.path.dirname(plugin_logic_dir)), top):
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
        except Exception as exc:
            warn("plugin folder cleanup: %s" % exc)
    else:
        warn("no map_logic.json written by Bake (%s); the runtime would spawn its own lights" % plugin_logic)
    ok = save_all()
    report["level_saved"] = bool(ok)
    report["seconds"] = round(time.time() - t0, 1)
    used = {}
    for name in report["rigs"].values():
        used[name] = used.get(name, 0) + 1
    n_bad_junction = sum(1 for v in report["junctions"].values() if v < 0)
    log("placed %d / %d rigs, %d failed, saved=%s, %.0f s" % (
        report["placed"], len(signals), len(report["failed"]), ok, report["seconds"]))
    log("rigs used: %s; junctions resolved: %d, unresolved (-1): %d" % (
        ", ".join("%s x%d" % kv for kv in sorted(used.items())),
        len(report["junctions"]) - n_bad_junction, n_bad_junction))
    rep = args.report or os.path.join(os.path.dirname(os.path.abspath(args.signals)), "traffic_lights_report.json")
    with open(rep, "w") as f:
        json.dump(report, f, indent=1)
    return 0 if report["placed"] else 1


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        unreal.log_error("[place_traffic_lights] FAILED\n" + traceback.format_exc())
        rc = 1
    print("[place_traffic_lights] exit %d" % rc, flush=True)
