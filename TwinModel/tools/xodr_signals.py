"""Signal landmarks of an OpenDRIVE file as the JSON ``ue/place_traffic_lights.py`` /
``ue/place_traffic_signs.py`` consume -- computed offline with the CARLA client library (no
server needed):

    python tools/xodr_signals.py out/v9_eixample/eixample.xodr out/v9_eixample/ue/tl_signals.json
    python tools/xodr_signals.py <xodr> <out.json> --types 205 206 274     # stop / yield / speed

Per signal: id, type, subtype, road id, s / t, orientation, country, x / y / z (m, CARLA frame)
and yaw (deg) of ``Landmark.transform`` -- the same pose the runtime would spawn its own actor
at, plus ``kind``: "through", "arrow" (a protected turn: its own signal, its own stage, type
1000001) or "ped" (a pedestrian head, type 1000002 -- never a traffic.traffic_light actor).
Default type filter: 1000001 and 1000002.

Requires final baked geometry (--twin, or a sibling .twin directory). Each record
also carries a validated physical ``placement``; its original transform remains
the logical control anchor. See ue/pole_placement.md.

Traffic lights additionally carry what a rig selector needs:
  ``validities``        the lane ranges the signal governs (from ``<validity>``);
  ``n_driving_lanes``   how many lanes that is;
  ``turns``             which movements leave those lanes ("through" / "left" / "right"),
                        derived by walking each lane through the junction and comparing
                        headings -- the twin's own turn labels are not in the xodr;
  ``has_crossing``      whether a ``<object type="crosswalk">`` sits on the same road within
                        ``--crossing-m`` of the signal.
"""
import argparse
import json
import math
from pathlib import Path
import sys

from lxml import etree

import carla

THROUGH_DEG = 30.0
JUNCTION_WALK_M = 90.0
TL_TYPE = "1000001"
PED_TYPE = "1000002"
# a protected-turn head keeps type 1000001 (SignalType::IsTrafficLight and InMemoryMap.cpp's
# junction-entry test key on that literal), so the name is the only marker
ARROW_NAME_PREFIX = "Signal_3Light_Arrow"


def signal_kind(lm) -> str:
    """"through" / "arrow" / "ped" for a traffic-light landmark; "" for anything else."""
    if lm.type == PED_TYPE:
        return "ped"
    if lm.type != TL_TYPE:
        return ""
    return "arrow" if str(lm.name).startswith(ARROW_NAME_PREFIX) else "through"


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def _turn_of(cmap, road_id, lane_id, s):
    """Walk one lane through the junction it feeds and label the movement it makes."""
    wp = cmap.get_waypoint_xodr(road_id, lane_id, s)
    if wp is None:
        return None
    h0 = math.radians(wp.transform.rotation.yaw)
    cur, walked, entered = wp, 0.0, False
    while walked < JUNCTION_WALK_M:
        nxt = cur.next(2.0)
        if not nxt:
            return None
        cur = nxt[0]
        walked += 2.0
        if cur.is_junction:
            entered = True
        elif entered:
            break
    if not entered:
        return None
    d = _wrap(math.radians(cur.transform.rotation.yaw) - h0)
    if abs(d) < math.radians(THROUGH_DEG):
        return "through"
    # CARLA is left-handed (yaw grows clockwise seen from above), so a positive delta is a
    # right turn.
    return "right" if d > 0 else "left"


def _crossings_by_road(xodr_path):
    root = etree.parse(xodr_path).getroot()
    out = {}
    for r in root.iter("road"):
        ss = [float(o.get("s", 0.0)) for o in r.iter("object")
              if o.get("type") == "crosswalk"]
        if ss:
            out[r.get("id")] = ss
    return out


def main(xodr_path: str, out_path: str, kinds=(TL_TYPE, PED_TYPE), crossing_m: float = 25.0, twin_path=None) -> int:
    with open(xodr_path) as f:
        text = f.read()
    xml_root = etree.fromstring(text.encode())
    rig_anchors = {s.get('id'):u.get('value') for s in xml_root.iter('signal')
                   for u in s.findall('userData') if u.get('code') == 'rig_anchor'}
    cmap = carla.Map("xodr_signals", text)
    crossings = _crossings_by_road(xodr_path)
    # Solve all poles together before filtering so signs and lights reserve space
    # against one another, regardless of which output file is requested.
    if twin_path is None:
        candidate = Path(xodr_path).with_suffix(".twin")
        twin_path = candidate if candidate.is_dir() else None
    if twin_path is None:
        raise ValueError("Final baked geometry is required: supply --twin <map.twin> (or place it beside the xodr)")
    placements = {}
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from twinmodel.model import TwinModel
    from twinmodel.pole_placement import PolePlacer
    from shapely.geometry import Point
    placer = PolePlacer(TwinModel.load(twin_path))
    for lm in sorted(cmap.get_all_landmarks(), key=lambda lm: lm.id):
        if rig_anchors.get(lm.id, lm.id) != lm.id:
            continue
        loc = lm.transform.location
        placements[lm.id] = placer.place(lm.id, Point(loc.x, -loc.y))
    unresolved = {k: v for k, v in placements.items() if v["status"] != "ok"}
    if unresolved:
        raise ValueError("Unsafe pole placement; no output written: " + json.dumps(unresolved))
    out = []
    for lm in cmap.get_all_landmarks():
        if kinds and lm.type not in kinds:
            continue
        t = lm.transform
        rec = {"id": lm.id, "type": lm.type, "subtype": lm.sub_type, "name": lm.name,
               "country": lm.country, "road_id": lm.road_id, "s": lm.s, "t": lm.t,
               "orientation": str(lm.orientation), "z_offset": lm.z_offset, "h_offset": lm.h_offset,
               "x": t.location.x, "y": t.location.y, "z": t.location.z, "yaw": t.rotation.yaw,
               "validities": [list(v) for v in lm.get_lane_validities()],
               "kind": signal_kind(lm)}
        lanes = sorted({l for a, b in rec["validities"]
                        for l in range(min(a, b), max(a, b) + 1) if l != 0})
        rec["lanes"] = lanes
        if lm.type == TL_TYPE:
            rec["n_driving_lanes"] = len(lanes)
            rec["lane_centers"] = []
            for lane in lanes:
                wp = cmap.get_waypoint_xodr(lm.road_id, lane, lm.s)
                if wp is not None and wp.lane_type == carla.LaneType.Driving:
                    p = wp.transform.location
                    rec["lane_centers"].append({"lane_id":lane, "x":p.x, "y":p.y,
                                                "z":p.z, "width":wp.lane_width})
            turns = {_turn_of(cmap, lm.road_id, l, lm.s) for l in lanes}
            rec["turns"] = sorted(x for x in turns if x)
            rec["has_crossing"] = any(abs(s - lm.s) <= crossing_m
                                      for s in crossings.get(str(lm.road_id), []))
        rec["rig_anchor"] = rig_anchors.get(lm.id, lm.id)
        rec["placement"] = placements[rec["rig_anchor"]]
        for lane in rec.get("lane_centers", []):
            lane["signal_id"] = lm.id
        out.append(rec)
    grouped = {r['id']:r for r in out if r['rig_anchor'] == r['id']}
    for rec in out:
        if rec['rig_anchor'] == rec['id']:continue
        anchor = grouped[rec['rig_anchor']]
        if (anchor['road_id'],anchor['yaw'],anchor['s']) != (rec['road_id'],rec['yaw'],rec['s']):
            raise ValueError('Shared rig signals disagree on their approach')
        anchor['lane_centers'].extend(rec['lane_centers'])
        anchor['lanes'].extend(rec['lanes'])
        anchor['n_driving_lanes'] = len(anchor['lane_centers'])
    out = list(grouped.values())
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{len(out)} signals -> {out_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("xodr")
    ap.add_argument("out")
    ap.add_argument("--types", nargs="*", default=[TL_TYPE, PED_TYPE],
                    help="signal types to keep (empty = all); default: traffic lights (1000001, "
                         "through + protected turn) and pedestrian heads (1000002)")
    ap.add_argument("--crossing-m", type=float, default=25.0,
                    help="a crosswalk this close on the same road marks the signal has_crossing")
    ap.add_argument("--twin", help="Final baked .twin geometry for safe physical pole placement; auto-detected beside xodr")
    a = ap.parse_args()
    sys.exit(main(a.xodr, a.out, tuple(a.types), a.crossing_m, a.twin))
