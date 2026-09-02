"""refresh-signals: the pure parts (build-arg recovery, geometry comparator, xodr stats, commands)."""
from __future__ import annotations

from pathlib import Path

import pytest

from twinmodel.refresh import (build_argv_from_report, chained_shell, editor_commands, geometry_identical,
                               graft_signals, strip_signals, topology_identical, xodr_stats)

REPORT = {"build": {"args": {"verbose": False, "quiet": False, "cmd": "build",
                             "bbox": [37.779, -122.408, 37.784, -122.4], "name": "sf_soma", "out": "out/v8_soma",
                             "fixture": "tests/fixtures/sf_soma_overpass.json", "cache": "data",
                             "no_imagery": False, "no_dem": False, "no_refine": True, "mask_method": "classical",
                             "profile": "us_urban", "step": 1.0, "junction_zooms": 3}}}

XODR = """<?xml version="1.0"?>
<OpenDRIVE><header revMajor="1" revMinor="4" date="2026-09-02T10:00:00" name="x"/>
<road id="1" length="10" junction="-1"><planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView>
<lanes><laneSection s="0"><right><lane id="-1" type="driving"/></right></laneSection></lanes>
<signals>%s</signals></road>
%s
<junction id="7"><connection id="0" incomingRoad="1" connectingRoad="2"/>%s</junction>
</OpenDRIVE>"""


def _x(signals="", controllers="", jrefs=""):
    return XODR % (signals, controllers, jrefs)


def test_build_argv_round_trips_the_recorded_args():
    argv = build_argv_from_report(REPORT, "out/v8_soma_refresh")
    assert argv[:3] == ["build", "--out", "out/v8_soma_refresh"]
    assert argv[argv.index("--bbox") + 1: argv.index("--bbox") + 5] == ["37.779", "-122.408", "37.784", "-122.4"]
    assert "--no-refine" in argv and "--no-imagery" not in argv and "--no-dem" not in argv
    assert argv[argv.index("--fixture") + 1] == "tests/fixtures/sf_soma_overpass.json"
    assert argv[argv.index("--cache") + 1] == "data"
    assert argv[argv.index("--profile") + 1] == "us_urban"
    assert argv[argv.index("--step") + 1] == "1.0"
    assert argv[argv.index("--junction-zooms") + 1] == "3"
    assert "--out" not in argv[3:]  # the recorded out dir is replaced, never reused


def test_build_argv_rejects_reports_without_build_args():
    with pytest.raises(ValueError):
        build_argv_from_report({"build": {}}, "x")


def test_geometry_comparator_ignores_signals_controllers_and_header():
    a = _x('<signal id="s1" s="1" t="-2" type="1000001" orientation="+"/>',
           '<controller id="c1"><control signalId="s1"/></controller>', '<controller id="c1"/>')
    b = _x('<signal id="s1" s="1" t="-2" type="1000001" orientation="+"><validity fromLane="-1" toLane="-1"/></signal>'
           '<signalReference id="s1" s="5" t="-2" orientation="+"/>',
           '<controller id="c1_p0" sequence="0"><control signalId="s1"/></controller>'
           '<controller id="c1_p1" sequence="1"><control signalId="s2"/></controller>',
           '<controller id="c1_p0"/><controller id="c1_p1"/>').replace("2026-09-02T10:00:00", "2026-09-03T11:11:11")
    assert geometry_identical(a, b)
    assert strip_signals(a) == strip_signals(b)


def test_geometry_comparator_catches_geometry_and_lane_changes():
    a = _x()
    assert not geometry_identical(a, a.replace('length="10"', 'length="11"'))
    assert not geometry_identical(a, a.replace('type="driving"', 'type="sidewalk"'))
    assert not geometry_identical(a, a.replace('connectingRoad="2"', 'connectingRoad="3"'))


def test_xodr_stats_counts_validities_controllers_and_types():
    x = _x('<signal id="s1" s="1" t="-2" type="1000001" orientation="+"><validity fromLane="-2" toLane="-1"/></signal>'
           '<signal id="s2" s="2" t="-2" type="274" subtype="30" orientation="+"/>'
           '<signal id="s3" s="3" t="-2" type="206" orientation="+"><validity fromLane="-1" toLane="-1"/></signal>',
           '<controller id="c1_p0"><control signalId="s1"/></controller><controller id="c1_p1"><control signalId="s3"/></controller>',
           '<controller id="c1_p0"/><controller id="c1_p1"/>')
    st = xodr_stats(x)
    assert st["signals"] == 3 and st["validities"] == 2 and st["controllers"] == 2
    assert st["by_type"] == {"1000001": 1, "206": 1, "274": 1}
    assert st["controllers_per_junction"] == {"7": 2}
    assert st["signal_references"] == 0


def test_graft_keeps_geometry_and_signal_positions_but_takes_validities_and_controllers():
    deployed = _x('<signal id="s1" s="9.0" t="-2" type="1000001" orientation="+"/>',
                  '<controller id="c7"><control signalId="s1"/></controller>', '<controller id="c7"/>')
    # the rebuild moved the reference line (length 11, signal at s=10) but has the same topology
    rebuilt = _x('<signal id="s1" s="10.0" t="-2" type="1000001" orientation="+" name="tl"><validity fromLane="-1" toLane="-1"/></signal>',
                 '<controller id="c7_p0" sequence="0"><control signalId="s1"/></controller>'
                 '<controller id="c7_p1" sequence="1"><control signalId="s1"/></controller>',
                 '<controller id="c7_p0"/><controller id="c7_p1"/>').replace('length="10"', 'length="11"')
    assert not geometry_identical(deployed, rebuilt) and topology_identical(deployed, rebuilt)
    out, st_graft = graft_signals(deployed, rebuilt)
    assert geometry_identical(deployed, out)
    st = xodr_stats(out)
    assert st["validities"] == 1 and st["controllers"] == 2 and st["controllers_per_junction"] == {"7": 2}
    assert 's="9.0"' in out and 's="10.0"' not in out  # the deployed s/t survive
    assert 'name="tl"' in out
    assert st_graft["matched"] == 1 and st_graft["added"] == 0 and st_graft["max_s_shift"] == 1.0


def test_graft_renumbers_matched_signals_and_adds_new_ones_next_to_their_anchor():
    deployed = _x('<signal id="sig1" s="9.0" t="-2" type="1000001" orientation="+"/>')
    # the rebuild renumbered the through light (sig1 -> sig7), moved it by 1 m, and added an arrow at
    # the same stop line plus a pedestrian head 3 m before it; controllers speak the new ids
    rebuilt = _x('<signal id="sig7" s="10.0" t="-2" type="1000001" orientation="+"><validity fromLane="-1" toLane="-1"/></signal>'
                 '<signal id="sig8" s="10.0" t="-2" type="1000001" subtype="left" orientation="+" name="arrow"/>'
                 '<signal id="sig9" s="7.0" t="-3" type="1000002" orientation="+"/>',
                 '<controller id="c7_p0"><control signalId="sig7"/><control signalId="sig8"/></controller>',
                 '<controller id="c7_p0"/>').replace('length="10"', 'length="11"')
    out, st = graft_signals(deployed, rebuilt)
    assert geometry_identical(deployed, out)
    assert st == {"matched": 1, "added": 2, "added_by_anchor": 2, "added_by_end_offset": 0, "max_s_shift": 1.0, "clamped": 0}
    assert 'id="sig1"' not in out and 'id="sig7" s="9.0"' in out
    assert 'id="sig8" s="9.0000"' in out          # arrow: same stop line as its through light
    assert 'id="sig9" s="6.0000"' in out          # ped head: 3 m before the anchor, on the old s
    assert xodr_stats(out)["by_type"] == {"1000001": 2, "1000002": 1}


def test_graft_refuses_when_a_deployed_signal_has_no_counterpart():
    deployed = _x('<signal id="sig1" s="9.0" t="-2" type="1000001" orientation="+"/>')
    rebuilt = _x('<signal id="sig1" s="1.0" t="-2" type="1000001" orientation="-"/>')
    with pytest.raises(ValueError, match="no rebuilt counterpart"):
        graft_signals(deployed, rebuilt)


def test_graft_refuses_a_different_topology():
    deployed = _x('<signal id="s1" s="9.0" t="-2" type="1000001" orientation="+"/>')
    other = deployed.replace('type="driving"', 'type="sidewalk"')
    assert not topology_identical(deployed, other)
    with pytest.raises(ValueError):
        graft_signals(deployed, other)


def test_editor_commands_chain_lights_then_signs_for_one_lock():
    cmds = editor_commands("Sf_Soma", Path("/tmp/b"), "na", "/tmp/rigs.json", Path("/p.uproject"), Path("/ue"))
    assert [c[0] for c in cmds] == ["/ue", "/ue"] and all(c[1] == "/p.uproject" for c in cmds)
    assert "place_traffic_lights.py" in cmds[0][3] and "place_traffic_signs.py" in cmds[1][3]
    assert "--rig-map /tmp/rigs.json" in cmds[0][3] and "--style na" in cmds[0][3]
    assert "/tmp/b/ue/tl_signals.json" in cmds[0][3] and "/tmp/b/ue/sign_signals.json" in cmds[1][3]
    assert all("-unattended" in c for c in cmds)
    sh = chained_shell(cmds)
    assert sh.count(" && ") == 1 and sh.index("place_traffic_lights") < sh.index("place_traffic_signs")
