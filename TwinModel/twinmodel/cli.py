"""``twinmodel`` command line: ``build`` (end-to-end pipeline) and ``validate`` (proxy).

    twinmodel build --bbox S W N E --name NAME --out DIR [--no-imagery] [--no-dem]
                    [--no-refine] [--fixture PATH] [--cache data] [--mask-method classical|sam|auto]
                    [--profile eu_dense|us_urban|us_suburban|auto]
    twinmodel validate <twin_dir> <xodr> [--out DIR] [--step 1.0]
    twinmodel compare BUILD_DIR NAME [--resolution 0.25] [--zoom 19]   (twinmodel.compare)

``build`` stages (each timed, everything recorded in ``model.metadata["build"]``):

0. Profile  ``--profile`` (default ``auto``: the bbox's country via
            ``ingest.osm.country_for_bbox`` + the OSM building coverage pick a
            :mod:`twinmodel.profiles` profile; unknown -> ``eu_dense``). Every stage below
            reads its regional constants from the active profile; ``metadata["profile"]``
            records the choice.
1. OSM      Overpass (cached) or ``--fixture`` -> ``parse_osm`` -> ``build_lanegraph``.
2. DEM      ``fetch_dem`` -> ``model.elevation``; z applied to every reference line with
            along-road smoothing (profile ``elevation.resample_m`` resample,
            ``elevation.smooth_window_m`` Savitzky-Golay window), connecting roads
            interpolated between their incoming/outgoing road ends, signal z set.
3. Imagery  ``fetch_ortho`` (kept for the preview and for refinement).
4. Surfaces ``build_surfaces``; then (unless ``--no-refine``) ``road_mask`` ->
            ``refine_drivable`` -> ``build_surfaces(refined_drivable=...)``; refinement is
            rejected (unrefined surfaces kept) when it drops ``lane_in_drivable`` < 0.98.
5. Exports  ``<name>.twin/``, ``<name>.xodr``, ``<name>.obj/.mtl``, previews (with/without
            ortho), one zoom per largest junction, DEM and mask quicklooks.
6. Validate ``validate(...)`` -> ``report.json`` (+ ``violations.geojson``); exit 1 when the
            xodr does not load in ``carla.Map`` or ``lane_in_drivable`` < 0.98.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import inspect
import json
import logging
import math
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from . import profiles
from .frame import LocalFrame
from .model import Elevation, Road, TwinModel, road_is_bridge, road_is_tunnel, road_osm_layer

log = logging.getLogger("twinmodel.cli")

DEFAULT_BBOX = (41.3905, 2.1630, 41.3945, 2.1690)  # Eixample, see DESIGN.md
LANE_IN_DRIVABLE_MIN = 0.98
JUNCTION_ZOOM_PAD_M = 25.0
PROFILE_CHOICES = tuple(sorted(profiles.PROFILES)) + ("auto",)
# regional (twinmodel.profiles): DEM resample step P.elevation.resample_m, smoothing window
# P.elevation.smooth_window_m, data-source preference P.sources


# --------------------------------------------------------------------------- elevation glue

def _smooth_z(z: np.ndarray, ds: float, window_m: Optional[float] = None) -> np.ndarray:
    """Savitzky-Golay (polyorder 1, i.e. a least-squares moving average) over a ``window_m``
    (default: profile ``elevation.smooth_window_m``) window; graceful for short lines."""
    if window_m is None:
        window_m = profiles.get().elevation.smooth_window_m
    n = len(z)
    if n < 3:
        return z.copy()
    win = int(round(window_m / ds))
    win = max(3, win | 1)  # odd, >= 3
    if win > n:
        win = n if n % 2 == 1 else n - 1
    if win < 3:
        return z.copy()
    try:
        from scipy.signal import savgol_filter
        # polyorder 1 == least-squares moving average: roads have negligible vertical
        # curvature over 10 m, and a quadratic over 5 samples would barely filter DEM noise
        return savgol_filter(z, win, polyorder=1, mode="interp")
    except Exception:  # pragma: no cover - scipy missing/odd input: moving average fallback
        k = np.ones(win) / win
        pad = win // 2
        zp = np.pad(z, pad, mode="edge")
        return np.convolve(zp, k, mode="valid")


def _vertex_s(xy: np.ndarray) -> np.ndarray:
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)])


def road_profile_from_dem(road: Road, elevation: Elevation,
                          mask: Optional[Any] = None,
                          hold: tuple[bool, bool] = (False, False)) -> tuple[np.ndarray, float]:
    """Smoothed z at the road's reference-line vertices; also the max |smoothed - raw DEM|.

    ``mask``: a geometry over which the DEM must not be believed — the footprint of the decks
    that pass *over* this road. A DTM keeps the ground under an overpass, but the abutments and
    the structure make the samples there meaningless (and a bridge left in the DTM would put a
    hill across the road); the samples inside the mask are replaced by a linear interpolation
    across the gap before smoothing.

    ``hold``: (start, end) — do not read the DEM past that end of the road; the half-window
    extension holds the end sample instead. The approach into a tunnel portal ends where the
    hill begins: sampled straight on, the DTM there is the ground *above* the tunnel, and the
    smoothing would lift the portal contact by metres."""
    E = profiles.get().elevation
    xy = np.asarray(road.reference_line.coords, dtype=np.float64)[:, :2]
    s_v = _vertex_s(xy)
    length = float(s_v[-1])
    line2d = LineString(xy)
    # sample every elevation.resample_m along the line, extended half a window past both ends
    # along the end tangents so the end vertices (junction contacts) get full-window smoothing
    ext = E.smooth_window_m / 2.0
    n_in = max(2, int(math.ceil(length / E.resample_m)) + 1)
    ds = length / (n_in - 1) if length > 0 else E.resample_m
    n_ext = int(math.ceil(ext / ds))
    s_dense = np.concatenate([-ds * np.arange(n_ext, 0, -1), np.linspace(0.0, length, n_in),
                              length + ds * np.arange(1, n_ext + 1)])
    inside = np.clip(s_dense, 0.0, length)
    pts = shapely.line_interpolate_point(line2d, inside)
    px = shapely.get_x(pts).astype(np.float64)
    py = shapely.get_y(pts).astype(np.float64)
    t0 = xy[1] - xy[0]
    t0 /= max(np.linalg.norm(t0), 1e-9)
    t1 = xy[-1] - xy[-2]
    t1 /= max(np.linalg.norm(t1), 1e-9)
    before = s_dense < 0
    after = s_dense > length
    px[before] += t0[0] * s_dense[before]
    py[before] += t0[1] * s_dense[before]
    px[after] += t1[0] * (s_dense[after] - length)
    py[after] += t1[1] * (s_dense[after] - length)
    z_raw = np.asarray(elevation.sample(px, py), dtype=np.float64)
    if hold[0] and before.any():
        z_raw[before] = z_raw[n_ext]
    if hold[1] and after.any():
        z_raw[after] = z_raw[n_ext + n_in - 1]
    if mask is not None and not mask.is_empty:
        # only the deck stretches that cross this road in its *interior* count: a deck whose
        # footprint reaches an end of the road is the same street continuing over the bridge
        # (a shared abutment), and its DEM there is the real approach level
        inter = line2d.intersection(mask)
        under = np.zeros(len(s_dense), dtype=bool)
        for g in getattr(inter, "geoms", [inter]):
            if g.geom_type != "LineString" or g.is_empty:
                continue
            s0 = line2d.project(Point(g.coords[0]))
            s1 = line2d.project(Point(g.coords[-1]))
            lo_s, hi_s = min(s0, s1), max(s0, s1)
            if lo_s <= 0.5 or hi_s >= length - 0.5:
                continue
            under |= (s_dense >= lo_s) & (s_dense <= hi_s)
        if under.any() and not under.all():
            good = ~under
            z_raw = z_raw.copy()
            z_raw[under] = np.interp(s_dense[under], s_dense[good], z_raw[good])
    z_smooth = _smooth_z(z_raw, ds, E.smooth_window_m)
    z_vertices = np.interp(s_v, s_dense, z_smooth)
    z_raw_vertices = np.asarray(elevation.sample(xy[:, 0], xy[:, 1]), dtype=np.float64)
    return z_vertices, float(np.max(np.abs(z_vertices - z_raw_vertices)))


def deck_road_ids(model: TwinModel) -> set[str]:
    """Roads whose z must not come from the DEM under them: a bridge deck (``bridge=*``) or any
    road stacked above the ground layer (``layer > 0``). A DTM has the structure removed, so a
    sample under either is bare earth — the street below, not the deck."""
    return {r.id for r in model.roads
            if r.junction_id is None and (road_is_bridge(r) or road_osm_layer(r) > 0)}


def tunnel_road_ids(model: TwinModel) -> set[str]:
    """Roads whose z must not come from the DEM *above* them: a tunnel (``tunnel=*``) or any
    road below the ground layer (``layer < 0``, an underpass is often tagged only with the
    layer). The DTM there is the hill / street the tunnel passes under."""
    return {r.id for r in model.roads if r.junction_id is None and road_is_tunnel(r)}


def _chain_adjacent(model: TwinModel, chain_ids: set[str]) -> set[str]:
    """Road ids that meet ``chain_ids`` — directly linked, or an arm of a junction the chain
    reaches. Their z is continuous with the deck at the abutment, so they are not "crossed"."""
    out = set(chain_ids)
    junctions: set[str] = set()
    for r in model.roads:
        for link in (r.predecessor, r.successor):
            if link is None:
                continue
            if link.element == "road" and (r.id in chain_ids or link.id in chain_ids):
                out.add(link.id)
                out.add(r.id)
            elif link.element == "junction" and r.id in chain_ids:
                junctions.add(link.id)
    if junctions:
        for r in model.roads:
            for link in (r.predecessor, r.successor):
                if link is not None and link.element == "junction" and link.id in junctions:
                    out.add(r.id)
    return out


def _crossing_samples(model: TwinModel, chain_xy: np.ndarray, half_width: float,
                      skip: set[str], other: Callable[[Road], bool]) -> list[tuple[float, float]]:
    """``[(s along the chain, z of the other road)]`` sampled every 2 m wherever a plain road
    for which ``other`` holds runs through the chain's footprint (its carriageway overlaps the
    chain's) without meeting it (``skip``: an abutment / portal is not a crossing). The other
    roads' z is already set."""
    line = LineString(chain_xy)
    deck = line.buffer(max(half_width, 2.0), cap_style="flat")
    out: list[tuple[float, float]] = []
    for r in model.roads:
        if r.junction_id is not None or r.id in skip or not other(r):
            continue
        c = np.asarray(r.reference_line.coords, dtype=np.float64)
        if c.shape[0] < 2 or c.shape[1] < 3:
            continue
        below = LineString(c[:, :2])
        w = max((r.width_left() + r.width_right()) / 2.0, 2.0)
        inter = below.buffer(w, cap_style="flat").intersection(deck)
        if inter.is_empty:
            continue
        # sample the road below where it runs under the deck
        seg = below.intersection(deck)
        pieces = [g for g in getattr(seg, "geoms", [seg])
                  if g.geom_type == "LineString" and not g.is_empty]
        if not pieces:
            pieces = [below]
        s_below = _vertex_s(c[:, :2])
        for g in pieces:
            gc = np.asarray(g.coords, dtype=np.float64)[:, :2]
            n = max(2, int(g.length / 2.0) + 1)
            pts = np.asarray([shapely.line_interpolate_point(g, t).coords[0]
                              for t in np.linspace(0.0, g.length, n)], dtype=np.float64)
            z_lo = np.interp([below.project(Point(p)) for p in pts], s_below, c[:, 2])
            s_up = np.asarray([line.project(Point(p)) for p in pts], dtype=np.float64)
            out.extend((float(s), float(z)) for s, z in zip(s_up, z_lo))
    return out


def _clearance_deficits(model: TwinModel, chain_xy: np.ndarray, chain_z: np.ndarray,
                        half_width: float, layer: int, skip: set[str],
                        clearance: float) -> list[tuple[float, float]]:
    """``[(s along the chain, how far the deck falls short of ``clearance``)]`` for every road
    the chain passes over: plain roads on a lower OSM layer whose carriageway overlaps the deck
    footprint and that do not meet the chain (an abutment is not a crossing).

    Both z's are already set: the roads below took their (deck-masked) DEM profile before the
    decks were resolved, and ``chain_z`` is the deck's straight line."""
    s_chain = _vertex_s(chain_xy)
    out: list[tuple[float, float]] = []
    for s, z_lo in _crossing_samples(model, chain_xy, half_width, skip,
                                     lambda r: road_osm_layer(r) < layer):
        deficit = z_lo + clearance - float(np.interp(s, s_chain, chain_z))
        if deficit > 0.0:
            out.append((s, deficit))
    return out


def _structure_chains(roads: dict[str, Road], ids: set[str]) -> list[list[tuple[Road, str]]]:
    """Group the roads in ``ids`` (decks, or tunnel roads) into chains of directly linked roads,
    each ``[(road, the end that faces the head of the chain), ...]`` in walking order. A
    structure is usually several roads (one per OSM way plus the width tapers ``lanegraph``
    splits off) and must be resolved as one."""
    def link_at(r: Road, end: str) -> tuple[Optional[str], Optional[str]]:
        link = r.predecessor if end == "start" else r.successor
        if link is not None and link.element == "road" and link.id in roads:
            return link.id, link.contact
        return None, None

    done: set[str] = set()
    chains: list[list[tuple[Road, str]]] = []
    for rid in sorted(ids):
        if rid in done:
            continue
        # walk to the head of the chain, then collect it in order
        cur, cur_end = roads[rid], "start"
        seen = {rid}
        while True:
            nb_id, contact = link_at(cur, cur_end)
            if nb_id is None or nb_id not in ids or nb_id in seen:
                break
            seen.add(nb_id)
            cur, cur_end = roads[nb_id], ("end" if contact == "start" else "start")
        chain: list[tuple[Road, str]] = []
        road, entry = cur, cur_end
        while True:
            chain.append((road, entry))
            done.add(road.id)
            nb_id, contact = link_at(road, "end" if entry == "start" else "start")
            if nb_id is None or nb_id not in ids or nb_id in {r.id for r, _ in chain}:
                break
            road, entry = roads[nb_id], contact
        chains.append(chain)
    return chains


def _chain_xy(chain: list[tuple[Road, str]]) -> np.ndarray:
    """The chain's reference line as one polyline, head to tail."""
    return np.concatenate([
        (np.asarray(r.reference_line.coords, dtype=np.float64)[:, :2]
         if entry == "start"
         else np.asarray(r.reference_line.coords, dtype=np.float64)[::-1, :2])[(1 if i else 0):]
        for i, (r, entry) in enumerate(chain)])


def apply_bridge_profiles(model: TwinModel, elevation: Elevation,
                          roads: dict[str, Road]) -> tuple[int, int]:
    """Give every bridge deck a straight profile between its two abutments; returns how many
    deck roads were set and how many chains had to be lifted for clearance. Call after the non-bridge roads have their DEM profile.

    A DTM (3DEP, ICGC MDT) has the deck removed, so sampling it along the deck drops the
    overpass into the trench of the road it crosses. A deck is usually several roads (one per
    OSM way, plus the width tapers ``lanegraph`` splits off), so the whole *chain* of linked
    bridge roads is resolved at once and shares one straight line: an abutment z comes from the
    non-bridge road linked at the end of the chain (its profile is already smoothed), else from
    the DEM at that chain end. Interpolating each piece on its own would pin the interior
    abutments to the DEM under the deck, which is exactly the trench.

    A chain end with no approach road is *free*: the deck leaves the bbox (SoMa's I-80 is
    elevated all the way across the tile) and there is no abutment anywhere in the data, so the
    DEM there is bare earth under the viaduct — street level — and the straight line would lay
    the deck on the road it flies over. A free end is therefore lifted until the deck clears
    every road it crosses by ``ElevationRules.min_clearance_m``: both ends free shifts the whole
    chain rigidly (its DEM-derived grade is kept), one end free pivots about the real abutment.
    """
    bridge_ids = deck_road_ids(model)

    def link_at(r: Road, end: str) -> tuple[Optional[str], Optional[str]]:
        link = r.predecessor if end == "start" else r.successor
        if link is not None and link.element == "road" and link.id in roads:
            return link.id, link.contact
        return None, None

    E = profiles.get().elevation
    window = E.bridge_abutment_m

    def anchor_z(r: Road, end: str) -> tuple[float, bool]:
        """``(z, anchored)`` of the abutment at the chain's outer ``end``. ``anchored`` is True
        only when a real approach road is linked there — otherwise the chain is clipped by the
        bbox and the z is a bare-earth guess that ``_lift_free_ends`` may raise.

        The DTM steps from the approach (deck level) down into the trench of the crossing road
        within a couple of cells, and OSM's ``bridge=yes`` extent rarely lands exactly on that
        step — so take the *highest* DEM sample within ``elevation.bridge_abutment_m`` of the
        end, walking outward along the approach, together with the linked approach road's own
        (already smoothed) contact z. Averaging or taking the end sample alone puts the deck
        one to two metres into the trench."""
        cands: list[float] = []
        anchored = False
        nb_id, contact = link_at(r, end)
        if nb_id is not None and nb_id not in bridge_ids:
            c = np.asarray(roads[nb_id].reference_line.coords, dtype=np.float64)
            if c.shape[1] >= 3 and np.any(c[:, 2] != 0.0):
                cands.append(float(c[0, 2] if contact == "start" else c[-1, 2]))
                anchored = True
        xy = np.asarray(r.reference_line.coords, dtype=np.float64)[:, :2]
        if end == "start":
            p, t = xy[0], xy[0] - xy[1]
        else:
            p, t = xy[-1], xy[-1] - xy[-2]
        n = np.linalg.norm(t)
        if n > 1e-9 and window > 0:
            t = t / n
            d = np.arange(0.0, window + 1e-9, 1.0)
            cands.append(float(np.max(np.asarray(
                elevation.sample(p[0] + t[0] * d, p[1] + t[1] * d), dtype=np.float64))))
        else:
            cands.append(float(np.asarray(elevation.sample(p[0], p[1]), dtype=np.float64)))
        return max(cands), anchored

    n = 0
    n_lifted = 0
    for chain in _structure_chains(roads, bridge_ids):
        z_head, head_anchored = anchor_z(chain[0][0], chain[0][1])
        z_tail, tail_anchored = anchor_z(chain[-1][0],
                                         "end" if chain[-1][1] == "start" else "start")
        lengths = [r.length for r, _ in chain]
        total = sum(lengths) or 1.0
        if not (head_anchored and tail_anchored):
            # the chain leaves the bbox: no abutment to interpolate from, so hold the clearance
            # over everything the deck flies over instead (see the docstring)
            chain_xy = _chain_xy(chain)
            s_chain = _vertex_s(chain_xy)
            span = float(s_chain[-1]) or 1.0
            chain_z = z_head + (z_tail - z_head) * (s_chain / span)
            half_w = max((r.width_left() + r.width_right()) / 2.0 for r, _ in chain) + 1.0
            layer = max(road_osm_layer(r) for r, _ in chain) or 1
            deficits = _clearance_deficits(
                model, chain_xy, chain_z, half_w, layer,
                _chain_adjacent(model, {r.id for r, _ in chain}), E.min_clearance_m)
            d_head = d_tail = 0.0
            for s, deficit in deficits:
                if head_anchored:
                    if s <= E.clearance_abutment_skip_m:
                        continue
                    d_tail = max(d_tail, deficit * span / s)
                elif tail_anchored:
                    if span - s <= E.clearance_abutment_skip_m:
                        continue
                    d_head = max(d_head, deficit * span / max(span - s, 1e-6))
                else:
                    d_head = d_tail = max(d_head, d_tail, deficit)
            d_head = min(d_head, E.max_deck_lift_m)
            d_tail = min(d_tail, E.max_deck_lift_m)
            if d_head > 0.0 or d_tail > 0.0:
                log.info("deck chain %s (%d roads, %.0f m, %s end%s free): lifted %+.2f / %+.2f m "
                         "to clear %d sample(s) of the roads below by %.1f m",
                         chain[0][0].id, len(chain), span,
                         "both" if not (head_anchored or tail_anchored)
                         else ("head" if not head_anchored else "tail"),
                         "s" if not (head_anchored or tail_anchored) else "",
                         d_head, d_tail, len(deficits), E.min_clearance_m)
                z_head += d_head
                z_tail += d_tail
                n_lifted += 1
        s0 = 0.0
        for (r, entry), L in zip(chain, lengths):
            za = z_head + (z_tail - z_head) * (s0 / total)
            zb = z_head + (z_tail - z_head) * ((s0 + L) / total)
            xy = np.asarray(r.reference_line.coords, dtype=np.float64)[:, :2]
            s_v = _vertex_s(xy)
            t = s_v / s_v[-1] if s_v[-1] > 0 else np.zeros_like(s_v)
            z = (za + (zb - za) * t) if entry == "start" else (zb + (za - zb) * t)
            r.reference_line = _with_z(r.reference_line, z)
            s0 += L
            n += 1
    return n, n_lifted


def _with_z(line: LineString, z: np.ndarray) -> LineString:
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    return LineString(np.column_stack([xy, np.asarray(z, dtype=np.float64)]))


def _densified_line(line: LineString, step: float) -> LineString:
    """``line`` (2D) with extra vertices so no segment is longer than ``step``; the original
    vertices are kept (a vertical alignment needs vertices where its grade changes)."""
    xy = np.asarray(line.coords, dtype=np.float64)[:, :2]
    s = _vertex_s(xy)
    if s[-1] <= 0.0:
        return LineString(xy)
    s_new = np.unique(np.concatenate([s, np.arange(step, s[-1], step)]))
    pts = shapely.line_interpolate_point(LineString(xy), s_new)
    xy_new = np.column_stack([shapely.get_x(pts), shapely.get_y(pts)])
    xy_new[0], xy_new[-1] = xy[0], xy[-1]
    return LineString(xy_new)


def apply_tunnel_profiles(model: TwinModel, elevation: Elevation,
                          roads: dict[str, Road]) -> tuple[int, int, float]:
    """Give every tunnel road (``tunnel_road_ids``) its vertical alignment; returns how many
    tunnel roads were set, how many chains had to be sunk for cover, and the deepest portal
    sink (metres the approach must be pulled down, see ``weld_deck_abutments``). Call after the
    surface roads have their DEM profile and the decks are resolved. The mirror image of
    ``apply_bridge_profiles``:

    * the DTM over a tunnel is the hill / street above it, so the tunnel road never samples it:
      the whole chain of linked tunnel roads runs *straight between its portals*, whose z is
      the contact z of the approach road linked there (already smoothed; a chain end at a
      junction takes the DEM there — the portal is at ground level in the junction);
    * wherever a plain road of a higher layer passes over the chain, the tunnel must sit
      ``ElevationRules.min_clearance_m`` below it (``_crossing_samples``): the profile is
      lowered to ``min(straight line, envelope)`` where the envelope rises from every cover
      requirement at ``tunnel_max_grade`` — a dip under the crossing with ramps at the maximum
      grade, and the portal itself only when the tunnel is too short for the ramp. The caller
      then welds the approach down into a trench;
    * a chain end without an approach (the bbox cut the tunnel) is *free*: its z is the DEM
      there minus the clearance, so a clipped tunnel stays sunk (the mirror of a clipped deck
      being lifted)."""
    tunnel_ids = tunnel_road_ids(model)
    if not tunnel_ids:
        return 0, 0, 0.0
    E = profiles.get().elevation
    depth = E.min_clearance_m

    def portal_z(r: Road, end: str) -> tuple[float, bool]:
        """``(z, anchored)`` of the portal at the chain's outer ``end``."""
        link = r.predecessor if end == "start" else r.successor
        if link is not None and link.element == "road" and link.id in roads \
                and link.id not in tunnel_ids:
            # the approach already has its (DEM) profile: its contact is the portal level
            return _contact_z(roads[link.id], link.contact), True
        xy = np.asarray(r.reference_line.coords, dtype=np.float64)
        p = xy[0] if end == "start" else xy[-1]
        z_dem = float(np.asarray(elevation.sample(p[0], p[1]), dtype=np.float64))
        if link is not None and link.element == "junction":
            return z_dem, True
        return z_dem - depth, False

    n = n_sunk = 0
    max_sink = 0.0
    for chain in _structure_chains(roads, tunnel_ids):
        z_head, head_anchored = portal_z(chain[0][0], chain[0][1])
        z_tail, tail_anchored = portal_z(chain[-1][0], "end" if chain[-1][1] == "start" else "start")
        chain_xy = _chain_xy(chain)
        s_chain = _vertex_s(chain_xy)
        span = float(s_chain[-1]) or 1.0
        layer = min(road_osm_layer(r) for r, _ in chain)
        layer = layer if layer < 0 else -1
        half_w = max((r.width_left() + r.width_right()) / 2.0 for r, _ in chain) + 1.0
        cover = _crossing_samples(model, chain_xy, half_w,
                                  _chain_adjacent(model, {r.id for r, _ in chain}),
                                  lambda r: road_osm_layer(r) > layer)
        # dense vertical alignment along the chain: the straight line between the portals,
        # lowered under every cover requirement with ramps at the maximum grade
        s_dense = np.unique(np.concatenate([s_chain, np.arange(0.0, span, 2.0), [span]]))
        z_line = z_head + (z_tail - z_head) * (s_dense / span)
        z_prof = z_line.copy()
        if cover:
            s_c = np.asarray([s for s, _ in cover])
            z_req = np.asarray([z - depth for _, z in cover])
            env = np.min(z_req[None, :] + E.tunnel_max_grade * np.abs(s_dense[:, None] - s_c[None, :]),
                         axis=1)
            z_prof = np.minimum(z_line, env)
            sunk = z_line - z_prof
            if sunk.max() > 0.01:
                n_sunk += 1
                sink_head = float(sunk[0]) if head_anchored else 0.0
                sink_tail = float(sunk[-1]) if tail_anchored else 0.0
                max_sink = max(max_sink, sink_head, sink_tail)
                log.info("tunnel chain %s (%d roads, %.0f m, %s): sunk up to %.2f m under %d "
                         "cover sample(s) for %.1f m of cover at <= %.0f %% grade; portals "
                         "%+.2f / %+.2f m", chain[0][0].id, len(chain), span,
                         "both portals in the data" if head_anchored and tail_anchored
                         else "clipped by the bbox", float(sunk.max()), len(cover), depth,
                         -sink_head, -sink_tail)
        s0 = 0.0
        for r, entry in chain:
            line2d = _densified_line(r.reference_line, 4.0)
            xy = np.asarray(line2d.coords, dtype=np.float64)
            s_v = _vertex_s(xy)
            s_on_chain = s0 + (s_v if entry == "start" else s_v[-1] - s_v)
            z = np.interp(s_on_chain, s_dense, z_prof)
            r.reference_line = LineString(np.column_stack([xy, z]))
            s0 += float(s_v[-1])
            n += 1
    return n, n_sunk, max_sink


def _contact_z(road: Road, contact: Optional[str]) -> float:
    c = road.reference_line.coords
    p = c[0] if contact == "start" else c[-1]
    return float(p[2]) if len(p) > 2 else 0.0


def _blend_from_contact(road: Road, contact: str, dz: float, budget: float,
                        step: float = 4.0) -> float:
    """Add ``dz`` to ``road``'s z at ``contact``, fading linearly to 0 after ``budget`` metres
    (the reference line is densified so the ramp has vertices). Returns the dz still owed at the
    far end when the road is shorter than ``budget`` — the caller carries it on to the next road.
    """
    c = np.asarray(road.reference_line.coords, dtype=np.float64)
    xy, z = c[:, :2], (c[:, 2] if c.shape[1] > 2 else np.zeros(len(c)))
    s = _vertex_s(xy)
    length = float(s[-1])
    if length <= 0.0:
        return 0.0
    span = min(budget, length)
    from_start = contact == "start"
    extra = np.arange(step, span, step)
    s_new = np.unique(np.concatenate([s, extra if from_start else length - extra]))
    line2d = LineString(xy)
    pts = shapely.line_interpolate_point(line2d, np.clip(s_new, 0.0, length))
    xy_new = np.column_stack([shapely.get_x(pts), shapely.get_y(pts)])
    xy_new[0], xy_new[-1] = xy[0], xy[-1]
    z_new = np.interp(s_new, s, z)
    d = s_new if from_start else (length - s_new)
    z_new += dz * np.clip(1.0 - d / budget, 0.0, 1.0)
    road.reference_line = LineString(np.column_stack([xy_new, z_new]))
    return dz * max(0.0, 1.0 - length / budget)


def weld_deck_abutments(model: TwinModel, roads: dict[str, Road],
                        deck_ids: set[str], blend_m: float, what: str = "abutment") -> int:
    """Make the road surface continuous where a deck meets its approach; returns how many
    abutments were welded.

    The two sides disagree by a metre or more otherwise: the deck's abutment z is the top of the
    DEM step beside it (``apply_bridge_profiles.anchor_z``), while the approach's own smoothed
    profile is dragged down by the half-window ``road_profile_from_dem`` extends *past* the road
    end — straight over the deck, i.e. into the trench of the road the deck flies over. The step
    that leaves is a real ledge in the exported surface: vehicles scrape it (``static.road``),
    and CARLA's runtime terrain raster, which hugs the lowest paved z per cell, reads the deck's
    height in the cells beside the approach and rises through it (``static.terrain``).

    The deck is the side to trust (its z came from ground the DTM actually shows), so the
    approach is pulled onto it and the offset faded out over ``blend_m``, continuing into the
    next road when the first is shorter than that. Junctions stop the walk: their plane is
    harmonized afterwards from the contacts this pass leaves behind.

    The same weld joins a tunnel portal to its approach (``deck_ids`` = the tunnel roads,
    ``what="portal"``): a portal that ``apply_tunnel_profiles`` had to sink pulls the approach
    down into a trench."""
    welded = 0
    for d in model.roads:
        if d.junction_id is not None or d.id not in deck_ids:
            continue
        for end, link in (("start", d.predecessor), ("end", d.successor)):
            if link is None or link.element != "road" or link.id in deck_ids:
                continue
            a = roads.get(link.id)
            if a is None or a.junction_id is not None or link.contact is None:
                continue
            dz = _contact_z(d, end) - _contact_z(a, link.contact)
            if abs(dz) < 0.02:
                continue
            log.info("%s %s/%s -> %s/%s: welded a %+.2f m step, blended over %.0f m",
                     what, d.id, end, a.id, link.contact, dz, blend_m)
            welded += 1
            cur, contact, rest, budget, guard = a, link.contact, dz, blend_m, 0
            while cur is not None and abs(rest) > 0.02 and budget > 0.5 and guard < 8:
                rest = _blend_from_contact(cur, contact, rest, budget)
                budget -= cur.length
                nxt = cur.successor if contact == "start" else cur.predecessor
                if (rest == 0.0 or nxt is None or nxt.element != "road"
                        or nxt.id in deck_ids or nxt.id not in roads):
                    break
                cur, contact = roads[nxt.id], (nxt.contact or "start")
                guard += 1
    return welded


def apply_elevation(model: TwinModel, elevation: Optional[Elevation] = None) -> dict[str, Any]:
    """Set z on every road reference line and signal from ``elevation`` (default:
    ``model.elevation``). Non-connecting roads take the smoothed DEM profile; connecting
    roads interpolate linearly between the z of the roads they link so junctions stay
    continuous. Returns the stats that ``build`` stores in ``metadata["elevation"]``."""
    el = elevation if elevation is not None else model.elevation
    if el is None:
        for r in model.roads:
            r.reference_line = _with_z(r.reference_line, np.zeros(len(r.reference_line.coords)))
        for s in model.signals:
            s.position = Point(s.position.x, s.position.y, 0.0)
        return {"source": "none", "applied": False}

    roads = {r.id: r for r in model.roads}
    max_resid = 0.0
    n_plain = n_conn = n_conn_fallback = n_bridge = n_lifted = 0
    # grade separation: the decks that pass over the roads below. Their footprint masks the DEM
    # for every road on layer <= 0 (``road_profile_from_dem``); the decks themselves get a
    # straight profile between their abutments (``bridge_profile``) after the roads they link to.
    upper = [r for r in model.roads
             if r.junction_id is None and (road_osm_layer(r) > 0 or road_is_bridge(r))]
    deck_mask = None
    if upper:
        deck_mask = shapely.union_all([
            LineString([(x, y) for x, y, *_ in r.reference_line.coords]).buffer(
                max(2.0, (r.width_left() + r.width_right()) / 2.0 + 1.0), cap_style="flat")
            for r in upper if len(r.reference_line.coords) >= 2])
    deck_ids = deck_road_ids(model)
    # ... and the tunnels under them: their z is their own alignment between the portals
    # (``apply_tunnel_profiles``), and an approach must not read the DEM past its portal
    tunnel_ids = tunnel_road_ids(model)
    E_rules = profiles.get().elevation
    for r in model.roads:
        if r.junction_id is not None or r.id in deck_ids or r.id in tunnel_ids:
            continue
        hold = tuple(link is not None and link.element == "road" and link.id in tunnel_ids
                     for link in (r.predecessor, r.successor))
        z, resid = road_profile_from_dem(
            r, el, mask=deck_mask if road_osm_layer(r) <= 0 else None, hold=hold)
        r.reference_line = _with_z(r.reference_line, z)
        max_resid = max(max_resid, resid)
        n_plain += 1
    n_bridge, n_lifted = apply_bridge_profiles(model, el, roads)
    n_welded = weld_deck_abutments(model, roads, deck_ids, E_rules.abutment_blend_m)
    n_tunnel, n_sunk, portal_sink = apply_tunnel_profiles(model, el, roads)
    n_portals = 0
    if n_tunnel:
        # a sunk portal pulls its approach down into a trench: over portal_blend_m, or as far
        # as the maximum grade needs for the step
        blend = max(E_rules.portal_blend_m, portal_sink / max(E_rules.tunnel_max_grade, 1e-3))
        n_portals = weld_deck_abutments(model, roads, tunnel_ids, blend, what="portal")
    for r in model.roads:
        if r.junction_id is None:
            continue
        z0 = z1 = None
        if r.predecessor is not None and r.predecessor.element == "road" and r.predecessor.id in roads:
            z0 = _contact_z(roads[r.predecessor.id], r.predecessor.contact)
        if r.successor is not None and r.successor.element == "road" and r.successor.id in roads:
            z1 = _contact_z(roads[r.successor.id], r.successor.contact)
        xy = np.asarray(r.reference_line.coords, dtype=np.float64)[:, :2]
        if z0 is None or z1 is None:
            z, _ = road_profile_from_dem(r, el)
            if z0 is not None:
                z = z - z[0] + z0
            elif z1 is not None:
                z = z - z[-1] + z1
            n_conn_fallback += 1
        else:
            s_v = _vertex_s(xy)
            t = s_v / s_v[-1] if s_v[-1] > 0 else np.zeros_like(s_v)
            z = z0 + (z1 - z0) * t
        r.reference_line = _with_z(r.reference_line, z)
        n_conn += 1
    # one plane per junction: contacts pulled onto it, connecting roads on it (twinmodel.datum)
    from .datum import harmonize_junction_z
    junction_stats = harmonize_junction_z(model)
    for s in model.signals:
        r = roads.get(s.road_id)
        if r is None:
            continue
        p = r.reference_line.interpolate(min(max(s.s, 0.0), r.length))
        s.position = Point(s.position.x, s.position.y, float(p.z) if p.has_z else 0.0)

    zs = np.concatenate([np.asarray(r.reference_line.coords)[:, 2] for r in model.roads]) \
        if model.roads else np.zeros(1)
    from .ingest.elevation import plane_fit
    E = profiles.get().elevation
    stats = plane_fit(el)
    stats.update({
        "applied": True,
        "roads": n_plain, "bridge_decks": n_bridge, "deck_chains_lifted": n_lifted,
        "abutments_welded": n_welded,
        "tunnel_roads": n_tunnel, "tunnel_chains_sunk": n_sunk, "portals_welded": n_portals,
        "portal_sink_m": round(portal_sink, 3),
        "connecting_roads": n_conn, "connecting_roads_dem_fallback": n_conn_fallback,
        "road_z_min": float(zs.min()), "road_z_max": float(zs.max()),
        "smoothing": {"resample_m": E.resample_m, "window_m": E.smooth_window_m, "filter": "savgol1"},
        "max_abs_smoothing_residual_m": max_resid,
        "junction_planes": junction_stats,
        "grid": {"shape": list(el.z.shape), "dx": el.dx, "dy": el.dy},
    })
    return stats


# --------------------------------------------------------------------------- helpers

class _Timer:
    def __init__(self):
        self.timings: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        log.info("== %s", name)
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            self.timings[name] = round(dt, 3)
            log.info("== %s done in %.2f s", name, dt)


def _drivable_union(model: TwinModel):
    geoms = [s.geometry for s in model.surfaces_of("drivable") if not s.geometry.is_empty]
    return shapely.union_all(geoms) if geoms else None


def _largest_junctions(model: TwinModel, n: int) -> list:
    js = [j for j in model.junctions if j.polygon is not None and not j.polygon.is_empty]
    js.sort(key=lambda j: j.polygon.area, reverse=True)
    return js[:n]


def _junction_window(j, pad: float = JUNCTION_ZOOM_PAD_M) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = j.polygon.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    half = max(maxx - minx, maxy - miny) / 2 + pad
    return (cx - half, cx + half, cy - half, cy + half)


def _rewrite_metadata(twin_dir: Path, model: TwinModel) -> None:
    p = twin_dir / "model.json"
    if not p.exists():
        return
    meta = json.loads(p.read_text())
    meta["metadata"] = model.metadata
    p.write_text(json.dumps(meta, indent=2, default=str))


def _json_safe(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return o


# --------------------------------------------------------------------------- profile selection

def _bbox_polygon(frame: LocalFrame, bbox: tuple[float, float, float, float]) -> Polygon:
    south, west, north, east = bbox
    x0, y0 = frame.to_local(west, south)
    x1, y1 = frame.to_local(east, north)
    return box(float(x0), float(y0), float(x1), float(y1))


def building_coverage(osm, frame: LocalFrame, bbox: tuple[float, float, float, float]) -> Optional[float]:
    """Building footprint area / bbox area (0-1) from the OSM download, computed *before* the
    lane graph so the profile can be chosen first. Uses ``lanegraph._buildings`` (footprints
    incl. multipolygon relations) when available, plain ``building=*`` closed ways otherwise;
    None when nothing can be measured."""
    try:
        area_box = _bbox_polygon(frame, bbox)
        if area_box.area <= 0:
            return None
        footprints: list = []
        from . import lanegraph as _lg
        fn = getattr(_lg, "_buildings", None)
        if fn is not None:
            footprints = [b.footprint for b in fn(osm, frame)]
        else:  # pragma: no cover - lanegraph helper renamed: closed building ways only
            for w in osm.ways_with("building"):
                pts = [frame.to_local(lon, lat) for lon, lat in osm.way_coords(w)]
                if len(pts) >= 4 and w.nodes[0] == w.nodes[-1]:
                    poly = Polygon([(float(x), float(y)) for x, y in pts])
                    if poly.is_valid and poly.area > 0:
                        footprints.append(poly)
        if not footprints:
            return 0.0
        covered = unary_union([shapely.make_valid(f) for f in footprints]).intersection(area_box)
        return float(covered.area / area_box.area)
    except Exception as exc:  # noqa: BLE001 - selection heuristic only
        log.warning("building coverage not measured: %s", exc)
        return None


def country_for_bbox(bbox: tuple[float, float, float, float], cache_dir: Path) -> Optional[str]:
    """ISO 3166-1 alpha-2 of the bbox via ``ingest.osm.country_for_bbox`` when that exists
    (worker D2), else None (-> the caller falls back to EU_DENSE)."""
    from .ingest import osm as osm_mod
    fn: Optional[Callable] = getattr(osm_mod, "country_for_bbox", None)
    if fn is None:
        log.info("profile: ingest.osm.country_for_bbox not available, country unknown")
        return None
    try:
        try:
            iso2 = fn(bbox, cache_dir)
        except TypeError:
            iso2 = fn(bbox)
    except Exception as exc:  # noqa: BLE001 - network / lookup failure -> unknown
        log.warning("profile: country lookup failed (%s), country unknown", exc)
        return None
    return str(iso2).upper() if iso2 else None


def select_profile(choice: str, osm, frame: LocalFrame, bbox: tuple[float, float, float, float],
                   cache_dir: Path) -> tuple[profiles.StreetProfile, dict[str, Any]]:
    """Resolve ``--profile``: a name, or ``auto`` = ``profiles.choose_for_country(iso2,
    building_coverage)``. Returns the profile and the ``metadata["profile"]`` record."""
    coverage = building_coverage(osm, frame, bbox)
    if choice != "auto":
        p = profiles.by_name(choice)
        return p, {"name": p.name, "source": "cli", "iso2": None, "building_coverage": coverage}
    iso2 = country_for_bbox(bbox, cache_dir)
    p = profiles.choose_for_country(iso2, coverage)
    return p, {"name": p.name, "source": "auto", "iso2": iso2, "building_coverage": coverage}


def _call_with_sources(fn: Callable, *args, sources: tuple[str, ...], **kw):
    """Call an ingest fetcher passing ``sources=`` only when it accepts it (worker D2 is
    adding the kwarg; works before and after)."""
    try:
        accepts = "sources" in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / odd callables
        accepts = False
    if accepts:
        try:
            return fn(*args, sources=list(sources), **kw)
        except TypeError as exc:
            if "sources" not in str(exc):
                raise
    return fn(*args, **kw)


# --------------------------------------------------------------------------- build

def build(args: argparse.Namespace) -> int:
    from .ingest.osm import fetch_overpass, load_fixture, parse_osm

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache)
    timer = _Timer()
    build_meta: dict[str, Any] = {
        "started": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()
                 if k != "func"},
        "timings": timer.timings, "outputs": {}, "notes": [],
    }

    # 1. OSM --------------------------------------------------------------------------------
    with timer.stage("osm"):
        if args.fixture:
            osm = load_fixture(args.fixture)
            bbox = tuple(args.bbox) if args.bbox else (tuple(osm.bbox_swne) if osm.bbox_swne else None)
            build_meta["osm_source"] = f"fixture:{args.fixture}"
        else:
            if not args.bbox:
                log.error("--bbox S W N E is required without --fixture")
                return 2
            bbox = tuple(args.bbox)
            osm = parse_osm(fetch_overpass(bbox, cache_dir=cache))
            build_meta["osm_source"] = "overpass"
        if bbox is None:
            log.error("no bbox: pass --bbox or use a fixture that records one")
            return 2
        build_meta["bbox_swne"] = list(bbox)
        frame = LocalFrame.from_bbox(*bbox)

    # 0. profile: chosen before the lane graph, active for every stage that follows --------
    with timer.stage("profile"):
        profile, profile_meta = select_profile(getattr(args, "profile", "auto") or "auto",
                                               osm, frame, bbox, cache)
        build_meta["profile"] = profile_meta
        cov = profile_meta.get("building_coverage")
        log.info("profile: %s (%s, country %s, building coverage %s)", profile.name,
                 profile_meta["source"], profile_meta.get("iso2") or "unknown",
                 f"{cov:.2f}" if cov is not None else "n/a")
        log.info("profile: %s", profiles.summary(profile))
    with profiles.use(profile):
        return _build_pipeline(args, osm, bbox, frame, out, cache, timer, build_meta, profile_meta)


def _build_pipeline(args: argparse.Namespace, osm, bbox, frame: LocalFrame, out: Path, cache: Path,
                    timer: _Timer, build_meta: dict[str, Any], profile_meta: dict[str, Any]) -> int:
    from .lanegraph import build_lanegraph
    from .surfaces import build_surfaces
    from .export.xodr import export_xodr
    from .export.mesh import export_obj, export_preview_png
    from . import validate as validate_mod

    outputs = build_meta["outputs"]
    P = profiles.get()
    with timer.stage("lanegraph"):
        model = build_lanegraph(osm, frame, bbox, name=args.name)
        model.metadata["profile"] = dict(profile_meta)
        log.info("lanegraph: %d roads, %d junctions, %d signals, %d buildings",
                 len(model.roads), len(model.junctions), len(model.signals), len(model.buildings))

    # 2. DEM --------------------------------------------------------------------------------
    with timer.stage("dem"):
        if args.no_dem:
            model.elevation = None
            build_meta["notes"].append("dem: skipped (--no-dem)")
        else:
            from .ingest.elevation import fetch_dem
            model.elevation = _call_with_sources(fetch_dem, frame, bbox, cache_dir=cache,
                                                 sources=P.sources.dem)
            if model.elevation is None:
                build_meta["notes"].append("dem: no source reachable, z=0")
        model.metadata["elevation"] = _json_safe(apply_elevation(model))
        el = model.metadata["elevation"]
        if el.get("applied"):
            log.info("elevation: %s z %.1f..%.1f m, slope %.2f%% uphill toward %s (%.0f deg)",
                     el["source"], el["z_min"], el["z_max"], el["slope_pct"], el["uphill_toward"],
                     el["uphill_azimuth_deg"])
        else:
            log.info("elevation: none (z = 0)")

    # 3. imagery ----------------------------------------------------------------------------
    ortho = None
    with timer.stage("imagery"):
        if args.no_imagery:
            build_meta["notes"].append("imagery: skipped (--no-imagery)")
        else:
            from .ingest.imagery import fetch_ortho
            ortho = _call_with_sources(fetch_ortho, frame, bbox, cache_dir=cache,
                                       sources=P.sources.ortho)
            if ortho is None:
                build_meta["notes"].append("imagery: fetch failed, no ortho")
            else:
                build_meta["imagery"] = {"source": ortho.source, "width": ortho.width,
                                         "height": ortho.height, "dx": ortho.dx}

    # 4. surfaces (+ refinement) ------------------------------------------------------------
    with timer.stage("surfaces"):
        build_surfaces(model)
        unrefined_stats = dict(model.metadata.get("surfaces", {}))
    xodr_text = None
    with timer.stage("refine"):
        refine_meta: dict[str, Any] = {"status": "skipped"}
        if args.no_refine:
            refine_meta["reason"] = "--no-refine"
        elif ortho is None:
            refine_meta["reason"] = "no ortho"
        else:
            from .refine import lane_keep_out, road_mask, refine_drivable, save_overlay
            prior = _drivable_union(model)
            if prior is None:
                refine_meta["reason"] = "no drivable surfaces"
            else:
                t0 = time.perf_counter()
                mask = road_mask(ortho, prior=prior, method=args.mask_method)
                refine_meta["mask_seconds"] = round(time.perf_counter() - t0, 2)
                refine_meta["mask_method"] = args.mask_method
                refine_meta["mask_fraction"] = float(mask.mean())
                t0 = time.perf_counter()
                refined, rstats = refine_drivable(prior, mask, ortho, keep=lane_keep_out(model))
                refine_meta["refine_seconds"] = round(time.perf_counter() - t0, 2)
                refine_meta["stats"] = _json_safe(rstats)
                try:
                    p = out / f"{args.name}_mask.png"
                    save_overlay(ortho, mask, p, prior=prior, refined=refined)
                    outputs["mask_png"] = str(p)
                except Exception as exc:  # noqa: BLE001 - quicklook only
                    log.warning("mask overlay failed: %s", exc)
                build_surfaces(model, refined_drivable=refined)
                refined_stats = dict(model.metadata.get("surfaces", {}))
                xodr_text = export_xodr(model)
                check = validate_mod.validate(model, xodr_text, step=args.step)
                lid = check.get("lane_in_drivable") or {}
                frac = lid.get("fraction")
                refine_meta["lane_in_drivable_refined"] = frac
                refine_meta["surfaces_refined"] = {k: v for k, v in refined_stats.items()
                                                   if not k.endswith("_wkt")}
                if not check["topology"].get("loaded") or frac is None or frac < LANE_IN_DRIVABLE_MIN:
                    log.warning("refine: rejected (lane_in_drivable %.4f < %.2f); keeping "
                                "lane-graph surfaces", frac or 0.0, LANE_IN_DRIVABLE_MIN)
                    build_surfaces(model)
                    refine_meta["status"] = "rejected"
                    build_meta["notes"].append("refine: rejected")
                else:
                    refine_meta["status"] = "accepted"
                    log.info("refine: accepted (IoU %.3f -> %.3f, lane_in_drivable %.4f)",
                             rstats.get("iou_before", 0), rstats.get("iou_after", 0), frac)
        refine_meta["surfaces_unrefined"] = {k: v for k, v in unrefined_stats.items()
                                             if not k.endswith("_wkt")}
        model.metadata["refine"] = refine_meta
    model.metadata["build"] = build_meta

    # 5. exports ----------------------------------------------------------------------------
    with timer.stage("export"):
        twin_dir = out / f"{args.name}.twin"
        model.save(twin_dir)
        outputs["twin"] = str(twin_dir)
        xodr_path = out / f"{args.name}.xodr"
        xodr_text = export_xodr(model, xodr_path)
        outputs["xodr"] = str(xodr_path)
        obj_path = out / f"{args.name}.obj"
        export_obj(model, obj_path)
        outputs["obj"] = str(obj_path)
        outputs["mtl"] = str(obj_path.with_suffix(".mtl"))
    with timer.stage("preview"):
        ortho_arr = ortho_ext = None
        if ortho is not None:
            ortho_arr = ortho.array[::-1]  # export_preview_png draws origin="upper"
            ortho_ext = ortho.extent()
        p = out / f"{args.name}_preview.png"
        export_preview_png(model, p, ortho=ortho_arr, extent=ortho_ext)
        outputs["preview_png"] = str(p)
        if ortho is not None:
            p = out / f"{args.name}_preview_plain.png"
            export_preview_png(model, p)
            outputs["preview_plain_png"] = str(p)
        outputs["junction_png"] = {}
        for j in _largest_junctions(model, args.junction_zooms):
            win = _junction_window(j)
            p = out / f"{args.name}_junction_{j.id}.png"
            export_preview_png(model, p, ortho=ortho_arr, extent=ortho_ext, window=win,
                               title=f"{args.name} junction {j.id} ({j.polygon.area:.0f} m2, "
                                     f"{len(j.connections)} connections)")
            outputs["junction_png"][j.id] = str(p)
        if model.elevation is not None:
            try:
                from .ingest.elevation import save_quicklook
                p = out / f"{args.name}_dem.png"
                save_quicklook(model.elevation, p)
                outputs["dem_png"] = str(p)
            except Exception as exc:  # noqa: BLE001 - quicklook only
                log.warning("dem quicklook failed: %s", exc)

    # 6. validate ---------------------------------------------------------------------------
    with timer.stage("validate"):
        report = validate_mod.validate(model, xodr_text, step=args.step, out_dir=out)
        report_path = validate_mod.write_report(report, out / "report.json")
        outputs["report"] = str(report_path)
        outputs["violations"] = str(out / "violations.geojson")

    build_meta["finished"] = _dt.datetime.now().replace(microsecond=0).isoformat()
    build_meta["total_seconds"] = round(sum(timer.timings.values()), 3)
    loaded = bool(report["topology"].get("loaded"))
    lid = report.get("lane_in_drivable")
    ok = loaded and lid is not None and lid["fraction"] >= LANE_IN_DRIVABLE_MIN
    build_meta["status"] = "ok" if ok else "failed"
    _rewrite_metadata(twin_dir, model)
    # report.json also carries the build metadata so a reader has everything in one file
    report_slim = json.loads(report_path.read_text())
    report_slim["build"] = _json_safe(build_meta)
    report_slim["profile"] = _json_safe(model.metadata.get("profile"))
    report_slim["elevation"] = model.metadata.get("elevation")
    report_slim["refine"] = _json_safe({k: v for k, v in model.metadata.get("refine", {}).items()})
    report_path.write_text(json.dumps(report_slim, indent=2, default=str))

    print(validate_mod.summary(report))
    print(f"profile: {profiles.summary(P)} [{profile_meta['source']}"
          + (f", {profile_meta['iso2']}" if profile_meta.get("iso2") else "") + "]")
    print("timings: " + ", ".join(f"{k} {v:.2f}s" for k, v in timer.timings.items())
          + f" (total {build_meta['total_seconds']:.2f}s)")
    print(f"outputs: {out}")
    if not ok:
        print("BUILD FAILED: " + ("xodr did not load in carla.Map" if not loaded else
                                  f"lane_in_drivable {lid['fraction'] if lid else 'n/a'} < {LANE_IN_DRIVABLE_MIN}"))
        return 1
    return 0


# --------------------------------------------------------------------------- compare

def compare(args: argparse.Namespace) -> int:
    from .compare import compare_build
    layers = compare_build(args.build_dir, args.name, out_dir=args.out,
                           resolution=args.resolution, zoom=args.zoom, cache_dir=args.cache,
                           n_junctions=args.junctions)
    st = layers.get("stats", {})
    if "iou" in st:
        print(f"mesh road vs OSM tiles: IoU {st['iou']:.3f}, agree {st['agree_m2']:.0f} m2, "
              f"mesh-only {st['mesh_only_m2']:.0f} m2, OSM-only {st['osm_only_m2']:.0f} m2")
    for k in ("shift_mesh_to_osm", "shift_mesh_to_ortho", "shift_osm_to_ortho"):
        if k in st:
            print(f"{k}: dx {st[k]['dx_m']:+.2f} m dy {st[k]['dy_m']:+.2f} m "
                  f"(corr {st[k]['peak_corr']:.3f} vs {st[k]['zero_corr']:.3f} at zero)")
    out = Path(args.out) if args.out else Path(args.build_dir) / "compare"
    print(f"files -> {out}: " + ", ".join(sorted(layers.get("files", {}).values())))
    if not getattr(args, "no_viewer", False):
        from .viewer import write_viewer
        html = write_viewer(args.build_dir, args.name, out_html=out / "viewer.html")
        print(f"viewer -> {html} ({html.stat().st_size / 1e6:.1f} MB, self-contained; publish or open in a browser)")
    return 0


# --------------------------------------------------------------------------- entry point

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="twinmodel", description=__doc__.split("\n\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="OSM -> twin model -> mesh + OpenDRIVE + report")
    b.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"),
                   help=f"WGS84 bbox (default from --fixture, else {DEFAULT_BBOX})")
    b.add_argument("--name", default="eixample")
    b.add_argument("--out", required=True, help="output directory")
    b.add_argument("--fixture", help="cached Overpass JSON instead of a network fetch")
    b.add_argument("--cache", default="data", help="Overpass/WMS/DEM cache directory")
    b.add_argument("--no-imagery", action="store_true")
    b.add_argument("--no-dem", action="store_true")
    b.add_argument("--no-refine", action="store_true")
    b.add_argument("--mask-method", default="classical", choices=["classical", "sam", "auto"])
    b.add_argument("--profile", default="auto", choices=list(PROFILE_CHOICES),
                   help="region profile (twinmodel.profiles); auto = by country + building density")
    b.add_argument("--step", type=float, default=1.0, help="waypoint step for validation (m)")
    b.add_argument("--junction-zooms", type=int, default=3, help="zoom PNGs for the N largest junctions")
    b.set_defaults(func=build)

    sub.add_parser("validate", add_help=False,
                   help="python -m twinmodel.validate <twin_dir> <xodr> [--out DIR] [--step S]")

    c = sub.add_parser("compare", help="OSM tiles | ortho | mesh top view | diff rasters "
                                       "-> <build_dir>/compare/ (twinmodel.compare)")
    c.add_argument("build_dir", help="directory holding <name>.twin and <name>.obj")
    c.add_argument("name")
    c.add_argument("--resolution", type=float, default=0.25, help="grid resolution (m)")
    c.add_argument("--zoom", type=int, default=19, help="OSM tile zoom")
    c.add_argument("--out", help="output directory (default <build_dir>/compare)")
    c.add_argument("--cache", default="data", help="tile/WMS cache directory")
    c.add_argument("--junctions", type=int, default=3, help="junction crops for the N largest")
    c.add_argument("--no-viewer", action="store_true", help="skip writing compare/viewer.html")
    c.set_defaults(func=compare)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `validate` is a pure proxy so its own argparse (and --help) stay authoritative
    if argv and argv[0] == "validate":
        from . import validate as validate_mod
        return validate_mod.main(argv[1:])
    ap = _build_parser()
    args = ap.parse_args(argv)
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.cmd == "build" and not args.bbox and not args.fixture:
        args.bbox = list(DEFAULT_BBOX)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
