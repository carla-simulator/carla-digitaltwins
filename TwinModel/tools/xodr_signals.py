"""Signal landmarks of an OpenDRIVE file as the JSON ``ue/place_traffic_lights.py`` /
``ue/place_traffic_signs.py`` consume -- computed offline with the CARLA client library (no
server needed):

    python tools/xodr_signals.py out/v9_eixample/eixample.xodr out/v9_eixample/ue/tl_signals.json
    python tools/xodr_signals.py <xodr> <out.json> --types 205 206 274     # stop / yield / speed

Per signal: id, type, subtype, road id, s / t, orientation, country, x / y / z (m, CARLA frame)
and yaw (deg) of ``Landmark.transform`` -- the same pose the runtime would spawn its own actor
at. Default type filter: traffic lights (1000001).
"""
import argparse
import json
import sys

import carla


def main(xodr_path: str, out_path: str, kinds=("1000001",)) -> int:
    with open(xodr_path) as f:
        cmap = carla.Map("xodr_signals", f.read())
    out = []
    for lm in cmap.get_all_landmarks():
        if kinds and lm.type not in kinds:
            continue
        t = lm.transform
        out.append({"id": lm.id, "type": lm.type, "subtype": lm.sub_type, "name": lm.name, "country": lm.country,
                    "road_id": lm.road_id, "s": lm.s, "t": lm.t,
                    "orientation": str(lm.orientation), "z_offset": lm.z_offset, "h_offset": lm.h_offset,
                    "x": t.location.x, "y": t.location.y, "z": t.location.z, "yaw": t.rotation.yaw,
                    "validities": [list(v) for v in lm.get_lane_validities()]})
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{len(out)} signals -> {out_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("xodr")
    ap.add_argument("out")
    ap.add_argument("--types", nargs="*", default=["1000001"], help="signal types to keep (empty = all)")
    a = ap.parse_args()
    sys.exit(main(a.xodr, a.out, tuple(a.types)))
