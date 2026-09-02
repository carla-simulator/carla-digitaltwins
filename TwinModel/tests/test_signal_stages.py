"""Signal staging: one OpenDRIVE ``<controller>`` per stage of a signalised junction.

``ATrafficLightGroup::Tick`` ticks exactly one ``UTrafficLightController`` and
``NextController()`` round-robins them, so N controllers on a junction are N sequential
stages. The twin used to emit one controller per junction, i.e. every approach green at once.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest
from lxml import etree

from twinmodel import profiles
from twinmodel.export.xodr import export_xodr
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import load_fixture
from twinmodel.lanegraph import build_lanegraph, _stage_plan, _wrap

FIXTURE = Path(__file__).parent / "fixtures" / "eixample_overpass.json"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)


@pytest.fixture(scope="module")
def model():
    profiles.activate("eu_dense")
    return build_lanegraph(load_fixture(FIXTURE), LocalFrame.from_bbox(*BBOX), BBOX,
                           name="eixample")


@pytest.fixture(scope="module")
def xodr(model):
    return etree.fromstring(export_xodr(model).encode())


# --------------------------------------------------------------------- the planner itself

def _headings(*degs):
    return [math.radians(d) for d in degs]


def test_four_way_two_way_junction_is_two_stages():
    """N/S and E/W: opposing approaches share a stage, crossing ones never do."""
    profiles.activate("eu_dense")
    stages = _stage_plan(_headings(0, 90, 180, 270))
    assert len(stages) == 2
    for st in stages:
        assert len(st) == 2
        a, b = st
        d = abs(_wrap(_headings(0, 90, 180, 270)[a] - _headings(0, 90, 180, 270)[b]))
        assert abs(d - math.pi) < math.radians(30)


def test_three_arm_junction_is_one_stage_per_approach():
    """A T-junction has an approach with no opposite, so pairing is abandoned wholesale --
    3 stages, exactly like stock Town10HD junction 23."""
    profiles.activate("eu_dense")
    assert len(_stage_plan(_headings(0, 90, 180))) == 3


def test_one_way_grid_is_one_stage_per_direction():
    """Eixample is one-way: a 4-arm crossing has 2 incoming directions, 90 degrees apart."""
    profiles.activate("eu_dense")
    stages = _stage_plan(_headings(0, 90))
    assert len(stages) == 2 and all(len(s) == 1 for s in stages)


def test_same_direction_approaches_share_a_stage():
    """The two carriageways of a divided street arrive on the same heading and never conflict,
    so they stage together with each other and with the opposing pair."""
    profiles.activate("eu_dense")
    # a divided E-W arterial (0/3 and 180/183 deg) crossing an undivided N-S street
    stages = _stage_plan(_headings(0, 3, 180, 183, 90, 270))
    assert len(stages) == 2
    arterial = next(st for st in stages if 0 in st)
    assert sorted(arterial) == [0, 1, 2, 3]
    assert sorted(next(st for st in stages if 4 in st)) == [4, 5]


def test_stage_count_is_capped():
    profiles.activate("eu_dense")
    stages = _stage_plan(_headings(0, 45, 100, 150, 200, 250))
    assert len(stages) <= profiles.get().junction.signal_max_stages
    assert sorted(i for st in stages for i in st) == list(range(6))


def test_single_approach_junction_keeps_one_stage():
    profiles.activate("eu_dense")
    assert _stage_plan(_headings(90)) == [[0]]


# ------------------------------------------------------------------- on the real fixture

def test_every_signalised_junction_has_at_least_one_stage(model):
    by_junction: dict[str, list] = {}
    for c in model.controllers:
        by_junction.setdefault(c.junction_id, []).append(c)
    assert by_junction, "no signalised junction in the fixture"
    for jid, ctls in by_junction.items():
        assert len(ctls) >= 1
        assert len(ctls) <= profiles.get().junction.signal_max_stages
        assert [c.sequence for c in sorted(ctls, key=lambda c: c.sequence)] == list(range(len(ctls)))
    # the point of the change: this is no longer one controller per junction everywhere
    assert max(len(v) for v in by_junction.values()) >= 2


def test_each_traffic_light_is_in_exactly_one_controller(model):
    seen: dict[str, str] = {}
    for c in model.controllers:
        for sid in c.signal_ids:
            assert sid not in seen, f"{sid} in {seen[sid]} and {c.id}"
            seen[sid] = c.id
    lights = [s for s in model.signals if s.kind == "traffic_light"]
    assert lights
    for s in lights:
        assert s.controller_id == seen[s.id]


def test_no_two_conflicting_approaches_share_a_stage(model):
    """Members of a stage are either parallel or opposed, never crossing."""
    tol = math.radians(profiles.get().junction.through_deg)
    heading = {s.id: s.heading for s in model.signals if s.kind == "traffic_light"}
    for c in model.controllers:
        hs = [heading[s] for s in c.signal_ids if s in heading]
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                d = abs(_wrap(hs[i] - hs[j]))
                assert d <= tol or abs(d - math.pi) <= tol, \
                    f"{c.id}: approaches {math.degrees(hs[i]):.0f} and " \
                    f"{math.degrees(hs[j]):.0f} deg conflict"


def test_junction_controller_refs_are_emitted_in_stage_order(xodr, model):
    """The junction ref is the only thing that fills Signal::GetControllers()
    (MapBuilder::SolveControllerAndJuntionReferences), and the runtime round-robins the refs
    in emission order because JunctionParser discards @sequence."""
    root_ids = {c.get("id") for c in xodr.findall("controller")}
    assert root_ids
    referenced = set()
    for j in xodr.iter("junction"):
        refs = [c.get("id") for c in j.findall("controller")]
        seqs = [int(c.get("sequence")) for c in j.findall("controller")]
        assert seqs == sorted(seqs), f"junction {j.get('id')}: refs out of stage order"
        for cid in refs:
            assert cid in root_ids, f"junction ref {cid} has no root <controller>"
        referenced.update(refs)
    assert referenced == root_ids, "every root controller must be referenced by its junction"


def test_placer_controller_map_rejects_a_signal_in_two_controllers(tmp_path, xodr):
    """ue/place_traffic_lights.py builds a flat {signal: controller} dict; it must not
    silently keep the last one."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ue"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ptl", Path(__file__).resolve().parents[1] / "ue" / "place_traffic_lights.py")
    # the module imports `unreal`; load only the function we need
    src = spec.origin
    ns: dict = {}
    text = Path(src).read_text()
    start = text.index("def controllers_from_xodr")
    end = text.index("def junctions_from_xodr")
    exec("import os\nimport xml.etree.ElementTree as ET\n" + text[start:end], ns)

    good = tmp_path / "good.xodr"
    good.write_bytes(etree.tostring(xodr))
    assert ns["controllers_from_xodr"](str(good))

    bad = tmp_path / "bad.xodr"
    bad.write_text('<OpenDRIVE>'
                   '<controller id="a"><control signalId="s1" type="0"/></controller>'
                   '<controller id="b"><control signalId="s1" type="0"/></controller>'
                   '</OpenDRIVE>')
    with pytest.raises(RuntimeError):
        ns["controllers_from_xodr"](str(bad))


# ------------------------------------------- protected turns (Phase 5) and pedestrian heads

SUNNYVALE = Path(__file__).parent / "fixtures" / "sunnyvale_overpass.json"
SV_BBOX = (37.369, -122.042, 37.374, -122.034)


def test_dedicated_turn_lane_needs_a_sibling_going_elsewhere():
    """A lane whose only movements are left turns is a *dedicated* turn lane -- but only if
    another lane of the same approach goes somewhere else. An approach that turns as a whole
    (a one-lane slip road) needs no arrow: its through signal already governs the movement."""
    from twinmodel.lanegraph import _dedicated_turn_lanes
    moves = {("r1", -1): {"left"}, ("r1", -2): {"through"}, ("r1", -3): {"through", "right"}}
    assert _dedicated_turn_lanes(moves, "r1", [-1, -2, -3], ("left",)) == [-1]
    # every lane turns left -> no arrow
    only = {("r1", -1): {"left"}, ("r1", -2): {"left"}}
    assert _dedicated_turn_lanes(only, "r1", [-1, -2], ("left",)) == []
    # a shared left+through lane is not dedicated
    shared = {("r1", -1): {"left", "through"}, ("r1", -2): {"through"}}
    assert _dedicated_turn_lanes(shared, "r1", [-1, -2], ("left",)) == []
    # two dedicated lanes side by side are one arrow signal over both
    two = {("r1", -1): {"left"}, ("r1", -2): {"left"}, ("r1", -3): {"through"}}
    assert _dedicated_turn_lanes(two, "r1", [-1, -2, -3], ("left",)) == [-1, -2]


@pytest.fixture(scope="module")
def us_model():
    """A US twin: unlike Eixample's one-way grid it has real dedicated left-turn lanes.

    The active profile is process-global, so it is restored on teardown -- otherwise every
    later test module would build with US curb heights and lane widths.
    """
    previous = profiles.get()
    profiles.activate("us_suburban")
    try:
        yield build_lanegraph(load_fixture(SUNNYVALE), LocalFrame.from_bbox(*SV_BBOX), SV_BBOX,
                              name="sunnyvale")
    finally:
        profiles.activate(previous)


def test_protected_turn_is_its_own_signal_on_its_own_lane(us_model):
    """One ATrafficLightBase carries one ETrafficLightState (TrafficLightBase.h:110) and every
    client/TM call is per actor, so a protected turn cannot be a second head of the through
    signal: it is a second <signal>, validated over exactly the dedicated lane(s)."""
    arrows = [s for s in us_model.signals if s.kind == "traffic_light_arrow"]
    assert arrows, "the US fixture must have at least one dedicated left-turn lane"
    by_road_through = {}
    for s in us_model.signals:
        if s.kind == "traffic_light":
            by_road_through.setdefault(s.road_id, []).append(s)
    for a in arrows:
        assert a.validities, f"{a.id} has no <validity>"
        arrow_lanes = {l for lo, hi in a.validities for l in range(lo, hi + 1)}
        assert arrow_lanes
        # disjoint from every through signal of the same approach: a lane may not carry two
        # trigger boxes with different states
        for t in by_road_through.get(a.road_id, []):
            if t.orientation != a.orientation:
                continue
            through_lanes = {l for lo, hi in t.validities for l in range(lo, hi + 1)}
            assert not (arrow_lanes & through_lanes), f"{a.id} and {t.id} share {arrow_lanes & through_lanes}"
        # and the arrow pole clears the through pole by more than the 50 cm radius
        # UMapLogicParser::ApplyLaneIdsFromMapLogic adopts within
        through = [t for t in by_road_through.get(a.road_id, []) if t.orientation == a.orientation]
        for t in through:
            assert abs(t.s - a.s) >= 0.6, f"{a.id} is {abs(t.s - a.s):.2f} m from {t.id}"


def test_protected_turn_gets_a_leading_stage_of_its_own(us_model):
    """The arrow's stage may hold no through movement -- that is the whole point of a
    protected turn -- and it runs *before* the through stage of its own approach."""
    ctl_of = {c.id: c for c in us_model.controllers}
    kind_of = {s.id: s.kind for s in us_model.signals}
    arrows = [s for s in us_model.signals if s.kind == "traffic_light_arrow"]
    assert arrows
    for a in arrows:
        assert a.controller_id, f"{a.id} is in no controller"
        ctl = ctl_of[a.controller_id]
        kinds = {kind_of[sid] for sid in ctl.signal_ids}
        assert "traffic_light" not in kinds, f"{ctl.id} mixes a protected turn with a through movement"
        through = next(s for s in us_model.signals
                       if s.kind == "traffic_light" and s.id == a.tags["through_signal"])
        assert ctl.sequence < ctl_of[through.controller_id].sequence, \
            f"{a.id} must lead {through.id}, not follow it"


def test_pedestrian_heads_are_type_1000002_over_sidewalk_lanes(model):
    """A pedestrian head is a phased prop: OpenDRIVE type 1000002, which CARLA does not know,
    so SignalType::IsTrafficLight is false, no ATrafficLightBase is generated and no client
    sees a traffic.traffic_light for it. Its <validity> names sidewalk lanes, which is also
    why UTrafficLightComponent::InitializeSign gives it no trigger box."""
    peds = [s for s in model.signals if s.kind == "traffic_light_ped"]
    crossings = [s for s in model.signals if s.kind == "crosswalk"]
    assert peds and len(peds) <= len(crossings)
    lanes_of = {(r.id): {l.id: l.type for l in r.lanes} for r in model.roads}
    for p in peds:
        assert p.validities, f"{p.id} has no <validity>"
        for lo, hi in p.validities:
            for lane in range(lo, hi + 1):
                assert lanes_of[p.road_id][lane] == "sidewalk", \
                    f"{p.id} validates {lane} which is {lanes_of[p.road_id][lane]}"


def test_pedestrian_head_walks_when_its_own_street_is_red(model):
    """The head is put in a stage that greens no approach of the street it crosses."""
    ctl_of = {c.id: c for c in model.controllers}
    # only the *vehicle* members of a stage matter: the other pedestrian heads in it are props
    veh_road = {s.id: s.road_id for s in model.signals
                if s.kind in ("traffic_light", "traffic_light_arrow")}
    peds = [s for s in model.signals if s.kind == "traffic_light_ped" and s.controller_id]
    assert peds
    for p in peds:
        green_roads = {veh_road[sid] for sid in ctl_of[p.controller_id].signal_ids
                       if sid in veh_road}
        assert p.road_id not in green_roads, \
            f"{p.id} crosses {p.road_id} in stage {p.controller_id}, which greens it"


def test_pedestrian_heads_do_not_change_the_vehicle_plan(model):
    """Adding pedestrian heads must not move a vehicle light into another stage."""
    veh = {s.id: s.controller_id for s in model.signals if s.kind == "traffic_light"}
    assert len(veh) == 27
    for c in model.controllers:
        veh_here = [s for s in c.signal_ids if s in veh]
        assert all(veh[s] == c.id for s in veh_here)
