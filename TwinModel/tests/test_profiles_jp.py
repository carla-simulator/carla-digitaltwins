"""The Japanese profiles (``profiles.JP_URBAN`` / ``JP_SUBURBAN``) and the GSI elevation tiles."""
from __future__ import annotations

import math

import numpy as np
import pytest

from twinmodel import profiles
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
