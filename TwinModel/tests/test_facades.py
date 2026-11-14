"""Tests for twinmodel.export.facades (facade styles, wall/roof UVs, clipping, textures)."""
from __future__ import annotations

import numpy as np
import pytest

from twinmodel.export import facades as fc


def _info(**kw):
    base = dict(id="NL.IMBAG.Pand.1", year=1960, uses={"woonfunctie": 100}, height_m=9.0, area_m2=120.0)
    base.update(kw)
    return fc.BuildingInfo(**base)


@pytest.mark.parametrize("kw,style,shop", [
    (dict(year=1920), "brick_old", False),
    (dict(year=1955), "brick_postwar", False),
    (dict(year=1985), "brick_modern", False),
    (dict(year=1960, uses={"woonfunctie": 300, "winkelfunctie": 120}, height_m=10.0), "brick_postwar", True),
    (dict(year=1970, uses={"kantoorfunctie": 2000}, height_m=20.0), "office_concrete", False),
    (dict(year=2005, uses={"kantoorfunctie": 2000}, height_m=40.0), "office_glass", False),
    (dict(year=1990, uses={"industriefunctie": 900}), "industrial", False),
    (dict(year=1975, uses={"winkelfunctie": 300}, height_m=4.5), "shopfront", False),
    (dict(year=1980, uses={}, height_m=2.5, area_m2=15.0), "blank_brick", False),
    (dict(year=1980, uses={}, osm_category="warehouse", area_m2=800.0), "industrial", False),
])
def test_classify(kw, style, shop):
    assert fc.classify(_info(**kw)) == (style, shop)


def test_classify_render_is_deterministic_share_of_modern_homes():
    picks = [fc.classify(_info(id="b%d" % i, year=2005))[0] for i in range(300)]
    share = picks.count("render_modern") / len(picks)
    assert set(picks) <= {"render_modern", "brick_modern"}
    assert 0.2 < share < 0.45
    assert picks == [fc.classify(_info(id="b%d" % i, year=2005))[0] for i in range(300)]


def test_storeys_fit_the_eaves():
    n, h = fc.storeys_for(9.3, 2.9)
    assert n == 3 and abs(n * h - 9.3) < 1e-9
    assert fc.storeys_for(0.0, 3.0) == (1, 3.0)
    # a 1.2 m wall would make a 1.2 m storey: keep the nominal one
    assert fc.storeys_for(1.2, 3.0) == (1, 3.0)


def _wall(x0, x1, z0, z1, y=0.0):
    """A vertical wall facing -y (outside towards -y), CCW from outside."""
    v = np.array([[x0, y, z0], [x1, y, z0], [x1, y, z1], [x0, y, z1]], float)
    return v, np.array([0.0, -1.0, 0.0])


def test_wall_uv_whole_bays_and_storeys():
    v, nrm = _wall(10.0, 17.3, 5.0, 14.0)
    uv = fc.wall_uv(v, nrm, base_z=5.0, storey_h=3.0, bay_m=2.4)
    bays = round(7.3 / 2.4)
    # u spans a whole number of bays (in texture tiles of TEX_BAYS)
    assert np.isclose(np.ptp(uv[:, 0]) * fc.TEX_BAYS, bays)
    assert np.isclose(uv[:, 0].min(), 0.0)
    # v: 0 at the floor, 3 storeys up at the eaves, growing downwards (glTF)
    assert np.isclose(uv[0, 1], 0.0)
    assert np.isclose(uv[2, 1], -3.0 / fc.TEX_STOREYS)


def test_wall_uv_pieces_share_the_mapping_of_their_ring():
    v, nrm = _wall(0.0, 6.0, 0.0, 9.0)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    (vb, _), (va, _) = fc.clip_triangles(v, tris, 4.0)
    full = fc.wall_uv(v, nrm, 0.0, 3.0, 3.0, ring=v)
    upper = fc.wall_uv(va, nrm, 0.0, 3.0, 3.0, ring=v)
    assert np.isclose(upper[:, 0].min(), full[:, 0].min())
    assert np.isclose(upper[:, 0].max(), full[:, 0].max())


def _area(v, t):
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum()


def test_clip_triangles_keeps_area_and_winding():
    v, nrm = _wall(0.0, 5.0, 0.0, 8.0)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    (vb, tb), (va, ta) = fc.clip_triangles(v, tris, 3.0)
    assert np.isclose(_area(vb, tb), 15.0) and np.isclose(_area(va, ta), 25.0)
    assert vb[:, 2].max() <= 3.0 + 1e-9 and va[:, 2].min() >= 3.0 - 1e-9
    for vv, tt in ((vb, tb), (va, ta)):
        n = np.cross(vv[tt[:, 1]] - vv[tt[:, 0]], vv[tt[:, 2]] - vv[tt[:, 0]])
        assert (n @ np.cross(v[1] - v[0], v[2] - v[0]) > 0).all()


def test_clip_triangles_plane_outside():
    v, _ = _wall(0.0, 5.0, 0.0, 8.0)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    (vb, tb), (va, ta) = fc.clip_triangles(v, tris, -1.0)
    assert len(tb) == 0 and len(ta) == 2


def test_roof_uv_north_up():
    v = np.array([[250.0, 500.0, 20.0], [500.0, 250.0, 20.0]])
    uv = fc.roof_uv(v, (1, 1), 250.0)
    assert np.allclose(uv, [[0.0, 0.0], [1.0, 1.0]])


@pytest.mark.parametrize("name", sorted(fc.STYLES))
def test_synth_facade_tiles_and_is_deterministic(name):
    st = fc.STYLES[name]
    a, n, o = fc.synth_facade(st, 0, px=256)
    assert a.shape == (256, 256, 3) and n.shape == (256, 256, 3) and o.shape == (256, 256, 4)
    assert a.dtype == np.uint8
    b, _, _ = fc.synth_facade(st, 0, px=256)
    assert np.array_equal(a, b)
    # seamless: the wrap-around step is no larger than the steps inside the texture (brick joints)
    for axis in (0, 1):
        x = np.moveaxis(a.astype(int), axis, 0)
        inner = np.abs(np.diff(x, axis=0)).mean(axis=(1, 2)).max()
        wrap = np.abs(x[0] - x[-1]).mean()
        assert wrap <= 1.1 * inner + 1
    if not st.blank:
        # windows: some glossy pixels in the roughness channel
        assert (o[..., 1] < 40).any()


def test_variants_differ():
    st = fc.STYLES["brick_postwar"]
    a0, _, _ = fc.synth_facade(st, 0, px=128)
    a1, _, _ = fc.synth_facade(st, 1, px=128)
    assert not np.array_equal(a0, a1)
    assert fc.variant_of("brick_postwar", "x") in (0, 1)
    assert fc.material_key("brick_postwar", 1) == "facade_brick_postwar_1"


def test_ortho_tile_and_mean_colour():
    # 100 m x 100 m photo at 1 m, x in [0, 100], y in [0, 100], north up; left half red
    img = np.zeros((100, 100, 3), np.uint8)
    img[:, :50] = (200, 0, 0)
    img[:, 50:] = (0, 0, 200)
    tf = (1.0, 0.0, 0.0, 0.0, -1.0, 100.0)
    t = fc.ortho_tile(img, tf, (0, 0), 100.0, 10)
    assert t.shape == (10, 10, 3)
    assert tuple(t[0, 0]) == (200, 0, 0) and tuple(t[9, 9]) == (0, 0, 200)
    assert fc.ortho_tile(img, tf, (5, 5), 100.0, 10) is None
    ring = np.array([[10.0, 10.0], [40.0, 10.0], [40.0, 40.0], [10.0, 40.0]])
    assert fc.mean_colour(img, tf, ring) == (200, 0, 0)


def test_height_to_normal_flat():
    n = fc.height_to_normal(np.zeros((8, 8)))
    assert (n == (128, 128, 255)).all()


def test_procedural_capable_buildings():
    assert fc.procedural_ok("brick_postwar", 12.0)
    assert not fc.procedural_ok("brick_postwar", 6.0)          # too low for door + window + top rows
    assert not fc.procedural_ok("industrial", 20.0)            # no warehouse kit
    assert not fc.procedural_ok("blank_brick", 20.0)
    assert not fc.procedural_ok("brick_postwar", 9.55)         # Skysc21 needs 5.08 + 3.5 + 0.99 m
    assert fc.procedural_family("office_glass", 30.0) == "Skysc19"
    assert fc.procedural_family("office_glass", 12.0) == "Skysc21"   # too low for 5 m office storeys
    assert fc.procedural_ok("office_glass", 12.0)
    assert fc.procedural_family("brick_old", 30.0) == "Skysc21"
