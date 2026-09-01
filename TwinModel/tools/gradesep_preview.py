"""Oblique 3D view of a twin's grade separation: surfaces coloured by OSM layer (a deck red,
the ground grey, a tunnel blue, with its ceiling as a translucent roof), with the tightest
crossing in the window annotated with its z gap — deck over street, or street over tunnel.

    PYTHONPATH=. python tools/gradesep_preview.py TWIN OUT.png CX CY HALF ["title"]

CX / CY / HALF are model metres: the square window to draw. See ``tools/tunnel_profile.py``
for the side view of a tunnel (z along s against the ground above it).
"""
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from shapely.geometry import box
from twinmodel.model import TwinModel, road_osm_layer

twin, out, cx, cy, half = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
m = TwinModel.load(twin)
win = box(cx - half, cy - half, cx + half, cy + half)

fig = plt.figure(figsize=(13, 7))
ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
COL = {0: "#8d8d8d", 1: "#c2483a", -1: "#3a6ec2", -2: "#3a6ec2"}
for s in m.surfaces:
    if s.kind not in ("drivable", "sidewalk", "crossing", "tunnel_ceiling"):
        continue
    g = s.geometry.intersection(win)
    if g.is_empty:
        continue
    lay = int(s.tags.get("layer") or 0)
    for p in getattr(g, "geoms", [g]):
        if p.geom_type != "Polygon":
            continue
        ring = p.exterior.segmentize(3.0)
        xy = np.asarray(ring.coords)[:, :2]
        if len(xy) < 3:
            continue
        z = np.asarray(m.sample_z(xy[:, 0], xy[:, 1], layer=lay)) + s.z_offset
        c = COL.get(lay, "#8d8d8d")
        ax.add_collection3d(Poly3DCollection(
            [np.column_stack([xy, z])], facecolor=c, edgecolor="none",
            alpha=0.25 if s.kind == "tunnel_ceiling" else (0.55 if s.kind != "drivable" else 0.92),
            zsort="min"))

for r in m.roads:
    if r.junction_id is not None:
        continue
    if not win.intersects(r.reference_line):
        continue
    c = np.asarray(r.reference_line.segmentize(5.0).coords)
    lay = road_osm_layer(r)
    ax.plot(c[:, 0], c[:, 1], c[:, 2] + 0.15, lw=1.6 if lay else 0.8,
            color="#000000" if lay == 0 else "#ffd400", zorder=5)
    if lay:
        mid = c[len(c) // 2]
        ax.text(mid[0], mid[1], mid[2] + 1.5, r.id, color="#ffd400", fontsize=7, zorder=6)

# the tightest crossing in the window: a sample of a higher layer right above one of a lower
# layer (deck over street, or street over tunnel), same pairing as validate.grade_separation
up = [(np.asarray(r.reference_line.segmentize(2.0).coords), road_osm_layer(r), r.id)
      for r in m.roads if r.junction_id is None and win.intersects(r.reference_line)]
layers_here = sorted({l for _, l, _ in up})
# roads that meet (linked, or arms of one junction) are not crossings: a portal / abutment
adjacent = set()
for r in m.roads:
    for link in (r.predecessor, r.successor):
        if link is not None and link.element == "road":
            adjacent.add((r.id, link.id)); adjacent.add((link.id, r.id))
for j in m.junctions:
    arms = {c.incoming_road for c in j.connections}
    arms |= {m.road(c.connecting_road).successor.id for c in j.connections
             if m.road(c.connecting_road).successor is not None}
    for a in arms:
        for b in arms:
            adjacent.add((a, b))
best = None
if len(layers_here) > 1:
    from scipy.spatial import cKDTree
    for lay in layers_here[1:]:
        hi = np.concatenate([c for c, l, _ in up if l == lay])
        hi_id = np.concatenate([[i] * len(c) for c, l, i in up if l == lay])
        lo = np.concatenate([c for c, l, _ in up if l < lay])
        lo_id = np.concatenate([[i] * len(c) for c, l, i in up if l < lay])
        d, k = cKDTree(lo[:, :2]).query(hi[:, :2], k=1)
        ok = d <= 4.0
        for a, b in zip(np.flatnonzero(ok), k[ok]):
            if (hi_id[a], lo_id[b]) in adjacent:
                continue
            gap = float(hi[a, 2] - lo[b, 2])
            if best is None or gap < best[0]:
                best = (gap, hi[a], lo[b])
if best is not None:
    g, a, b = best
    ax.plot([a[0], a[0]], [a[1], a[1]], [b[2], a[2]], color="#0b7a0b", lw=2.5, zorder=9)
    ax.text(a[0], a[1], (a[2] + b[2]) / 2, f"  {g:.2f} m", color="#0b7a0b",
            fontsize=11, weight="bold", zorder=10)

ax.set_xlim(cx - half, cx + half)
ax.set_ylim(cy - half, cy + half)
zs = np.concatenate([c[:, 2] for c, _, _ in up]) if up else np.zeros(1)
ax.set_zlim(float(zs.min()) - 2.0, float(zs.max()) + 8.0)
ax.set_box_aspect((1, 1, 0.85))
ax.view_init(elev=19, azim=-62)
ax.set_xlabel("x (m, east)"); ax.set_ylabel("y (m, north)"); ax.set_zlabel("z (m)")
ax.set_title(sys.argv[6] if len(sys.argv) > 6 else "grade separation")
handles = [plt.Line2D([], [], color=COL.get(l, "#8d8d8d"), lw=8,
                      label=f"layer {l} ({'deck' if l > 0 else ('tunnel' if l < 0 else 'surface streets')})")
           for l in sorted(layers_here, reverse=True)]
ax.legend(handles=handles, loc="upper left", fontsize=9)
fig.tight_layout()
fig.savefig(out, dpi=110)
print("wrote", out)
