"""Side view of a twin's tunnels: z along s of every tunnel chain against the DEM (the ground
above it) and the roads crossing over it, plus the approaches on both sides — the picture that
shows the cover, the ramps and the portal welds of ``cli.apply_tunnel_profiles``.

    PYTHONPATH=. python tools/tunnel_profile.py TWIN OUT.png ["title"]
"""
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import LineString, Point
from twinmodel import profiles
from twinmodel.cli import _chain_xy, _structure_chains, _vertex_s, tunnel_road_ids
from twinmodel.model import TwinModel, road_osm_layer

twin, out = sys.argv[1], sys.argv[2]
m = TwinModel.load(twin)
with profiles.use(m.metadata.get("profile", {}).get("name") or "us_urban"):
    E = profiles.get().elevation
roads = {r.id: r for r in m.roads}
ids = tunnel_road_ids(m)
chains = _structure_chains(roads, ids)
if not chains:
    sys.exit("no tunnel roads in this twin")

fig, axes = plt.subplots(len(chains), 1, figsize=(13, 4.2 * len(chains)), squeeze=False)
for ax, chain in zip(axes[:, 0], chains):
    # the chain plus its approach on either side, as one polyline (s = 0 at the head portal)
    def approach(r, end):
        link = r.predecessor if end == "start" else r.successor
        if link is None or link.element != "road" or link.id in ids or link.id not in roads:
            return None
        c = np.asarray(roads[link.id].reference_line.coords)
        return c if link.contact == "start" else c[::-1]
    head_r, head_e = chain[0]
    tail_r, tail_e = chain[-1]
    tail_end = "end" if tail_e == "start" else "start"
    xy = _chain_xy(chain)
    z = np.concatenate([(np.asarray(r.reference_line.coords)[:, 2] if e == "start"
                         else np.asarray(r.reference_line.coords)[::-1, 2])[(1 if i else 0):]
                        for i, (r, e) in enumerate(chain)])
    s = _vertex_s(xy)
    L = float(s[-1])
    a0 = approach(head_r, head_e)
    a1 = approach(tail_r, tail_end)
    if a0 is not None:
        sa = -_vertex_s(a0[:, :2])[::-1]
        ax.plot(sa, a0[::-1, 2], color="#444", lw=1.5, label="approach (layer 0)")
    if a1 is not None:
        sb = L + _vertex_s(a1[:, :2])
        ax.plot(sb, a1[:, 2], color="#444", lw=1.5)
    ax.plot(s, z, color="#3a6ec2", lw=2.5, label=f"tunnel road (layer {road_osm_layer(head_r)})")
    ax.plot(s, z + E.tunnel_height_m, color="#3a6ec2", lw=1.0, ls="--", label="tunnel ceiling")
    # the ground above: DEM along the alignment, extended along the approaches
    if m.elevation is not None:
        pts = [(sa[i], a0[::-1][i, 0], a0[::-1][i, 1]) for i in range(len(a0))] if a0 is not None else []
        pts += [(s[i], xy[i, 0], xy[i, 1]) for i in range(len(s))]
        pts += [(sb[i], a1[i, 0], a1[i, 1]) for i in range(len(a1))] if a1 is not None else []
        pts = np.asarray(pts)
        sd = np.linspace(pts[0, 0], pts[-1, 0], 400)
        px = np.interp(sd, pts[:, 0], pts[:, 1])
        py = np.interp(sd, pts[:, 0], pts[:, 2])
        zd = np.asarray(m.elevation.sample(px, py))
        ax.fill_between(sd, zd, zd.min() - 3, color="#c9b99a", alpha=0.35, lw=0)
        ax.plot(sd, zd, color="#8a6a3c", lw=1.2, label="ground (DEM)")
    # roads of a higher layer crossing over the chain, at their z
    line = LineString(xy)
    for r in m.roads:
        if r.junction_id is not None or r.id in ids or road_osm_layer(r) <= road_osm_layer(head_r):
            continue
        c = np.asarray(r.reference_line.segmentize(1.0).coords)
        d = np.asarray([line.distance(Point(p[:2])) for p in c])
        near = d <= max(r.width_left(), r.width_right()) + 1.0
        if near.any():
            sc = np.asarray([line.project(Point(p[:2])) for p in c[near]])
            ax.plot(sc, c[near, 2], ".", color="#c2483a", ms=3)
            ax.text(float(sc.mean()), float(c[near, 2].mean()) + 0.6, r.name or r.id,
                    fontsize=7, ha="center", color="#c2483a")
    ax.axvline(0, color="#3a6ec2", lw=0.8, ls=":")
    ax.axvline(L, color="#3a6ec2", lw=0.8, ls=":")
    ax.text(0, ax.get_ylim()[1], "portal", fontsize=8, color="#3a6ec2", va="top")
    ax.text(L, ax.get_ylim()[1], "portal", fontsize=8, color="#3a6ec2", va="top", ha="right")
    ax.set_xlabel("s along the tunnel [m]")
    ax.set_ylabel("z [m]")
    ax.set_title(f"{m.name}: tunnel chain {head_r.id} ({len(chain)} roads, {L:.0f} m), "
                 f"cover >= {E.min_clearance_m:.1f} m, ramps <= {E.tunnel_max_grade * 100:.0f} %")
    ax.grid(True, color="#00000022", lw=0.4)
    ax.legend(loc="lower right", fontsize=8)
if len(sys.argv) > 3:
    fig.suptitle(sys.argv[3])
fig.tight_layout()
fig.savefig(out, dpi=110)
print("wrote", out)
