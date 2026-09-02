"""Traffic-light landmarks of an OpenDRIVE file as the JSON ``ue/place_traffic_lights.py``
consumes -- computed offline with the CARLA client library (no server needed):

    python tools/xodr_signals.py out/v9_eixample/eixample.xodr out/v9_eixample/ue/tl_signals.json

Per signal of type 1000001: id, road id, s / t, orientation, x / y / z (m, CARLA frame) and yaw
(deg) of ``Landmark.transform`` -- the same pose the runtime would spawn its own light at.
"""
import json
import sys

import carla


def main(xodr_path: str, out_path: str, kinds=("1000001",)) -> int:
    with open(xodr_path) as f:
        cmap = carla.Map("xodr_signals", f.read())
    out = []
    for lm in cmap.get_all_landmarks():
        if lm.type not in kinds:
            continue
        t = lm.transform
        out.append({"id": lm.id, "type": lm.type, "road_id": lm.road_id, "s": lm.s, "t": lm.t,
                    "orientation": str(lm.orientation), "z_offset": lm.z_offset, "h_offset": lm.h_offset,
                    "x": t.location.x, "y": t.location.y, "z": t.location.z, "yaw": t.rotation.yaw,
                    "validities": [list(v) for v in lm.get_lane_validities()]})
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{len(out)} signals -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
