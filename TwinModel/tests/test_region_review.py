"""``tools/region_review_page.py``: payload extraction, coordinate flip, rounding, the CARLA
top-down base layer and the size budget.

No ``unreal``/``carla`` import: the generator is pure geojson + JPEG -> JSON -> HTML.  The tile
grid / footprint maths of ``tools/carla_topview.py`` is covered here too -- only its ``main`` needs
the carla wheel.
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


def _load(path: Path = TOOL, name: str = "region_review_page"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load()
tv = _load(ROOT / "tools" / "carla_topview.py", "carla_topview")

# the generator never decodes the mosaic, it only base64s it, so a marker payload is enough
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"twin-topview-mosaic" * 8 + b"\xff\xd9"


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


# --------------------------------------------------------------------------- base imagery

@pytest.fixture()
def images(tmp_path) -> Path:
    """A ``carla_topview.py`` output directory for the toy twin (bounds padded to the 250 m grid)."""
    d = tmp_path / "review"
    d.mkdir()
    (d / "topview_ToyLevel.json").write_text(json.dumps({
        "map": "ToyLevel", "bounds": [-250.0, -250.0, 750.0, 500.0], "size": [5000, 3750],
        "px_per_m": 5.0, "cm_per_px": 0.2, "tiles": [4, 3], "tile_px": 1250,
        "camera": {"alt": 300.0, "fov": 50.0, "res": 3072, "yaw": -90.0}}))
    (d / "topview_ToyLevel.jpg").write_bytes(FAKE_JPEG)
    return d


def test_image_pixel_round_trip(images):
    meta, _ = rr.load_image(images, "ToyLevel")
    for x, y in [(-250.0, -250.0), (750.0, 500.0), (0.0, 0.0), (123.75, -87.25), (250.0, 250.0)]:
        px, py = rr.model_to_image_pixel(meta, x, y)
        bx, by = rr.image_pixel_to_model(meta, px, py)
        assert bx == pytest.approx(x, abs=1e-6) and by == pytest.approx(y, abs=1e-6)


def test_image_pixel_corners_are_north_up(images):
    """Pixel (0,0) is the north-west corner: xmin / ymax."""
    meta, _ = rr.load_image(images, "ToyLevel")
    assert rr.image_pixel_to_model(meta, 0, 0) == (-250.0, 500.0)
    assert rr.model_to_image_pixel(meta, -250.0, 500.0) == (0.0, 0.0)
    assert rr.model_to_image_pixel(meta, 750.0, -250.0) == (5000.0, 3750.0)
    # one metre east is +px_per_m px right, one metre north is px_per_m px *up*
    x0, y0 = rr.model_to_image_pixel(meta, 0.0, 0.0)
    x1, y1 = rr.model_to_image_pixel(meta, 1.0, 1.0)
    assert (x1 - x0, y1 - y0) == (pytest.approx(5.0), pytest.approx(-5.0))


def test_load_image_returns_a_data_uri_and_meta(images):
    import base64
    meta, src = rr.load_image(images, "ToyLevel")
    assert meta["bounds"] == [-250.0, -250.0, 750.0, 500.0]
    assert meta["size"] == [5000, 3750] and meta["px_per_m"] == 5.0
    assert meta["bytes"] == len(FAKE_JPEG)
    assert src.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(src.split(",", 1)[1]) == FAKE_JPEG


def test_load_image_is_optional(tmp_path):
    assert rr.load_image(tmp_path, "Nothing") == (None, None)


def test_image_bounds_contain_the_twin_bounds(twin, images):
    """The mosaic rectangle is the twin bounds padded out to the tile grid, so it must contain
    them -- otherwise the page would draw vectors off the edge of the render."""
    meta, _ = rr.load_image(images, "ToyLevel")
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    b, ib = p["bounds"], meta["bounds"]
    assert ib[0] <= b[0] and ib[1] <= b[1] and ib[2] >= b[2] and ib[3] >= b[3]


def test_page_embeds_the_base_image_and_its_bounds(twin, images):
    meta, src = rr.load_image(images, "ToyLevel")
    p = rr.extract_twin(twin, "Toy", "ToyLevel")
    p["image"] = meta
    html = rr.build_page([p], {"ToyLevel": src})
    assert '<img class="basemap" data-mapimg="ToyLevel"' in html
    assert "data:image/jpeg;base64," in html
    assert '"image":{"bounds":[-250.0,-250.0,750.0,500.0]' in html.replace(" ", "")
    assert 'data-layer="image"' in html
    assert 'id="vecop"' in html                       # the overlay opacity slider


def test_page_defaults_to_the_render_with_signals_and_junctions_on(twin):
    html = rr.build_page([rr.extract_twin(twin, "Toy", "ToyLevel")])
    body = html.split("const DEFAULT_ON = ", 1)[1].split(";", 1)[0]
    on = dict(re.findall(r"(\w+):(\d)", body))
    assert on["image"] == "1" and on["signals"] == "1" and on["junctions"] == "1"
    assert set(on) - {"image", "signals", "junctions"}, "no vector layers left"
    for k, v in on.items():
        if k not in ("image", "signals", "junctions"):
            assert v == "0", f"{k} should default off now the render is the base"


def test_page_without_images_is_still_vector_only(twin):
    html = rr.build_page([rr.extract_twin(twin, "Toy", "ToyLevel")])
    assert "data:image/jpeg" not in html
    assert "basemap" in html                          # the css rule survives; no img element does
    assert '<img class="basemap"' not in html


# ---------------------------------------------------------------- carla_topview tile geometry

def test_tile_grid_pads_out_to_the_250_m_grid():
    assert tv.tile_grid([-394.6, -342.9, 426.6, 526.9]) == (-500.0, -500.0, 4, 5)
    assert tv.tile_grid([0.0, 0.0, 250.0, 250.0]) == (0.0, 0.0, 1, 1)
    assert tv.tile_grid([1.0, 1.0, 249.0, 249.0]) == (0.0, 0.0, 1, 1)


def test_capture_footprint_covers_the_tile_with_margin():
    """The defaults must see more than one tile, or the crop would read past the image."""
    fp = tv.footprint(300.0, 50.0)
    assert fp == pytest.approx(279.8, abs=0.2)
    assert fp > tv.TILE * 1.05


def test_exposure_gains_level_tiles_and_skip_the_void():
    import numpy as np
    mostly_void = np.zeros((8, 8, 3), np.uint8)
    mostly_void[0] = 120                                       # one row of content out of eight
    tiles = {(0, 0): np.full((8, 8, 3), 200, np.uint8),
             (1, 0): np.full((8, 8, 3), 220, np.uint8),
             (2, 0): np.full((8, 8, 3), 180, np.uint8),
             (3, 0): np.zeros((8, 8, 3), np.uint8),            # empty sky outside the map
             (4, 0): mostly_void}
    g = tv.exposure_gains(tiles)
    assert g[(3, 0)] == 1.0 and g[(4, 0)] == 1.0               # void / edge tiles left alone
    assert g[(0, 0)] == pytest.approx(1.0)                     # the median tile is the target
    assert g[(1, 0)] < 1.0 < g[(2, 0)]
    assert all(0.93 <= v <= 1.07 for v in g.values())          # clamped, never a heavy regrade


def test_elevation_sampler_is_bilinear_and_nan_safe(tmp_path):
    import numpy as np
    p = tmp_path / "elevation.npz"
    np.savez(p, z=np.array([[0.0, 10.0], [20.0, np.nan]]), x0=0.0, y0=0.0, dx=100.0, dy=100.0)
    e = tv.Elevation(p)
    assert e.at(0.0, 0.0) == pytest.approx(0.0)
    assert e.at(100.0, 0.0) == pytest.approx(10.0)
    assert e.at(50.0, 0.0) == pytest.approx(5.0)
    assert np.isfinite(e.at(100.0, 100.0))                    # the NaN corner falls back
    assert e.at(-500.0, -500.0) == pytest.approx(0.0)         # clamped to the grid


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
    payloads, images = [], {}
    for name, level, d in _real_maps():
        p = rr.extract_twin(d, name, level)
        size = len(json.dumps(p, separators=(",", ":")).encode())
        assert size < 1_500_000, f"{name} payload is {size/1e6:.2f} MB"
        assert p["layers"]["drivable"] or p["layers"]["sidewalk"], name
        assert p["layers"]["signals"], name
        assert p["bounds"][2] > p["bounds"][0]
        meta, src = rr.load_image(ROOT / "out" / "review", level)
        if meta:
            p["image"] = meta
            images[level] = src
        payloads.append(p)
    html = rr.build_page(payloads, images)
    # the whole page, base64 mosaics included, has to stay well under the 16 MB artifact cap
    assert len(html.encode()) < 12_000_000, f"page is {len(html.encode())/1e6:.2f} MB"


@pytest.mark.skipif(not (ROOT / "out" / "review" / "topview_Sunnyvale.json").exists(),
                    reason="no captured mosaics in out/review")
def test_captured_mosaics_match_the_twins_they_cover():
    for name, level, d in _real_maps():
        meta, src = rr.load_image(ROOT / "out" / "review", level)
        if meta is None:
            continue
        b = rr.extract_twin(d, name, level)["bounds"]
        ib = meta["bounds"]
        assert ib[0] <= b[0] and ib[1] <= b[1] and ib[2] >= b[2] and ib[3] >= b[3], name
        # the sidecar's px/m must be the one the pixels were actually written at
        assert meta["size"][0] == pytest.approx((ib[2] - ib[0]) * meta["px_per_m"], abs=1), name
        assert meta["size"][1] == pytest.approx((ib[3] - ib[1]) * meta["px_per_m"], abs=1), name
        assert meta["px_per_m"] >= 1 / 0.30, f"{name} coarser than 30 cm/px"
        assert src.startswith("data:image/jpeg;base64,")
