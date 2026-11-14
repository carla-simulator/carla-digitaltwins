"""LocalFrame: WGS84 <-> local ENU metres (transverse mercator centred at the origin)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer


def _proj4_str(origin_lat: float, origin_lon: float) -> str:
    return (f"+proj=tmerc +lat_0={origin_lat} +lon_0={origin_lon} +k=1 "
            f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")


@lru_cache(maxsize=64)
def _cached_frame(origin_lat: float, origin_lon: float) -> tuple[CRS, Transformer, Transformer]:
    crs = CRS.from_proj4(_proj4_str(origin_lat, origin_lon))
    to_local = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return crs, to_local, to_wgs


@lru_cache(maxsize=64)
def _cached_transformer_from(origin_lat: float, origin_lon: float, src_wkt: str) -> Transformer:
    crs, _, _ = _cached_frame(origin_lat, origin_lon)
    return Transformer.from_crs(CRS.from_wkt(src_wkt), crs, always_xy=True)


@dataclass(frozen=True)
class LocalFrame:
    origin_lat: float
    origin_lon: float

    @property
    def proj4(self) -> str:
        return _proj4_str(self.origin_lat, self.origin_lon)

    @property
    def crs(self) -> CRS:
        return _cached_frame(self.origin_lat, self.origin_lon)[0]

    def _to_local(self) -> Transformer:
        return _cached_frame(self.origin_lat, self.origin_lon)[1]

    def _to_wgs(self) -> Transformer:
        return _cached_frame(self.origin_lat, self.origin_lon)[2]

    def to_local(self, lon, lat):
        """(lon, lat) -> (x, y) metres. Scalars or arrays."""
        return self._to_local().transform(np.asarray(lon), np.asarray(lat))

    def to_wgs84(self, x, y):
        """(x, y) metres -> (lon, lat)."""
        return self._to_wgs().transform(np.asarray(x), np.asarray(y))

    def transformer_from(self, crs: str | CRS) -> Transformer:
        """Transformer from an arbitrary CRS (e.g. EPSG:25831 for ICGC) into model space."""
        src = crs if isinstance(crs, CRS) else CRS.from_user_input(crs)
        return _cached_transformer_from(self.origin_lat, self.origin_lon, src.to_wkt())

    @classmethod
    def from_bbox(cls, south: float, west: float, north: float, east: float) -> "LocalFrame":
        return cls(origin_lat=(south + north) / 2.0, origin_lon=(west + east) / 2.0)
