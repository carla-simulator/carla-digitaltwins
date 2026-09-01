"""Layer-aware refinement (refine.refine_layers, surfaces.build_surfaces(refined_drivable={...})).

A ground street (layer 0) and a diagonal viaduct (layer 1, bridge=yes) crossing it; a synthetic
"imagery" mask that paints the deck across the street and the street 1.5 m wider than OSM
everywhere else. After refinement the street under the deck and the deck itself are unchanged,
the street outside the deck footprint follows the mask, and the layers are kept apart.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, Point

from twinmodel import refine
from twinmodel.ingest.imagery import OrthoImage
from twinmodel.model import Lane, Road, TwinModel
from twinmodel.surfaces import build_surfaces
from tests.synthetic import _empty

HALF_W = 3.5          # one driving lane per side
WIDEN = 1.5           # the mask shows the street this much wider than OSM (per side)


def _lanes() -> list[Lane]:
    return [Lane(id=1, type="driving", width=HALF_W), Lane(id=-1, type="driving", width=HALF_W)]


def layered_model() -> TwinModel:
    m = _empty("layers")
    m.roads = [
        Road(id="street", reference_line=LineString([(-80, 0, 0), (80, 0, 0)]), lanes=_lanes(),
             highway="residential"),
        Road(id="viaduct", reference_line=LineString([(-60, -60, 6), (60, 60, 6)]), lanes=_lanes(),
             highway="secondary", tags={"layer": "1", "bridge": "yes"}),
    ]
    build_surfaces(m)
    return m


def blank_ortho(half: float = 90.0, dx: float = 0.25) -> OrthoImage:
    n = int(round(2 * half / dx))
    return OrthoImage(np.zeros((n, n, 3), dtype=np.uint8), x0=-half + dx / 2, y0=-half + dx / 2,
                      dx=dx, dy=dx, source="synthetic")


def _surface(m: TwinModel, layer: int):
    parts = [s for s in m.surfaces_of("drivable") if refine.surface_layer(s) == layer]
    assert parts, f"no drivable surface on layer {layer}"
    return parts


@pytest.fixture(scope="module")
def setup():
    m = layered_model()
    ortho = blank_ortho()
    groups = refine.drivable_by_layer(m)
    assert sorted(groups) == [0, 1]
    street, deck = groups[0], groups[1]
    # "imagery": the street 1.5 m wider than OSM, and the deck painted over everything under it
    mask = refine.rasterize(street.buffer(WIDEN), ortho) | refine.rasterize(deck, ortho)
    return m, ortho, mask, street, deck


def test_model_has_one_drivable_surface_per_layer(setup):
    m, *_ = setup
    assert m.metadata["surfaces"]["drivable_layers"] == [0, 1]
    assert m.metadata["surfaces"]["drivable_area_by_layer"].keys() == {"0", "1"}
    assert refine.ground_layer([0, 1]) == 0
    assert refine.ground_layer([None]) is None
    assert refine.ground_layer([-1, 1]) == 1
    assert refine.ground_layer([-2, -1]) == -1


def test_deck_footprint_is_the_grown_elevated_surfaces(setup):
    m, _, _, _, deck = setup
    fp = refine.deck_footprint(m)
    assert fp.covers(deck)
    assert fp.area == pytest.approx(deck.buffer(refine.DECK_MASK_MARGIN_M, join_style="mitre",
                                                mitre_limit=2.0).area, rel=0.02)
    assert refine.deck_footprint(_empty("flat")).is_empty


def test_mask_under_the_deck_is_the_ground_prior(setup):
    m, ortho, mask, street, deck = setup
    fp = refine.deck_footprint(m)
    out = refine.mask_out_decks(mask, fp, street, ortho)
    d = refine.rasterize(fp, ortho)
    assert np.array_equal(out[d], refine.rasterize(street, ortho)[d])
    assert np.array_equal(out[~d], mask[~d])


def test_without_the_layer_logic_the_street_fuses_with_the_deck(setup):
    # control: plain refine_drivable on the same mask moves the street boundary under the deck
    m, ortho, mask, street, deck = setup
    fp = refine.deck_footprint(m)
    fused, st = refine.refine_drivable(street, mask, ortho)
    assert st["n_frozen"] == 0
    changed_under_deck = fused.intersection(fp).symmetric_difference(street.intersection(fp)).area
    assert changed_under_deck > 5.0


def test_refine_layers_keeps_street_under_deck_and_deck_and_refines_the_rest(setup):
    m, ortho, mask, street, deck = setup
    fp = refine.deck_footprint(m)
    refined, st, ground_mask = refine.refine_layers(m, ortho, mask=mask)
    assert list(refined) == [0]
    assert st["layers"]["refined"] == 0 and st["layers"]["kept"] == [1]
    assert st["n_frozen"] > 0
    assert st["layers"]["ground_prior_under_deck_m2"] > 0
    ground = refined[0]
    # (a) under the deck footprint: the OSM geometry, untouched
    under = ground.intersection(fp).symmetric_difference(street.intersection(fp)).area
    assert under < 0.5, under
    # (b) away from the deck the street followed the mask: ~1.5 m wider per side
    for x in (-50.0, 40.0):
        assert ground.contains(Point(x, HALF_W + WIDEN - 0.3))
        assert not ground.contains(Point(x, HALF_W + WIDEN + 0.5))
        assert not street.contains(Point(x, HALF_W + 0.3))
    outside = ground.difference(fp).area / street.difference(fp).area
    assert 1.3 < outside < 1.5
    # (c) the model: the deck surface is the lane-graph one, the street is from imagery
    build_surfaces(m, refined_drivable=refined)
    assert m.metadata["surfaces"]["drivable_layers"] == [0, 1]
    assert m.metadata["surfaces"]["refined_iou_by_layer"].keys() == {"0"}
    deck_after = refine.drivable_by_layer(m)[1]
    assert deck_after.symmetric_difference(deck).area < 1e-6
    assert {s.source for s in _surface(m, 1)} == {"osm_tags"}
    assert {s.source for s in _surface(m, 0)} == {"imagery"}
    street_after = refine.drivable_by_layer(m)[0]
    assert street_after.symmetric_difference(ground).area < 1.0
    # and the deck is still its own surface: nothing fused across layers
    assert not any(s.geometry.covers(Point(0, 40)) for s in _surface(m, 0))
    assert any(s.geometry.covers(Point(40, 40)) for s in _surface(m, 1))


def test_bare_refined_polygon_is_taken_as_the_ground_layer(setup):
    m, ortho, mask, street, deck = setup
    refined, _, _ = refine.refine_layers(m, ortho, mask=mask)
    build_surfaces(m, refined_drivable=refined[0])          # legacy call form
    assert m.metadata["surfaces"]["drivable_layers"] == [0, 1]
    assert refine.drivable_by_layer(m)[1].symmetric_difference(deck).area < 1e-6
    assert {s.source for s in _surface(m, 1)} == {"osm_tags"}
    assert {s.source for s in _surface(m, 0)} == {"imagery"}
    build_surfaces(m)                                         # back to the lane graph
    assert {s.source for s in m.surfaces_of("drivable")} == {"osm_tags"}
    assert refine.drivable_by_layer(m)[0].symmetric_difference(street).area < 1e-6


def test_single_layer_dict_is_the_plain_refinement():
    m = _empty("flat")
    m.roads = [Road(id="street", reference_line=LineString([(-80, 0, 0), (80, 0, 0)]),
                    lanes=_lanes(), highway="residential")]
    build_surfaces(m)
    ortho = blank_ortho()
    prior = refine.drivable_by_layer(m)
    assert list(prior) == [None]
    mask = refine.rasterize(prior[None].buffer(WIDEN), ortho)
    refined, st, _ = refine.refine_layers(m, ortho, mask=mask)
    assert list(refined) == [None] and st["n_frozen"] == 0
    build_surfaces(m, refined_drivable=refined)
    assert "drivable_layers" not in m.metadata["surfaces"]
    assert m.metadata["surfaces"]["drivable_source"] == "imagery"
    assert m.metadata["surfaces"]["drivable_area"] > prior[None].area * 1.3
