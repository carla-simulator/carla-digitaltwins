"""Road datum: the z reference every surface vertex is placed on.

The OpenDRIVE carries a smoothed elevation profile on each road's reference line
(``cli.apply_elevation``) and every lane point takes the reference-line z at its ``s`` (flat
cross-sections). The mesh must sit on *that* profile, not on the raw DEM, or the two exports
disagree wherever the DTM dips under a courtyard or a wide sidewalk.

``RoadDatum`` indexes all reference lines (with z, densified to 1 m) and answers ``z(x, y)``
with the z of the reference-line point that *covers* the query: candidates within
``2 * max_dist`` are ranked by their distance normalised by the road's cross-section
half-width on that side (all lanes incl. sidewalks, + ``reach_pad``), so a lane centre 8 m
right of a one-way street's left-edge reference line still takes its own road, not the
neighbouring lateral's. z is interpolated linearly along the winning road's segment.

Points far from every road (``> max_dist``) blend linearly into the DEM between ``max_dist``
and ``2 * max_dist`` so far-field ground still follows the terrain; without a DEM the road
z is used everywhere.

``cross_slope`` is a hook for a crown (2 % = 0.02): z drops by ``cross_slope * distance``
away from the reference line (capped at ``cross_slope_cap`` metres). Default 0.

``harmonize_junction_z`` makes the road z consistent *inside* junctions: connecting roads
that cross each other cannot both be matched by one surface unless all of a junction's
contacts share one plane. It fits a plane through the contact points of each junction, pulls
the incoming/outgoing road ends onto it (blended over ``blend_m``) and sets every connecting
road's z from that plane. ``cli.apply_elevation`` calls it.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LineString
from shapely.strtree import STRtree

from . import profiles
from .model import Elevation, Road, TwinModel, road_osm_layer

log = logging.getLogger("twinmodel.datum")

SEGMENT_M = 1.0
REACH_PAD_M = 1.0
K_NEIGHBOURS = 64  # far-field fallback only
# regional (twinmodel.profiles, read at call time): P.elevation.datum_max_dist_m,
# P.elevation.junction_blend_m, P.elevation.connecting_blend_m


def _densify(xyz: np.ndarray, step: float) -> np.ndarray:
    """Insert vertices so that no segment is longer than ``step`` (z interpolated linearly)."""
    out = [xyz[0]]
    for p, q in zip(xyz[:-1], xyz[1:]):
        d = float(np.hypot(q[0] - p[0], q[1] - p[1]))
        n = max(1, int(np.ceil(d / step)))
        for k in range(1, n + 1):
            out.append(p + (q - p) * (k / n))
    return np.asarray(out, dtype=np.float64)


def _side_widths(road: Road) -> tuple[float, float]:
    """Total cross-section half-widths (left, right): every lane of every type."""
    wl = sum(l.width for l in road.lanes if l.id > 0)
    wr = sum(l.width for l in road.lanes if l.id < 0)
    return float(wl), float(wr)


class RoadDatum:
    """Reference-line z over model space (see module docstring)."""

    def __init__(self, roads: Iterable[Road], elevation: Optional[Elevation] = None,
                 max_dist: Optional[float] = None, segment: float = SEGMENT_M,
                 reach_pad: float = REACH_PAD_M, k: int = K_NEIGHBOURS,
                 cross_slope: float = 0.0, cross_slope_cap: float = 10.0):
        """``max_dist`` defaults to the active profile's ``elevation.datum_max_dist_m``."""
        if max_dist is None:
            max_dist = profiles.get().elevation.datum_max_dist_m
        self.elevation = elevation
        self.max_dist = float(max_dist)
        self.reach_pad = float(reach_pad)
        self.k = int(k)
        self.cross_slope = float(cross_slope)
        self.cross_slope_cap = float(cross_slope_cap)
        lines, covers, s_arrays, z_arrays, wl, wr = [], [], [], [], [], []
        pts, zs, rid = [], [], []
        road_layers: list[int] = []
        for r in roads:
            c = np.asarray(r.reference_line.coords, dtype=np.float64)
            if len(c) < 2:
                continue
            if c.shape[1] < 3:
                c = np.column_stack([c, np.zeros(len(c))])
            seg = np.hypot(*np.diff(c[:, :2], axis=0).T)
            if seg.sum() <= 0:
                continue
            i = len(lines)
            l, rr = _side_widths(r)
            line = LineString(c[:, :2])
            lines.append(line)
            # coverage: symmetric buffer with flat caps (no reach past the road's ends); the
            # side-specific reach is applied when ranking
            covers.append(line.buffer(max(l, rr) + self.reach_pad, cap_style="flat"))
            s_arrays.append(np.concatenate([[0.0], np.cumsum(seg)]))
            z_arrays.append(c[:, 2])
            wl.append(l + self.reach_pad)
            wr.append(rr + self.reach_pad)
            road_layers.append(road_osm_layer(r))
            dense = _densify(c[:, :3], segment)
            pts.append(dense[:, :2])
            zs.append(dense[:, 2])
            rid.append(np.full(len(dense), i, dtype=np.int64))
        self.n_roads = len(lines)
        self.layers = np.asarray(road_layers, dtype=np.int64)
        if not lines:
            self.tree = None
            self.cover_tree = None
            return
        self.lines = np.asarray(lines, dtype=object)
        self.cover_tree = STRtree(covers)
        self.s_arrays, self.z_arrays = s_arrays, z_arrays
        self.wl, self.wr = np.asarray(wl), np.asarray(wr)
        self.xy = np.concatenate(pts)
        self.zs = np.concatenate(zs)
        self.rid = np.concatenate(rid)
        self.tree = cKDTree(self.xy)
        log.debug("road datum: %d roads, %d samples, max_dist %.0f m", self.n_roads, len(self.xy),
                  self.max_dist)

    @property
    def empty(self) -> bool:
        return self.tree is None

    # -- core ----------------------------------------------------------------------------
    def _project(self, q: np.ndarray, i: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Project query points ``q`` onto the two 1 m sample segments adjacent to sample ``i``
        (same road only); returns (z, distance) of the closer one. Far-field fallback."""
        n = len(self.xy)
        best_z = self.zs[i].copy()
        best_d = np.hypot(q[:, 0] - self.xy[i, 0], q[:, 1] - self.xy[i, 1])
        for j in (i - 1, i + 1):
            ok = (j >= 0) & (j < n)
            jj = np.clip(j, 0, n - 1)
            ok &= self.rid[jj] == self.rid[i]
            a, b = self.xy[i], self.xy[jj]
            ab = b - a
            ab2 = np.einsum("ij,ij->i", ab, ab)
            t = np.einsum("ij,ij->i", q - a, ab) / np.where(ab2 > 0, ab2, 1.0)
            t = np.clip(np.where(ab2 > 0, t, 0.0), 0.0, 1.0)
            foot = a + ab * t[:, None]
            d = np.hypot(q[:, 0] - foot[:, 0], q[:, 1] - foot[:, 1])
            z = self.zs[i] + (self.zs[jj] - self.zs[i]) * t
            better = ok & (d < best_d)
            best_d = np.where(better, d, best_d)
            best_z = np.where(better, z, best_z)
        return best_z, best_d

    def nearest(self, x, y, layer: Optional[int] = None) -> tuple[np.ndarray, np.ndarray]:
        """(road z at the covering reference-line point, distance to it) for each xy.

        Candidates are the roads whose coverage polygon contains the point; they are ranked
        by lateral distance over the road's reach on that side (own road wins inside its own
        cross-section) and z is interpolated along the winning road's reference line. Points
        outside every coverage polygon take the nearest 1 m sample of any road.

        ``layer``: restrict the candidates to roads on that OSM layer (grade separation — the
        deck of an overpass and the road under it cover the same xy). Points that no road of
        that layer covers fall back to the nearest sample of *any* road, as before."""
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))
        n = len(x)
        if self.tree is None:
            return np.zeros(n), np.full(n, np.inf)
        q = np.column_stack([x, y])
        pts = shapely.points(x, y)
        z_out = np.full(n, np.nan)
        d_out = np.full(n, np.inf)
        pi, ri = self.cover_tree.query(pts, predicate="within")
        if layer is not None and len(pi):
            keep = self.layers[ri] == int(layer)
            pi, ri = pi[keep], ri[keep]
        if len(pi):
            lines = self.lines[ri]
            p_sel = pts[pi]
            s = shapely.line_locate_point(lines, p_sel)
            foot = shapely.line_interpolate_point(lines, s)
            ahead = shapely.line_interpolate_point(lines, s + 0.1)
            behind = shapely.line_interpolate_point(lines, s - 0.1)
            tx = shapely.get_x(ahead) - shapely.get_x(behind)
            ty = shapely.get_y(ahead) - shapely.get_y(behind)
            dx = x[pi] - shapely.get_x(foot)
            dy = y[pi] - shapely.get_y(foot)
            dist = np.hypot(dx, dy)
            left = (tx * dy - ty * dx) >= 0
            reach = np.where(left, self.wl[ri], self.wr[ri])
            nd = dist / reach
            z = np.empty(len(pi))
            for road in np.unique(ri):
                sel = ri == road
                z[sel] = np.interp(s[sel], self.s_arrays[road], self.z_arrays[road])
            order = np.lexsort((nd, pi))
            first = np.concatenate([[True], pi[order][1:] != pi[order][:-1]])
            win = order[first]
            z_out[pi[win]] = z[win]
            d_out[pi[win]] = dist[win]
        miss = np.isnan(z_out)
        if miss.any():
            # nothing on ``layer`` covers these points: fall back to the nearest sample of any
            # road, as without a layer. Restricting the fallback too would drag a point beside
            # a ground road onto a deck 30 m away just because the deck is the only layer-1 road.
            _, i0 = self.tree.query(q[miss], k=1)
            z0, d0 = self._project(q[miss], np.atleast_1d(i0))
            z_out[miss] = z0
            d_out[miss] = d0
        return z_out, d_out

    def z(self, x, y, layer: Optional[int] = None):
        """Datum z at xy (scalar in -> float, array in -> array). ``layer``: see
        :meth:`nearest` — only roads on that OSM layer are considered."""
        scalar = np.ndim(x) == 0
        xa = np.atleast_1d(np.asarray(x, dtype=np.float64))
        ya = np.atleast_1d(np.asarray(y, dtype=np.float64))
        if self.tree is None:
            z = (np.asarray(self.elevation.sample(xa, ya), dtype=np.float64)
                 if self.elevation is not None else np.zeros(xa.shape))
            return float(z[0]) if scalar else z
        z, dist = self.nearest(xa, ya, layer=layer)
        if self.cross_slope:
            z = z - self.cross_slope * np.minimum(dist, self.cross_slope_cap)
        if self.elevation is not None:
            far = dist > self.max_dist
            if far.any():
                w = np.clip((dist[far] - self.max_dist) / self.max_dist, 0.0, 1.0)
                zd = np.asarray(self.elevation.sample(xa[far], ya[far]), dtype=np.float64)
                z = z.copy()
                z[far] = (1.0 - w) * z[far] + w * zd
        return float(z[0]) if scalar else z

def roads_have_z(roads: Iterable[Road]) -> bool:
    """True when at least one reference line carries a non-zero z."""
    for r in roads:
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        if c.shape[1] >= 3 and np.any(c[:, 2] != 0.0):
            return True
    return False


# --------------------------------------------------------------------------- junction planes

def _with_z(line: LineString, z: np.ndarray) -> LineString:
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    return LineString(np.column_stack([xy, np.asarray(z, dtype=np.float64)]))


def _fit_plane(P: np.ndarray) -> np.ndarray:
    """Least-squares z = a x + b y + c through points (n, 3); minimum-norm gradient when the
    points are collinear / a single point (lstsq handles the rank deficiency)."""
    A = np.column_stack([P[:, 0] - P[:, 0].mean(), P[:, 1] - P[:, 1].mean(), np.ones(len(P))])
    coef, *_ = np.linalg.lstsq(A, P[:, 2], rcond=None)
    a, b = coef[0], coef[1]
    c = coef[2] - a * P[:, 0].mean() - b * P[:, 1].mean()
    return np.array([a, b, c])


def harmonize_junction_z(model: TwinModel, blend_m: Optional[float] = None,
                         conn_blend_m: Optional[float] = None) -> dict[str, Any]:
    """Pull every junction's contact points onto one plane per junction (fitted through the
    contacts of its incoming/outgoing roads), blending each arm's profile over ``blend_m``
    (default ``profile.elevation.junction_blend_m``) from the contact, and set
    connecting-road z from that plane (ends pinned to the linked lanes' z, blended over
    ``conn_blend_m``, default ``profile.elevation.connecting_blend_m``). Mutates the
    reference lines; returns stats (max/rms contact adjustment, junctions touched)."""
    E = profiles.get().elevation
    if blend_m is None:
        blend_m = E.junction_blend_m
    if conn_blend_m is None:
        conn_blend_m = E.connecting_blend_m
    roads = {r.id: r for r in model.roads}
    contacts: dict[str, list[tuple[str, bool, np.ndarray]]] = {}  # jid -> (road, at_end, xyz)
    for r in model.roads:
        if r.junction_id is not None:
            continue
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        if len(c) < 2 or c.shape[1] < 3:
            continue
        if r.successor is not None and r.successor.element == "junction":
            contacts.setdefault(r.successor.id, []).append((r.id, True, c[-1]))
        if r.predecessor is not None and r.predecessor.element == "junction":
            contacts.setdefault(r.predecessor.id, []).append((r.id, False, c[0]))
    planes: dict[str, np.ndarray] = {}
    adjustments: list[float] = []
    deltas: dict[str, list[float]] = {}  # road id -> [start delta, end delta]
    for jid, items in contacts.items():
        P = np.array([xyz for _, _, xyz in items])
        plane = _fit_plane(P)
        planes[jid] = plane
        target = plane[0] * P[:, 0] + plane[1] * P[:, 1] + plane[2]
        for (rid, at_end, xyz), zt in zip(items, target):
            dz = float(zt - xyz[2])
            adjustments.append(dz)
            d = deltas.setdefault(rid, [0.0, 0.0])
            d[1 if at_end else 0] = dz
    # blend the arms
    for rid, (d0, d1) in deltas.items():
        r = roads[rid]
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        seg = np.hypot(*np.diff(c[:, :2], axis=0).T)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(s[-1]) if len(s) else 0.0
        if L <= 0:
            continue
        if L >= 2.0 * blend_m:
            w0 = np.clip(1.0 - s / blend_m, 0.0, 1.0)
            w1 = np.clip(1.0 - (L - s) / blend_m, 0.0, 1.0)
        else:
            w0 = 1.0 - s / L
            w1 = s / L
        r.reference_line = _with_z(r.reference_line, c[:, 2] + d0 * w0 + d1 * w1)
    # connecting roads: on the plane in the interior (so crossing roads agree), but starting
    # and ending at the linked road's contact z -- lanes have flat cross-sections, so a lane
    # 13 m from the arm's reference line sits at the reference z, not at the plane z of its
    # own xy; blend that lateral-offset delta out over the first/last conn_blend_m
    n_conn = 0
    conn_blend_m = min(blend_m, conn_blend_m)
    for r in model.roads:
        if r.junction_id is None or r.junction_id not in planes:
            continue
        a, b, cc = planes[r.junction_id]
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        z = a * c[:, 0] + b * c[:, 1] + cc
        seg = np.hypot(*np.diff(c[:, :2], axis=0).T)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(s[-1]) if len(s) else 0.0
        d0 = d1 = 0.0
        for link, k in ((r.predecessor, 0), (r.successor, -1)):
            if link is None or link.element != "road" or link.id not in roads:
                continue
            lc = np.asarray(roads[link.id].reference_line.coords, dtype=np.float64)
            if lc.shape[1] < 3:
                continue
            z_link = float(lc[0, 2] if link.contact == "start" else lc[-1, 2])
            delta = z_link - float(z[k])
            if k == 0:
                d0 = delta
            else:
                d1 = delta
        if L > 0 and (d0 or d1):
            bl = min(conn_blend_m, 0.5 * L)
            w0 = np.clip(1.0 - s / bl, 0.0, 1.0)
            w1 = np.clip(1.0 - (L - s) / bl, 0.0, 1.0)
            z = z + d0 * w0 + d1 * w1
        r.reference_line = _with_z(r.reference_line, z)
        n_conn += 1
    adj = np.abs(np.asarray(adjustments)) if adjustments else np.zeros(1)
    stats = {
        "junctions": len(planes), "contacts": len(adjustments), "connecting_roads": n_conn,
        "blend_m": blend_m,
        "contact_adjust_max_m": float(adj.max()),
        "contact_adjust_rms_m": float(np.sqrt((adj ** 2).mean())),
    }
    log.info("junction planes: %d junctions, %d contacts moved (rms %.3f m, max %.3f m), "
             "%d connecting roads on planes", stats["junctions"], stats["contacts"],
             stats["contact_adjust_rms_m"], stats["contact_adjust_max_m"], n_conn)
    return stats
