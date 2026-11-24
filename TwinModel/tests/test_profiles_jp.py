"""The Japanese profiles (``profiles.JP_URBAN`` / ``JP_SUBURBAN``) and the GSI elevation tiles."""
from __future__ import annotations

import math

import numpy as np
import pytest

from twinmodel import profiles
from twinmodel.ingest import elevation
from twinmodel.ingest.elevation import _gsi_decode


def test_japan_picks_a_japanese_profile():
    assert profiles.choose_for_country("JP", 0.1).name == "jp_suburban"
    assert profiles.choose_for_country("JP", 0.4).name == "jp_urban"
    assert profiles.choose_for_country("JP").drives_on == "left"


def test_japanese_profiles_drive_on_the_left_and_turn_right_on_the_arrow():
    for p in (profiles.JP_URBAN, profiles.JP_SUBURBAN):
        assert p.drives_on == "left" and p.signal_country == "JP"
        assert p.junction.signal_arrow_turns == ("right",)
        assert p.junction.unsignalised_control == "minor_stop"
        assert p.sources.dem[0] == "gsi_dem5a"
        assert profiles.right_handed(p).junction.signal_arrow_turns == ("left",)


def test_gsi_tile_decoding():
    rgb = np.array([[[0, 0, 100], [0x80, 0, 0], [0xFF, 0xFF, 0xFF], [0, 0x27, 0x10]]], dtype=np.uint8)
    z = _gsi_decode(rgb)
    assert z[0, 0] == pytest.approx(1.0)
    assert math.isnan(z[0, 1])
    assert z[0, 2] == pytest.approx(-0.01)
    assert z[0, 3] == pytest.approx(100.0)


def _grid(n=4):
    from rasterio.transform import from_origin
    return 0.0, 0.0, n, n, from_origin(0.0, float(n), 1.0, 1.0)


def test_gsi_5m_holes_are_filled_from_the_10m_dem_and_the_sea_is_zero(monkeypatch):
    nan = np.nan
    dem5a = np.array([[10, 10, nan, nan]] * 2 + [[nan] * 4] * 2, dtype=np.float32)   # unsurveyed patch
    dem10 = np.array([[11, 11, 12, 12]] * 2 + [[13, 13, nan, nan]] * 2, dtype=np.float32)  # sea, lower right
    calls = []

    def fake(frame, bbox, grid, layer):
        calls.append(layer)
        return {"dem5a_png": dem5a, "dem_png": dem10}[layer].copy()

    monkeypatch.setattr(elevation, "_gsi_grid", fake)
    el = elevation.PROVIDERS["gsi_dem5a"](None, None, _grid(), 5.0)
    z = el.z[::-1]   # back to north-up
    assert calls == ["dem5a_png", "dem_png"]
    assert z[0, 0] == 10 and z[0, 2] == 12 and z[2, 0] == 13   # 5 m first, 10 m in its holes
    assert z[3, 3] == 0.0                                       # no layer: sea level, not the shore's 13
    assert "dem5a_png" in el.source and "dem_png" in el.source


def test_gsi_10m_is_not_fetched_when_the_5m_dtm_covers_the_grid(monkeypatch):
    calls = []

    def fake(frame, bbox, grid, layer):
        calls.append(layer)
        return np.full((4, 4), 5.0, dtype=np.float32)

    monkeypatch.setattr(elevation, "_gsi_grid", fake)
    el = elevation.PROVIDERS["gsi_dem5a"](None, None, _grid(), 5.0)
    assert calls == ["dem5a_png"] and "dem_png" not in el.source


def test_gsi_outside_japan_gives_none(monkeypatch):
    monkeypatch.setattr(elevation, "_gsi_grid", lambda *a: None)
    assert elevation.PROVIDERS["gsi_dem5a"](None, None, _grid(), 5.0) is None
    monkeypatch.setattr(elevation, "_gsi_grid", lambda *a: np.full((4, 4), np.nan, dtype=np.float32))
    assert elevation.PROVIDERS["gsi_dem10b"](None, None, _grid(), 5.0) is None
