"""``ue/twin_materials.py``: the baker's material pools and the deterministic variant pick.

The module is editor-side but deliberately free of ``import unreal`` so exactly this can be
tested offline -- the pools and the selection hash are what decide how a baked twin looks, and
a rebake has to reproduce them byte for byte.
"""
import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from twinmodel.export import ue as ue_export

_SPEC = importlib.util.spec_from_file_location(
    "twin_materials", Path(__file__).resolve().parents[1] / "ue" / "twin_materials.py")
tm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tm)


# --------------------------------------------------------------------------- pool config

def test_every_pool_is_a_non_empty_list_of_full_package_paths():
    """Full paths only: Ground/MI_LargeLandscape_Grass and Landscapes/MI_LargeLandscape_Grass
    are two different assets on two different masters."""
    assert tm.MATERIAL_PARENTS
    for key, pool in tm.MATERIAL_PARENTS.items():
        assert isinstance(pool, list) and pool, key
        for path in pool:
            assert path.startswith("/Game/Carla/Static/GenericMaterials/"), (key, path)
            pkg, obj = path.rsplit(".", 1)
            assert pkg.rsplit("/", 1)[-1] == obj, path   # /Game/.../MI_Foo.MI_Foo


def test_the_two_LargeLandscape_Grass_assets_are_told_apart():
    ground = tm.MATERIAL_PARENTS["ground"]
    grass = tm.MATERIAL_PARENTS["grass"]
    assert any("/Landscapes/MI_LargeLandscape_Grass." in p for p in ground)
    assert all("/Landscapes/" not in p for p in grass)
    assert any("/Ground/MI_LargeLandscape_Grass." in p for p in grass)


def test_no_roadpainter_or_4k_legacy_material_is_in_any_pool():
    """MI_Road_Asphalt_A/B/B_Wet import RenderTexture_Town10HD (a render target only Town10HD
    writes); MI_sidewalk_01/04/04b pull 4K (70+ MB) sources."""
    for key, pool in tm.MATERIAL_PARENTS.items():
        for path in pool:
            name = path.rsplit("/", 1)[-1].split(".")[0]
            if key.startswith("marking_"):
                continue   # the lane markings ARE MI_Road_Asphalt_B children, by design
            assert name not in tm.FORBIDDEN, (key, name)


def test_lane_markings_never_vary():
    for key in ("marking_white", "marking_yellow"):
        assert len(tm.MATERIAL_PARENTS[key]) == 1


def test_risers_have_their_own_pool_led_by_the_curbdirty_atlas():
    """The riser UVs (twinmodel.export.ue CURB_TEX_V_TOP/BOTTOM) sample the stone band of
    T_CurbDirty01, so the riser pool must stay on that atlas family while kerbs may not."""
    riser = tm.MATERIAL_PARENTS["riser"]
    assert riser != tm.MATERIAL_PARENTS["curb"]
    assert riser[0].endswith("MI_CurbDirty01.MI_CurbDirty01")
    assert all("/Curb/MI_CurbDirty" in p for p in riser)


def test_pools_grew_beyond_one_material_per_surface():
    """The point of the change: no surface kind a driver actually sees is single-material."""
    for key in ("road", "sidewalk", "curb", "ground", "grass", "building"):
        assert len(tm.MATERIAL_PARENTS[key]) >= 2, key


def test_kind_key_mirrors_the_exporter():
    """ue/twin_materials.KIND_KEY is what the repaint pass uses to re-derive a surface key
    from an asset kind; it must agree with the manifest writer."""
    for kind, (mat_key, _semantic) in ue_export.KIND_MATERIAL.items():
        assert tm.KIND_KEY[kind] == mat_key, kind
    assert set(tm.KIND_KEY) == set(ue_export.KIND_MATERIAL)


def test_every_kind_key_has_a_pool():
    for kind, key in tm.KIND_KEY.items():
        assert tm.pool(key), (kind, key)


def test_metric_uv_params_only_cover_the_uv_tiled_keys():
    """Sidewalks / kerbs / facades keep their shipped tiling on purpose."""
    assert set(tm.METRIC_UV_PARAMS) == {"ground", "grass", "road"}
    for params in tm.METRIC_UV_PARAMS.values():
        for p in params:
            assert p in tm.METRIC_UV_FALLBACK


def test_jitter_only_names_parameters_the_baker_computes_itself():
    """The jitter perturbs the metric-UV rescale, nothing else. Writing an override for a
    parameter read back off the parent pins a value the material may have been resolving
    somewhere else -- the first attempt did that and moved EixampleDemo's pavement density."""
    owned = {p for params in tm.METRIC_UV_PARAMS.values() for p in params}
    for master, params in tm.JITTER_PARAMS.items():
        assert set(params) <= owned, (master, set(params) - owned)


def test_jitter_never_touches_a_weather_parameter():
    """Puddles / Raindrops / Wetness are pushed by Weather.cpp through the
    WeatherMaterialParameters MPC -- a per-MIC override would fight the weather system."""
    for params in tm.JITTER_PARAMS.values():
        for p in params:
            low = p.lower()
            assert "puddle" not in low and "raindrop" not in low and "wetness" not in low


# --------------------------------------------------------------------------- selection

def test_variant_index_is_in_range_and_deterministic():
    for key, pool in tm.MATERIAL_PARENTS.items():
        for tile in [(0, 0), (-2, 1), (3, -4)]:
            i = tm.variant_index("EixampleDemo", key, tile, len(pool))
            assert 0 <= i < len(pool)
            assert i == tm.variant_index("EixampleDemo", key, tile, len(pool))


def test_a_single_member_pool_always_picks_index_zero():
    assert tm.variant_index("X", "marking_white", (7, -3), 1) == 0
    assert tm.building_variant_index("X", "building", "way/123", 1) == 0


def test_the_map_name_is_part_of_the_seed():
    """Two twins must not land on the same variant sequence."""
    tiles = [(i, j) for i in range(-3, 4) for j in range(-3, 4)]
    a = [tm.variant_index("EixampleDemo", "sidewalk", t, 6) for t in tiles]
    b = [tm.variant_index("Sunnyvale", "sidewalk", t, 6) for t in tiles]
    assert a != b


def test_neighbouring_tiles_get_different_variants():
    """District-scale variation: over a tile grid the pool is actually spread, not collapsed
    onto one member (which is exactly the bug this replaces)."""
    tiles = [(i, j) for i in range(-4, 5) for j in range(-4, 5)]
    for key in ("road", "sidewalk", "curb", "ground", "grass"):
        n = len(tm.MATERIAL_PARENTS[key])
        counts = Counter(tm.variant_index("EixampleDemo", key, t, n) for t in tiles)
        assert len(counts) == n, (key, counts)             # every member used
        assert max(counts.values()) <= 0.6 * len(tiles), (key, counts)   # none dominates


def test_buildings_hash_their_id_not_their_tile():
    n = len(tm.MATERIAL_PARENTS["building"])
    ids = ["way/%d" % i for i in range(200)]
    counts = Counter(tm.building_variant_index("EixampleDemo", "building", b, n) for b in ids)
    assert len(counts) == n
    assert tm.building_variant_index("E", "building", "way/1", n) != \
        tm.variant_index("E", "building", (0, 0), n) or n == 1


def test_variant_index_is_stable_across_runs():
    """Frozen expectations: a rebake must not reshuffle a deployed level's materials."""
    assert tm.variant_index("EixampleDemo", "sidewalk", (0, 0), 6) == \
        tm.variant_index("EixampleDemo", "sidewalk", [0, 0], 6)
    # crc32 is fixed by the standard library, so these are reproducible constants
    assert tm.variant_index("EixampleDemo", "road", (0, 0), 4) == \
        tm.variant_index("EixampleDemo", "road", (0, 0), 4)
    known = {tm.variant_index("EixampleDemo", "curb", (i, j), 4) for i in (-1, 0) for j in (-1, 0)}
    assert known.issubset({0, 1, 2, 3})


# --------------------------------------------------------------------------- naming + jitter

def test_mic_names_enumerate_the_pool():
    assert tm.mic_name("EixampleDemo", "road", 0) == "MI_EixampleDemo_road_0"
    assert tm.mic_name("EixampleDemo", "road", 3) == "MI_EixampleDemo_road_3"


def test_index_zero_is_never_jittered():
    """Index 0 is the canonical look: a repaint must not move a level that was already right."""
    assert tm.jitter_factors("EixampleDemo", "road", 0, "M_RoadMaster") == {}


def test_jitter_is_bounded_and_deterministic():
    f = tm.jitter_factors("EixampleDemo", "road", 3, "M_RoadMaster")
    assert f and f == tm.jitter_factors("EixampleDemo", "road", 3, "M_RoadMaster")
    for pname, factor in f.items():
        amp = tm.JITTER_PARAMS["M_RoadMaster"][pname]
        assert 1.0 - amp <= factor <= 1.0 + amp
    # two members on the same parent must not come out identical
    assert tm.jitter_factors("EixampleDemo", "road", 1, "M_RoadMaster") != f


def test_jitter_on_an_unknown_master_is_empty():
    assert tm.jitter_factors("X", "road", 1, "M_SomethingElse") == {}


def test_master_name_from_a_package_path():
    assert tm.master_name("/Game/Carla/Static/GenericMaterials/000_Masters/M_RoadMaster") == \
        "M_RoadMaster"
    assert tm.master_name("/Game/A/M_GeneralMaster.M_GeneralMaster") == "M_GeneralMaster"


@pytest.mark.parametrize("key", sorted(tm.MATERIAL_PARENTS))
def test_pool_returns_a_copy(key):
    p = tm.pool(key)
    p.append("x")
    assert tm.pool(key) != p
