"""Small hand-built TwinModels for the xodr/validate tests (worker C).

Kept separate from worker B's ``tests/synthetic.py`` so the two can evolve independently.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box

from twinmodel.model import (Connection, Elevation, Junction, Lane, LaneLink, Marking, Road,
                             RoadLink, Signal, Surface, TwinModel)

ORIGIN = (41.3925, 2.1660)
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)
W = 3.25   # driving lane width
SW = 2.0   # sidewalk width


def _two_way_lanes(sidewalks: bool = True, speed: float = 50 / 3.6) -> list[Lane]:
    lanes = [
        Lane(id=1, type="driving", width=W, direction="backward", speed_limit=speed,
             marking=Marking("solid", "white", 0.12)),
        Lane(id=-1, type="driving", width=W, direction="forward", speed_limit=speed,
             marking=Marking("solid", "white", 0.12)),
    ]
    if sidewalks:
        lanes += [Lane(id=2, type="sidewalk", width=SW, direction="backward"),
                  Lane(id=-2, type="sidewalk", width=SW, direction="forward")]
    return lanes


def _carriageway(line: LineString, road: Road) -> Polygon:
    left = line.buffer(road.width_left(), single_sided=True, cap_style="flat", join_style="mitre")
    right = line.buffer(-road.width_right(), single_sided=True, cap_style="flat", join_style="mitre")
    return shapely.union_all([left, right])


def _sidewalks(line: LineString, road: Road, drivable):
    out = []
    for sign, lanes in ((1, road.lanes_left()), (-1, road.lanes_right())):
        cw = road.width_left() if sign > 0 else road.width_right()
        sw = sum(l.width for l in lanes if l.type == "sidewalk")
        if sw <= 0:
            continue
        band = line.buffer(sign * (cw + sw), single_sided=True, cap_style="flat", join_style="mitre")
        out.append(band.difference(drivable))
    return out


def straight_road(with_elevation: bool = True) -> TwinModel:
    """Two-way street, 100 m, sidewalks both sides, 2 % grade, one speed sign + crosswalk."""
    xs = np.array([-50.0, 0.0, 50.0])
    z = 0.02 * xs if with_elevation else np.zeros(3)
    line = LineString([(x, 0.0, zz) for x, zz in zip(xs, z)])
    road = Road(id="r1", reference_line=line, lanes=_two_way_lanes(), name="Carrer Test",
                highway="residential", center_marking=Marking("solid", "yellow", 0.12))
    m = TwinModel(name="straight", origin_lat=ORIGIN[0], origin_lon=ORIGIN[1], bbox_wgs84=BBOX,
                  roads=[road])
    m.signals = [
        Signal(id="s_speed", kind="speed_limit", road_id="r1", s=10.0, t=-(W + 0.5),
               position=Point(-40.0, -(W + 0.5)), value=30 / 3.6, orientation="+"),
        Signal(id="x_1", kind="crosswalk", road_id="r1", s=70.0, t=0.0, position=Point(20.0, 0.0)),
    ]
    drivable = _carriageway(line, road)
    m.surfaces = [Surface(id="d1", kind="drivable", geometry=drivable, road_ids=["r1"])]
    m.surfaces += [Surface(id=f"sw{i}", kind="sidewalk", geometry=g, z_offset=0.15, road_ids=["r1"])
                   for i, g in enumerate(_sidewalks(line, road, drivable))]
    if with_elevation:
        gx = np.arange(-80.0, 81.0, 10.0)
        gy = np.arange(-40.0, 41.0, 10.0)
        zz = 0.02 * gx[None, :] + 0.0 * gy[:, None]
        m.elevation = Elevation(zz, gx[0], gy[0], 10.0, 10.0, source="synthetic")
    return m


def _arc(cx, cy, r, a0, a1, n) -> list[tuple[float, float, float]]:
    return [(cx + r * math.cos(a), cy + r * math.sin(a), 0.0) for a in np.linspace(a0, a1, n)]


def junction_model() -> TwinModel:
    """Three arms (west ``a``, east ``b``, north ``n``) meeting in junction ``j1`` with three
    connecting roads: ``c1`` a->b straight, ``c2`` a->n quarter-circle (paramPoly3 fit),
    ``c3`` b->a straight (from b's backward lane).  Traffic lights on a and b under one
    controller, a stop sign on n, a yield on b."""
    R = 15.0
    a = Road(id="a", reference_line=LineString([(-60, 0, 0), (-R, 0, 0)]), lanes=_two_way_lanes(),
             name="west", highway="residential", successor=RoadLink("junction", "j1"),
             center_marking=Marking("broken", "white", 0.12))
    b = Road(id="b", reference_line=LineString([(R, 0, 0), (60, 0, 0)]), lanes=_two_way_lanes(),
             name="east", highway="residential", predecessor=RoadLink("junction", "j1"),
             center_marking=Marking("broken", "white", 0.12))
    n = Road(id="n", reference_line=LineString([(0, R, 0), (0, 60, 0)]), lanes=_two_way_lanes(),
             name="north", highway="residential", predecessor=RoadLink("junction", "j1"),
             center_marking=Marking("solid", "yellow", 0.12))
    conn_lanes = [Lane(id=-1, type="driving", width=W, direction="forward")]
    c1 = Road(id="c1", reference_line=LineString([(-R, 0, 0), (R, 0, 0)]), lanes=list(conn_lanes),
              junction_id="j1", predecessor=RoadLink("road", "a", "end"),
              successor=RoadLink("road", "b", "start"))
    # quarter circle centred (-R, R): from (-R, 0) heading east to (0, R) heading north
    c2 = Road(id="c2", reference_line=LineString(_arc(-R, R, R, -math.pi / 2, 0.0, 8)),
              lanes=list(conn_lanes), junction_id="j1",
              predecessor=RoadLink("road", "a", "end"), successor=RoadLink("road", "n", "start"))
    c3 = Road(id="c3", reference_line=LineString([(R, 0, 0), (-R, 0, 0)]), lanes=list(conn_lanes),
              junction_id="j1", predecessor=RoadLink("road", "b", "start"),
              successor=RoadLink("road", "a", "end"))
    j1 = Junction(id="j1", polygon=box(-R, -R, R, R), name="j1", connections=[
        Connection(id="k1", incoming_road="a", connecting_road="c1", contact_point="start",
                   lane_links=[LaneLink(-1, -1)]),
        Connection(id="k2", incoming_road="a", connecting_road="c2", contact_point="start",
                   lane_links=[LaneLink(-1, -1)]),
        Connection(id="k3", incoming_road="b", connecting_road="c3", contact_point="start",
                   lane_links=[LaneLink(1, -1)]),
    ])
    m = TwinModel(name="junction3", origin_lat=ORIGIN[0], origin_lon=ORIGIN[1], bbox_wgs84=BBOX,
                  roads=[a, b, n, c1, c2, c3], junctions=[j1])
    m.signals = [
        Signal(id="tl_a", kind="traffic_light", road_id="a", s=a.length - 1.0, t=-(W + 0.5),
               position=Point(-R - 1.0, -(W + 0.5)), controller_id="ctl_j1"),
        Signal(id="tl_b", kind="traffic_light", road_id="b", s=1.0, t=(W + 0.5),
               position=Point(R + 1.0, W + 0.5), orientation="-", controller_id="ctl_j1"),
        Signal(id="stop_n", kind="stop", road_id="n", s=1.0, t=(W + 0.5),
               position=Point(W + 0.5, R + 1.0), orientation="-"),
        Signal(id="yield_b", kind="yield", road_id="b", s=20.0, t=-(W + 0.5),
               position=Point(R + 20.0, -(W + 0.5))),
    ]
    arms = [a, b, n]
    cws = [_carriageway(r.reference_line, r) for r in arms] + [j1.polygon]
    drivable = shapely.union_all(cws)
    m.surfaces = [Surface(id="d1", kind="drivable", geometry=drivable,
                          road_ids=[r.id for r in arms], junction_id="j1")]
    i = 0
    for r in arms:
        for g in _sidewalks(r.reference_line, r, drivable):
            m.surfaces.append(Surface(id=f"sw{i}", kind="sidewalk", geometry=g, z_offset=0.15,
                                      road_ids=[r.id]))
            i += 1
    return m


def shifted_junction_model(dx: float = 6.0) -> TwinModel:
    """Junction model whose drivable surface is shifted so that validation must fail."""
    m = junction_model()
    for s in m.surfaces:
        s.geometry = shapely.affinity.translate(s.geometry, yoff=dx)
    return m
