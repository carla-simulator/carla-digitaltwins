"""Profile-driven lane graph on the cached Eixample fixture (no network): the EU_DENSE build is
pinned by a checksum computed before the regional constants moved to ``twinmodel.profiles``;
the US profiles must change the cross sections in the documented ways on the same fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from twinmodel import profiles
from twinmodel.frame import LocalFrame
from twinmodel.ingest.osm import load_fixture
from twinmodel.lanegraph import build_lanegraph, lanes_for_way

FIXTURE = Path(__file__).parent / "fixtures" / "eixample_overpass.json"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)
FT = 0.3048
CARRIAGEWAY = ("driving", "parking", "biking", "shoulder")

# sha256 over ((road id, [(lane id, type, width in mm)]) for every road incl. connecting roads,
# (junction id, connection count) per junction). History of deliberate re-pins:
# - e7c5b0d5edc1... / (194, 15, 129): 2026-09-01, the lane graph BEFORE its constants moved to
#   twinmodel.profiles.
# - 603eb47de332... / (195, 15, 129): 2026-09-01 ambulance pass — EU_DENSE residential /
#   living_street / pedestrian lanes 3.0 -> 3.3 m, service 3.0 -> 3.25 m, min_width (taper
#   floor) 2.75 -> 3.0 m: a 2.3 m ambulance could not hold the old lanes. The extra road is a
#   taper piece split at the new floor; junctions and connections are unchanged.
# - d1a5dd259e6d... / (194, 15, 129): 2026-09-02 second ambulance pass — every EU_DENSE urban
#   class at 3.5-3.6 m, min_width (taper floor) 3.0 -> 3.3 m, canyon_max_width 3.5 -> 3.8 m,
#   max_width 3.75 -> 4.0 m (CARLA's ambulance is 2.35 m wide, the fire truck 2.90 m). One
#   taper piece fewer (the wider floor no longer splits it); junctions and connections unchanged.
# - 00e2ee5d4009... / (194, 15, 129): 2026-09-02 corner fillets — every interior corner of a
#   reference line is rounded with an arc (GeometryRules.fillet_radius_m 30 m, capped by the
#   legs), so the laterals of Passeig de Gracia no longer saw-tooth at the OSM jog nodes;
#   trimmed lines simplified at 3 cm instead of 10 cm. Lane widths/links unchanged except where
#   a rounded line changed a taper split; junctions and connections unchanged.
# EU_DENSE must reproduce the pin exactly: a different value means the default build changed
# behaviour, not just its plumbing.
EU_DENSE_CHECKSUM = "00e2ee5d40096ff29c0b42056938fc59cce7eb2fb549850b9186035d8bea7cd8"
EU_DENSE_COUNTS = (194, 15, 129)  # roads (incl. connecting), junctions, connections


def _digest(model) -> tuple[str, tuple[int, int, int]]:
    roads = sorted(model.roads, key=lambda r: r.id)
    payload = {
        "roads": [(r.id, [(l.id, l.type, int(round(l.width * 1000))) for l in r.lanes]) for r in roads],
        "junctions": [(j.id, len(j.connections)) for j in sorted(model.junctions, key=lambda j: j.id)],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    counts = (len(payload["roads"]), len(payload["junctions"]), sum(n for _, n in payload["junctions"]))
    return hashlib.sha256(blob.encode()).hexdigest(), counts


@pytest.fixture(scope="module")
def osm():
    return load_fixture(FIXTURE)


def _build(osm, profile: str):
    with profiles.use(profile):
        return build_lanegraph(osm, LocalFrame.from_bbox(*BBOX), BBOX, name="eixample")


@pytest.fixture(scope="module")
def eu(osm):
    return _build(osm, "eu_dense")


@pytest.fixture(scope="module")
def suburban(osm):
    return _build(osm, "us_suburban")


@pytest.fixture(scope="module")
def urban(osm):
    return _build(osm, "us_urban")


SUNNYVALE_FIXTURE = Path(__file__).parent / "fixtures" / "sunnyvale_overpass.json"
SUNNYVALE_BBOX = (37.3690, -122.0420, 37.3740, -122.0340)


@pytest.fixture(scope="module")
def sunnyvale():
    """A real US suburban area (El Camino Real x S Mathilda Ave): the Eixample fixture has
    almost no two-way arterial left once the US cross sections are applied, so the US-specific
    street furniture is checked where it actually occurs."""
    osm_us = load_fixture(SUNNYVALE_FIXTURE)
    with profiles.use("us_suburban"):
        return build_lanegraph(osm_us, LocalFrame.from_bbox(*SUNNYVALE_BBOX), SUNNYVALE_BBOX,
                               name="sunnyvale")


def _plain(model):
    return [r for r in model.roads if r.junction_id is None]


def _twoway(road) -> bool:
    return not road.tags.get("oneway_road")


def _way_tags(osm, road) -> dict:
    out: dict = {}
    for wid in road.osm_way_ids:
        out.update(osm.way(wid).tags)
    return out


# --------------------------------------------------------------------------- (a) EU regression

def test_eu_dense_reproduces_the_pre_profile_build(eu):
    digest, counts = _digest(eu)
    assert counts == EU_DENSE_COUNTS
    assert digest == EU_DENSE_CHECKSUM
    assert eu.metadata["profile"] == "eu_dense"
    assert eu.metadata["lanegraph"]["profile"] == "eu_dense"
    # the EU cross sections carry no verge and only white markings; crossings are 4 m
    assert not any(l.type == "verge" for r in eu.roads for l in r.lanes)
    colours = {l.marking.color for r in eu.roads for l in r.lanes if l.marking}
    colours |= {r.center_marking.color for r in eu.roads if r.center_marking}
    assert colours == {"white"}
    crossings = [s for s in eu.signals if s.kind == "crosswalk"]
    assert crossings and all(s.tags["width"] == pytest.approx(4.0) for s in crossings)


def test_default_profile_is_eu_dense_and_switching_is_scoped():
    assert profiles.get().name == "eu_dense"
    with profiles.use("us_suburban"):
        assert profiles.get().name == "us_suburban"
        assert lanes_for_way({}, "residential").center_marking is None
    assert profiles.get().name == "eu_dense"
    spec = lanes_for_way({}, "residential")
    assert spec.center_marking is not None and spec.center_marking.color == "white"
    assert [l.type for l in spec.lanes] == ["sidewalk", "driving", "driving", "sidewalk"]


# --------------------------------------------------------------------------- (b) US suburban

def test_us_suburban_residential_parking_both_sides_and_no_centre_line(osm, suburban):
    res = [r for r in _plain(suburban) if r.highway == "residential"]
    assert len(res) >= 5
    silent = [r for r in res if not any(k.startswith("parking") for k in _way_tags(osm, r))]
    assert len(silent) >= 4
    for r in silent:
        if r.tags.get("dual_carriageway"):
            # a carriageway of a divided arterial (Rambla de Catalunya) has no curb on the
            # median side: no parking there, a median lane instead
            assert not any(l.type == "parking" and l.id > 0 for l in r.lanes), r.id
            assert any(l.type == "median" and l.id > 0 for l in r.lanes), r.id
            continue
        assert any(l.type == "parking" and l.id > 0 for l in r.lanes), r.id
        assert any(l.type == "parking" and l.id < 0 for l in r.lanes), r.id
    assert any(_twoway(r) for r in res)
    for r in res:
        if _twoway(r):
            assert r.center_marking is None, r.id
        else:  # a oneway's reference line is its left carriageway edge: an edge line, white
            assert r.center_marking is not None and r.center_marking.color == "white", r.id
    with profiles.use("us_suburban"):
        spec = lanes_for_way({}, "residential")
        assert spec.center_marking is None
        assert [l.type for l in spec.lanes] == ["sidewalk", "verge", "parking", "driving", "driving",
                                                "parking", "verge", "sidewalk"]
        assert all(l.width == pytest.approx(8 * FT) for l in spec.lanes if l.type == "parking")
        assert all(l.width == pytest.approx(11 * FT) for l in spec.lanes if l.type == "driving")
        # tags always win over the class default
        assert not any(l.type == "parking" for l in lanes_for_way({"parking:both": "no"}, "residential").lanes)
        one_side = lanes_for_way({"parking:right": "no"}, "residential").lanes
        assert not any(l.type == "parking" for l in one_side)  # any parking:* tag silences the default


def test_us_suburban_centre_lines_are_yellow_from_tertiary_up(suburban, sunnyvale):
    # Eixample keeps a single two-way street once the US cross sections are applied (and the
    # divided-arterial model removed the 1 m Passeig de Gracia remnants that used to stand in
    # for one), so the arterial check runs on the Sunnyvale fixture.
    tert = [r for r in _plain(sunnyvale) if _twoway(r)
            and r.highway in ("tertiary", "secondary", "primary", "trunk")]
    assert len(tert) >= 3
    for r in tert:
        assert r.center_marking is not None, r.id
        assert (r.center_marking.kind, r.center_marking.color) == ("solid", "yellow"), r.id
    for r in _plain(suburban):  # never a white centre line on a two-way road
        if _twoway(r):
            assert r.center_marking is None or r.center_marking.color == "yellow", r.id
    with profiles.use("us_suburban"):
        for hw in ("tertiary", "secondary", "primary"):
            assert lanes_for_way({}, hw).center_marking.color == "yellow", hw
            drv = [l for l in lanes_for_way({"lanes": "4"}, hw).lanes if l.type == "driving"]
            assert {l.marking.color for l in drv} == {"white"}  # lane and edge lines stay white
            assert {l.marking.kind for l in drv} == {"broken", "solid"}


def test_us_suburban_crossings_are_10_ft(suburban):
    crossings = [s for s in suburban.signals if s.kind == "crosswalk"]
    assert len(crossings) >= 50
    for s in crossings:
        if "crossing:width" in s.tags:
            continue  # tagged widths win
        assert s.tags["width"] == pytest.approx(10 * FT, abs=1e-6), s.id  # 3.048 m
    assert profiles.by_name("us_suburban").crossing.width == pytest.approx(3.048, abs=1e-3)


def test_us_suburban_no_driving_lane_under_10_ft(suburban):
    widths = [l.width for r in suburban.roads for l in r.lanes if l.type == "driving"]
    assert widths and min(widths) >= 10 * FT - 1e-9
    assert any(w == pytest.approx(12 * FT) for w in widths)  # arterial lanes
    with profiles.use("us_suburban"):
        # a 5 m tagged oneway carriageway cannot squeeze two lanes below 10 ft each
        spec = lanes_for_way({"oneway": "yes", "lanes": "2", "width": "5"}, "residential")
        assert all(l.width >= 10 * FT - 1e-9 for l in spec.lanes if l.type == "driving")


# --------------------------------------------------------------------------- (c) US urban

def test_us_urban_verge_sits_between_carriageway_and_sidewalk(urban, eu):
    res = [r for r in _plain(urban) if r.highway == "residential"]
    with_verge = [r for r in res if any(l.type == "verge" for l in r.lanes)]
    assert len(with_verge) >= 5, [r.id for r in res]
    n_verge = 0
    for r in _plain(urban):
        for side in (r.lanes_left(), r.lanes_right()):  # inner -> outer
            types = [l.type for l in side]
            for i, t in enumerate(types):
                if t != "verge":
                    continue
                n_verge += 1
                assert i + 1 < len(types) and types[i + 1] == "sidewalk", (r.id, types)
                assert all(x in CARRIAGEWAY for x in types[:i]), (r.id, types)
            assert types.count("verge") <= 1, (r.id, types)
        # the verge is not carriageway: Road.width_left/right ignore it
        assert r.width_left() == pytest.approx(sum(l.width for l in r.lanes if l.id > 0 and l.type in CARRIAGEWAY))
        assert r.width_right() == pytest.approx(sum(l.width for l in r.lanes if l.id < 0 and l.type in CARRIAGEWAY))
    assert n_verge >= 10
    with profiles.use("us_urban"):
        spec = lanes_for_way({}, "residential")
        assert [l.type for l in spec.lanes] == ["sidewalk", "verge", "parking", "driving", "driving",
                                                "parking", "verge", "sidewalk"]
        assert all(l.width == pytest.approx(4 * FT) for l in spec.lanes if l.type == "verge")
        assert not any(l.type == "verge" for l in lanes_for_way({}, "primary").lanes)
        assert not any(l.type in ("verge", "sidewalk")
                       for l in lanes_for_way({"sidewalk": "no"}, "residential").lanes)
    assert not any(l.type == "verge" for r in eu.roads for l in r.lanes)


def test_us_profiles_build_a_sane_graph(suburban, urban):
    for m in (suburban, urban):
        assert _digest(m)[0] != EU_DENSE_CHECKSUM
        assert m.metadata["profile"] in ("us_suburban", "us_urban")
        assert 10 <= len(m.junctions) <= 25
        # movements per junction, not a raw total: merging the junctions on either side of a
        # 1 m sliver (junction.sliver_m) legitimately removes junctions and their duplicates
        n_conn = sum(len(j.connections) for j in m.junctions)
        assert n_conn >= 8 * len(m.junctions), (n_conn, len(m.junctions))
        assert m.metadata["lanegraph"]["restrictions_unresolved"] == 0
        for r in m.roads:
            assert any(l.type == "driving" for l in r.lanes), r.id
            ids = [l.id for l in r.lanes]
            assert 0 not in ids and ids == sorted(ids, reverse=True), r.id
            assert r.length >= 1.0 - 1e-6, (r.id, r.length)  # cuts may land exactly on min_road_length
