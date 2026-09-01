"""LocalFrame: WGS84 <-> local ENU metres (transverse mercator centred at the origin)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Transformer


@dataclass(frozen=True)
class LocalFrame:
    origin_lat: float
    origin_lon: float

    @property
    def proj4(self) -> str:
        return (f"+proj=tmerc +lat_0={self.origin_lat} +lon_0={self.origin_lon} +k=1 "
                f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")

    @property
    def crs(self) -> CRS:
        return CRS.from_proj4(self.proj4)

    def _to_local(self) -> Transformer:
        return Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)

    def _to_wgs(self) -> Transformer:
        return Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def to_local(self, lon, lat):
        """(lon, lat) -> (x, y) metres. Scalars or arrays."""
        return self._to_local().transform(np.asarray(lon), np.asarray(lat))

    def to_wgs84(self, x, y):
        """(x, y) metres -> (lon, lat)."""
        return self._to_wgs().transform(np.asarray(x), np.asarray(y))

    def transformer_from(self, crs: str | CRS) -> Transformer:
        """Transformer from an arbitrary CRS (e.g. EPSG:25831 for ICGC) into model space."""
        return Transformer.from_crs(CRS.from_user_input(crs), self.crs, always_xy=True)

    @classmethod
    def from_bbox(cls, south: float, west: float, north: float, east: float) -> "LocalFrame":
        return cls(origin_lat=(south + north) / 2.0, origin_lon=(west + east) / 2.0)
