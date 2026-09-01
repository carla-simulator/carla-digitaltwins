"""Synthetic TwinModels for tests (no network, no OSM).

Four cases:

* ``straight_road()``        - one two-way road with sidewalks, a crosswalk and a building.
* ``four_way_junction()``    - four two-way arms ending at a small junction (12 m across),
                               connecting roads as cubic Hermite curves for every in->out pair.
* ``eixample_junction()``    - two one-way streets (3 lanes + parking, 5 m sidewalks) crossing at a
                               ~40 m octagonal cluster, chamfered buildings on the corners.
* ``eixample_single_node()`` - the same corner as OSM maps it: one node, arms ending at the
                               crossing carriageway, 20 m streets between four blocks with 45°
                               15 m chamfers — the plaza has to come from the buildings.

All arms are oriented so that *incoming* arms point into the junction (successor = junction) and
*outgoing* arms point away from it (predecessor = junction). Connecting roads carry one driving
lane (id -1) per incoming lane they serve, right of their reference line, so that the reference
line runs from the incoming road's lane boundary to the outgoing road's lane boundary.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from shapely.geometry import LineString, Point, Polygon

from twinmodel.model import (
    Building, Connection, Junction, Lane, LaneLink, Marking, Road, RoadLink, Signal, TwinModel,
)

ORIGIN = (41.3925, 2.1660)
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


def _empty(name: str) -> TwinModel:
    return TwinModel(name=name, origin_lat=ORIGIN[0], origin_lon=ORIGIN[1], bbox_wgs84=BBOX)


def hermite(p0, t0, p1, t1, step: float = 1.0) -> LineString:
    """Cubic Hermite curve from p0 (unit tangent t0) to p1 (unit tangent t1), sampled ~every
    ``step`` metres. Tangent magnitudes are scaled by the chord length."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    t0, t1 = np.asarray(t0, float), np.asarray(t1, float)
    chord = float(np.linalg.norm(p1 - p0))
    m0, m1 = t0 / np.linalg.norm(t0) * chord, t1 / np.linalg.norm(t1) * chord

    def eval_(u):
        u = np.asarray(u)[:, None]
        h00 = 2 * u ** 3 - 3 * u ** 2 + 1
        h10 = u ** 3 - 2 * u ** 2 + u
        h01 = -2 * u ** 3 + 3 * u ** 2
        h11 = u ** 3 - u ** 2
        return h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1

    coarse = eval_(np.linspace(0, 1, 200))
    length = float(np.sum(np.linalg.norm(np.diff(coarse, axis=0), axis=1)))
    n = max(2, int(math.ceil(length / step)) + 1)
    pts = eval_(np.linspace(0, 1, n))
    return LineString([(float(x), float(y), 0.0) for x, y in pts])


def two_way_lanes(lane_w: float = 3.25, sidewalk_w: float = 2.0, n_per_side: int = 1,
                  markings: bool = False) -> list[Lane]:
    lanes: list[Lane] = []
    for i in range(1, n_per_side + 1):
        lanes.append(Lane(id=i, type="driving", width=lane_w, direction="backward",
                          marking=Marking("solid", "white") if markings else None))
        lanes.append(Lane(id=-i, type="driving", width=lane_w, direction="forward",
                          marking=Marking("solid", "white") if markings else None))
    if sidewalk_w > 0:
        lanes.append(Lane(id=n_per_side + 1, type="sidewalk", width=sidewalk_w, direction="backward"))
        lanes.append(Lane(id=-(n_per_side + 1), type="sidewalk", width=sidewalk_w, direction="forward"))
    return lanes


def one_way_lanes(n_driving: int = 3, lane_w: float = 3.2, parking_w: float = 2.2,
                  sidewalk_w: float = 5.0) -> list[Lane]:
    """One-way Eixample-style street: the reference line is the *left* carriageway edge, all
    driving lanes are to its right, parking outermost right, sidewalk on both sides."""
    lanes = [Lane(id=1, type="sidewalk", width=sidewalk_w, direction="backward")]
    for i in range(1, n_driving + 1):
        lanes.append(Lane(id=-i, type="driving", width=lane_w, direction="forward"))
    nxt = n_driving + 1
    if parking_w > 0:
        lanes.append(Lane(id=-nxt, type="parking", width=parking_w, direction="forward"))
        nxt += 1
    lanes.append(Lane(id=-nxt, type="sidewalk", width=sidewalk_w, direction="forward"))
    return lanes


# --------------------------------------------------------------------------- case 1

def straight_road(length: float = 120.0, with_building: bool = True) -> TwinModel:
    m = _empty("synthetic_straight")
    ref = LineString([(-length / 2, 0.0, 0.0), (length / 2, 0.0, 0.0)])
    road = Road(id="r1", reference_line=ref, lanes=two_way_lanes(), name="Straight",
                highway="residential")
    m.roads.append(road)
    s = length * 0.6
    m.signals.append(Signal(id="x1", kind="crosswalk", road_id="r1", s=s, t=0.0,
                            position=Point(-length / 2 + s, 0.0), heading=0.0,
                            tags={"crossing:width": 4.0}))
    m.signals.append(Signal(id="sl1", kind="speed_limit", road_id="r1", s=10.0, t=-5.5,
                            position=Point(-length / 2 + 10.0, -5.5), value=30 / 3.6))
    if with_building:
        # footprint that intrudes 0.5 m into the left sidewalk band (y in [3.25, 5.25])
        m.buildings.append(Building(id="b1", footprint=Polygon(
            [(-20, 4.75), (10, 4.75), (10, 25), (-20, 25)]), levels=5))
    return m


# --------------------------------------------------------------------------- case 2

def _arm(road_id: str, angle: float, r_end: float, length: float, lanes: list[Lane],
         incoming: bool, junction_id: str, name: str = "") -> Road:
    """Straight arm along direction ``angle`` (radians) ending/starting at radius ``r_end``
    from the origin. Incoming arms point toward the origin."""
    ux, uy = math.cos(angle), math.sin(angle)
    far = (ux * (r_end + length), uy * (r_end + length), 0.0)
    near = (ux * r_end, uy * r_end, 0.0)
    ref = LineString([far, near]) if incoming else LineString([near, far])
    road = Road(id=road_id, reference_line=ref, lanes=lanes, name=name or road_id,
                highway="residential")
    if incoming:
        road.successor = RoadLink("junction", junction_id)
    else:
        road.predecessor = RoadLink("junction", junction_id)
    return road


def _end_tangent(road: Road, at_end: bool) -> np.ndarray:
    c = np.asarray(road.reference_line.coords)[:, :2]
    d = c[-1] - c[-2] if at_end else c[1] - c[0]
    return d / np.linalg.norm(d)


def four_way_junction(arm_length: float = 60.0, half: float = 6.0, lane_w: float = 3.25,
                      sidewalk_w: float = 2.0) -> TwinModel:
    m = _empty("synthetic_fourway")
    jid = "j1"
    arms = []
    for k, (name, ang) in enumerate([("E", 0.0), ("N", math.pi / 2), ("W", math.pi),
                                     ("S", -math.pi / 2)]):
        arms.append(_arm(f"arm_{name}", ang, half, arm_length, two_way_lanes(lane_w, sidewalk_w),
                         incoming=True, junction_id=jid, name=f"Arm {name}"))
    m.roads.extend(arms)
    junction = Junction(id=jid, name="fourway")
    for a in arms:
        for b in arms:
            if a is b:
                continue
            p0 = np.asarray(a.reference_line.coords)[-1][:2]
            p1 = np.asarray(b.reference_line.coords)[-1][:2]
            t0 = _end_tangent(a, True)
            t1 = -_end_tangent(b, True)
            cid = f"c_{a.id[-1]}{b.id[-1]}"
            ref = hermite(p0, t0, p1, t1)
            croad = Road(id=cid, reference_line=ref,
                         lanes=[Lane(id=-1, type="driving", width=lane_w, direction="forward")],
                         junction_id=jid, highway="residential",
                         predecessor=RoadLink("road", a.id, "end"),
                         successor=RoadLink("road", b.id, "end"))
            m.roads.append(croad)
            junction.connections.append(Connection(
                id=f"{a.id}->{cid}", incoming_road=a.id, connecting_road=cid,
                contact_point="start", lane_links=[LaneLink(-1, -1)]))
    m.junctions.append(junction)
    # traffic lights at every arm end + crosswalks 5 m before the junction
    for i, a in enumerate(arms):
        L = a.length
        end = np.asarray(a.reference_line.coords)[-1][:2]
        t = _end_tangent(a, True)
        n = np.array([-t[1], t[0]])
        pos = end - n * (lane_w + 0.5)
        m.signals.append(Signal(id=f"tl_{a.id}", kind="traffic_light", road_id=a.id, s=L,
                                t=-(lane_w + 0.5), position=Point(*pos),
                                heading=math.atan2(t[1], t[0]), controller_id="ctrl_j1"))
        pc = end - t * 5.0
        m.signals.append(Signal(id=f"x_{a.id}", kind="crosswalk", road_id=a.id, s=L - 5.0, t=0.0,
                                position=Point(*pc), heading=math.atan2(t[1], t[0])))
    # a building on the NE corner that pokes into the sidewalk band
    d = half + lane_w + sidewalk_w - 0.4
    m.buildings.append(Building(id="b_ne", footprint=Polygon(
        [(d, d), (d + 30, d), (d + 30, d + 30), (d, d + 30)]), levels=4))
    return m


# --------------------------------------------------------------------------- case 3

def eixample_junction(arm_length: float = 60.0, half: float = 20.0, lane_w: float = 3.2,
                      parking_w: float = 2.2, sidewalk_w: float = 5.0,
                      n_driving: int = 3, chamfer: float | None = None,
                      face_setback: float | None = None, intrude: float = 0.3,
                      name: str = "synthetic_eixample") -> TwinModel:
    """Two one-way streets (E-bound and N-bound) crossing at a cluster of radius ``half``.

    Reference lines are the left carriageway edge (all lanes right of it), so the carriageway is
    off-centre with respect to the reference line — exercises asymmetric buffering.

    ``chamfer``: length of the 45° chamfer edge of the corner blocks (default: parallel to the
    junction octagon's diagonal, one sidewalk width out). ``face_setback``: distance from the
    street axis to the building face (default: street half width minus ``intrude``, i.e. the
    footprints poke ``intrude`` m into the sidewalk band)."""
    m = _empty(name)
    jid = "j1"
    cw = n_driving * lane_w + parking_w  # carriageway width, right of the reference line
    off = cw / 2.0                      # street centred on the axis -> ref line at +off (left)

    def lanes():
        return one_way_lanes(n_driving, lane_w, parking_w, sidewalk_w)

    # E-W street, eastbound: ref line at y = +off
    west = Road(id="arm_W", reference_line=LineString([(-half - arm_length, off, 0), (-half, off, 0)]),
                lanes=lanes(), name="Carrer W", highway="residential",
                successor=RoadLink("junction", jid))
    east = Road(id="arm_E", reference_line=LineString([(half, off, 0), (half + arm_length, off, 0)]),
                lanes=lanes(), name="Carrer E", highway="residential",
                predecessor=RoadLink("junction", jid))
    # N-S street, northbound: left edge is the west side -> ref line at x = -off
    south = Road(id="arm_S", reference_line=LineString([(-off, -half - arm_length, 0), (-off, -half, 0)]),
                 lanes=lanes(), name="Carrer S", highway="residential",
                 successor=RoadLink("junction", jid))
    north = Road(id="arm_N", reference_line=LineString([(-off, half, 0), (-off, half + arm_length, 0)]),
                 lanes=lanes(), name="Carrer N", highway="residential",
                 predecessor=RoadLink("junction", jid))
    m.roads.extend([west, east, south, north])
    junction = Junction(id=jid, name="eixample")

    def connect(cid: str, inc: Road, out: Road, in_lane: int, n_lanes: int, t_in: float):
        """Connecting road from the inner edge of lane ``in_lane`` of ``inc`` (offset t_in from
        the reference line) to the matching edge of ``out``."""
        p0 = np.asarray(inc.reference_line.coords)[-1][:2]
        t0 = _end_tangent(inc, True)
        n0 = np.array([-t0[1], t0[0]])
        p1 = np.asarray(out.reference_line.coords)[0][:2]
        t1 = _end_tangent(out, False)
        n1 = np.array([-t1[1], t1[0]])
        ref = hermite(p0 + n0 * t_in, t0, p1 + n1 * t_in, t1)
        croad = Road(id=cid, reference_line=ref,
                     lanes=[Lane(id=-(i + 1), type="driving", width=lane_w, direction="forward")
                            for i in range(n_lanes)],
                     junction_id=jid, highway="residential",
                     predecessor=RoadLink("road", inc.id, "end"),
                     successor=RoadLink("road", out.id, "start"))
        m.roads.append(croad)
        junction.connections.append(Connection(
            id=f"{inc.id}->{cid}", incoming_road=inc.id, connecting_road=cid, contact_point="start",
            lane_links=[LaneLink(in_lane - i, -(i + 1)) for i in range(n_lanes)]))

    connect("c_WE", west, east, -1, n_driving, 0.0)                       # straight
    connect("c_SN", south, north, -1, n_driving, 0.0)                     # straight
    connect("c_WN", west, north, -1, 1, 0.0)                              # left turn, inner lane
    connect("c_SE", south, east, -n_driving, 1, -(n_driving - 1) * lane_w)  # right turn, outer lane
    m.junctions.append(junction)

    for inc in (west, south):
        L = inc.length
        end = np.asarray(inc.reference_line.coords)[-1][:2]
        t = _end_tangent(inc, True)
        n = np.array([-t[1], t[0]])
        m.signals.append(Signal(id=f"tl_{inc.id}", kind="traffic_light", road_id=inc.id, s=L,
                                t=-(cw + 0.5), position=Point(*(end - n * (cw + 0.5))),
                                heading=math.atan2(t[1], t[0]), controller_id="ctrl_j1"))
        pc = end - t * 6.0
        m.signals.append(Signal(id=f"x_{inc.id}", kind="crosswalk", road_id=inc.id, s=L - 6.0,
                                t=0.0, position=Point(*pc), heading=math.atan2(t[1], t[0]),
                                tags={"crossing:width": 4.0}))
    # chamfered building blocks on the four corners (Eixample style), 0.3 m into the sidewalk;
    # the chamfer line is parallel to the junction octagon's diagonal edge (x + y = half + off)
    # offset outward by the sidewalk width minus the 0.3 m intrusion.
    e = (off + sidewalk_w - intrude) if face_setback is None else face_setback
    if chamfer is None:
        ch = (half + off + (sidewalk_w - 0.3) * math.sqrt(2.0)) - 2 * e
    else:
        ch = chamfer / math.sqrt(2.0)  # chamfer edge length -> offset along each axis
    for sx in (1, -1):
        for sy in (1, -1):
            fp = Polygon([(sx * (e + ch), sy * e), (sx * (e + 60), sy * e), (sx * (e + 60), sy * (e + 60)),
                          (sx * e, sy * (e + 60)), (sx * e, sy * (e + ch))])
            m.buildings.append(Building(id=f"b_{sx}_{sy}", footprint=fp, levels=6))
    return m


def eixample_single_node(arm_length: float = 60.0, lane_w: float = 3.0, parking_w: float = 2.0,
                         sidewalk_w: float = 4.5, n_driving: int = 3, chamfer: float = 15.0,
                         face_setback: float | None = None) -> TwinModel:
    """Eixample corner as a single OSM node: 20 m streets (11 m carriageway = 3 x 3 m + 2 m
    parking, 4.5 m sidewalks), the arms end 2 m short of the crossing carriageway so the convex
    cover of the arm ends is a plain cross; four blocks with 45° chamfers of ``chamfer`` m
    surround it. ``face_setback`` (axis -> building face) lets the buildings sit further away
    than the lane graph's sidewalk width (sidewalks must then grow to the face)."""
    cw = n_driving * lane_w + parking_w
    return eixample_junction(arm_length=arm_length, half=cw / 2.0 + 2.0, lane_w=lane_w,
                             parking_w=parking_w, sidewalk_w=sidewalk_w, n_driving=n_driving,
                             chamfer=chamfer, face_setback=face_setback,
                             name="synthetic_eixample_node")


ALL_CASES = {
    "straight": straight_road,
    "fourway": four_way_junction,
    "eixample": eixample_junction,
    "eixample_node": eixample_single_node,
}
