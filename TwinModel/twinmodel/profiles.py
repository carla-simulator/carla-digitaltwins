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
    # paved shoulders (freeway/expressway classes; exported as OpenDRIVE ``shoulder`` lanes and
    # part of the drivable surface). ``shoulder`` is the outside (right-hand) shoulder of a
    # carriageway, ``shoulder_inner`` the median-side one of a divided/oneway carriageway.
    # None = no shoulder lane. Undivided two-way roads get ``shoulder`` on both sides.
    shoulder: Optional[float] = None
    shoulder_inner: Optional[float] = None


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
    # grade-separated classes: ways of these classes only ever meet at a gore (merge/diverge),
    # never at an at-grade intersection, so their intersection nodes do not cluster
    # (JunctionRules.gore_cluster_m instead of cluster_m) and get no crossings/signals.
    grade_separated_classes: frozenset[str] = frozenset({"motorway", "motorway_link"})
    # ramp classes: an end of one of these on a grade-separated mainline is a gore
    link_classes: frozenset[str] = frozenset({"motorway_link", "trunk_link"})

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
class BuildingRules:
    """Extrusion of OSM building footprints (export.ue): ``height=*`` wins, else
    ``building:levels`` x ``level_height_m``, else ``default_levels``."""
    level_height_m: float = 3.5
    default_levels: int = 3


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
    # junction cover polygon: "convex" = convex hull of the arm-end cross-sections (compact dense-
    # city clusters, DESIGN.md); "bounded" = union of the arm corridors and the connecting roads'
    # carriageways (40-60 m clusters that swallow internal ways: the hull would pave the block)
    cover: Literal["convex", "bounded"] = "convex"
    # a corner plaza may not exceed this multiple of (widest arm street width)^2; above it the
    # junction falls back to its cover polygon. None = uncapped.
    plaza_max_area_factor: Optional[float] = 3.0
    # when a junction corner (between two adjacent arms) may open beyond the hull of the arm
    # ends towards the buildings: "always" (chamfered blocks — the arms are cut at the chamfer
    # line, the open corner needs no receding face) or "recess" (only when one of the two arms'
    # canyon face steps back past its end; 90-degree corners never open)
    corner_opening: Literal["always", "recess"] = "recess"
    # freeway gores (LaneRules.grade_separated_classes): the clustering radius for intersection
    # nodes on those ways. A ramp gore is a point, not a plaza: 0 m keeps every gore its own
    # junction instead of swallowing the whole speed-change lane into one 90 m "intersection".
    gore_cluster_m: float = 0.0
    # a gore junction gets no plaza, no chamfer, no sidewalk band and no signals; its cover is
    # the union of the arm cross sections and the connecting roads' carriageways
    gore_cover: Literal["convex", "bounded"] = "bounded"
    # ---- ramp gores as speed-change lanes (lanegraph 7k, DESIGN.md "Ramp gores") ----------
    # "junction": every gore is an OpenDRIVE junction with connecting roads (the 2026-09-01
    # model). "taper": the mainline gains an *auxiliary lane* on the ramp side whose width is a
    # polynomial along the mainline's own reference line — an acceleration lane that tapers
    # out after a merge, a deceleration lane that tapers in before a diverge — and the ramp
    # links to that lane road-to-road. A merge then has no junction at all; a diverge keeps a
    # nose junction ``2 * gore_nose_m`` long, because a road with two successors must be a
    # junction in OpenDRIVE (and CARLA's MapBuilder::GetLaneNext only follows one).
    gore_model: Literal["taper", "junction"] = "junction"
    # parallel-type entrance: acceleration lane at full width, then an end taper to zero
    # (AASHTO Green Book 7th ed. §10.9.6 / Table 10-3: 900-1,200 ft for a 60-70 mph freeway
    # and a 25-30 mph ramp; the end taper 300 ft, i.e. 25:1 on a 12 ft lane). Both are capped
    # by the mainline road available in the tile; when the road is shorter than the taper the
    # lane runs its full length as a weaving lane.
    gore_merge_lane_m: float = 275.0
    gore_merge_taper_m: float = 90.0
    # parallel-type exit: a taper from zero, then the deceleration lane at full width up to the
    # nose (AASHTO Table 10-5: ~500 ft from 70 mph to a 30 mph ramp; taper 300 ft = 25:1)
    gore_diverge_lane_m: float = 150.0
    gore_diverge_taper_m: float = 90.0
    # half length of the diverge nose junction's connecting roads (the mainline is cut this far
    # before the nose, the mainline continuation and the ramp start this far after it)
    gore_nose_m: float = 2.5
    # the ramp's last / first metres are re-laid as a Hermite into the nose so its heading
    # matches the mainline there; the blend starts where the ramp's reference line is at least
    # ``gore_clear_m`` further from the mainline than the auxiliary lane's inner edge
    gore_blend_m: float = 40.0
    gore_clear_m: float = 1.5

    # ---- divided (dual) carriageways -------------------------------------------------------
    # A divided arterial is mapped in OSM as two ``oneway=yes`` ways with the same name/ref
    # running in opposite directions with a median between them (El Camino Real, S Mathilda Ave).
    # ``dual_carriageway_max_gap_m == 0`` switches the whole model off — EU_DENSE keeps the
    # 2026-09-01 behaviour that way.
    dual_carriageway_max_gap_m: float = 0.0     # max centreline separation of the two carriageways
    dual_carriageway_min_gap_m: float = 3.0     # below this the two ways are one carriageway
    dual_carriageway_parallel_deg: float = 25.0  # max deviation from anti-parallel per sample
    dual_carriageway_min_fraction: float = 0.5  # of a carriageway's length that must be paired
    dual_carriageway_min_paired_m: float = 50.0  # paired length a name group needs to count
    # cluster radius used at a node of a divided carriageway instead of ``cluster_m``: the
    # junction is then the median box (both carriageways + the crossing street), never the next
    # intersection one block up the arterial.
    dual_carriageway_cluster_m: float = 25.0
    # widest explicit ``median`` lane put on the median side of each carriageway (0 = none);
    # the two lanes meet in the middle of the gap, so the mesh gets one contiguous median strip
    median_max_width_m: float = 0.0
    # a road left between two junctions shorter than this is a sliver: no vehicle can use it
    # (its lanes get no links in the xodr and the traffic manager deletes anything routed onto
    # it), so the two junctions are merged instead. 0 = off (EU_DENSE: 2026-09-01 behaviour).
    # It must be >= twinmodel.validate.SLIVER_M (5 m, one passenger car plus a gap): the
    # validator flags every road shorter than that between two junctions, so a profile that
    # merges at a smaller radius leaves failures the lane graph considers acceptable.
    sliver_m: float = 0.0
    # ---- service ways (frontage roads, parking-lot access, driveways, alleys) --------------
    # Cluster radius at a *service node*: an intersection node that is one only because a
    # non-aisle ``highway=service`` way meets the street there (at most one street runs through
    # it). Service nodes never seed or extend a street junction — the generic ``cluster_m``
    # crawl would otherwise hop from a lot entrance to the next along a frontage road and fuse a
    # whole parking loop into the street intersection (Sunnyvale, W Olive Ave x S Taaffe St:
    # 7100 m2, ten nodes). A service node joins the street junction only when it is directly
    # linked to one of its street nodes by a chain shorter than this; two service nodes fuse
    # only within this radius and never beyond it (the fused group stays inside one throat).
    # 0 = off (EU_DENSE: 2026-09-01 behaviour, the Eixample lane graph is pinned).
    service_cluster_m: float = 0.0


@dataclass(frozen=True)
class ParkingAisleRules:
    """Circulation inside a parking lot: ``highway=service`` + ``service=parking_aisle`` (and
    ``service=driveway`` when ``include_driveways``).

    An aisle is ingested as a narrow service road with driving lanes only — no sidewalk, no
    verge, no on-street parking, no centre line, no crossings — so cars can drive into the
    ``amenity=parking`` lots instead of facing an empty slab. Its carriageway is cut out of the
    lot's ``parking`` surface (see DESIGN.md), so the two never overlap."""
    include: bool              # ingest parking aisles at all
    two_way_width: float       # total carriageway width of a two-way aisle (both lanes)
    one_way_width: float       # width of a one-way aisle (its single lane)
    min_length: float          # aisle ways shorter than this are not roads
    speed_limit: float         # m/s written on the aisle lanes
    include_driveways: bool = False   # same code path for service=driveway
    driveway_width: float = 3.0       # width of a one-way driveway (two-way: two_way_width)
    # A hole in the drivable surface (an area enclosed by roads) whose boundary is at least
    # this fraction lot circulation — aisles, driveways, unnamed service roads — is the stall
    # field of a lot OSM did not draw as ``amenity=parking``: a ``parking`` surface at grade,
    # not a raised curbed island (surfaces._fill_holes). Medians and islands are bounded by
    # streets and keep their curb. 0 = off.
    lot_enclosure_fraction: float = 0.0
    # ... but never for an enclosure larger than this (a service ring around a block is a
    # block, not a lot)
    inferred_lot_max_area: float = 20000.0


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
    # bridge decks: a DTM has the deck removed, so the deck z is a straight line between its
    # abutments. The abutment z is the highest DEM sample within this distance of the deck end
    # (outward along the approach): the DTM steps from deck level down into the trench of the
    # road below within a couple of cells, and the top of that step is the abutment.
    bridge_abutment_m: float = 12.0
    # a deck whose chain leaves the bbox has no abutment to interpolate from: the DEM at the
    # clipped end is bare earth (a DTM has the structure removed), i.e. the street below. Such a
    # free chain end is then lifted until the deck clears every road it crosses by this much,
    # measured deck surface to road surface. AASHTO/Caltrans want 16 ft 6 in (5.03 m) to the
    # *soffit*; with a girder + deck the road above sits about a metre higher again.
    min_clearance_m: float = 6.0
    # a crossing this close to an *anchored* chain end is the abutment itself, not a crossing
    clearance_abutment_skip_m: float = 8.0
    max_deck_lift_m: float = 30.0
    # over how much of the approach road the deck-to-approach step is faded out
    # (``cli.weld_deck_abutments``)
    abutment_blend_m: float = 40.0
    # tunnels / underpasses (``tunnel=yes`` or ``layer < 0``, ``cli.apply_tunnel_profiles``): the
    # mirror image of a deck. The DTM over a tunnel is the ground above it, so the tunnel road
    # runs straight between its portals (the z of the approach roads) and is sunk wherever a
    # road of a higher layer passes over it until it sits ``min_clearance_m`` below that road
    # (5 m of interior height + about a metre of ceiling slab). The dip ramps down and up at no
    # more than ``tunnel_max_grade`` (AASHTO tunnels: 3-4 % desirable, 8 % is the ceiling for
    # a short urban underpass); when the ramp reaches a portal, the approach is pulled down
    # into a trench over ``portal_blend_m`` (``cli.weld_deck_abutments``, or the length the
    # grade needs, whichever is longer).
    portal_blend_m: float = 40.0
    tunnel_max_grade: float = 0.08
    # the enclosure box the mesh draws over a tunnel road (``surfaces.tunnel_enclosure``):
    # interior clear height (US: 16 ft 6 in = 5.03 m minimum for a highway tunnel) and wall
    # thickness (1 ft 4 in) — walls and ceiling are their own surface kinds so an exporter
    # can skip them
    tunnel_height_m: float = 5.0
    tunnel_wall_m: float = 0.4


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
    parking_aisle: ParkingAisleRules
    junction: JunctionRules
    geometry: GeometryRules
    streetspace: StreetSpaceRules
    elevation: ElevationRules
    sources: DataSources
    drives_on: Literal["right", "left"] = "right"
    building: BuildingRules = BuildingRules()

    def with_(self, **overrides) -> "StreetProfile":
        """Copy with top-level fields replaced (``profile.with_(name="x", lane=...)``)."""
        return replace(self, **overrides)


# --------------------------------------------------------------------------- EU dense (Eixample) — the 2026-09-01 values

_EU_CLASSES = {
    # freeway classes: no sidewalk / verge / parking, hard shoulder outside + a narrow
    # median-side shoulder (Spanish IC-1: arcén exterior 2.5 m, arcén interior 1.0 m;
    # ramps 1.5 m / 0.75 m)
    "motorway":       ClassDefaults(3.5,  2, None, shoulder=2.5, shoulder_inner=1.0),
    "motorway_link":  ClassDefaults(3.5,  1, None, shoulder=1.5, shoulder_inner=0.75),
    "trunk":          ClassDefaults(3.5,  2, None, shoulder=1.5),
    "trunk_link":     ClassDefaults(3.5,  1, None, shoulder=1.0),
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
    # Aisles are OFF for EU_DENSE: the Eixample fixture has 13 service=parking_aisle ways and
    # the EU_DENSE build is the pinned regression (tests/test_profiles_lanegraph checksum,
    # tests/test_profiles_surfaces OBJ checksum). The widths are the ones to use when it is
    # turned on: 6.0 m two-way / 3.5 m one-way aisle (typical European lot), 20 km/h.
    parking_aisle=ParkingAisleRules(include=False, two_way_width=6.0, one_way_width=3.5,
                                    min_length=8.0, speed_limit=20 / 3.6,
                                    include_driveways=False, driveway_width=3.0),
    junction=JunctionRules(cluster_m=30.0, trim_margin_m=2.0, through_deg=30.0, uturn_deg=150.0,
                           through_align_m=1.0, signal_search_m=25.0, signal_lateral_m=0.5,
                           plaza_radius_m=45.0, chamfer_scan_m=60.0, dead_end_stub_m=10.0,
                           stub_m=3.0, short_road_m=5.0, band_overlap_m2=0.5,
                           # 2026-09-01 behaviour: hull cover, no plaza cap (Eixample's chamfer
                           # octagons reach 3.6x, the Passeig de Gracia 8-arm plazas 5.7x)
                           cover="convex", plaza_max_area_factor=None, corner_opening="always",
                           # no divided-carriageway model: the Eixample laterals (Passeig de
                           # Gracia) are two-way, not paired one-ways, and the regression build
                           # is pinned on the 2026-09-01 lane graph — keep both switches off
                           dual_carriageway_max_gap_m=0.0, median_max_width_m=0.0, sliver_m=0.0,
                           # gores stay junctions (2026-09-01 lane graph); the speed-change lane
                           # lengths below are what "taper" would use: Norma 3.1-IC (ES) style
                           # carril de aceleracion ~200 m + cuna 75 m, deceleracion ~120 m + 75 m
                           gore_model="junction", gore_merge_lane_m=200.0, gore_merge_taper_m=75.0,
                           gore_diverge_lane_m=120.0, gore_diverge_taper_m=75.0),
    geometry=GeometryRules(min_road_length=1.0, connect_sample_m=1.0, simplify_m=0.1,
                           width_step_m=1.0, taper_max_m=15.0, taper_pieces_max=3, jog_max_m=5.0,
                           jog_min_turn_deg=45.0, jog_transition_m=10.0, street_width_outlier=0.25),
    streetspace=StreetSpaceRules(canyon_min_fraction=0.6, max_face_dist_m=40.0, face_sample_step_m=4.0,
                                 building_pad_m=0.3, face_tol_m=1.5, blocker_min_dist_m=1.0,
                                 ground_reach_m=12.0, sidewalk_to_face_max_m=12.0),
    elevation=ElevationRules(resample_m=2.0, smooth_window_m=10.0, datum_max_dist_m=25.0,
                             junction_blend_m=20.0, connecting_blend_m=15.0, mesh_grid_m=5.0,
                             bridge_abutment_m=12.0),   # ICGC MDT 2 m: ~6 cells of abutment ramp
    sources=DataSources(ortho=("icgc", "ign_es", "osm_tiles"), dem=("icgc_mdt2m", "ign_wcs", "opentopo", "copernicus_aws")),
    building=BuildingRules(level_height_m=3.2, default_levels=5),
)


# --------------------------------------------------------------------------- US urban (downtown grid)
# Lane widths: MUTCD/AASHTO 10–12 ft; NACTO recommends 10 ft (3.0 m) on urban streets, 11 ft
# where buses/trucks run. Sidewalks: PROWAG 5 ft (1.5 m) minimum clear, downtown 8–12 ft.
# Parking: 7–8 ft parallel (2.1–2.4 m), 17–18 ft diagonal/perpendicular. Centre lines yellow
# (MUTCD 3A.05), lane lines white, broken line 10 ft dash / 30 ft gap (MUTCD 3A.06),
# marking width 4–6 in. Crosswalks 6 ft minimum, 10 ft typical (MUTCD 3B.18).

_US_URBAN_CLASSES = {
    # freeway classes: no sidewalk / verge / parking (PROWAG does not apply, pedestrians are
    # prohibited); AASHTO Green Book 7th ed. shoulders: 10 ft outside / 4 ft median-side on a
    # 4-lane freeway, 8 ft / 4 ft on ramps, 8 ft on an expressway (trunk)
    "motorway":       ClassDefaults(12 * FT, 6, None, center_marking=False,
                                    shoulder=10 * FT, shoulder_inner=4 * FT),
    "motorway_link":  ClassDefaults(12 * FT, 1, None, center_marking=False,
                                    shoulder=8 * FT, shoulder_inner=4 * FT),
    "trunk":          ClassDefaults(12 * FT, 4, 8 * FT, shoulder=8 * FT),
    "trunk_link":     ClassDefaults(12 * FT, 1, 8 * FT, shoulder=6 * FT),
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
    # Parking-lot aisles: 24 ft two-way / 13 ft one-way (typical US zoning minimum for a
    # drive aisle serving 90-degree stalls is 24 ft two-way, 12-14 ft one-way), 10 mph.
    # Driveways are on (service=driveway, 12 ft one-way): the lots' links between the street
    # and their aisles. A driveway that leads nowhere (a garage entrance off the street, no
    # aisle or other service road at its far end) is not a road (lanegraph._driveway_is_road);
    # the free end of one that does lead into a lot is a documented dead end.
    parking_aisle=ParkingAisleRules(include=True, two_way_width=24 * FT, one_way_width=13 * FT,
                                    min_length=8.0, speed_limit=10 * 1609.344 / 3600,
                                    include_driveways=True, driveway_width=12 * FT,
                                    # 2/3 of the enclosure's boundary is lot circulation: a lot
                                    # in the corner of a block still has a street on two sides;
                                    # 2 acres (8100 m2) holds a ~300-stall lot
                                    lot_enclosure_fraction=0.66, inferred_lot_max_area=8100.0),
    junction=JunctionRules(cluster_m=40.0, trim_margin_m=2.0, through_deg=30.0, uturn_deg=150.0,
                           through_align_m=1.0, signal_search_m=35.0, signal_lateral_m=0.5,
                           plaza_radius_m=50.0, chamfer_scan_m=60.0, dead_end_stub_m=10.0,
                           stub_m=3.0, short_road_m=5.0, band_overlap_m2=0.5,
                           cover="bounded", plaza_max_area_factor=3.0, corner_opening="recess",
                           # divided arterials (Howard/Folsom in SoMa, El Camino Real /
                           # S Mathilda Ave in Sunnyvale): 25 m of median at most, junctions
                           # clustered over the median box only, 8 ft of explicit median lane
                           dual_carriageway_max_gap_m=25.0, dual_carriageway_min_gap_m=3.0,
                           dual_carriageway_parallel_deg=25.0, dual_carriageway_min_fraction=0.5,
                           dual_carriageway_min_paired_m=50.0, dual_carriageway_cluster_m=25.0,
                           median_max_width_m=8 * FT, sliver_m=20 * FT,
                           # a service throat: a 24 ft two-way driveway plus its 10-15 ft curb
                           # returns is ~40 ft wide; two service entrances closer than that
                           # share one throat, further apart they are two T-junctions
                           service_cluster_m=40 * FT,
                           # ramp gores are speed-change lanes, not junctions. AASHTO Green Book
                           # 7th ed.: parallel-type entrance 900 ft acceleration lane (Table
                           # 10-3, 60 mph freeway / 25 mph ramp) + 300 ft end taper (25:1 on a
                           # 12 ft lane, §10.9.6.3); parallel-type exit 300 ft taper (25:1) +
                           # 500 ft deceleration lane (Table 10-5, 70 mph / 30 mph ramp)
                           gore_model="taper", gore_merge_lane_m=900 * FT, gore_merge_taper_m=300 * FT,
                           gore_diverge_lane_m=500 * FT, gore_diverge_taper_m=300 * FT,
                           gore_nose_m=2.5, gore_blend_m=40.0, gore_clear_m=5 * FT),
    geometry=GeometryRules(min_road_length=1.0, connect_sample_m=1.0, simplify_m=0.1,
                           width_step_m=1.0, taper_max_m=25.0, taper_pieces_max=3, jog_max_m=5.0,
                           jog_min_turn_deg=45.0, jog_transition_m=10.0, street_width_outlier=0.25),
    streetspace=StreetSpaceRules(canyon_min_fraction=0.6, max_face_dist_m=40.0, face_sample_step_m=4.0,
                                 building_pad_m=0.3, face_tol_m=1.5, blocker_min_dist_m=1.0,
                                 ground_reach_m=15.0, sidewalk_to_face_max_m=15.0),
    elevation=ElevationRules(resample_m=2.0, smooth_window_m=10.0, datum_max_dist_m=25.0,
                             junction_blend_m=20.0, connecting_blend_m=15.0, mesh_grid_m=5.0,
                             # 3DEP 1 m hydro-flattened: the abutment ramp smears over ~40 ft
                             bridge_abutment_m=12.0),
    sources=DataSources(ortho=("naip", "osm_tiles"), dem=("usgs_3dep", "opentopo", "copernicus_aws")),
    building=BuildingRules(level_height_m=3.5, default_levels=3),
)


# --------------------------------------------------------------------------- US suburban (arterial + subdivision)
# 12 ft arterial lanes (AASHTO), 4-lane divided arterials with medians (clusters up to 60 m),
# residential 26–36 ft curb-to-curb with parking both sides and no centre line, 5 ft sidewalk
# behind a 6 ft planting strip, buildings set back (canyon regime rarely triggers).

_US_SUBURBAN_CLASSES = {
    # freeway classes as US_URBAN: 10 ft outside / 4 ft median-side shoulders, 8 ft / 4 ft on
    # ramps, 8 ft on an expressway (AASHTO Green Book 7th ed.); no sidewalk / verge / parking
    "motorway":       ClassDefaults(12 * FT, 6, None, center_marking=False,
                                    shoulder=10 * FT, shoulder_inner=4 * FT),
    "motorway_link":  ClassDefaults(12 * FT, 1, None, center_marking=False,
                                    shoulder=8 * FT, shoulder_inner=4 * FT),
    "trunk":          ClassDefaults(12 * FT, 4, None, shoulder=8 * FT),
    "trunk_link":     ClassDefaults(12 * FT, 1, None, shoulder=6 * FT),
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
    building=BuildingRules(level_height_m=3.5, default_levels=2),
    sidewalk=replace(US_URBAN.sidewalk, max_width=10 * FT),
    junction=replace(US_URBAN.junction, cluster_m=60.0, plaza_radius_m=60.0, signal_search_m=45.0,
                     dead_end_stub_m=15.0,
                     # suburban arterials carry wider medians (left-turn pockets, planted strips)
                     dual_carriageway_max_gap_m=30.0, dual_carriageway_cluster_m=28.0,
                     median_max_width_m=12 * FT, sliver_m=20 * FT),
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
