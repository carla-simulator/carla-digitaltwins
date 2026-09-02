"""``tools/region_review_page.py``: payload extraction, coordinate flip, rounding, size budget.

No ``unreal``/``carla`` import: the generator is pure geojson -> JSON -> HTML.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "region_review_page.py"


def _load():
    spec = importlib.util.spec_from_file_location("region_review_page", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load()


# --------------------------------------------------------------------------------- primitives

def test_model_to_carla_flips_y_only():
    assert rr.model_to_carla_xy(12.5, 30.0) == (12.5, -30.0)
    assert rr.model_to_carla_xy(-4.0, -7.25) == (-4.0, 7.25)


def test_quantize_rounds_to_5_cm():
    assert rr.QUANT == 0.05
    assert rr.quantize(0.0) == 0
    assert rr.quantize(0.049) == 1          # 0.05 m
    assert rr.quantize(1.0) == 20
    assert rr.quantize(-1.0) == -20
    assert rr.quantize(123.4567) * rr.QUANT == pytest.approx(123.45, abs=0.026)


def test_simplify_line_drops_duplicates_and_collinear_vertices():
    line = [[0, 0], [1, 0], [1, 0], [2, 0], [3, 0]]      # all on one straight run
    assert rr.simplify(line, closed=False) == [0, 0, 60, 0]


def test_simplify_line_keeps_a_real_corner():
    flat = rr.simplify([[0, 0], [5, 0], [5, 5]], closed=False)
    assert flat == [0, 0, 100, 0, 100, 100]


def test_simplify_ring_unwraps_the_repeated_first_vertex():
    ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    flat = rr.simplify(ring, closed=True)
    assert len(flat) == 8                                # 4 corners, not 5
    assert flat[:2] != flat[-2:]


def test_simplify_rejects_degenerate_geometry():
    assert rr.simplify([[1, 1], [1, 1]], closed=False) == []
    assert rr.simplify([[0, 0], [1, 0], [2, 0]], closed=True) == []   # zero-area ring


def test_simplify_quantises_to_the_grid():
    flat = rr.simplify([[0.0, 0.0], [0.03, 4.0], [0.07, 8.0]], closed=False)
    assert all(isinstance(v, int) for v in flat)
    # 0.03 and 0.07 both snap to the same 0.05 m column; nothing lands off the grid
    assert sorted({round(v * rr.QUANT, 6) for v in flat[::2]}) == [0.0, 0.05]
    assert [v * rr.QUANT for v in flat[1::2]] == [0.0, pytest.approx(4.0), pytest.approx(8.0)]


# ---------------------------------------------------------------------------- synthetic twin

def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _poly(ring, props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _line(coords, props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": coords}}


def _pt(xy, props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": list(xy)}}


SQ = [[0, 0], [40, 0], [40, 40], [0, 40], [0, 0]]


@pytest.fixture()
def twin(tmp_path) -> Path:
    d = tmp_path / "toy.twin"
    d.mkdir()
    (d / "model.json").write_text(json.dumps(
        {"schema": "0.1", "name": "toy", "origin_lat": 37.5, "origin_lon": -122.0,
         "geo_reference": "+proj=tmerc"}))
    surf = []
    for i, kind in enumerate(rr.SURFACE_KINDS):
        off = i * 100
        surf.append(_poly([[x + off, y] for x, y in SQ], {"id": f"s{i}", "kind": kind,
                                                          "road_ids": ["r1"], "junction_id": None}))
    # a polygon with a hole, and a kind the page does not draw
    holed = {"type": "Feature", "properties": {"kind": "drivable", "id": "sh"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0, 200], [60, 200], [60, 260], [0, 260], [0, 200]],
                                          [[10, 210], [20, 210], [20, 220], [10, 220], [10, 210]]]}}
    surf.append(holed)
    surf.append(_poly(SQ, {"kind": "tunnel_wall", "id": "sx"}))
    (d / "surfaces.geojson").write_text(json.dumps(_fc(surf)))
    (d / "buildings.geojson").write_text(json.dumps(_fc(
        [_poly([[x, y + 300] for x, y in SQ], {"id": "b1", "height": 12.0})])))
    (d / "markings.geojson").write_text(json.dumps(_fc([
        _line([[0, 0], [50, 0]], {"kind": "solid", "color": "white"}),
        _line([[0, 5], [50, 5]], {"kind": "broken", "color": "yellow"}),
        _line([[0, 9], [50, 9]], {"kind": "solid", "color": "blue"}),      # unknown bucket
    ])))
    (d / "curbs.geojson").write_text(json.dumps(_fc(
        [_line([[0, 1], [40, 1], [40, 41]], {"id": "c0", "height": 0.15})])))
    (d / "roads.geojson").write_text(json.dumps(_fc(
        [_line([[0, -20], [80, -20]], {"id": "r1"})])))
    (d / "signals.geojson").write_text(json.dumps(_fc([
        _pt((10.02, 20.03), {"id": "sig1", "kind": "traffic_light", "road_id": "r1",
                             "controller_id": "ctl1", "value": None}),
        _pt((12, 22), {"id": "sig2", "kind": "speed_limit", "road_id": "r1", "value": 40}),
        _pt((14, 24), {"id": "sig3", "kind": "stop", "road_id": "r1", "value": None}),
        _pt((16, 26), {"id": "sig4", "kind": "yield", "road_id": "r1", "value": None}),
        _pt((18, 28), {"id": "sig5", "kind": "crosswalk", "road_id": "r1", "value": None}),
    ])))
    (d / "objects.geojson").write_text(json.dumps(_fc([
        _pt((5, 5), {"id": "tree1", "kind": "tree"}),
        _pt((6, 7), {"id": "tree2", "kind": "tree"}),
        _pt((9, 9), {"id": "ts1", "kind": "traffic_sign"}),
    ])))
    (d / "junctions.geojson").write_text(json.dumps(_fc([
        _poly(SQ, {"id": "j1", "has_polygon": True, "tags": {"centre": [20.0, 20.0]}}),
        _poly([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], {"id": "j2", "has_polygon": True, "tags": {}}),
    ])))
    return d


def test_extract_has_every_surface_kind_layer(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    for kind in rr.SURFACE_KINDS:
        assert kind in p["layers"], kind
        assert p["layers"][kind], f"{kind} came back empty"
    assert p["name"] == "Toy" and p["carla_map"] == "ToyLevel"
    assert p["origin"] == {"lat": 37.5, "lon": -122.0}
    assert p["scale"] == rr.QUANT


def test_extract_drops_kinds_the_page_cannot_draw(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    assert "tunnel_wall" not in p["layers"]
    # the drivable layer keeps its own square plus the holed polygon (exterior + interior ring)
    assert len(p["layers"]["drivable"]) == 2
    assert [len(f) for f in p["layers"]["drivable"]] == [1, 2]


def test_extract_polygon_rings_are_flat_int_arrays(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    ring = p["layers"]["ground"][0][0]
    assert all(isinstance(v, int) for v in ring)
    assert len(ring) == 8                                    # square, unwrapped
    xs = [v * p["scale"] for v in ring[::2]]
    assert min(xs) == 0.0 and max(xs) == 40.0


def test_extract_buckets_markings_and_ignores_unknown_colours(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    mk = p["layers"]["markings"]
    assert set(mk) == set(rr.MARK_BUCKETS.values())
    assert len(mk["sw"]) == 1 and len(mk["by"]) == 1
    assert not mk["sy"] and not mk["bw"]


def test_extract_signals_carry_opendrive_types(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    by_id = {s["id"]: s for s in p["layers"]["signals"]}
    assert by_id["sig1"]["t"] == "1000001" and by_id["sig1"]["c"] == "ctl1"
    assert by_id["sig2"]["t"] == "274" and by_id["sig2"]["s"] == "40"     # subtype = km/h
    assert by_id["sig3"]["t"] == "206"
    assert by_id["sig4"]["t"] == "205"
    assert by_id["sig5"]["k"] == "crosswalk"
    # the signal position is quantised, not raw
    assert by_id["sig1"]["x"] == rr.quantize(10.02)
    assert by_id["sig1"]["x"] * p["scale"] == pytest.approx(10.0)


def test_extract_splits_objects_into_trees_and_signs(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    assert p["counts"]["trees"] == 2
    assert len(p["layers"]["trees"]) == 4                     # flat x,y pairs
    assert any(s["k"] == "traffic_sign" and s["id"] == "ts1" for s in p["layers"]["signals"])


def test_extract_junction_centroids(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    j = {x["id"]: x for x in p["junctions"]}
    assert set(j) == {"j1", "j2"}
    assert [j["j1"]["x"] * p["scale"], j["j1"]["y"] * p["scale"]] == [20.0, 20.0]
    # j2 has no `centre` tag -> centroid of its exterior ring
    assert j["j2"]["x"] * p["scale"] == pytest.approx(4.0, abs=2.1)


def test_bounds_and_carla_bounds_are_the_y_flip(twin):
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    minx, miny, maxx, maxy = p["bounds"]
    assert minx <= 0 and maxy >= 340
    assert p["bounds_carla"] == [minx, -maxy, maxx, -miny]
    assert p["centre_carla"] == [p["centre"][0], -p["centre"][1]]


# ------------------------------------------------------------------------------------- page

def test_page_is_artifact_content_not_a_document(twin):
    html = rr.build_page([rr.extract_twin(twin, "Toy", "ToyLevel")])
    low = html.lower()
    assert "<!doctype" not in low
    # `<header>` is fine; a document wrapper tag is not
    assert not re.search(r"</?(html|head|body)\s*>", low)
    assert html.startswith("<title>Twin Region Review</title>")
    assert '<script type="application/json" data-map="ToyLevel">' in html
    # theme tokens must exist on bare :root, not only inside a media/theme block
    root = html.split(":root{", 1)[1].split("}", 1)[0]
    assert "--bg:" in root and "--l-drivable:" in root and "--canvas:" in root
    assert '@media (prefers-color-scheme: dark){:root:not([data-theme="light"])' in html
    assert ':root[data-theme="dark"]' in html
    # only the two allowed external origins
    for token in ("http://", "https://"):
        for chunk in html.split(token)[1:]:
            host = chunk.split("/")[0].split('"')[0]
            assert host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com"), host


def test_page_uses_the_db_capability_contract(twin):
    html = rr.build_page([rr.extract_twin(twin, "Toy", "ToyLevel")])
    assert 'claude.use("db")' in html
    assert "window.claude.db" not in html
    assert 'db.doc("regions/"' in html
    assert 'db.collection("regions").onSnapshot' in html
    assert "localStorage" in html                 # guarded by the store helper's try/catch


# ------------------------------------------------------------------------------- real twins

def _real_maps():
    out = []
    for name, level, rel in rr.DEFAULT_MAPS:
        d = ROOT / rel
        if d.exists():
            out.append((name, level, d))
    return out


@pytest.mark.skipif(not _real_maps(), reason="no baked twin directories in out/")
def test_real_twins_stay_inside_the_size_budget():
    payloads = []
    for name, level, d in _real_maps():
        p = rr.extract_twin(d, name, level)
        size = len(json.dumps(p, separators=(",", ":")).encode())
        assert size < 1_500_000, f"{name} payload is {size/1e6:.2f} MB"
        assert p["layers"]["drivable"] or p["layers"]["sidewalk"], name
        assert p["layers"]["signals"], name
        assert p["bounds"][2] > p["bounds"][0]
        payloads.append(p)
    html = rr.build_page(payloads)
    assert len(html.encode()) < 5_000_000, f"page is {len(html.encode())/1e6:.2f} MB"
