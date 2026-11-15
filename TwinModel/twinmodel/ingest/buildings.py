"""Real 3D building providers, chosen by where the twin is (twinmodel.export.realbuildings does the rest).

Each provider is a module with ``COVERAGE`` (s, w, n, e; or a ``covers(bbox)`` function when its data come in
pieces) and ``load(build, man, looks, **opts) -> (list[RealBuilding], Source)``. ``choose`` picks the first
provider that covers the twin's bbox; a new country or city is one more module and one line in ``PROVIDERS``.

| name | where | data |
| --- | --- | --- |
| ``3dbag`` | the Netherlands | 3DBAG LoD 2.2 (CC BY 4.0) + BAG uses (CC0) |
| ``nyc3d`` | New York City | NYC 3D Building Model LoD 2 + Building Footprints + PLUTO (NYC Open Data) |
| ``plateau`` | Japanese cities in ``buildings_plateau.DATASETS`` (Shibuya) | PLATEAU CityGML LoD 2 / LoD 1 (MLIT) |
"""
from __future__ import annotations

import importlib
from typing import Optional

# name -> module (in this package); the order is the order ``choose`` tries them
PROVIDERS = {
    "3dbag": "buildings_3dbag",
    "nyc3d": "buildings_nyc",
    "plateau": "buildings_plateau",
}


def module(name: str):
    try:
        return importlib.import_module("." + PROVIDERS[name], __package__)
    except KeyError:
        raise KeyError(f"unknown building provider {name!r}; known: {sorted(PROVIDERS)}") from None


def covers(name: str, bbox_swne) -> bool:
    mod = module(name)
    s, w, n, e = bbox_swne
    cs, cw, cn, ce = mod.COVERAGE
    inside = cs <= s and n <= cn and cw <= w and e <= ce
    return inside and (mod.covers(bbox_swne) if hasattr(mod, "covers") else True)


def choose(bbox_swne) -> Optional[str]:
    """The first provider whose coverage holds the bbox, or None (the twin keeps its OSM buildings)."""
    return next((p for p in PROVIDERS if covers(p, bbox_swne)), None)


def load(name: str, build, man: dict, looks: bool = True, **opts):
    return module(name).load(build, man, looks=looks, **opts)
