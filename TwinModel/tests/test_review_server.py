"""``tools/review_server.py`` + ``tools/lowfly_viewer.py``: the tile route, the regions API and
the page the local low-fly viewer serves.

No CARLA and no baked pyramid: the fixture is a hand-written manifest over a 4x4 leaf grid with a
handful of real (tiny) WebP tiles, served by the real server on an ephemeral port.  The headless
render check runs only where Chrome is installed.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


srv = _load(ROOT / "tools" / "review_server.py", "review_server")
viewer = _load(ROOT / "tools" / "lowfly_viewer.py", "lowfly_viewer")

MAP = "Fixture"
X0, Y0 = -500.0, 250.0                      # a padded grid that is not at the origin
LEAF_M, LEAF_PX, LEAVES = 5.0, 500, 4       # 4x4 leaves = 20 m, two levels (z0 4x4, z1 2x2)
BOUNDS = [X0, Y0, X0 + LEAVES * LEAF_M, Y0 + LEAVES * LEAF_M]

STUB_TWIN = {
    "name": MAP, "carla_map": MAP, "twin_dir": "", "origin": {"lat": 37.0, "lon": -122.0},
    "geo_reference": "", "scale": 0.05, "bounds": [X0 + 1, Y0 + 1, X0 + 19, Y0 + 19],
    "bounds_carla": [X0 + 1, -(Y0 + 19), X0 + 19, -(Y0 + 1)],
    "centre": [X0 + 10, Y0 + 10], "centre_carla": [X0 + 10, -(Y0 + 10)],
    "layers": {"ground": [], "verge": [], "median": [], "island": [], "parking": [],
               "drivable": [], "sidewalk": [], "crossing": [], "buildings": [],
               "curbs": [], "roads": [], "markings": {"sw": [], "sy": [], "bw": [], "by": []},
               "signals": [{"x": 20, "y": 20, "k": "stop", "id": "sig-1", "t": "206",
                            "s": "-1", "r": "3", "c": ""}],
               "trees": [40, 40]},
    "junctions": [{"id": "j0", "x": 100, "y": 100}],
    "counts": {"drivable": 0, "signals": 1, "trees": 1},
}


def _tile(path: Path, rgb: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (LEAF_PX, LEAF_PX), rgb).save(path, format="WEBP", quality=60)


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("lowfly") / MAP
    d.mkdir(parents=True)
    manifest = {
        "map": MAP, "twin_dir": "", "bounds": BOUNDS,
        "bounds_model_data": [X0 + 1, Y0 + 1, X0 + 19, Y0 + 19],
        "x0": X0, "y0": Y0, "material_tiles": [1, 1], "tile_m": 250.0,
        "leaf_m": LEAF_M, "leaf_px": LEAF_PX, "leaves": [LEAVES, LEAVES],
        "levels": 2, "cm_per_px": 1.0,
        "camera": {"alt": 5.0, "fov": 90.0, "res": 1024, "yaw": -90.0},
        "webp_quality": 60, "leaf_tiles": 4, "bytes": 4096, "frames": 16, "seconds": 1.0,
        "tile_url": "z{z}/{i}_{j}.webp",
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for i, j, rgb in [(0, 0, (200, 40, 40)), (1, 0, (40, 200, 40)),
                      (0, 1, (40, 40, 200)), (3, 3, (220, 220, 40))]:
        _tile(d / "z0" / f"{i}_{j}.webp", rgb)
    _tile(d / "z1" / "0_0.webp", (120, 120, 120))
    return d.parent


@pytest.fixture(scope="module")
def server(root):
    httpd = srv.make_server(root, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def get(url: str, method: str = "GET", body=None):
    """-> (status, headers, bytes); HTTP errors come back as a value, not an exception."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def getjson(url: str, method: str = "GET", body=None):
    code, _, raw = get(url, method, body)
    return code, (json.loads(raw) if raw else None)


# ------------------------------------------------------------------------------- build_page

def test_build_page_is_a_self_contained_document():
    man = {"map": MAP, "bounds": BOUNDS, "x0": X0, "y0": Y0, "leaves": [LEAVES, LEAVES],
           "levels": 2, "cm_per_px": 1.0, "leaf_m": LEAF_M, "tile_m": 250.0}
    html = viewer.build_page(MAP, man, STUB_TWIN)
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    # everything the page needs is inline: no CDN, no stylesheet or script src
    assert "https://" not in html
    assert "<script src=" not in html and '<link rel="stylesheet"' not in html
    assert 'src="http' not in html and 'href="http' not in html
    assert MAP in html
    embedded = json.loads(html.split('id="manifest">')[1].split("</script>")[0])
    assert embedded["bounds"] == BOUNDS
    assert embedded["tile_base"] == "/tiles/%s/" % MAP
    assert embedded["api_base"] == "/api/%s/regions" % MAP
    assert 'id="twin"' in html
    # the draw tools, the form and the region schema are all there
    for marker in ['data-mode="polygon"', 'data-mode="polyline"', 'data-mode="point"',
                   'id="f-cat"', 'id="f-prio"', 'id="f-comment"', 'id="regions"',
                   "coords_model", "centroid_model", "ro-carla"]:
        assert marker in html, marker


def test_build_page_without_a_twin_still_builds():
    html = viewer.build_page(MAP, {"map": MAP, "bounds": BOUNDS, "x0": X0, "y0": Y0,
                                   "leaves": [4, 4], "levels": 2, "cm_per_px": 1.0}, None)
    assert 'id="twin"' not in html
    assert 'id="manifest"' in html


def test_region_defaults_derives_the_carla_flip_and_centroid():
    doc = viewer.region_defaults({"kind": "polygon", "coords_model": [[0, 0], [10, 0], [10, 20]],
                                  "title": " kerb ", "category": "nonsense", "priority": "high"},
                                 map_name=MAP, carla_map=MAP)
    assert doc["coords_carla"] == [[0.0, -0.0], [10.0, -0.0], [10.0, -20.0]]
    assert doc["centroid_model"] == [6.67, 6.67]
    assert doc["centroid_carla"] == [6.67, -6.67]
    assert doc["title"] == "kerb"
    assert doc["category"] == "other"        # unknown categories fall back
    assert doc["priority"] == "high"
    assert doc["status"] == "open" and doc["replies"] == []


# ------------------------------------------------------------------------------------ routes

def test_index_lists_the_map(server):
    code, _, raw = get(server + "/")
    assert code == 200
    body = raw.decode()
    assert MAP in body and 'href="/%s/"' % MAP in body


def test_map_page_carries_the_manifest(server):
    code, hdr, raw = get(server + "/%s/" % MAP)
    assert code == 200 and hdr["Content-Type"].startswith("text/html")
    body = raw.decode()
    assert MAP in body
    embedded = json.loads(body.split('id="manifest">')[1].split("</script>")[0])
    assert embedded["bounds"] == BOUNDS
    assert embedded["leaves"] == [LEAVES, LEAVES] and embedded["levels"] == 2


def test_unknown_map_is_404(server):
    code, _ = getjson(server + "/Nope/")
    assert code == 404


def test_tile_hit_miss_and_traversal(server):
    code, hdr, raw = get(server + "/tiles/%s/z0/0_0.webp" % MAP)
    assert code == 200
    assert hdr["Content-Type"] == "image/webp"
    assert "max-age=3600" in hdr["Cache-Control"]
    assert raw[:4] == b"RIFF" and b"WEBP" in raw[:16]

    code, _, _ = get(server + "/tiles/%s/z0/2_2.webp" % MAP)        # inside the grid, no tile
    assert code == 404
    code, _, _ = get(server + "/tiles/%s/z1/0_0.webp" % MAP)
    assert code == 200

    for bad in ["/tiles/%s/z0/../../manifest.json" % MAP,
                "/tiles/%s/../%s/z0/0_0.webp" % (MAP, MAP),
                "/tiles/%s/z0/0_0.webp/../../../../etc/passwd" % MAP,
                "/tiles/Fixture%2F..%2Fmanifest.json/z0/0_0.webp",
                "/tiles/../etc/z0/0_0.webp"]:
        code, _, _ = get(server + bad)
        assert code == 404, bad


def test_manifest_api(server):
    code, doc = getjson(server + "/api/%s/manifest" % MAP)
    assert code == 200 and doc["map"] == MAP and doc["leaves"] == [LEAVES, LEAVES]


# -------------------------------------------------------------------------------- regions

def test_regions_crud_round_trip(server, root):
    base = server + "/api/%s/regions" % MAP
    code, rows = getjson(base)
    assert code == 200 and rows == []

    code, doc = getjson(base, "POST", {
        "kind": "polygon", "coords_model": [[-499.0, 251.0], [-495.0, 251.0], [-495.0, 255.0]],
        "title": "kerb texture wrong", "category": "materials", "priority": "high",
        "comment": "the stone band is stretched"})
    assert code == 201
    rid = doc["id"]
    assert rid.startswith(MAP + "-") and len(rid) == len(MAP) + 9
    assert doc["created_at"].endswith("Z")
    assert doc["map"] == MAP and doc["carla_map"] == MAP
    assert doc["coords_carla"] == [[-499.0, -251.0], [-495.0, -251.0], [-495.0, -255.0]]
    assert doc["centroid_carla"][1] == -doc["centroid_model"][1]
    assert doc["status"] == "open" and doc["replies"] == []

    # persisted on disk, atomically, as a plain list
    stored = json.loads((root / MAP / "regions.json").read_text())
    assert isinstance(stored, list) and len(stored) == 1 and stored[0]["id"] == rid
    assert not list((root / MAP).glob("regions.json.tmp*"))

    code, rows = getjson(base)
    assert code == 200 and [r["id"] for r in rows] == [rid]

    code, doc = getjson(base + "/" + rid, "PUT",
                        {"status": "done",
                         "replies": [{"author": "claude", "text": "repainted", "at": "2026-09-03T00:00:00Z"}]})
    assert code == 200 and doc["status"] == "done"
    assert doc["replies"][0]["author"] == "claude"
    assert doc["created_at"] == json.loads((root / MAP / "regions.json").read_text())[0]["created_at"]
    assert doc["coords_carla"][0] == [-499.0, -251.0]      # untouched fields survive the merge

    code, _ = getjson(base + "/" + rid, "PUT", {"comment": "still off"})
    assert code == 200

    code, _ = getjson(base + "/nope-12345678", "PUT", {"status": "done"})
    assert code == 404

    code, doc = getjson(base + "/" + rid, "DELETE")
    assert code == 200 and doc["deleted"] == rid
    code, rows = getjson(base)
    assert rows == []
    code, _ = getjson(base + "/" + rid, "DELETE")
    assert code == 404


def test_regions_reject_bad_payloads(server):
    base = server + "/api/%s/regions" % MAP
    code, _, _ = get(base + "/x", "POST", {"kind": "point", "coords_model": [[0, 0]]})
    assert code == 405
    code, _, _ = get(server + "/api/Nope/regions", "POST", {"kind": "point"})
    assert code == 404
    req = urllib.request.Request(base, data=b"not json", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400


def test_l0_root_adds_the_coarse_mosaic_under_the_pyramid(root, tmp_path):
    """--l0-root's 20 cm/px mosaic is announced in the page and served at /l0/<Map>.jpg."""
    l0 = tmp_path / "review"
    l0.mkdir()
    (l0 / ("topview_%s.json" % MAP)).write_text(json.dumps(
        {"bounds": BOUNDS, "size": [100, 100], "px_per_m": 5.0}))
    Image.new("RGB", (16, 16), (10, 20, 30)).save(l0 / ("topview_%s.jpg" % MAP))
    httpd = srv.make_server(root, "127.0.0.1", 0, l0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        code, _, raw = get(base + "/%s/" % MAP)
        assert code == 200
        man = json.loads(raw.decode().split('id="manifest">')[1].split("</script>")[0])
        assert man["l0"] == {"url": "/l0/%s.jpg" % MAP, "bounds": BOUNDS}
        code, hdr, blob = get(base + "/l0/%s.jpg" % MAP)
        assert code == 200 and hdr["Content-Type"] == "image/jpeg" and blob[:2] == b"\xff\xd8"
        code, _, _ = get(base + "/l0/Nope.jpg")
        assert code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


# ---------------------------------------------------------------------------- headless render

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or \
    shutil.which("chromium-browser") or shutil.which("google-chrome-stable")


@pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium on PATH")
def test_page_renders_without_console_errors(server, tmp_path):
    shot = tmp_path / "shot.png"
    cmd = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--hide-scrollbars", "--virtual-time-budget=6000", "--window-size=1200,800",
           "--enable-logging=stderr", "--v=0", "--user-data-dir=%s" % (tmp_path / "profile"),
           "--screenshot=%s" % shot, "--dump-dom", server + "/%s/" % MAP]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    dom, err = p.stdout, p.stderr
    assert shot.exists() and shot.stat().st_size > 1000, err[-4000:]
    assert 'id="regions"' in dom and MAP in dom
    # the JS ran: the layer UI and the form selects are built from the payload at boot
    assert 'data-layer="drivable"' in dom, dom[:2000]
    assert '<option value="materials"' in dom
    bad = [ln for ln in err.splitlines()
           if (":ERROR:" in ln or "SEVERE" in ln or "Uncaught" in ln)
           and "GPU" not in ln and "gpu" not in ln and "dbus" not in ln.lower()
           and "voice_transcription" not in ln and "sandbox" not in ln.lower()]
    assert not bad, "\n".join(bad[:20])


@pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium on PATH")
def test_page_fetches_only_tiles_inside_the_grid(server, root, tmp_path):
    """Fitting the view must never ask for a tile outside the manifest's grid."""
    logf = tmp_path / "access.log"
    handler = logging.FileHandler(logf)
    handler.setLevel(logging.DEBUG)
    logging.getLogger("review_server").addHandler(handler)
    logging.getLogger("review_server").setLevel(logging.DEBUG)
    try:
        # fitted (a coarse level) and deep-linked at 100 px/m (level 0, 1 cm/px)
        for n, url in enumerate(["/%s/" % MAP, "/%s/#%g,%g,100" % (MAP, X0 + 2.5, Y0 + 2.5)]):
            cmd = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                   "--disable-dev-shm-usage", "--virtual-time-budget=6000",
                   "--window-size=1200,800", "--user-data-dir=%s" % (tmp_path / ("profile%d" % n)),
                   "--dump-dom", server + url]
            subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        time.sleep(0.5)
    finally:
        logging.getLogger("review_server").removeHandler(handler)
        handler.close()
    asked = [ln for ln in logf.read_text().splitlines() if "/tiles/" in ln]
    assert asked, "the page never requested a tile"
    levels = {int(ln.split("/tiles/")[1].split("/")[1][1:]) for ln in asked}
    assert 0 in levels, "the 1 cm/px level was never reached at 100 px/m: %s" % sorted(levels)
    for ln in asked:
        frag = ln.split("/tiles/")[1].split()[0]
        _, z, ij = frag.split("/")
        i, j = ij.replace(".webp", "").split("_")
        zi = int(z[1:])
        assert zi < 2
        span = LEAVES if zi == 0 else 2
        assert 0 <= int(i) < span and 0 <= int(j) < span, ln
