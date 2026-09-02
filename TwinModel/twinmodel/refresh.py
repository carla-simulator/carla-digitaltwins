"""``twinmodel refresh-signals``: bring an already baked twin level up to date with the current
signal exporter without rebaking it.

    python -m twinmodel refresh-signals <build_dir> <LevelName> [--style eu|na] [--rig-map J]
                                        [--dry-run] [--no-editor]

Steps:
  1. rebuild the twin into ``<build_dir>_refresh`` with the exact ``build`` arguments recorded
     in ``<build_dir>/report.json`` (same fixture, cache, imagery/DEM/refine flags, profile);
  2. byte-compare the new xodr with the deployed one
     (``<Content>/Carla/Maps/Twins/<Level>/OpenDrive/<Level>.xodr``) outside ``<signals>``,
     ``<controller>``, ``<signalReference>`` and the junction ``<controller>`` refs. Identical:
     the new xodr is the candidate (mode ``rebuild``). Geometry moved but the *topology* (road
     ids, lane sections, links, junction connections, signal set) is the same -- the usual case
     for a level baked before a reference-line change: the new validities and controllers are
     grafted onto the deployed geometry, every signal keeps its own s/t (mode ``graft``).
     Anything else means the level needs a rebake, and the command stops;
  3. run ``tools/xodr_tl_check.py`` on the candidate;
  4. write the new xodr over the deployed one (``.bak`` kept next to it);
  5. regenerate ``ue/tl_signals.json`` and ``ue/sign_signals.json`` with ``tools/xodr_signals.py``;
  6. unless ``--dry-run``/``--no-editor``: stop the demo units, run the two placement commandlets
     (lights, then signs) in ONE flock'd shell through ``systemd-run --wait``, relaunch the units
     that were running;
  7. write ``<build_dir>/ue/refresh_report.json`` and print a summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("twinmodel.refresh")

TWINMODEL_DIR = Path(__file__).resolve().parents[1]
CARLA_SOURCE = Path(os.environ.get("CARLA_SOURCE", str(TWINMODEL_DIR.parents[1])))
DEFAULT_CONTENT = CARLA_SOURCE / "carla-ue5/Unreal/CarlaUnreal/Content"
DEFAULT_UPROJECT = CARLA_SOURCE / "carla-ue58-dtupgrade/Unreal/CarlaUnreal/CarlaUnreal.uproject"
DEFAULT_UE_CMD = CARLA_SOURCE / "UnrealEngine_5/Engine/Binaries/Linux/UnrealEditor-Cmd"
DEFAULT_UE = CARLA_SOURCE / "UnrealEngine_5/Engine/Binaries/Linux/UnrealEditor"
UE_LOCK = CARLA_SOURCE / ".omc/ue.lock"
LOG_DIR = CARLA_SOURCE / ".omc/logs"
DEMO_UNITS = ("int-demo-tour", "int-game-demo")
DEMO_GAME_ARGS = ("/Game/Carla/Maps/Twins/EixampleDemo/EixampleDemo -game -windowed -ResX=1600 -ResY=900 -nosound "
                  "-norelativemousemode -carla-rpc-port=3000 -carla-streaming-port=3001 -carla-secondary-port=3002")

# build args recorded in report.json["build"]["args"] -> CLI flags (order = CLI order)
_BUILD_FLAGS = (
    ("bbox", "--bbox", "list"), ("name", "--name", "value"), ("fixture", "--fixture", "value"),
    ("cache", "--cache", "value"), ("no_imagery", "--no-imagery", "flag"), ("no_dem", "--no-dem", "flag"),
    ("no_refine", "--no-refine", "flag"), ("mask_method", "--mask-method", "value"),
    ("profile", "--profile", "value"), ("step", "--step", "value"), ("junction_zooms", "--junction-zooms", "value"),
)


# ------------------------------------------------------------------ pure helpers (unit-tested)

def build_argv_from_report(report: dict, out: str) -> list[str]:
    """Reconstruct the ``twinmodel build`` argv of a build from its report.json, targeting ``out``."""
    args = (report.get("build") or {}).get("args")
    if not args or args.get("cmd", "build") != "build":
        raise ValueError("report.json carries no build args (report['build']['args'])")
    argv = ["build", "--out", out]
    for key, flag, kind in _BUILD_FLAGS:
        v = args.get(key)
        if v is None or v == "" or (kind == "flag" and not v):
            continue
        if kind == "flag":
            argv.append(flag)
        elif kind == "list":
            argv.append(flag)
            argv.extend(str(x) for x in v)
        else:
            argv.extend([flag, str(v)])
    return argv


_STRIP = [
    (re.compile(r"<\?xml[^>]*\?>"), ""),
    (re.compile(r"<signals>.*?</signals>", re.S), ""),
    (re.compile(r"<signals\s*/>"), ""),
    (re.compile(r"<signalReference\b.*?(?:/>|</signalReference>)", re.S), ""),
    (re.compile(r"<controller\b[^>]*/>"), ""),
    (re.compile(r"<controller\b.*?</controller>", re.S), ""),
    (re.compile(r"<header\b[^>]*>"), "<header>"),
    (re.compile(r">\s+<"), "><"),
    (re.compile(r"\s+/>"), "/>"),
    (re.compile(r"\s+"), " "),
]


def strip_signals(xodr_text: str) -> str:
    """The part of an xodr that a signal refresh must not change (geometry, lanes, links)."""
    for rx, rep in _STRIP:
        xodr_text = rx.sub(rep, xodr_text)
    return xodr_text.strip()


def geometry_identical(a: str, b: str) -> bool:
    return strip_signals(a) == strip_signals(b)


def xodr_stats(xodr_text: str) -> dict[str, Any]:
    """Counts the refresh reports: validities, controllers per junction, signals by type."""
    from lxml import etree
    root = etree.fromstring(xodr_text.encode() if isinstance(xodr_text, str) else xodr_text)
    by_type: dict[str, int] = {}
    validities = 0
    signals = 0
    for sig in root.iter("signal"):
        signals += 1
        by_type[sig.get("type", "?")] = by_type.get(sig.get("type", "?"), 0) + 1
        validities += len(sig.findall("validity"))
    ctl_per_junction = {j.get("id"): len(j.findall("controller")) for j in root.iter("junction")
                        if j.findall("controller")}
    return {
        "signals": signals,
        "by_type": dict(sorted(by_type.items())),
        "validities": validities,
        "controllers": len([c for c in root.findall("controller")]),
        "controllers_per_junction": ctl_per_junction,
        "signal_references": len(list(root.iter("signalReference"))),
    }


def _parse(xodr_text):
    from lxml import etree
    # keep the <geoReference> CDATA so a re-serialised tree stays byte-comparable with the original
    parser = etree.XMLParser(strip_cdata=False)
    return etree.fromstring(xodr_text.encode() if isinstance(xodr_text, str) else xodr_text, parser)


def topology_signature(xodr_text: str) -> dict[str, Any]:
    """Everything a signal graft relies on: road ids + junction membership, lane sections (the
    validity lane ids), junction connections and the signal set (id, road, type, subtype,
    orientation) -- but not geometry. Road <link>s are reported separately (``links``): a
    relinked connecting road does not move a signal or its lanes, so it must not block a graft."""
    root = _parse(xodr_text)
    roads = {}
    links = {}
    for rd in root.iter("road"):
        secs = tuple(tuple(sorted((l.get("id"), l.get("type")) for l in ls.iter("lane"))) for ls in rd.iter("laneSection"))
        link = rd.find("link")
        links[rd.get("id")] = tuple(sorted((e.tag, e.get("elementType"), e.get("elementId"), e.get("contactPoint"))
                                           for e in (link if link is not None else [])))
        roads[rd.get("id")] = (rd.get("junction"), secs)
    junctions = {}
    for j in root.iter("junction"):
        junctions[j.get("id")] = tuple(sorted(
            (c.get("incomingRoad"), c.get("connectingRoad"), c.get("contactPoint"),
             tuple((l.get("from"), l.get("to")) for l in c.iter("laneLink"))) for c in j.iter("connection")))
    signals = {}
    for s in root.iter("signal"):
        road = s.getparent().getparent()
        signals[s.get("id")] = (road.get("id"), s.get("type"), s.get("subtype"), s.get("orientation"))
    return {"roads": roads, "junctions": junctions, "signals": signals, "links": links}


def topology_diff(a: str, b: str) -> dict[str, list]:
    """Per signature part, the ids whose entries differ (empty lists = identical)."""
    sa, sb = topology_signature(a), topology_signature(b)
    return {k: sorted(i for i in set(sa[k]) | set(sb[k]) if sa[k].get(i) != sb[k].get(i)) for k in sa}


def topology_identical(a: str, b: str) -> bool:
    """Roads (ids, junction membership, lane sections) and junction connections are the same.
    Signals are *not* part of this: the exporter numbers them sequentially, so ids move whenever
    a signal is added; ``graft_signals`` matches them by position instead."""
    d = topology_diff(a, b)
    return not (d["roads"] or d["junctions"])


_SIG_ATTRS = ("name", "country", "value", "unit", "text", "dynamic", "height", "width", "hOffset", "zOffset",
              "pitch", "roll", "subtype", "countryRevision")


def _road_lengths(root) -> dict[str, float]:
    return {rd.get("id"): float(rd.get("length")) for rd in root.iter("road")}


def match_signals(old_root, new_root, tol_m: float = 15.0) -> tuple[dict, list, list]:
    """Pair every deployed signal with a rebuilt one of the same (road, type, subtype, orientation)
    by nearest s (one to one, closest pairs first). Returns (old id -> new signal element,
    unmatched old ids, unmatched new signal elements)."""
    def key(sig):
        road = sig.getparent().getparent()
        return (road.get("id"), sig.get("type"), sig.get("subtype") or "-1", sig.get("orientation"))
    old_by, new_by = {}, {}
    for s in old_root.iter("signal"):
        old_by.setdefault(key(s), []).append(s)
    for s in new_root.iter("signal"):
        new_by.setdefault(key(s), []).append(s)
    pairs, unmatched_old, unmatched_new = {}, [], []
    for k, olds in old_by.items():
        news = list(new_by.get(k, []))
        cands = sorted(((abs(float(o.get("s")) - float(n.get("s"))), i, j) for i, o in enumerate(olds)
                        for j, n in enumerate(news) if abs(float(o.get("s")) - float(n.get("s"))) <= tol_m))
        used_o, used_n = set(), set()
        for ds, i, j in cands:
            if i in used_o or j in used_n:
                continue
            used_o.add(i); used_n.add(j)
            pairs[olds[i].get("id")] = news[j]
        unmatched_old += [olds[i].get("id") for i in range(len(olds)) if i not in used_o]
        unmatched_new += [news[j] for j in range(len(news)) if j not in used_n]
    for k, news in new_by.items():
        if k not in old_by:
            unmatched_new += news
    return pairs, unmatched_old, unmatched_new


def graft_signals(deployed_text: str, new_text: str, tol_m: float = 15.0) -> tuple[str, dict[str, Any]]:
    """Transplant the signal semantics of ``new_text`` onto ``deployed_text``, keeping the deployed
    geometry. Requires ``topology_identical``.

    * a deployed signal matched to a rebuilt one (``match_signals``) keeps its own s/t and takes
      the rebuilt signal's id, attributes and <validity> children -- the whole file then speaks
      the rebuilt ids, which is what the rebuilt <controller>s reference;
    * a rebuilt signal with no deployed counterpart (Phase 5 arrows, pedestrian heads, rule stops)
      is added to the same road at ``s_anchor_old + (s_new - s_anchor_new)`` of the nearest matched
      signal on that road (an arrow shares its through light's stop line exactly), or, if the road
      has no matched signal, at the same offset from the nearer road end; t is the rebuilt one;
    * a deployed signal with no counterpart within ``tol_m`` is an error (the signal set changed
      in a way a refresh cannot express);
    * root <controller>s and junction <controller> refs are replaced by the rebuilt ones.
    Returns (xodr text, stats)."""
    from lxml import etree
    if not topology_identical(deployed_text, new_text):
        raise ValueError("topology differs; a graft would attach signals to the wrong lanes")
    old = _parse(deployed_text)
    new = _parse(new_text)
    pairs, unmatched_old, unmatched_new = match_signals(old, new, tol_m)
    if unmatched_old:
        raise ValueError("%d deployed signal(s) have no rebuilt counterpart within %.0f m: %s" % (
            len(unmatched_old), tol_m, ", ".join(unmatched_old[:8])))
    len_old, len_new = _road_lengths(old), _road_lengths(new)
    stats = {"matched": len(pairs), "added": len(unmatched_new), "added_by_anchor": 0, "added_by_end_offset": 0,
             "max_s_shift": 0.0, "clamped": 0}
    # anchors: per road, [(s_old, s_new)] of matched signals
    anchors: dict[str, list[tuple[float, float]]] = {}
    old_road_of: dict[str, Any] = {}
    for s in list(old.iter("signal")):
        src = pairs[s.get("id")]
        road = s.getparent().getparent()
        anchors.setdefault(road.get("id"), []).append((float(s.get("s")), float(src.get("s"))))
        old_road_of[road.get("id")] = road
        stats["max_s_shift"] = max(stats["max_s_shift"], abs(float(s.get("s")) - float(src.get("s"))))
        for v in s.findall("validity"):
            s.remove(v)
        for v in src.findall("validity"):
            s.append(etree.fromstring(etree.tostring(v)))
        for k in _SIG_ATTRS:
            if src.get(k) is not None:
                s.set(k, src.get(k))
            elif s.get(k) is not None:
                del s.attrib[k]
        s.set("id", src.get("id"))
    old_roads = {rd.get("id"): rd for rd in old.iter("road")}
    for src in unmatched_new:
        rid = src.getparent().getparent().get("id")
        road = old_roads[rid]
        s_new = float(src.get("s"))
        if anchors.get(rid):
            s_a_old, s_a_new = min(anchors[rid], key=lambda a: abs(a[1] - s_new))
            s_old = s_a_old + (s_new - s_a_new)
            stats["added_by_anchor"] += 1
        else:
            # no matched signal on this road: keep the offset from the nearer road end (signals
            # sit at junction mouths, and a reference-line change moves the road's *ends*)
            s_old = s_new if s_new <= len_new[rid] / 2 else len_old[rid] - (len_new[rid] - s_new)
            stats["added_by_end_offset"] += 1
        lo, hi = 0.0, max(0.0, len_old[rid])
        if not (lo <= s_old <= hi):
            stats["clamped"] += 1
            s_old = min(max(s_old, lo), hi)
        el = etree.fromstring(etree.tostring(src))
        el.set("s", "%.4f" % s_old)
        sigs = road.find("signals")
        if sigs is None:
            sigs = etree.SubElement(road, "signals")
        sigs.append(el)
    # root controllers: drop the old ones, insert the new ones at the old position (or before junctions)
    old_ctls = old.findall("controller")
    anchor = old_ctls[0] if old_ctls else (old.find("junction"))
    idx = list(old).index(anchor) if anchor is not None else len(old)
    for c in old_ctls:
        old.remove(c)
    for i, c in enumerate(new.findall("controller")):
        old.insert(idx + i, etree.fromstring(etree.tostring(c)))
    new_j = {j.get("id"): j for j in new.iter("junction")}
    for j in old.iter("junction"):
        for c in j.findall("controller"):
            j.remove(c)
        for c in new_j[j.get("id")].findall("controller"):
            j.append(etree.fromstring(etree.tostring(c)))
    stats["max_s_shift"] = round(stats["max_s_shift"], 2)
    return etree.tostring(old, xml_declaration=True, encoding="UTF-8", pretty_print=True).decode(), stats


def editor_commands(level: str, build_dir: Path, style: str, rig_map: Optional[str],
                    uproject: Path = DEFAULT_UPROJECT, ue_cmd: Path = DEFAULT_UE_CMD) -> list[list[str]]:
    """The two placement commandlets (lights, then signs) as argv lists."""
    ue = TWINMODEL_DIR / "ue"
    lights = ["--name", level, "--signals", str(build_dir / "ue/tl_signals.json"), "--rig", str(ue / "rigs"),
              "--style", style, "--report", str(build_dir / "ue/traffic_lights_report.json")]
    if rig_map:
        lights += ["--rig-map", str(Path(rig_map).resolve())]
    signs = ["--name", level, "--signals", str(build_dir / "ue/sign_signals.json"),
             "--manifest", str(ue / "assets/sign_catalog_manifest.json"),
             "--report", str(build_dir / "ue/traffic_signs_report.json")]
    common = ["-unattended", "-nosplash", "-nosound", "-stdout", "-FullStdOutLogOutput"]
    return [
        [str(ue_cmd), str(uproject), "-run=pythonscript",
         "-script=%s %s" % (ue / "place_traffic_lights.py", " ".join(lights))] + common,
        [str(ue_cmd), str(uproject), "-run=pythonscript",
         "-script=%s %s" % (ue / "place_traffic_signs.py", " ".join(signs))] + common,
    ]


def chained_shell(cmds: list[list[str]]) -> str:
    return " && ".join(" ".join(shlex.quote(a) for a in c) for c in cmds)


# ------------------------------------------------------------------ the command

def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(shlex.quote(a) for a in argv))
    return subprocess.run(argv, **kw)


def _unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "-q", unit]).returncode == 0


def _relaunch_demo(units: list[str]) -> None:
    env = ["--setenv=DISPLAY=:1", "--setenv=DLSS_SDK=%s" % os.environ.get("DLSS_SDK", "/home/german/SDKs/DLSS")]
    if "int-game-demo" in units:
        logf = LOG_DIR / "game-demo.log"
        _run(["systemd-run", "--user", "--collect", "--unit", "int-game-demo",
              "-p", "StandardOutput=file:%s" % logf, "-p", "StandardError=file:%s" % logf, *env,
              "/usr/bin/flock", str(UE_LOCK), str(DEFAULT_UE), str(DEFAULT_UPROJECT), *DEMO_GAME_ARGS.split()],
             check=True)
        for _ in range(60):
            time.sleep(5)
            if subprocess.run("ss -ltn | grep -q ':3000 '", shell=True).returncode == 0:
                break
        time.sleep(60)
    if "int-demo-tour" in units:
        tour = os.environ.get("DEMO_TOUR")
        if not tour:
            log.warning("int-demo-tour was running but $DEMO_TOUR (path to demo_tour.py) is unset; not relaunched")
            return
        logf = LOG_DIR / "demo-tour.log"
        _run(["systemd-run", "--user", "--collect", "--unit", "int-demo-tour",
              "-p", "StandardOutput=file:%s" % logf, "-p", "StandardError=file:%s" % logf,
              str(CARLA_SOURCE / ".venv-twins/bin/python"), tour, "3000"], check=True)


def refresh(args: argparse.Namespace) -> int:
    build_dir = Path(args.build_dir).resolve()
    level = args.name
    content = Path(args.content).resolve()
    report_path = build_dir / "report.json"
    if not report_path.exists():
        log.error("no report.json in %s", build_dir)
        return 2
    with open(report_path) as f:
        report = json.load(f)
    twin_name = report["build"]["args"]["name"]
    deployed = content / "Carla/Maps/Twins" / level / "OpenDrive" / (level + ".xodr")
    if not deployed.exists():
        log.error("deployed xodr not found: %s", deployed)
        return 2
    out: dict[str, Any] = {"level": level, "build_dir": str(build_dir), "deployed_xodr": str(deployed),
                           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 1. rebuild with the recorded args
    rebuild = Path(args.rebuild_out).resolve() if args.rebuild_out else build_dir.with_name(build_dir.name + "_refresh")
    argv = build_argv_from_report(report, os.path.relpath(rebuild, TWINMODEL_DIR))
    out["build_argv"] = argv
    new_xodr = rebuild / (twin_name + ".xodr")
    if args.reuse_rebuild and new_xodr.exists():
        log.info("reusing existing rebuild %s", rebuild)
    else:
        r = _run([sys.executable, "-m", "twinmodel", *argv], cwd=str(TWINMODEL_DIR))
        if r.returncode != 0 or not new_xodr.exists():
            log.error("rebuild failed (rc=%s)", r.returncode)
            return 3

    # 2. the level's geometry must stay what it is: either the rebuild reproduces it byte for byte
    #    (level baked with the current exporter) or, if only the geometry moved while the
    #    topology is the same, graft the new signal semantics onto the deployed geometry
    old_text = deployed.read_text()
    new_text = new_xodr.read_text()
    out["geometry_identical"] = geometry_identical(old_text, new_text)
    out["topology_diff"] = topology_diff(old_text, new_text)
    out["topology_identical"] = out["geometry_identical"] or topology_identical(old_text, new_text)
    if out["topology_diff"]["links"]:
        log.warning("%s: %d road <link>(s) differ between the deployed and the rebuilt xodr (%s); a graft "
                    "keeps the deployed links", level, len(out["topology_diff"]["links"]),
                    ", ".join(out["topology_diff"]["links"][:5]))
    out["before"] = xodr_stats(old_text)
    out["after_build"] = xodr_stats(new_text)
    if out["geometry_identical"] and args.mode in ("auto", "rebuild"):
        out["mode"] = "rebuild"
        candidate_text = new_text
    elif out["topology_identical"] and args.mode in ("auto", "graft"):
        out["mode"] = "graft"
        candidate_text, out["graft"] = graft_signals(old_text, new_text)
        assert geometry_identical(old_text, candidate_text), "graft changed the geometry"
        log.info("graft: %s", out["graft"])
    else:
        log.error("%s: the rebuilt xodr differs from the deployed one in topology (roads/lanes/links/"
                  "junctions/signal set), not only in signals -- the level needs a rebake, not a refresh "
                  "(nothing written)", level)
        _write_report(build_dir, out, status="topology-mismatch")
        return 4
    candidate = rebuild / (level + ".candidate.xodr")
    candidate.write_text(candidate_text)
    out["after"] = xodr_stats(candidate_text)
    out["candidate_xodr"] = str(candidate)

    # 3. post-check on the candidate
    chk = _run([sys.executable, str(TWINMODEL_DIR / "tools/xodr_tl_check.py"), str(candidate), "--quiet",
                "--json", str(rebuild / "xodr_tl_check.json")])
    out["xodr_tl_check_rc"] = chk.returncode
    if chk.returncode != 0:
        log.error("xodr_tl_check failed on the candidate xodr (rc=%d); nothing written", chk.returncode)
        _write_report(build_dir, out, status="check-failed")
        return 5

    if args.dry_run:
        out["editor_commands"] = editor_commands(level, build_dir, args.style, args.rig_map)
        _write_report(build_dir, out, status="dry-run")
        _summary(out)
        return 0

    # 4. swap the deployed xodr
    bak = deployed.with_suffix(".xodr.bak")
    shutil.copy2(deployed, bak)
    shutil.copy2(candidate, deployed)
    out["backup"] = str(bak)
    log.info("xodr -> %s (%s, backup %s)", deployed, out["mode"], bak.name)

    # 5. signal JSONs for the placers
    ue_dir = build_dir / "ue"
    ue_dir.mkdir(exist_ok=True)
    xs = TWINMODEL_DIR / "tools/xodr_signals.py"
    r1 = _run([sys.executable, str(xs), str(deployed), str(ue_dir / "tl_signals.json")])
    r2 = _run([sys.executable, str(xs), str(deployed), str(ue_dir / "sign_signals.json"), "--types", "205", "206", "274"])
    if r1.returncode or r2.returncode:
        log.error("xodr_signals failed")
        _write_report(build_dir, out, status="signals-json-failed")
        return 6
    with open(ue_dir / "tl_signals.json") as f:
        out["tl_signals"] = len(json.load(f))
    with open(ue_dir / "sign_signals.json") as f:
        out["sign_signals"] = len(json.load(f))

    # 6. placement commandlets
    cmds = editor_commands(level, build_dir, args.style, args.rig_map)
    out["editor_commands"] = cmds
    out["editor_shell"] = chained_shell(cmds)
    if args.no_editor:
        _write_report(build_dir, out, status="xodr-swapped, editor pending")
        _summary(out)
        return 0
    running = [u for u in DEMO_UNITS if _unit_active(u)]
    out["demo_units_stopped"] = running
    if running:
        _run(["systemctl", "--user", "stop", *running], check=True)
        time.sleep(3)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = LOG_DIR / ("refresh-signals-%s.log" % level.lower())
    unit = "int-refresh-%s" % level.lower()
    r = _run(["systemd-run", "--user", "--collect", "--wait", "--unit", unit,
              "-p", "StandardOutput=file:%s" % logf, "-p", "StandardError=file:%s" % logf,
              "--setenv=DLSS_SDK=%s" % os.environ.get("DLSS_SDK", "/home/german/SDKs/DLSS"),
              "/usr/bin/flock", str(UE_LOCK), "/bin/bash", "-c", chained_shell(cmds)])
    out["editor_rc"] = r.returncode
    out["editor_log"] = str(logf)
    for name in ("traffic_lights_report.json", "traffic_signs_report.json"):
        p = ue_dir / name
        if p.exists():
            with open(p) as f:
                out[name.replace(".json", "")] = json.load(f)
    if running:
        _relaunch_demo(running)
        out["demo_units_relaunched"] = running
    status = "ok" if r.returncode == 0 else "editor-failed"
    _write_report(build_dir, out, status=status)
    _summary(out)
    return 0 if r.returncode == 0 else 7


def _write_report(build_dir: Path, out: dict, status: str) -> None:
    out["status"] = status
    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (build_dir / "ue").mkdir(exist_ok=True)
    with open(build_dir / "ue/refresh_report.json", "w") as f:
        json.dump(out, f, indent=1)


def _summary(out: dict) -> None:
    b, a = out["before"], out.get("after") or out["after_build"]
    print("refresh-signals %s: %s" % (out["level"], out["status"]))
    print("  geometry identical: %s; topology identical: %s; mode: %s" % (
        out["geometry_identical"], out.get("topology_identical"), out.get("mode", "-")))
    print("  validities %d -> %d; controllers %d -> %d; per junction %s -> %s" % (
        b["validities"], a["validities"], b["controllers"], a["controllers"],
        sorted(b["controllers_per_junction"].values()), sorted(a["controllers_per_junction"].values())))
    print("  signals by type: %s" % a["by_type"])
    if "tl_signals" in out:
        print("  placement inputs: %d traffic lights, %d signs" % (out["tl_signals"], out["sign_signals"]))
    for key in ("traffic_lights_report", "traffic_signs_report"):
        if key in out:
            rep = out[key]
            print("  %s: %s" % (key, {k: rep[k] for k in rep if k in ("placed", "failed", "removed", "adopted", "signs")}))
    if "editor_shell" in out and out["status"] != "ok":
        print("  editor (pending): flock %s bash -c %s" % (UE_LOCK, shlex.quote(out["editor_shell"])))


def add_parser(sub) -> None:
    p = sub.add_parser("refresh-signals", help="re-export a baked twin's signals (validity, staged controllers) "
                                                "into the deployed xodr and re-place its lights and signs, "
                                                "without rebaking (twinmodel.refresh)")
    p.add_argument("build_dir", help="the build directory the level was baked from (holds report.json)")
    p.add_argument("name", help="baked level name under /Game/Carla/Maps/Twins")
    p.add_argument("--style", default="eu", choices=("eu", "na"), help="traffic-light rig family")
    p.add_argument("--rig-map", default=None, help="JSON {signal id: rig} passed to place_traffic_lights.py")
    p.add_argument("--content", default=str(DEFAULT_CONTENT), help="UE Content root of the shared content")
    p.add_argument("--mode", default="auto", choices=("auto", "rebuild", "graft"),
                   help="rebuild: the rebuilt xodr replaces the deployed one (requires identical geometry); "
                        "graft: transplant validities/controllers onto the deployed geometry (requires "
                        "identical topology); auto: rebuild if possible, else graft")
    p.add_argument("--rebuild-out", default=None, help="where to rebuild (default <build_dir>_refresh)")
    p.add_argument("--reuse-rebuild", action="store_true", help="skip the rebuild if <rebuild_out> already has the xodr")
    p.add_argument("--dry-run", action="store_true", help="rebuild + compare only; write nothing")
    p.add_argument("--no-editor", action="store_true", help="swap the xodr and regenerate the signal JSONs, "
                                                             "but print the placement commands instead of running them")
    p.set_defaults(func=refresh)
