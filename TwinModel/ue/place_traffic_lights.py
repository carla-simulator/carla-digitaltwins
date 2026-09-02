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


# A protected turn and a pedestrian crossing are their *own* OpenDRIVE signals (the exporter
# writes kind "arrow"/"ped" into tl_signals.json), and no European pole has an arrow or a
# pedestrian head to show, so the kind wins over --style.
KIND_RIG = {"arrow": "na_arrow_left", "ped": "na_ped_only"}
PED_TYPE = "1000002"


def pick_rig(sig, rigs, style, default):
    """Choose a rig preset for one traffic-light signal.

    ``tl_signals.json`` carries what the choice needs (tools/xodr_signals.py):
    ``kind`` ("through" / "arrow" / "ped"), ``n_driving_lanes`` (how wide the approach is),
    ``turns`` (which movements leave it) and ``has_crossing``. The North-American presets are
    only reachable with ``--style na``; ``--style eu`` keeps every *through* approach on the
    European pole, which is what a European twin should look like whatever its lane count.
    """
    forced = KIND_RIG.get(sig.get("kind"))
    if forced and forced in rigs:
        return rigs[forced]
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


def rig_entry(value):
    """A --rig-map value: either a rig name/path, or {"rig": ..., "signals": {token: signal}}.

    The symbolic tokens ("@through", "@left", "@ped") appear as the ``SignalID`` of a head in
    the preset; the placer substitutes them before building, so one baked rig can carry heads
    that belong to different OpenDRIVE signals. ``@through`` defaults to the signal the rig is
    placed at.
    """
    if isinstance(value, dict):
        return value.get("rig"), dict(value.get("signals") or {})
    return value, {}


def substitute_head_signals(rig_path, anchor_signal, tokens):
    """The preset's text with every head ``SignalID`` token replaced by a real signal id.

    Returns (json text, {signal id: [head prefixes]}). A head whose SignalID is missing or
    unresolved falls back to the anchor, which is what ExportLogicToJSON does for an empty one.
    """
    with open(rig_path) as f:
        rig = json.load(f)
    table = {"@through": anchor_signal}
    table.update(tokens or {})
    bound = {}
    for pi, pole in enumerate(rig.get("Poles", [])):
        for hi, head in enumerate(pole.get("Heads", [])):
            sid = head.get("SignalID") or "@through"
            if sid.startswith("@"):
                sid = table.get(sid, anchor_signal)
            head["SignalID"] = sid
            bound.setdefault(sid, []).append("Pole_%02d_Head_%02d" % (pi, hi))
    return json.dumps(rig), bound


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
    ap.add_argument("--ped-yaw-offset", type=float, default=90.0,
                    help="the same, for a pedestrian head (type 1000002), whose signal yaw "
                         "already looks across the crossing through its @hOffset")
    # 0, not 0.25: the transform in tl_signals.json is carla.Landmark.transform, which already
    # contains CARLA's own +0.25 m traffic-light nudge (MapBuilder.cpp:858). Adding it again
    # put the rig ~0.5 m from the road point and left only 25 cm of the 50 cm match radius
    # UMapLogicParser::ApplyLaneIdsFromMapLogic searches in.
    ap.add_argument("--forward-m", type=float, default=0.0)
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    t0 = time.time()
    with open(args.signals) as f:
        signals = [s for s in json.load(f)
                   if s.get("type", "1000001") in ("1000001", PED_TYPE)]
    ped_ids = {str(s["id"]) for s in signals if s.get("kind") == "ped"}
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
    # a signal bound to another rig's head gets no rig of its own: the gantry carrying it is
    # split into one ADigitalTwinsTrafficLight per signal at load time (UMapLogicParser), and
    # a second actor for the same signal would be a duplicate opendrive id.
    claimed = {}
    for anchor, value in rig_map.items():
        for token, sid in rig_entry(value)[1].items():
            if str(sid) != str(anchor):
                claimed[str(sid)] = str(anchor)
    if claimed:
        log("%d signal(s) carried by another rig's heads: %s"
            % (len(claimed), ", ".join("%s on %s" % kv for kv in sorted(claimed.items()))))
        signals = [s for s in signals if str(s["id"]) not in claimed]
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
    report = {"placed": 0, "failed": [], "labels": [], "rigs": {}, "junctions": {}, "timing": {},
              "kinds": {}}
    placed_at = {}
    for s in signals:
        label = "TL_%s" % s["id"]
        sid = str(s["id"])
        ctl = ctl_of.get(sid, "")
        junction = int(junction_of_ctl.get(ctl, "-1"))
        n_stages = stages_of.get(junction_of_ctl.get(ctl), 1)
        green = args.green if args.green is not None else \
            STAGE_GREEN_S[min(n_stages, len(STAGE_GREEN_S) - 1)]
        override, tokens = rig_entry(rig_map.get(sid))
        rig_path = (rigs.get(override, override) if override
                    else pick_rig(s, rigs, args.style, args.default_rig))
        report["rigs"][sid] = os.path.splitext(os.path.basename(rig_path))[0]
        report["junctions"][sid] = junction
        report["kinds"][sid] = s.get("kind", "through")
        report["timing"][sid] = {"stages": n_stages, "green": green,
                                 "amber": args.amber, "red": args.red}
        # A pedestrian head is aimed *across* the crossing by the signal's own @hOffset, which
        # CARLA has already folded into the landmark yaw here; the offset added on top is only
        # the rig model's convention (its lamps sit on the actor's +Y, so the spawn yaw is the
        # signal yaw + 90, exactly as ATrafficLightManager::SpawnSignals does).
        yaw_offset = args.ped_yaw_offset if s.get("kind") == "ped" else args.yaw_offset
        yaw = float(s["yaw"]) + yaw_offset
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
            # Built from text, not from the file: the preset's head SignalID tokens are
            # resolved to real signal ids here, and ExportLogicToJSON carries them into
            # map_logic.json as the "Heads" array UMapLogicParser splits the rig by.
            rig.set_editor_property("json_file", unreal.FilePath(rig_path))
            rig_text, bound = substitute_head_signals(rig_path, sid, tokens)
            rig.build_from_json_string(rig_text)
            if len(bound) > 1:
                report.setdefault("split_rigs", {})[sid] = bound
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
        # UMapLogicParser looks the rig up by this tag first. An actor *label* is
        # editor-only data; a tag is a plain runtime UPROPERTY, so the binding survives a
        # cook. Without it the parser falls back to the nearest actor within 50 cm -- which
        # is a guess, and wrong for two rigs sharing a junction corner.
        try:
            baked.set_editor_property("tags", [unreal.Name(label)])
        except Exception as exc:
            warn("could not tag %s: %s" % (label, exc))
        report["placed"] += 1
        report["labels"].append(label)
        placed_at[sid] = (loc.x, loc.y, loc.z)
        if report["placed"] == 1:
            log("first rig: %d mesh components, baked as %s at (%.0f, %.0f, %.0f)" % (n_mesh, label, loc.x, loc.y, loc.z))

    # No two rigs may sit inside UMapLogicParser::ApplyLaneIdsFromMapLogic's 50 cm match
    # radius: it adopts the nearest actor, so a protected-turn pole 20 cm from its through pole
    # would be handed the through signal's identity (or steal it). The exporter puts the arrow
    # JunctionRules.signal_arrow_offset_m upstream of the through pole for exactly this reason.
    MIN_SEPARATION_M = 0.60
    close = []
    ids = sorted(placed_at)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = placed_at[a], placed_at[b]
            d = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2) ** 0.5 / 100.0
            if d < MIN_SEPARATION_M:
                close.append({"a": a, "b": b, "m": round(d, 3)})
    report["min_separation_m"] = MIN_SEPARATION_M
    report["too_close"] = close
    if close:
        warn("%d rig pair(s) closer than %.2f m: %s" % (len(close), MIN_SEPARATION_M, close[:5]))

    # move the logic file next to the xodr (what FindPathToXODRFile + InitializeTrafficLights read)
    dst = os.path.join(xodr_dir, "map_logic.json")
    if os.path.exists(plugin_logic):
        os.makedirs(xodr_dir, exist_ok=True)
        # A pedestrian head is a prop, not a traffic light: dropping its entry from
        # map_logic.json is what keeps UMapLogicParser from turning it into an
        # ADigitalTwinsTrafficLight -- i.e. an ATrafficLightBase that every client script would
        # see as a traffic.traffic_light actor. Walkers never read a light anyway
        # (AWalkerAIController), so the baked rig stays a static, correctly-styled prop.
        with open(plugin_logic) as f:
            logic = json.load(f)
        kept = [e for e in logic.get("TrafficLights", []) if str(e.get("SignalID")) not in ped_ids]
        n_dropped = len(logic.get("TrafficLights", [])) - len(kept)
        logic["TrafficLights"] = kept
        with open(plugin_logic, "w") as f:
            json.dump(logic, f, indent=1)
        shutil.move(plugin_logic, dst)
        n_logic = len(kept)
        report["ped_entries_dropped"] = n_dropped
        log("map_logic.json -> %s (%d entries, %d pedestrian heads left out on purpose)"
            % (dst, n_logic, n_dropped))
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
    kinds = {}
    for k in report["kinds"].values():
        kinds[k] = kinds.get(k, 0) + 1
    log("rigs used: %s; kinds: %s; junctions resolved: %d, unresolved (-1): %d" % (
        ", ".join("%s x%d" % kv for kv in sorted(used.items())),
        ", ".join("%s x%d" % kv for kv in sorted(kinds.items())),
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
