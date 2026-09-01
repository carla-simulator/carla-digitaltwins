"""Region profiles — every dimensional / urban-form constant of the Twin Model in ONE place.

    from twinmodel import profiles
    P = profiles.get()            # the active StreetProfile (default EU_DENSE)
    P.lane.width_for("residential")
    with profiles.use(profiles.US_SUBURBAN): ...   # or profiles.activate("us_suburban")

Rules
- Modules never keep their own copies of these numbers: they call ``profiles.get()`` *at call
  time* (not import time) so tests and the CLI can switch profiles.
- Pure numerical tolerances (precision grids, triangle area epsilons, k-neighbours) stay in the
  modules; they are not regional.
- ``EU_DENSE`` reproduces the values the pipeline shipped with on 2026-09-01 exactly; changing
  them changes the Eixample regression build.
- Units: metres, degrees, m/s. US values are converted from feet in the comments so the source
  standard is visible (FHWA MUTCD 2009/2023, AASHTO Green Book 7th ed., NACTO Urban Street
  Design Guide, ADA/PROWAG for sidewalks).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Iterator, Literal, Optional

FT = 0.3048
IN = 0.0254

MarkColor = Literal["white", "yellow"]
ParkingSides = Literal["both", "right", "left", "none"]


# --------------------------------------------------------------------------- building blocks

@dataclass(frozen=True)
class ClassDefaults:
    """Defaults for one OSM ``highway=*`` class when tags are silent."""
    lane_width: float          # per driving lane
    lanes: int                 # total lanes (both directions); oneway -> handled by the graph
    sidewalk: Optional[float]  # per side; None = no sidewalk
    verge: Optional[float] = None      # planting strip between curb and sidewalk (US); None = none
    parking: ParkingSides = "none"     # on-street parking assumed when tags are silent
    center_marking: bool = True        # two-way roads get a centre line


@dataclass(frozen=True)
class LaneRules:
    classes: dict[str, ClassDefaults]
    fallback: ClassDefaults
    min_width: float           # never narrower than this (also the taper floor)
    max_width: float
    canyon_max_width: float    # building-derived cross-sections widen driving lanes up to this
    bike_width: float
    parking_width: dict[str, float]    # by orientation: parallel / diagonal / perpendicular
    parking_min: float         # canyon leftover -> parking lanes of this width range
    parking_max: float
    drivable_classes: frozenset[str]
    service_min_length: float  # unnamed service ways shorter than this are not roads

    def for_class(self, highway: str) -> ClassDefaults:
        return self.classes.get(highway, self.fallback)

    def width_for(self, highway: str) -> float:
        return self.for_class(highway).lane_width


@dataclass(frozen=True)
class SidewalkRules:
    min_width: float
    max_width: float
    canyon_fraction: float     # canyon regime, no mapped footway: sidewalk per side = this x street width
    z: float                   # raised above the road datum
    curb_height: float
    verge_z: float             # planting strip height above datum (0 = at curb top level)
    search_m: float            # sidewalk=separate: look for footway=sidewalk ways this far out
    parallel_deg: float
    sample_m: float


@dataclass(frozen=True)
class MarkingRules:
    width: float
    center_color: MarkColor    # two-way centre line
    lane_color: MarkColor      # between same-direction lanes
    edge_color: MarkColor
    broken_dash: float
    broken_gap: float
    z: float                   # lift above the surface in the mesh


@dataclass(frozen=True)
class CrossingRules:
    width: float               # along s
    z: float
    keep_m: float              # crossing stays whole on its road: cut >= this past the node
    near_cut_m: float          # crossing nodes this close to a trim cut pull the cut back


@dataclass(frozen=True)
class JunctionRules:
    cluster_m: float           # cluster intersection nodes closer than this joined by short ways
    trim_margin_m: float       # extra distance outside the cluster hull where roads are cut
    through_deg: float         # |heading change| below this = through movement
    uturn_deg: float           # |heading change| above this = u-turn (never connected)
    through_align_m: float     # a through departure must overlap the arrival laterally (+ this)
    signal_search_m: float     # traffic_signals node within this of a junction hull -> lights
    signal_lateral_m: float    # signal placed this far outside the carriageway edge
    plaza_radius_m: float      # corner void radius around a junction centre
    chamfer_scan_m: float      # look for the end of the building faces this far from a junction end
    dead_end_stub_m: float     # dead-end stubs off a junction shorter than this are absorbed
    stub_m: float              # trimmed remnants shorter than this are absorbed into their neighbour
    short_road_m: float        # non-junction roads shorter than this merge into a neighbour
    band_overlap_m2: float     # a road's full band may not cover another road's carriageway more
    plaza_sidewalk_m: float = 4.5     # sidewalk band along the corner buildings when no arm has one
    chamfer_allowance_m: float = 15.0  # how far beyond an arm's face line a corner chamfer may open


@dataclass(frozen=True)
class GeometryRules:
    min_road_length: float
    connect_sample_m: float    # connecting road sampling step
    simplify_m: float          # Douglas-Peucker tolerance on trimmed reference lines
    width_step_m: float        # carriageway width jumps larger than this get reconciled/tapered
    taper_max_m: float
    taper_pieces_max: int
    jog_max_m: float           # a lateral jog: segment shorter than this ...
    jog_min_turn_deg: float    # ... turning at least this at both ends, same heading after
    jog_transition_m: float
    street_width_outlier: float  # a piece's street width may deviate this much from its street's median


@dataclass(frozen=True)
class StreetSpaceRules:
    canyon_min_fraction: float  # building face hit on >= this fraction of samples, both sides
    max_face_dist_m: float      # beyond this we don't call it a canyon
    face_sample_step_m: float
    building_pad_m: float       # footprints are drawn slightly inside the real face
    face_tol_m: float           # a face further out than the street's width + this has ended (chamfer)
    blocker_min_dist_m: float
    ground_reach_m: float       # ground fill: within this of a sidewalk/drivable surface
    sidewalk_to_face_max_m: float  # sidewalk band may extend at most this far from the carriageway
    plaza_canyon_min_fraction: float = 0.5  # mean canyon fraction of a junction's arms to trust the buildings


@dataclass(frozen=True)
class ElevationRules:
    resample_m: float           # along-road DEM sampling step
    smooth_window_m: float      # Savitzky-Golay window on the sampled profile
    datum_max_dist_m: float     # road datum blends to the DEM beyond this distance from any road
    junction_blend_m: float
    connecting_blend_m: float
    mesh_grid_m: float          # subdivision grid for surfaces when elevation is present


@dataclass(frozen=True)
class DataSources:
    """Ordered preference of imagery / DEM providers (names understood by ingest.imagery /
    ingest.elevation). Region-specific open data first, global fallbacks last."""
    ortho: tuple[str, ...]
    dem: tuple[str, ...]


@dataclass(frozen=True)
class StreetProfile:
    name: str
    description: str
    countries: frozenset[str]  # ISO 3166-1 alpha-2 codes this profile is the default for
    lane: LaneRules
    sidewalk: SidewalkRules
    marking: MarkingRules
    crossing: CrossingRules
    junction: JunctionRules
    geometry: GeometryRules
    streetspace: StreetSpaceRules
    elevation: ElevationRules
    sources: DataSources
    drives_on: Literal["right", "left"] = "right"

    def with_(self, **overrides) -> "StreetProfile":
        """Copy with top-level fields replaced (``profile.with_(name="x", lane=...)``)."""
        return replace(self, **overrides)


# --------------------------------------------------------------------------- EU dense (Eixample) — the 2026-09-01 values

_EU_CLASSES = {
    "motorway":       ClassDefaults(3.5,  2, None),
    "motorway_link":  ClassDefaults(3.5,  1, None),
    "trunk":          ClassDefaults(3.5,  2, None),
    "trunk_link":     ClassDefaults(3.5,  1, None),
    "primary":        ClassDefaults(3.5,  2, 2.0),
    "primary_link":   ClassDefaults(3.5,  1, 2.0),
    "secondary":      ClassDefaults(3.25, 2, 2.0),
    "secondary_link": ClassDefaults(3.25, 1, 2.0),
    "tertiary":       ClassDefaults(3.25, 2, 2.0),
    "tertiary_link":  ClassDefaults(3.25, 1, 2.0),
    "unclassified":   ClassDefaults(3.25, 2, 2.0),
    "residential":    ClassDefaults(3.0,  2, 2.0),
    "living_street":  ClassDefaults(3.0,  2, 2.0),
    "pedestrian":     ClassDefaults(3.0,  1, 2.0),
    "service":        ClassDefaults(3.0,  2, None),
}
_DRIVABLE = frozenset({"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
                       "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
                       "residential", "living_street", "service"})

EU_DENSE = StreetProfile(
    name="eu_dense",
    description="Dense European city (Barcelona Eixample calibration): buildings at the curb, "
                "3.0–3.5 m lanes, white centre lines, 4 m zebra crossings.",
    countries=frozenset({"ES", "PT", "FR", "IT", "DE", "AT", "CH", "NL", "BE", "LU", "DK", "SE",
                         "NO", "FI", "PL", "CZ", "SK", "HU", "GR", "RO", "BG", "HR", "SI", "EE",
                         "LV", "LT", "IE"}),
    lane=LaneRules(
        classes=_EU_CLASSES, fallback=ClassDefaults(3.25, 2, 2.0),
        min_width=2.75, max_width=3.75, canyon_max_width=3.5, bike_width=1.5,
        parking_width={"parallel": 2.0, "diagonal": 4.5, "perpendicular": 5.0},
        parking_min=2.0, parking_max=2.5, drivable_classes=_DRIVABLE, service_min_length=30.0),
    sidewalk=SidewalkRules(min_width=1.5, max_width=6.0, canyon_fraction=0.22, z=0.15,
                           curb_height=0.15, verge_z=0.15, search_m=12.0, parallel_deg=15.0, sample_m=5.0),
    marking=MarkingRules(width=0.12, center_color="white", lane_color="white", edge_color="white",
                         broken_dash=2.0, broken_gap=4.0, z=0.002),
    crossing=CrossingRules(width=4.0, z=0.003, keep_m=2.5, near_cut_m=5.0),
    junction=JunctionRules(cluster_m=30.0, trim_margin_m=2.0, through_deg=30.0, uturn_deg=150.0,
                           through_align_m=1.0, signal_search_m=25.0, signal_lateral_m=0.5,
                           plaza_radius_m=45.0, chamfer_scan_m=60.0, dead_end_stub_m=10.0,
                           stub_m=3.0, short_road_m=5.0, band_overlap_m2=0.5),
    geometry=GeometryRules(min_road_length=1.0, connect_sample_m=1.0, simplify_m=0.1,
                           width_step_m=1.0, taper_max_m=15.0, taper_pieces_max=3, jog_max_m=5.0,
                           jog_min_turn_deg=45.0, jog_transition_m=10.0, street_width_outlier=0.25),
    streetspace=StreetSpaceRules(canyon_min_fraction=0.6, max_face_dist_m=40.0, face_sample_step_m=4.0,
                                 building_pad_m=0.3, face_tol_m=1.5, blocker_min_dist_m=1.0,
                                 ground_reach_m=12.0, sidewalk_to_face_max_m=12.0),
    elevation=ElevationRules(resample_m=2.0, smooth_window_m=10.0, datum_max_dist_m=25.0,
                             junction_blend_m=20.0, connecting_blend_m=15.0, mesh_grid_m=5.0),
    sources=DataSources(ortho=("icgc", "ign_es", "osm_tiles"), dem=("icgc_mdt2m", "ign_wcs", "opentopo", "copernicus_aws")),
)


# --------------------------------------------------------------------------- US urban (downtown grid)
# Lane widths: MUTCD/AASHTO 10–12 ft; NACTO recommends 10 ft (3.0 m) on urban streets, 11 ft
# where buses/trucks run. Sidewalks: PROWAG 5 ft (1.5 m) minimum clear, downtown 8–12 ft.
# Parking: 7–8 ft parallel (2.1–2.4 m), 17–18 ft diagonal/perpendicular. Centre lines yellow
# (MUTCD 3A.05), lane lines white, broken line 10 ft dash / 30 ft gap (MUTCD 3A.06),
# marking width 4–6 in. Crosswalks 6 ft minimum, 10 ft typical (MUTCD 3B.18).

_US_URBAN_CLASSES = {
    "motorway":       ClassDefaults(12 * FT, 6, None, center_marking=False),
    "motorway_link":  ClassDefaults(12 * FT, 1, None, center_marking=False),
    "trunk":          ClassDefaults(12 * FT, 4, 8 * FT),
    "trunk_link":     ClassDefaults(12 * FT, 1, 8 * FT),
    "primary":        ClassDefaults(11 * FT, 4, 8 * FT, parking="both"),
    "primary_link":   ClassDefaults(11 * FT, 1, 8 * FT),
    "secondary":      ClassDefaults(11 * FT, 2, 6 * FT, parking="both"),
    "secondary_link": ClassDefaults(11 * FT, 1, 6 * FT),
    "tertiary":       ClassDefaults(10 * FT, 2, 5 * FT, parking="both"),
    "tertiary_link":  ClassDefaults(10 * FT, 1, 5 * FT),
    "unclassified":   ClassDefaults(10 * FT, 2, 5 * FT, parking="both"),
    "residential":    ClassDefaults(10 * FT, 2, 5 * FT, verge=4 * FT, parking="both", center_marking=False),
    "living_street":  ClassDefaults(10 * FT, 2, 5 * FT, verge=4 * FT, parking="both", center_marking=False),
    "pedestrian":     ClassDefaults(10 * FT, 1, 5 * FT, center_marking=False),
    "service":        ClassDefaults(10 * FT, 2, None, center_marking=False),
}

US_URBAN = StreetProfile(
    name="us_urban",
    description="US downtown / streetcar-suburb grid: 10–11 ft lanes, yellow centre lines, "
                "parking both sides, 5–8 ft sidewalks with planting strips on residential streets.",
    countries=frozenset(),  # chosen for US by density, see choose_for_country
    lane=LaneRules(
        classes=_US_URBAN_CLASSES, fallback=ClassDefaults(10 * FT, 2, 5 * FT, parking="both"),
        min_width=9 * FT, max_width=12 * FT, canyon_max_width=12 * FT, bike_width=5 * FT,
        parking_width={"parallel": 8 * FT, "diagonal": 17 * FT, "perpendicular": 18 * FT},
        parking_min=7 * FT, parking_max=8 * FT, drivable_classes=_DRIVABLE, service_min_length=30.0),
    sidewalk=SidewalkRules(min_width=4 * FT, max_width=16 * FT, canyon_fraction=0.20, z=6 * IN,
                           curb_height=6 * IN, verge_z=6 * IN, search_m=15.0, parallel_deg=15.0, sample_m=5.0),
    marking=MarkingRules(width=4 * IN, center_color="yellow", lane_color="white", edge_color="white",
                         broken_dash=10 * FT, broken_gap=30 * FT, z=0.002),
    crossing=CrossingRules(width=10 * FT, z=0.003, keep_m=2.5, near_cut_m=5.0),
    junction=JunctionRules(cluster_m=40.0, trim_margin_m=2.0, through_deg=30.0, uturn_deg=150.0,
                           through_align_m=1.0, signal_search_m=35.0, signal_lateral_m=0.5,
                           plaza_radius_m=50.0, chamfer_scan_m=60.0, dead_end_stub_m=10.0,
                           stub_m=3.0, short_road_m=5.0, band_overlap_m2=0.5),
    geometry=GeometryRules(min_road_length=1.0, connect_sample_m=1.0, simplify_m=0.1,
                           width_step_m=1.0, taper_max_m=25.0, taper_pieces_max=3, jog_max_m=5.0,
                           jog_min_turn_deg=45.0, jog_transition_m=10.0, street_width_outlier=0.25),
    streetspace=StreetSpaceRules(canyon_min_fraction=0.6, max_face_dist_m=40.0, face_sample_step_m=4.0,
                                 building_pad_m=0.3, face_tol_m=1.5, blocker_min_dist_m=1.0,
                                 ground_reach_m=15.0, sidewalk_to_face_max_m=15.0),
    elevation=ElevationRules(resample_m=2.0, smooth_window_m=10.0, datum_max_dist_m=25.0,
                             junction_blend_m=20.0, connecting_blend_m=15.0, mesh_grid_m=5.0),
    sources=DataSources(ortho=("naip", "osm_tiles"), dem=("usgs_3dep", "opentopo", "copernicus_aws")),
)


# --------------------------------------------------------------------------- US suburban (arterial + subdivision)
# 12 ft arterial lanes (AASHTO), 4-lane divided arterials with medians (clusters up to 60 m),
# residential 26–36 ft curb-to-curb with parking both sides and no centre line, 5 ft sidewalk
# behind a 6 ft planting strip, buildings set back (canyon regime rarely triggers).

_US_SUBURBAN_CLASSES = {
    "motorway":       ClassDefaults(12 * FT, 6, None, center_marking=False),
    "motorway_link":  ClassDefaults(12 * FT, 1, None, center_marking=False),
    "trunk":          ClassDefaults(12 * FT, 4, None),
    "trunk_link":     ClassDefaults(12 * FT, 1, None),
    "primary":        ClassDefaults(12 * FT, 4, 5 * FT, verge=6 * FT),
    "primary_link":   ClassDefaults(12 * FT, 1, 5 * FT),
    "secondary":      ClassDefaults(12 * FT, 4, 5 * FT, verge=6 * FT),
    "secondary_link": ClassDefaults(12 * FT, 1, 5 * FT),
    "tertiary":       ClassDefaults(11 * FT, 2, 5 * FT, verge=6 * FT, parking="both"),
    "tertiary_link":  ClassDefaults(11 * FT, 1, 5 * FT),
    "unclassified":   ClassDefaults(11 * FT, 2, 5 * FT, verge=6 * FT, parking="both"),
    "residential":    ClassDefaults(11 * FT, 2, 5 * FT, verge=6 * FT, parking="both", center_marking=False),
    "living_street":  ClassDefaults(10 * FT, 2, 5 * FT, verge=6 * FT, parking="both", center_marking=False),
    "pedestrian":     ClassDefaults(10 * FT, 1, 5 * FT, center_marking=False),
    "service":        ClassDefaults(11 * FT, 2, None, center_marking=False),
}

US_SUBURBAN = US_URBAN.with_(
    name="us_suburban",
    description="US suburban: 12 ft arterial lanes, divided arterials, residential streets "
                "26–36 ft curb-to-curb without centre lines, 5 ft sidewalks behind 6 ft planting strips.",
    lane=replace(US_URBAN.lane, classes=_US_SUBURBAN_CLASSES,
                 fallback=ClassDefaults(11 * FT, 2, 5 * FT, verge=6 * FT, parking="both"),
                 min_width=10 * FT, canyon_max_width=12 * FT, service_min_length=40.0),
    sidewalk=replace(US_URBAN.sidewalk, max_width=10 * FT),
    junction=replace(US_URBAN.junction, cluster_m=60.0, plaza_radius_m=60.0, signal_search_m=45.0,
                     dead_end_stub_m=15.0),
    geometry=replace(US_URBAN.geometry, taper_max_m=40.0),
    streetspace=replace(US_URBAN.streetspace, ground_reach_m=20.0, sidewalk_to_face_max_m=20.0),
)


# --------------------------------------------------------------------------- registry / selection

PROFILES: dict[str, StreetProfile] = {p.name: p for p in (EU_DENSE, US_URBAN, US_SUBURBAN)}

US_DENSITY_THRESHOLD = 0.30   # building footprint area / land area above this -> us_urban


def by_name(name: str) -> StreetProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown profile {name!r}; known: {sorted(PROFILES)}") from None


def choose_for_country(iso2: Optional[str], building_coverage: Optional[float] = None) -> StreetProfile:
    """Default profile for an ISO 3166-1 alpha-2 country. For the US the building coverage of
    the area (footprint area / bbox area) picks urban vs suburban; unknown -> EU_DENSE."""
    if iso2 is None:
        return EU_DENSE
    iso2 = iso2.upper()
    if iso2 in ("US", "CA"):
        if building_coverage is not None and building_coverage >= US_DENSITY_THRESHOLD:
            return US_URBAN
        return US_SUBURBAN
    for p in PROFILES.values():
        if iso2 in p.countries:
            return p
    return EU_DENSE


_active: ContextVar[StreetProfile] = ContextVar("twinmodel_profile", default=EU_DENSE)


def get() -> StreetProfile:
    """The active profile. Call at use time, never cache at import time."""
    return _active.get()


def activate(profile: StreetProfile | str) -> StreetProfile:
    p = by_name(profile) if isinstance(profile, str) else profile
    _active.set(p)
    return p


@contextmanager
def use(profile: StreetProfile | str) -> Iterator[StreetProfile]:
    p = by_name(profile) if isinstance(profile, str) else profile
    token = _active.set(p)
    try:
        yield p
    finally:
        _active.reset(token)


def summary(p: StreetProfile | None = None) -> str:
    p = p or get()
    res = p.lane.for_class("residential")
    pri = p.lane.for_class("primary")
    return (f"{p.name}: residential {res.lane_width:.2f} m x{res.lanes} sw {res.sidewalk} verge {res.verge} "
            f"| primary {pri.lane_width:.2f} m x{pri.lanes} sw {pri.sidewalk} | centre {p.marking.center_color} "
            f"| crossing {p.crossing.width:.2f} m | cluster {p.junction.cluster_m:.0f} m | sources {p.sources.ortho[0]}/{p.sources.dem[0]}")
