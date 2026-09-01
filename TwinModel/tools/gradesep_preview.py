"""Oblique 3D view of a twin's grade separation: surfaces coloured by OSM layer, with the
tightest crossing in the window annotated with its z gap.

    PYTHONPATH=. python tools/gradesep_preview.py TWIN OUT.png CX CY HALF ["title"]

CX / CY / HALF are model metres: the square window to draw.
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
COL = {0: "#8d8d8d", 1: "#c2483a", -1: "#3a6ec2"}
for s in m.surfaces:
    if s.kind not in ("drivable", "sidewalk", "crossing"):
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
        z = np.asarray(m.sample_z(xy[:, 0], xy[:, 1], layer=lay))
        c = COL.get(lay, "#8d8d8d")
        ax.add_collection3d(Poly3DCollection(
            [np.column_stack([xy, z])], facecolor=c, edgecolor="none",
            alpha=0.55 if s.kind != "drivable" else 0.92, zsort="min"))

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

# the tightest crossing in the window: a layer-1 sample right above a layer-0 one
up = [(np.asarray(r.reference_line.segmentize(2.0).coords), road_osm_layer(r))
      for r in m.roads if r.junction_id is None]
hi = np.concatenate([c for c, l in up if l > 0]) if any(l > 0 for _, l in up) else None
lo = np.concatenate([c for c, l in up if l <= 0])
if hi is not None:
    from scipy.spatial import cKDTree
    d, k = cKDTree(lo[:, :2]).query(hi[:, :2], k=1)
    ok = d <= 4.0
    if ok.any():
        gap = hi[ok][:, 2] - lo[k[ok]][:, 2]
        j = int(np.argmin(gap))
        a, b = hi[ok][j], lo[k[ok]][j]
        ax.plot([a[0], a[0]], [a[1], a[1]], [b[2], a[2]], color="#0b7a0b", lw=2.5, zorder=9)
        ax.text(a[0], a[1], (a[2] + b[2]) / 2, f"  {gap[j]:.2f} m", color="#0b7a0b",
                fontsize=11, weight="bold", zorder=10)

ax.set_xlim(cx - half, cx + half)
ax.set_ylim(cy - half, cy + half)
ax.set_zlim(0, 14)
ax.set_box_aspect((1, 1, 0.85))
ax.view_init(elev=19, azim=-62)
ax.set_xlabel("x (m, east)"); ax.set_ylabel("y (m, north)"); ax.set_zlabel("z (m)")
ax.set_title(sys.argv[6] if len(sys.argv) > 6 else "grade separation")
handles = [plt.Line2D([], [], color=COL[1], lw=8, label="layer 1 (I-80 viaduct + ramps)"),
           plt.Line2D([], [], color=COL[0], lw=8, label="layer 0 (surface streets)")]
ax.legend(handles=handles, loc="upper left", fontsize=9)
fig.tight_layout()
fig.savefig(out, dpi=110)
print("wrote", out)
