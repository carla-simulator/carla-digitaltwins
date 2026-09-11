"""Immutable review, bounded fitting and fail-closed consumers; no builds/network."""

import copy
import json
from xml.etree import ElementTree as ET
import pytest
from shapely.geometry import LineString, Polygon, box, mapping
from shapely.ops import transform
from twinmodel.frame import LocalFrame
from twinmodel.model import Lane, Road, Surface, TwinModel
from twinmodel.reviewed_map import (
    fit_boundary,
    resolve_reviewed_map,
    apply_reviewed_geometry,
    apply_reviewed_logic,
)
from twinmodel.export.xodr import export_xodr


def model():
    m = TwinModel("review", 41, 2, (40, 1, 42, 3))
    m.roads = [Road("r1", LineString([(0, 0), (50, 0)]), [Lane(-1, "driving", 3.5)])]
    m.surfaces = [Surface("base", "drivable", box(0, -8, 50, 8))]
    return m


def wgs(g):
    return json.loads(
        json.dumps(mapping(transform(LocalFrame(41, 2)._to_wgs().transform, g)))
    )


def space(oid="p1", kind="parking", bounds=(5, 1, 15, 4)):
    x0, y0, x1, y1 = bounds
    return dict(
        id=oid,
        op="space.set",
        kind=kind,
        left=wgs(LineString([(x0, y1), (x1, y1)]))["coordinates"],
        right=wgs(LineString([(x0, y0), (x1, y0)]))["coordinates"],
        target={"road_id": "r1"},
    )


def crossing(kind="crosswalk"):
    return dict(
        id="c1",
        op="control.set",
        kind=kind,
        type="zebra" if kind == "crosswalk" else "marked",
        geometry=wgs(box(20, -4, 24, 4)),
        target={"road_id": "r1", "lane_ids": [-1]},
        relationships={"connects_to": ["p1", "p2"]},
    )


def test_bounded_smoothing():
    pts = [(0, 0), (4, 0.15), (8, 0), (12, 0.1)]
    f = fit_boundary(pts, 0.03)
    assert f == fit_boundary(pts, 0.03) and f[0] == pts[0] and f[-1] == pts[-1]
    assert LineString(pts).hausdorff_distance(LineString(f)) <= 0.03
    assert pts[1] in fit_boundary(pts, 0.03, pinned=[1])
    assert fit_boundary([(0, 0), (1, 0), (1, 1)]) == [(0, 0), (1, 0), (1, 1)]


def test_immutable_overlap_review():
    m = model()
    ops = [space(), space("p2", bounds=(10, 2, 18, 5))]
    before = copy.deepcopy(ops)
    r = resolve_reviewed_map(m, ops)
    assert ops == before and m.metadata == {} and not r["geometry_ready"]
    assert any(d["code"] == "space_overlap" for d in r["diagnostics"])
    with pytest.raises(ValueError):
        apply_reviewed_geometry(m, r)
    assert m.surfaces[0].geometry.area == 800


def test_legacy_and_unbound_block_export():
    op = space()
    op.update(replaces="lane:r1:-1:0", target={})
    r = resolve_reviewed_map(model(), [op])
    assert not r["geometry_ready"] and not r["logic_ready"]
    assert {"needs_source_coverage", "needs_road_binding"} <= {
        d["code"] for d in r["diagnostics"]
    }
    m = model()
    m.metadata["reviewed_map"] = r
    with pytest.raises(ValueError, match="not ready"):
        export_xodr(m)


def test_exact_parking_and_idempotence():
    m = model()
    r = resolve_reviewed_map(m, [space()])
    assert r["geometry_ready"] and r["logic_ready"], r["diagnostics"]
    apply_reviewed_geometry(m, r)
    apply_reviewed_logic(m, r)
    apply_reviewed_geometry(m, r)
    assert len(m.surfaces) == 2 and sum(
        s.geometry.area for s in m.surfaces
    ) == pytest.approx(800, abs=1e-5)
    obj = ET.fromstring(export_xodr(m)).find('.//object[@id="review:p1"]')
    assert obj.attrib["type"] == "parkingSpace"
    coords = [
        (
            float(c.attrib["u"]) + float(obj.attrib["s"]),
            float(c.attrib["v"]) + float(obj.attrib["t"]),
        )
        for c in obj.findall("./outline/cornerLocal")
    ]
    assert Polygon(coords).hausdorff_distance(box(5, 1, 15, 4)) < 1e-5
    with pytest.raises(ValueError, match="fresh"):
        apply_reviewed_geometry(m, resolve_reviewed_map(model(), [space("p2")]))


def test_crossing_requires_connections():
    op = crossing()
    op.pop("relationships")
    r = resolve_reviewed_map(model(), [op])
    assert not r["logic_ready"]
    assert any(d["code"] == "needs_crossing_connections" for d in r["diagnostics"])


def test_bike_crossing_not_pedestrian_trigger():
    m = model()
    m.roads[0].lanes[0].type = "biking"
    m.roads.append(
        Road("r2", LineString([(0, 6.5), (50, 6.5)]), [Lane(-1, "biking", 3.5)])
    )
    a = space("p1", "biking", (0, -3.5, 50, 0))
    a["target"]["lane_id"] = -1
    b = space("p2", "biking", (0, 3, 50, 6.5))
    b["target"] = {"road_id": "r2", "lane_id": -1}
    r = resolve_reviewed_map(m, [a, b, crossing("bike_crosswalk")])
    assert r["geometry_ready"] and r["logic_ready"], r["diagnostics"]
    apply_reviewed_geometry(m, r)
    apply_reviewed_logic(m, r)
    obj = ET.fromstring(export_xodr(m)).find('.//object[@id="review:c1"]')
    assert (
        obj.attrib["type"] == "roadMark"
        and json.loads(obj.find("userData").attrib["value"])["kind"] == "bike_crosswalk"
    )
    assert any(s.tags.get("crossing_kind") == "bike_crosswalk" for s in m.surfaces)


def test_lane_fit_and_bus_access():
    m = model()
    op = space(kind="bus", bounds=(0, -4, 50, 0))
    op["target"]["lane_id"] = -1
    r = resolve_reviewed_map(m, [op])
    assert r["logic_ready"], r["diagnostics"]
    apply_reviewed_geometry(m, r)
    apply_reviewed_logic(m, r)
    lane = ET.fromstring(export_xodr(m)).find(".//right/lane")
    assert lane.attrib["type"] == "driving" and float(
        lane.find("width").attrib["a"]
    ) == pytest.approx(4, abs=1e-6)
    assert (
        lane.find("access").attrib["restriction"] == "bus"
        and m.roads[0].lanes[0].type == "driving"
    )


def test_partial_and_curved_fit_blocked():
    m = model()
    op = space(kind="driving", bounds=(1, -3.5, 49, 0))
    op["target"]["lane_id"] = -1
    assert not resolve_reviewed_map(m, [op])["logic_ready"]
    m.roads[0].reference_line = LineString([(0, 0), (25, 2), (50, 0)])
    op = space(kind="driving", bounds=(0, -3.5, 50, 0))
    op["target"]["lane_id"] = -1
    assert not resolve_reviewed_map(m, [op])["logic_ready"]


def test_stop_line_creates_one_signal():
    m = model()
    op = dict(
        id="s1",
        op="control.set",
        kind="stop_line",
        type="stop",
        width_m=0.3,
        geometry=wgs(LineString([(25, -3.5), (25, 0)])),
        target={"road_id": "r1", "lane_ids": [-1]},
    )
    r = resolve_reviewed_map(m, [op])
    assert r["logic_ready"], r["diagnostics"]
    apply_reviewed_geometry(m, r)
    apply_reviewed_logic(m, r)
    apply_reviewed_logic(m, r)
    assert (
        len(m.signals) == 1
        and m.signals[0].kind == "stop"
        and m.signals[0].validities == [(-1, -1)]
    )
    assert (
        len(m.markings) == 1
        and ET.fromstring(export_xodr(m)).find(".//signals/signal") is not None
    )


def test_malformed_optional_fields():
    op = space()
    op["source_geometry"] = {"type": "Polygon", "coordinates": []}
    op["target"] = "invalid"
    r = resolve_reviewed_map(model(), [op])
    assert not r["geometry_ready"] and not r["logic_ready"]


def test_review_cli_readonly(tmp_path):
    from twinmodel.cli import main
    from twinmodel.corrections import Corrections

    model().save(tmp_path / "model")
    source = tmp_path / "edits.json"
    Corrections(name="review", ops=[space()]).save(source)
    before = source.read_bytes()
    meta = (tmp_path / "model/model.json").read_bytes()
    assert (
        main(
            [
                "review-annotations",
                str(tmp_path / "model"),
                str(source),
                "--out",
                str(tmp_path / "report"),
            ]
        )
        == 0
    )
    assert (
        source.read_bytes() == before
        and (tmp_path / "model/model.json").read_bytes() == meta
    )
    assert (tmp_path / "report/reviewed-map.json").exists()


def test_carla_reads_reviewed_lane_and_parking():
    carla = pytest.importorskip("carla")
    m = model()
    op = space("lane", "bus", (0, -4, 50, 0))
    op["target"]["lane_id"] = -1
    r = resolve_reviewed_map(m, [op, space()])
    assert r["geometry_ready"] and r["logic_ready"]
    apply_reviewed_geometry(m, r)
    apply_reviewed_logic(m, r)
    loaded = carla.Map("review", export_xodr(m))
    wp = loaded.get_waypoint(carla.Location(x=25, y=2, z=0), project_to_road=True)
    assert wp is not None and wp.lane_width == pytest.approx(4, abs=0.001)


def test_source_coverage_fill_preserves_area():
    m = model()
    op = space(bounds=(10, 1, 20, 4))
    op["source_geometry"] = wgs(box(5, 1, 15, 4))
    r = resolve_reviewed_map(m, [op])
    assert not r["geometry_ready"]
    op["replacement_kind"] = "drivable"
    r = resolve_reviewed_map(m, [op])
    assert r["geometry_ready"]
    apply_reviewed_geometry(m, r)
    assert sum(s.geometry.area for s in m.surfaces) == pytest.approx(800, abs=1e-5)
    assert any(s.id == "review:remainder:p1" for s in m.surfaces)


def test_wrong_crossing_connection_type_rejected():
    r = resolve_reviewed_map(
        model(), [space(), space("p2", bounds=(30, 1, 40, 4)), crossing()]
    )
    assert not r["logic_ready"]
    assert any(d["code"] == "invalid_crossing_connections" for d in r["diagnostics"])


def test_mesh_export_after_marking_split(tmp_path):
    from twinmodel.model import Marking
    from twinmodel.export.mesh import export_obj

    m = model()
    m.markings = [Marking(kind="broken", geometry=LineString([(0, 2), (50, 2)]))]
    r = resolve_reviewed_map(m, [space()])
    apply_reviewed_geometry(m, r)
    assert len(m.markings) == 2 and all(
        mk.geometry.geom_type == "LineString" for mk in m.markings
    )
    export_obj(m, tmp_path / "synthetic.obj")
    assert (tmp_path / "synthetic.obj").stat().st_size > 0
