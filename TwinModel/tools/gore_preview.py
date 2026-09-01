"""Zoomed top-down previews of every ramp gore of a ``twinmodel build`` output.

    python -m tools.gore_preview out/v6_us101 us101_mathilda us_suburban [HALF_M]

One PNG per gore (merge tapers, diverge nose junctions, gores kept as junctions):
``<name>_gore_<id>_<kind>.png`` next to the twin, plus a printed table of the gores.
"""
from __future__ import annotations

import sys
from pathlib import Path

from twinmodel import profiles
from twinmodel.export.mesh import export_preview_png
from twinmodel.model import TwinModel


def gores(m: TwinModel) -> list[tuple[str, str, tuple[float, float], str]]:
    """[(gore id, kind, centre xy, description)]"""
    out = []
    seen = set()
    roads = {r.id: r for r in m.roads}
    for r in m.roads:
        if r.tags.get("gore_model") != "taper":
            continue
        gid = r.tags.get("gore")
        if gid in seen:
            continue
        seen.add(gid)
        kind = r.tags["gore_kind"]
        main = roads[r.tags["gore_mainline"]]
        c = r.reference_line.coords[-1] if kind == "merge" else r.reference_line.coords[0]
        aux = [l for l in main.lanes if l.tags.get("aux")]
        desc = (f"{kind}: ramp {r.id} -> {main.id}" if kind == "merge" else f"{kind}: {main.id} -> ramp {r.id}")
        if aux:
            desc += "; aux " + ", ".join(f"{l.tags['aux']} s=[{float(l.tags['aux_s0']):.0f},{float(l.tags['aux_s1']):.0f}]"
                                        + (f" taper [{float(l.tags['taper_s0']):.0f},{float(l.tags['taper_s1']):.0f}]"
                                           if l.tags.get("taper_s0") is not None else "") for l in aux)
        out.append((gid, kind, (float(c[0]), float(c[1])), desc))
    for j in m.junctions:
        if j.tags.get("kind") == "gore" and j.id not in seen:
            seen.add(j.id)
            cx, cy = j.tags["centre"]
            out.append((j.id, "junction", (float(cx), float(cy)),
                        f"gore junction, {j.polygon.area:.0f} m2" if j.polygon is not None else "gore junction"))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        print(__doc__)
        return 2
    build_dir, name, profile = Path(argv[0]), argv[1], argv[2]
    half = float(argv[3]) if len(argv) > 3 else 90.0
    profiles.activate(profile)
    m = TwinModel.load(build_dir / f"{name}.twin")
    for gid, kind, (cx, cy), desc in gores(m):
        png = build_dir / f"{name}_gore_{gid}_{kind}.png"
        export_preview_png(m, png, window=(cx - half, cx + half, cy - half, cy + half),
                           title=f"{name} {gid} {desc}")
        print(f"{gid:>5} {kind:<9} ({cx:8.1f}, {cy:8.1f})  {desc}  -> {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
