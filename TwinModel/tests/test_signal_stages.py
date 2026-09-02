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
