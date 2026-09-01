"""Worker D2 tests: region-aware ingest — provider chains, country lookup, building coverage.
No network: every provider is monkeypatched or pointed at a closed port."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from twinmodel.frame import LocalFrame
from twinmodel.model import Elevation
from twinmodel.ingest import imagery, elevation, osm
from twinmodel.ingest.imagery import OrthoImage
from twinmodel import profiles

FIX = Path(__file__).parent / "fixtures"
BBOX = (41.3905, 2.1630, 41.3945, 2.1690)
US_BBOX = (37.3690, -122.0420, 37.3740, -122.0340)
DEAD = "http://127.0.0.1:9/x"


def _fake_ortho(name):
    def fn(frame, bbox, resolution, grid, **_):
        arr = np.full((grid[3], grid[2], 3), 128, dtype=np.uint8)
        return OrthoImage(arr, grid[0], grid[1], resolution, resolution, source="raw", detail=name)
    return fn


def _fail(*a, **k):
    raise RuntimeError("boom")


def _none(*a, **k):
    return None


# --------------------------------------------------------------------------- imagery chain

def test_ortho_provider_order_first_success_wins(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(imagery, "PROVIDERS", {
        "a": lambda *a, **k: (calls.append("a"), None)[1],
        "b": lambda *a, **k: (calls.append("b"), _fake_ortho("B")(*a, **k))[1],
        "c": lambda *a, **k: (calls.append("c"), _fake_ortho("C")(*a, **k))[1],
    })
    frame = LocalFrame.from_bbox(*BBOX)
    img = imagery.fetch_ortho(frame, BBOX, resolution=2.0, cache_dir=tmp_path, sources=("a", "b", "c"))
    assert img is not None and img.source == "b" and img.detail == "B"
    assert calls == ["a", "b"]
    assert img.dx == 2.0 and img.array.shape[0] > 100
    # the winner is cached under its own key and the cache hit carries the provider name
    calls.clear()
    again = imagery.fetch_ortho(frame, BBOX, resolution=2.0, cache_dir=tmp_path, sources=("a", "b", "c"))
    assert again.source == "b" and calls == ["a"]  # 'a' has no cache -> tried again, still None


def test_ortho_provider_exception_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(imagery, "PROVIDERS", {"x": _fail, "y": _fake_ortho("Y")})
    frame = LocalFrame.from_bbox(*BBOX)
    img = imagery.fetch_ortho(frame, BBOX, resolution=2.0, cache_dir=tmp_path, sources=("nope", "x", "y"))
    assert img.source == "y"


def test_ortho_all_fail_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(imagery, "PROVIDERS", {"x": _fail, "y": _none})
    frame = LocalFrame.from_bbox(*BBOX)
    assert imagery.fetch_ortho(frame, BBOX, resolution=2.0, cache_dir=tmp_path, sources=("x", "y")) is None


def test_ortho_real_providers_unreachable(monkeypatch, tmp_path):
    for name in ("WMS_URL", "IGN_ES_WMS", "NAIP_EXPORT_URL", "NAIP_WMS_URL"):
        monkeypatch.setattr(imagery, name, DEAD)
    monkeypatch.setattr(imagery, "REQUEST_TIMEOUT", 2)
    frame = LocalFrame.from_bbox(*US_BBOX)
    assert imagery.fetch_ortho(frame, US_BBOX, resolution=4.0, cache_dir=tmp_path,
                               sources=("icgc", "ign_es", "naip")) is None


def test_ortho_cache_key_compat():
    # the pre-existing ICGC cache files (keyed by layer|resolution) must still be found
    p = imagery._cache_path(Path("data"), BBOX, 0.25, imagery.DEFAULT_LAYER, provider="icgc")
    assert p.name == "ortho_41.39050_2.16300_41.39450_2.16900_0.25m_7c1164d9.tif"
    q = imagery._cache_path(Path("data"), BBOX, 0.25, "", provider="naip")
    assert q != p


def test_profile_source_names_are_known():
    for p in profiles.PROFILES.values():
        assert set(p.sources.ortho) <= set(imagery.PROVIDERS), p.name
        assert set(p.sources.dem) <= set(elevation.PROVIDERS), p.name


# --------------------------------------------------------------------------- elevation chain

def _fake_dem(name, value=10.0):
    def fn(frame, bbox, grid, spacing):
        x0, y0, w, h, t = grid
        return Elevation(np.full((h, w), value), x0, y0, t.a, t.a, source=f"desc {name}")
    return fn


def test_dem_provider_order_and_fallback(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(elevation, "PROVIDERS", {
        "p": lambda *a: (calls.append("p"), _fail())[1],
        "q": lambda *a: (calls.append("q"), None)[1],
        "r": lambda *a: (calls.append("r"), _fake_dem("R", 33.0)(*a))[1],
        "s": lambda *a: (calls.append("s"), _fake_dem("S")(*a))[1],
    })
    frame = LocalFrame.from_bbox(*US_BBOX)
    el = elevation.fetch_dem(frame, US_BBOX, cache_dir=tmp_path, sources=("p", "q", "zz", "r", "s"))
    assert el is not None and el.source == "r" and el.detail == "desc R"
    assert calls == ["p", "q", "r"]
    assert float(el.z.mean()) == 33.0 and el.dx == 2.0
    # cached -> second call hits the npz, no provider called
    calls.clear()
    el2 = elevation.fetch_dem(frame, US_BBOX, cache_dir=tmp_path, sources=("p", "q", "r", "s"))
    assert calls == [] and el2.source == "r"


def test_dem_non_finite_result_is_rejected(monkeypatch, tmp_path):
    def nan_dem(frame, bbox, grid, spacing):
        x0, y0, w, h, t = grid
        z = np.full((h, w), 1.0); z[0, 0] = np.nan
        return Elevation(z, x0, y0, t.a, t.a)
    monkeypatch.setattr(elevation, "PROVIDERS", {"bad": nan_dem, "good": _fake_dem("G")})
    frame = LocalFrame.from_bbox(*BBOX)
    el = elevation.fetch_dem(frame, BBOX, cache_dir=tmp_path, sources=("bad", "good"))
    assert el.source == "good"


def test_usgs_3dep_unreachable_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "USGS_3DEP_URL", DEAD)
    frame = LocalFrame.from_bbox(*US_BBOX)
    assert elevation.fetch_dem(frame, US_BBOX, cache_dir=tmp_path, sources=("usgs_3dep",)) is None


# --------------------------------------------------------------------------- country

def test_country_for_bbox_cache_hit(tmp_path):
    s, w, n, e = US_BBOX
    p = osm._country_cache_path(tmp_path, (s + n) / 2, (w + e) / 2)
    p.write_text(json.dumps({"iso2": "US", "name": "United States"}))
    assert osm.country_for_bbox(US_BBOX, cache_dir=tmp_path, url=DEAD, retries=1) == "US"


def test_country_for_bbox_unreachable_returns_none(tmp_path):
    assert osm.country_for_bbox(BBOX, cache_dir=tmp_path, url=DEAD, retries=2, retry_sleep=0) is None
    assert not list(tmp_path.glob("country_*.json"))  # failures are not cached


def test_country_for_bbox_parses_overpass_payload(monkeypatch, tmp_path):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"elements": [{"type": "area", "id": 1, "tags": {"admin_level": "2",
                                  "ISO3166-1": "es", "name": "España", "name:en": "Spain"}}]}
    monkeypatch.setattr(osm.requests, "post", lambda *a, **k: R())
    assert osm.country_for_bbox(BBOX, cache_dir=tmp_path) == "ES"
    cached = json.loads(next(tmp_path.glob("country_*.json")).read_text())
    assert cached["iso2"] == "ES" and cached["name"] == "Spain"
    assert osm.country_for_bbox(BBOX, cache_dir=tmp_path, url=DEAD, retries=1) == "ES"


# --------------------------------------------------------------------------- building coverage

@pytest.mark.parametrize("fixture,bbox,iso2,lo,hi,profile", [
    ("eixample_overpass.json", BBOX, "ES", 0.45, 0.65, "eu_dense"),
    ("sunnyvale_overpass.json", US_BBOX, "US", 0.15, 0.28, "us_suburban"),
    ("sf_soma_overpass.json", (37.7790, -122.4080, 37.7840, -122.4000), "US", 0.40, 0.65, "us_urban"),
])
def test_building_coverage_fixture(fixture, bbox, iso2, lo, hi, profile):
    data = osm.load_fixture(FIX / fixture)
    cov = osm.building_coverage(data, LocalFrame.from_bbox(*bbox), bbox)
    assert lo < cov < hi, cov
    assert profiles.choose_for_country(iso2, cov).name == profile


def test_building_coverage_empty():
    frame = LocalFrame.from_bbox(*BBOX)
    assert osm.building_coverage(osm.OsmData(), frame, BBOX) == 0.0
