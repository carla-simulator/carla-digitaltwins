"""Slippy-map tile fetching shared by the OSM tile layer and the GSI elevation tiles, with a
fake HTTP session (no network)."""
from __future__ import annotations

import io

import numpy as np
import pytest
import requests
from PIL import Image

from twinmodel.frame import LocalFrame
from twinmodel.ingest import elevation, osmtiles

LAT, LON = 35.9, 139.93
D = 0.0005
BBOX = (LAT - D, LON - D, LAT + D, LON + D)


def _png(rgb) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.full((256, 256, 3), rgb, dtype=np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, status, content=b"", ctype="image/png"):
        self.status_code, self.content, self.headers = status, content, {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _Session:
    """Serves tile URLs from ``handler(url) -> _Resp`` and records them."""

    def __init__(self, handler):
        self.handler, self.urls, self.headers = handler, [], []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        return self.handler(url)

    def mount(self, *a):
        pass


@pytest.fixture
def serve(monkeypatch):
    def install(handler):
        s = _Session(handler)
        monkeypatch.setattr(requests, "Session", lambda: s)
        return s
    return install


def test_osm_tiles_are_stitched_warped_and_cached(serve, tmp_path):
    s = serve(lambda url: _Resp(200, _png((200, 100, 50))))
    frame = LocalFrame.from_bbox(*BBOX)
    img = osmtiles.fetch_osm_tiles(frame, BBOX, zoom=17, resolution=1.0, cache_dir=tmp_path)
    assert img is not None and img.array.shape[2] == 3
    centre = img.array[img.array.shape[0] // 2, img.array.shape[1] // 2]
    assert tuple(centre) == (200, 100, 50)
    tx0, ty0, tx1, ty1 = osmtiles.tile_range(BBOX, 17)
    assert len(s.urls) == (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    assert all("User-Agent" in h for h in s.headers)
    assert (tmp_path / "osmtiles" / "17" / str(tx0) / f"{ty0}.png").exists()
    # every tile now on disk: a second fetch (warped-raster cache off) downloads nothing
    s.urls.clear()
    assert osmtiles.fetch_osm_tiles(frame, BBOX, zoom=17, resolution=1.0, cache_dir=tmp_path,
                                    use_cache=False) is not None
    assert s.urls == []


def test_a_missing_osm_tile_fails_the_layer(serve, tmp_path):
    serve(lambda url: _Resp(404))
    frame = LocalFrame.from_bbox(*BBOX)
    assert osmtiles.fetch_osm_tiles(frame, BBOX, zoom=17, resolution=1.0, cache_dir=tmp_path) is None


def _gsi_rgb(height_m: float):
    v = int(round(height_m / 0.01)) & 0xFFFFFF
    return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF


def test_gsi_tiles_decode_and_a_404_is_no_data(serve):
    """The 5 m layer: one tile, east of the bbox centre GSI's no-data pixel; every other tile a 404.
    Both are holes, which the 10 m layer fills."""
    fx, fy = osmtiles.lonlat_to_tile(LON, LAT, 15)
    cx, cy, col = int(fx), int(fy), int((fx % 1) * 256)   # split at the bbox centre
    half = np.zeros((256, 256, 3), dtype=np.uint8)
    half[:, :col] = _gsi_rgb(12.34)
    half[:, col:] = (128, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(half).save(buf, format="PNG")

    def handler(url):
        layer, z, x, y = url.rsplit("/", 4)[-4:]
        if layer == "dem5a_png":
            return _Resp(200, buf.getvalue()) if (int(x), int(y[:-4])) == (cx, cy) else _Resp(404)
        return _Resp(200, _png(_gsi_rgb(-1.5)))

    s = serve(handler)
    frame = LocalFrame.from_bbox(*BBOX)
    from twinmodel.ingest.imagery import model_grid
    el = elevation.PROVIDERS["gsi_dem5a"](frame, BBOX, model_grid(frame, BBOX, 2.0, 0.0), 2.0)
    assert el is not None
    assert np.nanmax(el.z) == pytest.approx(12.34, abs=0.01)
    assert np.nanmin(el.z) == pytest.approx(-1.5, abs=0.01)
    assert any("/dem_png/" in u for u in s.urls)
