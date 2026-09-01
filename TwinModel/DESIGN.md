# Twin Model — MVP design (spike, 2026-09-01)

Goal of the spike: prove that **OSM → Twin Model → {surface mesh, OpenDRIVE}** gives better
road/sidewalk/junction geometry than the current **OSM → SUMO/osm2odr → OpenDRIVE → mesh** chain,
in pure Python, on one Barcelona test area, before any Unreal work.

```
OSM (Overpass) ─┐
imagery (ICGC) ─┼─► Twin Model ──┬─► surface mesh (.obj)   [UE later: Nanite/WP baker]
DEM (ICGC/Cop.) ┘                ├─► OpenDRIVE (.xodr)
                                 └─► validation report (.json)
```

The mesh and the xodr are **independent exports of the same model**. The invariant that keeps them
consistent (checked by `twinmodel.validate`):

> every sampled driving-lane centreline point of the xodr lies inside the model's `drivable`
> surface, at the surface z (±5 cm); every junction connecting road lies inside its junction polygon.

## Layout

```
TwinModel/
  DESIGN.md                 this file
  pyproject.toml            package `twinmodel`, console script `twinmodel`
  twinmodel/
    model.py                THE CONTRACT — dataclasses + GeoJSON I/O. Do not change shapes without
                            updating this doc; add fields, don't rename.
    frame.py                LocalFrame: WGS84 <-> local ENU metres (transverse mercator at origin)
    ingest/osm.py           Overpass download (cached), OSM element parsing            [worker A]
    lanegraph.py            OSM ways -> Roads/Lanes/Junctions/Signals                  [worker A]
    surfaces.py             lane graph (+ optional mask) -> Surface polygons, curbs     [worker B]
    export/mesh.py          Surfaces -> triangulated .obj (groups per kind)             [worker B]
    export/xodr.py          Roads/Lanes/Junctions/Signals -> .xodr                      [worker C]
    validate.py             invariant checks, xodr parse via `carla.Map`, report        [worker C]
    ingest/imagery.py       ICGC ortho WMS fetch -> GeoTIFF in local frame              [worker D]
    ingest/elevation.py     DEM fetch (ICGC / Copernicus) -> GeoTIFF, `sample_z()`      [worker D]
    refine.py               road mask from imagery -> adjust Surface boundaries         [worker D]
    cli.py                  `twinmodel build --bbox S W N E --name X --out DIR`         [integrator]
  tests/                    pytest, no network (use cached fixtures in tests/fixtures)
  data/                     Overpass/WMS caches (gitignored)
```

Python: `/home/german/Projects/CARLA_SOURCE/.venv-twins/bin/python` (3.13; shapely 2, geopandas,
pyproj, rasterio, osmnx, networkx, scipy, scenariogeneration, trimesh, mapbox-earcut, and the
ue58 `carla` wheel for xodr parsing). Run everything from `TwinModel/`.

## Coordinate conventions

- Model space is **local ENU, metres, right-handed**: x east, y north, z up. Origin = centre of
  the requested bbox. Projection: transverse mercator centred on the origin (`frame.LocalFrame`),
  same convention CARLA's `GeoLocation`/`OpenDriveGenerator` use, so the xodr `<geoReference>`
  is `+proj=tmerc +lat_0=<lat> +lon_0=<lon> +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m`.
- OpenDRIVE is written in model space unchanged (OpenDRIVE is also x-east/y-north, right-handed).
- The `.obj` is written in model space too. Whoever loads it into Unreal does `(x, -y, z) * 100`.
- Headings in radians, CCW from +x (OpenDRIVE `hdg` convention).
- Raster grids (`Elevation`, `OrthoImage.array`) are **south-up**: row index increases with y. Flip
  (`[::-1]`) before `imshow(origin="upper")`; `OrthoImage.extent()` is for `origin="lower"`.

## Test area

Eixample, Barcelona — chamfered corners make every intersection an 8-node octagon in OSM, which
is exactly what breaks netconvert. Default bbox (S W N E): `41.3905 2.1630 41.3945 2.1690`
(≈ 500 × 450 m, ~6 full blocks). Sources that are open for this area:
- OSM via Overpass (`https://overpass-api.de/api/interpreter`); cache the raw JSON in `data/`.
- ICGC orthophoto 25 cm: WMS `https://geoserveis.icgc.cat/servei/catalunya/orto-territorial/wms`
  (layer names to be discovered by worker D; EPSG:25831 native).
- ICGC DEM 2 m (MDT): ICGC open download / WCS — worker D discovers; fallback Copernicus GLO-30
  via OpenTopography (needs API key → if unavailable, flat z=0 is acceptable for the spike and
  must be reported as such).

## Model summary (see `twinmodel/model.py` for the exact fields)

- `Road`: reference line (polyline in model space, metres, with z), `lanes` ordered left→right in
  OpenDRIVE id convention (positive ids left of reference line, negative right, 0 = centre),
  each `Lane` with `type` (`driving|sidewalk|shoulder|parking|biking|median|none`), constant
  `width`, `direction` (`forward|backward`), and optional `marking` on its outer edge.
  `junction_id` set when the road is a *connecting road* inside a junction. Links via
  `predecessor`/`successor` (`RoadLink`: element type `road|junction`, id, contact point).
- `Junction`: id, polygon (model space) and `connections` (incoming road → connecting road with
  `lane_links`).
- `Signal`: traffic light / stop / yield / speed limit / crosswalk, positioned on a road by
  `(road_id, s, t)` plus an absolute xy for convenience; traffic lights carry `controller_id`
  (one controller per junction).
- `Surface`: polygon(s) with `kind` (`drivable|sidewalk|island|crossing|median|parking|ground`),
  `z_offset` above the road datum (sidewalk = 0.15 m), `source`
  (`osm_tags|area_highway|imagery`), `confidence` 0–1.
- `CurbLine`: linestring where a `sidewalk`/`island` meets `drivable`, height 0.15 m.
- `Marking`: linestring, `kind` (`solid|broken`), `color` (`white|yellow`), width.
- `Building`: footprint polygon, height (m), levels, osm tags. `Tree`/`Pole`: points.
- `Elevation`: optional grid (numpy) in model space with `sample(x, y) -> z`.

Everything serialises to one directory `<name>.twin/` with `model.json` (metadata + frame) and
one GeoJSON per layer (`roads`, `junctions`, `signals`, `surfaces`, `curbs`, `markings`,
`buildings`, `trees`), geometry in **model space** (not WGS84 — set `"crs": "local-enu"` in the
metadata). `TwinModel.save(dir)` / `TwinModel.load(dir)` in `model.py` are the only I/O.

## Widths when OSM does not say

`lanegraph.DEFAULTS` per `highway=*`: lane width 3.25 m (3.0 residential/service, 3.5 primary+),
lanes: 2 default, `oneway=yes` → 1; `lanes`, `lanes:forward/backward`, `width`, `sidewalk[:both|:left|:right][:width]`,
`turn:lanes`, `cycleway*`, `parking:*` override. Sidewalk default: 2.0 m both sides for
residential/tertiary/secondary/primary/living_street/pedestrian, none for motorway/trunk/service.

## Parking lots and their aisles

An OSM `amenity=parking` way/relation becomes a `parking` surface (`metadata["parking_lots_wkt"]`
→ `surfaces.build_surfaces`). The circulation inside it — `highway=service` + `service=parking_aisle`
— is ingested as a road so cars can drive into the lot, under the profile's `ParkingAisleRules`:

- **Cross section** (`lanegraph._parking_aisle_lanes`): driving lanes only, `two_way_width` split
  over two lanes or one lane of `one_way_width` (an OSM `width` wins), the profile's low
  `speed_limit`, **no** sidewalk/verge/parking bands, **no** centre/lane/edge marking, and no
  crossings or traffic lights (`_build_signals` skips aisle roads). A lot squeezed between two
  buildings is not a street canyon: aisles never take the building-derived cross section.
- **Connection to the street**: the shared OSM node makes an ordinary junction, but an aisle node
  is a *minor junction* — it never merges into a neighbouring node cluster (`minor_nodes` in
  `build_lanegraph`), so two lot entrances 50 m apart on the same street cannot pull the street
  between them into one 60 m cluster. The one exception is `junction.sliver_m`: two nodes joined
  by a chain shorter than that are one junction whatever they are, because nothing usable is left
  between them after the trims. Aisle–aisle junctions inside a lot are single-node clusters
  (~70–200 m² with the `bounded` cover); the only large junction with an aisle arm is a large
  street intersection that the aisle happens to join.
- **Lot links**: a short unnamed `highway=service` way (below `lane.service_min_length`, normally
  dropped as noise) that touches an aisle is the lot's link to the street and is kept — without it
  the aisles are an unreachable island. Likewise an aisle shorter than
  `ParkingAisleRules.min_length` is kept when *both* its end nodes are shared with another
  drivable way: it is a connector between two parts of the lot, not a spur, and dropping it cuts
  the circulation in two.
- **Surfaces**: the aisle is a normal road, so its carriageway is part of `drivable`; the lot's
  `parking` surface is the lot polygon **minus** `drivable` (and minus raised surfaces and
  buildings), as it already was for the streets. One surface per square metre, both at
  `z_offset = 0`, no z-fighting, no curb between them. A hole in `drivable` that lies inside a lot
  (the stall field enclosed by a ring of aisles) stays a hole and is filled by the lot — it must
  not become a raised `island`.
- **OpenDRIVE**: aisles are ordinary roads with `driving` lanes and a low `<speed>`; they link to
  the street through the junction like any minor road, so `carla.Map` waypoints run through them.
- `service=driveway` uses the same code path behind `ParkingAisleRules.include_driveways`
  (`driveway_width` for the one-way case), **off by default**: in a subdivision every mapped house
  driveway would become a dead-end stub. Turning it on for Sunnyvale adds 46 roads / 7 junctions
  and connects 4 more lots — see the profile comment.
- `EU_DENSE` sets `include=False`: the Eixample fixture contains 13 `service=parking_aisle` ways
  and the Eixample build is the pinned regression (lane-graph SHA + OBJ checksum). The EU widths
  (6.0 m two-way / 3.5 m one-way) are in the profile, ready for when it is switched on.

## Junction detection (the Eixample problem)

Cluster OSM intersection nodes that are within `JUNCTION_CLUSTER_M = 30 m` of each other *and*
connected by ways shorter than that into one `Junction`. The junction polygon is the union of the
drivable buffers of all ways inside the cluster (worker B). Roads that end at the cluster boundary
become incoming roads; for every legal in→out pair (respecting `oneway` and `restriction`
relations), worker A builds a connecting road as a cubic Hermite curve between the two lane ends,
tangent-continuous, sampled every 1 m. Its centreline must stay inside the junction polygon —
`validate` checks this.

## Divided carriageways (the US arterial problem)

A US arterial is mapped in OSM as **two `oneway=yes` ways with the same `name`/`ref` running in
opposite directions** with a median between them (El Camino Real and South Mathilda Avenue in the
Sunnyvale fixture; Rambla de Catalunya if the US profiles are pointed at Eixample). With
`JunctionRules.cluster_m` at 40–60 m the generic clustering above hops *along* a carriageway from
one intersection to the next and fuses them into a single 80–130 m blob, and each carriageway is
given the class-default sidewalk/verge/parking on **both** sides, so the two carriageways' bands
overlap across the median and the band-overlap rule (step 7f) grinds the road between two
junctions down to a 1 m sliver that can carry no lane link.

`lanegraph._dual_carriageways` pairs the carriageways geometrically, per chain: same street key
(`name` or `ref`, lower-cased), both `oneway`, anti-parallel within
`junction.dual_carriageway_parallel_deg` over at least `dual_carriageway_min_fraction` of the
chain's length at a separation inside
`[dual_carriageway_min_gap_m, dual_carriageway_max_gap_m]`; a street counts as divided only when
its chains are paired over `dual_carriageway_min_paired_m` in total. Setting
`dual_carriageway_max_gap_m = 0` switches the whole model off — that is what `EU_DENSE` does, so
the pinned Eixample lane graph is untouched.

Three things follow from a pairing:

1. **Cross section.** `_apply_median` strips the parking / cycle / verge / sidewalk lanes from the
   *median side* of the carriageway (left of travel where traffic drives on the right — there is
   no curb there, the other carriageway is) and adds one explicit `median` lane reaching half-way
   across the gap, capped at `junction.median_max_width_m`. The two carriageways' median lanes
   meet, so `surfaces.py` (which already treats `median` as a raised lane type) produces **one
   contiguous median strip** in the mesh and the xodr gets a `median` lane rather than a hole.
   `median` is not a carriageway lane type, so the reference line stays where it was.
   *Design choice*: two roads + a median lane on each, rather than one road carrying both
   carriageways. One road would need the median as a centre lane with driving lanes on both sides
   travelling the same way, which OpenDRIVE lane-direction rules do not express, and would break
   every `oneway` movement rule in the junction builder. Two roads keep `carla.Map` happy and the
   mesh contiguous because the medians meet in the middle.
2. **Clustering.** At any node of a paired carriageway the cluster radius drops from `cluster_m`
   to `junction.dual_carriageway_cluster_m` (≈ the widest median we accept). The short internal
   ways that cross the median — the side street between the two carriageways, or the box sides of
   a divided × divided crossing — are shorter than that and still merge, so **the crossing is one
   junction spanning both carriageways**. The 40–75 m hops along a carriageway do not, so a side
   street that hits the two carriageways at *offset* nodes stays two junctions joined by the
   arterial segment between them.
3. **Band overlap.** Two carriageways of the same arterial never cut each other in step 7f; the
   overlap between them is the median, which the cross section above owns. `_road_band` also
   leaves `median` lanes out of the "full" band, so the street crossing the arterial is not
   shortened for running over its median.

### Slivers and dead ends

A road left between two junctions shorter than `junction.sliver_m` carries no lane link — CARLA's
traffic manager routes a vehicle onto it and then deletes it. `sliver_m` must be **at least
`validate.SLIVER_M` (5 m, one passenger car plus a gap)**, the length the validator flags: the US
profiles use 20 ft, `EU_DENSE` keeps 0 (off, the 2026-09-01 behaviour). Five passes remove them:

1. the node clustering joins two *minor* (aisle-only) nodes when the chain between them is
   shorter than `sliver_m` — a lot entrance still never merges with the intersection up the
   street, but two nodes that close cannot be told apart at all;
2. the clustering loop merges the two junctions when a *trim* leaves a sliver;
3. the band-overlap pass merges them when a *band cut* would (only when the two hulls are within
   `cluster_m` — two junctions a block apart are not one junction, so there the band overlap is
   kept instead and logged);
4. a sliver with a junction at one end only is absorbed into it; and
5. a final sweep merges whatever the passes after the clustering loop (re-cuts, band cuts, the
   merges themselves) shortened below `sliver_m`.

A road with the *same* junction at both ends (a parking-lot ring off one driveway) is split in the
middle: OpenDRIVE's `<connection>` names the incoming road by id alone, so CARLA cannot tell its
two ends apart and half the movements dead-end. A chain that *starts and ends on the same OSM
node* is such a ring and is never absorbed as "internal to the junction", however short it is:
absorbing it paves the loop and leaves the lot entrance with a single arm.

**A junction must keep an exit.** The trim, the stub rule and the sliver absorption all remove
short arms, and removing the wrong one leaves a junction every remaining arm only *arrives* at:
step 8 then finds no departure, the arriving lanes get no lane link, and the traffic manager
deletes whatever it routed there. Two guards:

- an arm the **bbox** cut short (its free end is a clip point, not an OSM node) is put back —
  re-cut if that leaves something drivable, untrimmed if not — when dropping it would leave some
  arrival at that junction with nowhere to go but back down itself (`junction_outlets_restored`);
  it is then exempt from the sliver absorption. Shipley Street and 5th Street in the SoMa fixture
  and the South Mathilda Avenue carriageway in Sunnyvale leave the map that way.
- a short **parking aisle** (below `ParkingAisleRules.min_length`) whose *both* ends are nodes
  another drivable way also uses is a connector inside the lot, not a spur, and is kept: dropping
  it severs the lot's circulation (Sunnyvale way 1374794719, 4.9 m).

What is left after those guards is a dead end in the data, not a defect of the twin, and the lane
graph tags the road end `dead_end_<end>` (with a `dead_end_<end>_reason`) so `terminal_lanes` and
`junction_lane_links` treat it exactly like a degree-1 cul-de-sac:

- `oneway_funnel` — every other arm of the junction is a one-way *into* it, so an arrival on the
  remaining two-way arm has no legal departure at all (Mint Street into Jessie Street in SoMa,
  two one-way service ways converging on a two-way one at US-101/Mathilda);
- `no_continuation` — the junction was dissolved because it had a single arm: every other way at
  that OSM node is outside the twin's scope (a private `service=driveway`, an underground ramp) or
  was clipped away by the bbox.

Across the three US fixtures that is four road ends in total; every other lane leads somewhere.

Two more lane-level rules keep every lane leading somewhere:

- every driving lane of an approach must feed at least one connection — an outer lane no movement
  picks up gets the nearest legal departure — and an approach with no legal departure at all
  (turn restrictions that leave nothing) keeps the straightest one;
- `export/xodr._lane_links` matches lanes by centre distance, which fails whenever two linked
  roads have different lane counts (both carriageways are centred on the same OSM way, so every
  centre is offset by half a lane). The match is now restricted to the correct travel side and
  falls back to an ordinal inner→outer match, clamped to the outermost lane.

## Surfaces (worker B)

1. Per road: carriageway polygon = reference line buffered by the per-side sum of driving/parking/
   biking lane widths (flat caps, mitre joins, limited).
2. `drivable` = unary union of all carriageway polygons ∪ junction polygons (simplify 5 cm, keep
   holes → `island` surfaces).
3. `sidewalk` = (reference line buffered by carriageway + sidewalk width) − drivable, per side,
   only where the road has a sidewalk on that side; union across roads; subtract building
   footprints.
4. `crossing` = rectangle across the carriageway at each OSM `highway=crossing` node
   (width 4 m, or from `crossing:width`).
5. `CurbLine` = boundary(drivable) ∩ boundary(sidewalk ∪ island).
6. If a refined boundary is supplied (`refine.py`), it replaces step 2's polygon; keep both and
   record `source`.

## Mesh (worker B)

Per Surface polygon: constrained triangulation (mapbox-earcut on polygon rings, or shapely
`delaunay_triangles` + inside filter), z from `Elevation.sample` + `z_offset` (sidewalks and
islands 0.15 m up), curb = quad strip between drivable edge z and sidewalk edge z along each
`CurbLine`. `.obj` groups: `drivable`, `sidewalk`, `island`, `crossing`, `ground`, `curb`, plus a `.mtl` with
one material per group. Also emit lane markings as thin quads (0.12 m wide, +2 mm) in group
`marking_white`/`marking_yellow`.

## OpenDRIVE (worker C)

Prefer `scenariogeneration.xodr` (Line/Arc/Spiral/ParamPoly3 planview builder, lanes, junctions,
signals, `<geoReference>`). Reference-line geometry: fit each polyline as piecewise cubic
`paramPoly3` (Catmull-Rom → local-frame cubic per segment) so curvature is continuous — **no**
line/arc quantisation. Exception, `export.xodr.MAX_PLANVIEW_OFFSET` (0.25 m): a vertex whose
rounding would pull the fitted curve further than that off the polyline (a right-angle corner
with long legs — an L-shaped parking aisle, a service road around a block) is written as a
*corner*: the two geometries take their own chord directions and meet with a heading kink, a leg
between two corners becomes a `line`. The polyline is what `surfaces` buffers, so following it is
what keeps the lanes inside `drivable` (before this, the sharp-cornered service roads of the
Sunnyvale build put 378 waypoints, 1.4 %, outside `drivable`). Elevation profile: piecewise cubic from the sampled z. Lanes: constant
width per lane; `<roadMark>` from `Marking`; sidewalks as `type="sidewalk"` lanes with `height`
inner/outer 0.15. Junctions: `<junction>` with `<connection>` + `<laneLink>`; connecting roads
have `junction=<id>`. Signals: `<signal>` with CARLA's expected `type`/`subtype` ("1000001" for
traffic lights, "206" stop, "205" yield, "274" + subtype speed) and `<controller>` per junction.
The output must parse with `carla.Map("twin", xodr_string)` (ue58 wheel, no server needed) and
`map.generate_waypoints(1.0)` must return points on every driving lane.

## Validation (worker C) — `twinmodel validate <dir>` → `report.json`

- `lane_in_drivable`: fraction of `generate_waypoints(1.0)` driving waypoints inside `drivable`
  (target ≥ 0.98; report the offenders as GeoJSON `violations.geojson`).
- `junction_containment`: fraction of connecting-road samples inside their junction polygon.
- `z_error`: |waypoint z − surface z| p50/p95.
- `topology`: `carla.Map` loaded, waypoint count, junction count, roads with no successor.
- `sidewalk_coverage`: total sidewalk area vs. drivable area (sanity).
- `terminal_lanes`: driving lanes whose last waypoint has no `next()` **more than 30 m inside the
  bbox** — the dead ends a traffic-manager soak deletes vehicles on. Lanes that stop at the bbox
  edge, and roads the lane graph tagged `dead_end_start`/`dead_end_end` (an OSM degree-1 node: a
  real cul-de-sac; a one-way funnel; a node where nothing continues — see "Slivers and dead ends"),
  do not count. Target 0.
- `junction_slivers`: non-junction roads shorter than `SLIVER_M` = 5 m between two junctions. Every
  profile's `junction.sliver_m` must be at least that, or the lane graph leaves failures behind.
  Target 0.
- `junction_lane_links`: arms with a driving lane that is the incoming lane of no connection. An
  arm the lane graph tagged `dead_end_<end>` is excluded — the same documented exception
  `terminal_lanes` makes, and inventing a movement out of it would be worse. Target 0.

`tools/junction_metrics.py <build_dir> <name> <profile>` prints the junction-quality numbers
(count, connecting-road length distribution, worst junction area / widest-arm street width²) next
to those checks; `tools/build_variant.py off build ...` re-runs a build with the
divided-carriageway and sliver models disabled, for before/after comparisons on one code base.

## Imagery / DEM / refinement (worker D) — best effort, must degrade gracefully

- `imagery.fetch(frame, bbox) -> GeoTIFF` in model space at 0.25 m via ICGC WMS (tile the request
  if the server caps size). Cache in `data/`.
- `elevation.fetch(...)` → DEM GeoTIFF in model space; `Elevation` object with bilinear `sample`.
  If nothing is reachable, return `None` and the pipeline uses z=0 (report says `elevation: none`).
- `refine.road_mask(image) -> binary mask`: try `segment-geospatial`/SAM if it installs and a GPU
  is present (this box has a 5090); otherwise a classical asphalt classifier (HSV thresholds +
  morphology) is fine for the spike.
- `refine.refine_surfaces(model, mask)`: for each `drivable` polygon, move boundary vertices along
  their normal (±2.5 m max) to the mask edge, smooth, record `source="imagery"` and IoU before/
  after in the metadata. Never move a boundary further than 2.5 m and never make a driving lane
  narrower than 2.5 m — the lane graph is the authority on topology, imagery only on shape.

## Integration (after A–D) — `twinmodel build` end-to-end on the default bbox

1. `build` runs ingest → lanegraph → (imagery/DEM/refine if available) → surfaces → exports →
   validate, writes `<out>/<name>.twin/`, `<name>.xodr`, `<name>.obj/.mtl`, `report.json`, and a
   top-down PNG overview (matplotlib: surfaces filled, lanes as lines, junction polygons outlined,
   ortho underneath if available).
2. Load the xodr in a ue58-dev server (`client.generate_opendrive_world(xodr, params)`; see
   memory notes for launch flags), spawn a spectator over 2–3 Eixample junctions, save camera
   captures. Note: this uses ue58's runtime `OpenDriveGenerator` mesh, which is *not* the twin
   mesh — it is only to prove the xodr topology is sound in CARLA. The OBJ overlay in Unreal is a
   stretch goal.

## Baker (UE 5.8): twin -> World Partition level in carla-ue58-dev

Two stages, both driven from `TwinModel/`; the first is pure Python (offline, tested), the
second is editor Python run by `UnrealEditor-Cmd -run=pythonscript`.

```
<build>/<name>.twin ──bake-export──► <build>/ue/{manifest.json, meshes/*.glb}
                                          │
                                          ▼  ue/bake_level.py (editor Python, Interchange)
        /Game/Carla/Static/<Label>/Twins/<Name>/<asset>      static meshes, Nanite on
        /Game/Carla/Maps/Twins/<Name>/Materials/MI_<Name>_*  MIC children of CARLA materials
        /Game/Carla/Maps/Twins/<Name>/<Name>                 World Partition level
        Content/Carla/Maps/Twins/<Name>/OpenDrive/<Name>.xodr
```

### Stage 1 — `python -m twinmodel bake-export <build_dir> <name> [--out DIR] [--tile 250]`
(`twinmodel/export/ue.py`) One glTF 2.0 binary per **(layer, kind, tile)**:
- kinds: every surface kind (`drivable`, `sidewalk`, `crossing`, `island`, `median`, `verge`,
  `parking`, `ground`), `curb` (vertical strips, per-segment normals, (along, height) UVs),
  `marking_white` / `marking_yellow` (lane lines as in the OBJ, plus **zebra stripes** cut from
  every `crossing` polygon: 0.5 m stripes / 0.5 m gaps across the road), `building` (OSM
  footprints extruded from the lowest datum sample −0.3 m to `Building.effective_height`, per
  `profile.building.level_height_m` / `default_levels`; flat-shaded walls, roof on top).
- layer = OSM `layer` of the surface/curb/marking (grade separation); tile = `tile_m` grid cell
  in model space (a World Partition cell streams whole tiles). Asset name
  `<name>_L<layer>_<kind>_<i>_<j>` (`m` for minus).
- coordinates: the glb vertices are written as `(x, z, −y)` metres so that Interchange's
  `UE = (gx, gz, gy)·100` lands them at CARLA's `(x, −y, z)·100` — actors go at the origin with
  no transform. Faces CCW in model space (the importer reverses winding for the handedness
  change). UVs metric planar (`u = x`, `v = −y`; 1 unit = 1 m) so the CARLA material tiling
  parameters mean metres.
- `manifest.json`: assets (file, kind, layer, tile, material key, ATagger semantic folder, bbox
  in UE cm, tri count), `spawn_points` (UE cm / yaw°: every 30 m along each driving lane of every
  non-junction road, 10 m clear of the ends, 0.5 m above the datum (less and try_spawn_actor hits the road), heading in the lane's travel
  direction), `junctions` (largest first, centre in UE and model space), origin lat/lon,
  geo_reference, xodr path, profile.

### Stage 2 — `ue/bake_level.py --manifest <build>/ue/manifest.json --name <Name>`
```
UE_ROOT=/home/german/Projects/CARLA_SOURCE/UnrealEngine_5
DLSS_SDK=/home/german/SDKs/DLSS flock /home/german/Projects/CARLA_SOURCE/.omc/ue.lock \
  $UE_ROOT/Engine/Binaries/Linux/UnrealEditor-Cmd \
  /home/german/Projects/CARLA_SOURCE/carla-ue58-dev/Unreal/CarlaUnreal/CarlaUnreal.uproject \
  -run=pythonscript "-script=<abs>/ue/bake_level.py --manifest <abs>/ue/manifest.json --name Eixample" \
  -unattended -nosplash -stdout -FullStdOutLogOutput
```
1. **Materials** — `MI_<Name>_<key>` instances under `Maps/Twins/<Name>/Materials`, parented to
   what `AOpenDriveGenerator` and the classic towns use: road → `GenericMaterials/Roads/
   MI_RoadAsphalt_Town15`, sidewalk → `Sidewalk/MI_Sidewalk_Apartment`, curb →
   `Gutters_Curbs/Curb/MI_CurbDirty01`, markings → `Roads/MI_Road_Asphalt_B_LaneMarkingWhite|
   Yellow` (the Town10HD lane-marking meshes use these), grass → `Ground/MI_LargeLandscape_Grass`,
   ground → `Ground/MI_Grass_Park`, building → `Facade/MI_Facade01|03|05|07, MI_Brick01`
   (rotated per tile). Tiling is tuned on the instance, not baked in the mesh.
2. **Meshes** — `AssetImportTask` + an `InterchangePipelineStackOverride` (generic assets
   pipeline: static meshes only, no materials/textures, `build_nanite`), destination
   `/Game/Carla/Static/<Label>/Twins/<Name>/`. The label folder is **required**: `ATagger`
   reads the folder right under `/Game/Carla/Static/` (`Road`, `SideWalk`, `RoadLine`, `Building`,
   `Terrain`) to tag components, which is what semantic segmentation and the collision sensor's
   `static.road` / `static.sidewalk` ids come from — a mesh under `Maps/Twins/` would tag as
   `None`. Then per mesh: material slot 0 = our MIC, Nanite on with `fallback_percent_triangles =
   1.0` (the physics fallback mesh is the full surface), collision *complex as simple* (markings:
   no collision).
3. **Level** — `LevelEditorSubsystem.new_level(path, is_partitioned_world=True)`; one
   `StaticMeshActor` per asset at the origin, `AVehicleSpawnPoint` per manifest spawn point
   (`ACarlaGameModeBase::StoreSpawnPoints` reads them; without any it would derive them from the
   xodr topology), a `PlayerStart`, `BP_Carla_Sky` (not spatially loaded). Actors are spawned with
   `UOpenDriveToMap.spawn_actor_with_check_no_collisions` (CarlaTools; plain `UWorld::SpawnActor`) — the editor placement path
   (`EditorActorSubsystem.spawn_actor_from_*`) segfaults under `-run=pythonscript`. World
   Partition runtime hash: the engine default for `new_level` (RuntimeHashSet, 256 m grid
   cells); with 250 m tiles every tile is ~one cell.
4. **OpenDRIVE** — copied to `Content/Carla/Maps/Twins/<Name>/OpenDrive/<Name>.xodr`.
   `UOpenDrive::GetXODR` looks in `<dir of the .umap>/OpenDrive/`, and because that directory is
   named after the map it accepts any `*.xodr` there (same layout as Town12/13/15).
   `client.get_available_maps()` finds the level through the AssetRegistry / `*.umap` scan — no
   registration; for a *packaged* server add `+MapsToCook` and `+DirectoriesToAlwaysStageAsUFS`
   (`Carla/Maps/Twins/<Name>/OpenDrive`) to `DefaultGame.ini`.
5. `bake_report.json` next to the manifest: per-mesh nanite/collision/material, actor and spawn
   point counts, content size.

Generated content is a build product (not committed): `Content/Carla/Maps/Twins/<Name>/`,
`Content/Carla/__ExternalActors__/Carla/Maps/Twins/<Name>/`,
`Content/Carla/Static/{Road,SideWalk,RoadLine,Building,Terrain}/Twins/<Name>/`.

### Packaging a baked twin as a content pack (evaluated 2026-09-01, not yet executed)

The content-pack tooling (commit `da94ed2b7`, branch `ue58-dev-carla`, checked out in
`carla-ue58-cosmos`) supports World Partition maps by design: `carla-pack add --map ...
--world-partition` copies the map's `__ExternalActors__`/`__ExternalObjects__` and `_BuiltData`
into the pack plugin and the manifest records `world_partition: true`; `FindPathToXODRFile`
searches mounted packs' `Content/Maps/OpenDrive/`. A baked twin would fit like this:
1. author the pack in the tree that has the tooling: `carla-pack init TwinEixample`, then run
   `ue/bake_level.py` with `--map-root /TwinEixample/Maps --mesh-root /TwinEixample/Static/
   {semantic}/Twins/{name}` after a *Save-As into the pack* (a WP map must be duplicated into
   the pack mount from the editor, file copies keep `/Game/...` references); the pack-side
   Tagger rule is `/<Pack>/Static/<Label>/...` (first `Static` folder), which the mesh-root
   above satisfies.
2. `carla-pack add TwinEixample --map .../Maps/Eixample.umap --xodr Eixample.xodr
   --world-partition`, `carla-pack build --base carla-0.10.0-Linux-release-metadata.tar.gz`
   (needs the source build's editor as DLC cooker + the base-release metadata, both present in
   `carla-ue58-cosmos/Build`), `carla-pack install` into the pack-capable packaged server.
Blockers for doing it from this lane: the tooling, the mount RPCs and the pack-aware Tagger are
not on `ue58-dev` (this tree) — authoring/cooking would have to happen in the
`carla-ue58-cosmos` worktree (`ue58-dev-carla`), which another task owns; and a WP pack has not
been verified end to end anywhere yet (TestPack is flat). No blocker is architectural.

### Stage 3 — `tools/carla_level_check.py <build_dir> <name> --level <Name> --port 4000`
Same soak as `carla_load_check.py` (20 TM vehicles, 600 sync frames, collision sensors, stuck
tracking, junction captures) but `client.load_world(<Name>)` instead of
`generate_opendrive_world`. Output in `<build_dir>/carla_level/`. A level "drives" only when the
report says so (0 `static.road` / `static.sidewalk` collisions, vehicles moving, captures show
the baked surfaces).

## Tools

- `tools/carla_load_check.py` — loads a `.xodr` into a running ue58-dev server (`generate_opendrive_world`), captures
  the largest junctions, runs a TM fleet soak with collision sensors and writes `carla_report.json`.
  Server recipe in the integrator section; check `ss -ltn` for a lingering port 3000 before relaunching.

## Region profiles (`twinmodel/profiles.py`)

Every dimensional or urban-form constant lives in ONE place: a `StreetProfile` (lane defaults per
`highway=*`, sidewalk/verge/parking rules, marking colours and dash pattern, crossing width,
junction clustering radii, chamfer/plaza scan distances, canyon thresholds, elevation smoothing,
and the ordered imagery/DEM providers). Modules read it with `profiles.get()` **at call time**;
never copy a number into a module. Pure numerical tolerances (mm precision grids, triangle-area
epsilons) stay in the modules.

- `EU_DENSE` — the values the pipeline was calibrated with on Eixample; the Eixample build is the
  regression check for any refactor (`tests/test_profiles_*` pin checksums).
- `US_URBAN` / `US_SUBURBAN` — MUTCD / AASHTO / NACTO / PROWAG values (feet in the comments):
  10–12 ft lanes, yellow centre lines, 10 ft / 30 ft dashes, 10 ft crosswalks, parking both
  sides on residential streets, 5 ft sidewalks behind 4–6 ft planting strips (`verge` lane and
  surface kind), 40–60 m junction clusters, NAIP + 3DEP sources, and the divided-carriageway
  model above (`dual_carriageway_*`, `median_max_width_m`, `sliver_m`; all off in `EU_DENSE`).
- Selection: `twinmodel build --profile auto` → `ingest.osm.country_for_bbox` (Overpass
  `is_in`) → `profiles.choose_for_country(iso2, building_coverage)`; US picks urban when building
  footprints cover ≥ 30 % of the bbox. `--profile <name>` overrides. The chosen profile is recorded
  in `metadata["profile"]` and printed at the top of the build log.
- Lane order outward from the reference line: driving… | biking | parking | verge | sidewalk.
  `verge` → OpenDRIVE `border` lane; mesh group `verge` (grass, curb-top level).
- `ParkingAisleRules` (see "Parking lots and their aisles"): `include`, two-way / one-way aisle
  width, minimum length, aisle speed limit, `include_driveways` + `driveway_width`. US: 24 ft
  two-way / 13 ft one-way, 10 mph, driveways off. EU_DENSE: 6.0 / 3.5 m, `include=False` (the
  Eixample regression).

## Freeways, ramp gores and grade separation

`highway=motorway|motorway_link|trunk|trunk_link` are the *freeway* classes: no sidewalk, no
verge, no on-street parking and no street canyon (buildings beside a freeway are behind the
right of way, never a cross section). They get paved **shoulders** instead —
`ClassDefaults.shoulder` (outside) and `shoulder_inner` (median side of a oneway carriageway),
AASHTO 10 ft / 4 ft on a US freeway, 2.5 m / 1.0 m in EU_DENSE — exported as OpenDRIVE
`shoulder` lanes and part of the `drivable` surface.

**Ramp gores.** A cluster whose arms are all in `LaneRules.grade_separated_classes` and of which
at least one is a ramp (`link_classes`) is a merge/diverge *gore*, not an intersection
(`_Cluster.kind` / `Junction.tags["kind"] == "gore"`). Gores differ from intersections in four
ways: their intersection nodes cluster at `JunctionRules.gore_cluster_m` (0 m) instead of
`cluster_m`, so a gore never swallows the whole speed-change lane; the band-overlap trim (7f)
is skipped, because a ramp *is* meant to run beside the mainline for 100–200 m; there is no
plaza, no chamfer, no sidewalk apron and no traffic light (`gore_cover` is the cover polygon);
and the lane mapping (`_gore_movements`) lays the arrival and departure lane groups side by
side in their lateral order and matches them one to one, so a 5-lane arrival splitting into 4
mainline lanes plus a 1-lane off-ramp maps straight across. The mainline lane count changes at
the gore and `7h` tapers the carriageway width step.

**Grade separation.** `layer` / `bridge` / `tunnel` travel from the OSM way into `Road.tags`
(`model.road_osm_layer`, `model.road_is_bridge`). Ways on different layers never meet: a node
interior to two drivable ways of different layers is split per layer (lanegraph 1b), a chain
that is a bridge or changes layer never joins a junction cluster, and a bridge deck is always
its own road (the chain compatibility test includes layer and bridge). Each layer then gets
its own `drivable` / `sidewalk` / `verge` / `crossing` surfaces (tagged `layer`), its own curbs
and markings, and `RoadDatum.z(x, y, layer=...)` only considers roads of that layer, so the
mesh does not fuse a deck onto the carriageway 6 m below it.

A DTM has the deck removed, so elevation is handled from both sides: a *deck*
(`cli.deck_road_ids` — `bridge=*` **or** `layer > 0`, a viaduct is often tagged only with the
layer) takes a straight profile between its abutments (`cli.apply_bridge_profiles`, the whole
chain of linked deck roads at once, abutment z = the highest DEM sample within
`ElevationRules.bridge_abutment_m` of the chain end); and the roads *under* a deck have the DEM
masked over the deck footprint and interpolated across (`road_profile_from_dem(mask=...)`, only
where the deck crosses the road's interior — a deck that reaches the road's end is the same
street continuing over the bridge). `validate` reports `grade_separation`: the minimum z gap
between driving waypoints of different layers that share an xy, which must be
`>= MIN_CLEARANCE_M` (4.5 m).

**Clipped decks.** A chain end with no approach road is *free*: the bbox cut the structure
(SF SoMa's I-80 is elevated right across the tile and its ramps leave it on every side), so no
abutment exists anywhere in the data and the DEM there is bare earth — the street the viaduct
flies over. `anchor_z` reports whether an end is a real abutment; a free end is lifted until the
deck clears every plain road on a lower layer whose carriageway its footprint passes over by
`ElevationRules.min_clearance_m` (6.0 m: AASHTO/Caltrans want 16 ft 6 in to the soffit and the
structure adds about a metre). Both ends free shifts the chain rigidly, so its DEM-derived grade
survives; one end free pivots about the real abutment and ignores crossings within
`clearance_abutment_skip_m` of it. Roads that *meet* the chain — linked, or arms of a junction
it reaches — are never counted as crossed, exactly as `validate.grade_separation` excludes them.
`ElevationRules.max_deck_lift_m` caps the result.

**Abutment welds.** The deck's abutment z is the top of the DEM step beside it, while the
approach's own smoothed profile is dragged down by the half smoothing window
`road_profile_from_dem` extends *past* the road end — straight over the deck, into the trench of
the road below. The two sides then disagree by 1–2 m (US-101 × Mathilda: 1.77 m), a real ledge
in the exported surface. `cli.weld_deck_abutments` pulls the approach onto the deck (the side
whose z comes from ground the DTM actually shows), fading the offset out over
`ElevationRules.abutment_blend_m` (40 m) and densifying the reference line so the ramp has
vertices; it walks on into the next road when the first is shorter than the blend, and stops at
junctions, whose plane `harmonize_junction_z` re-fits afterwards. In a CARLA soak that ledge
shows up twice: as `static.road` scrapes on the step, and as `static.terrain`, because CARLA's
runtime terrain heightfield hugs the lowest paved z per raster cell and read the deck's height
in the cells beside the approach.

`RoadDatum.z(x, y, layer=...)` keeps a layer-restricted query on its own layer while a road of
that layer is within `datum.LAYER_FALLBACK_M` (20 m) before falling back to the nearest road of
any layer — a deck's surface polygon reaches a little past its coverage buffer, and the plain
fallback dropped those vertices onto the street 6 m below, a cliff at the end of the viaduct.

`tools/gradesep_preview.py TWIN OUT.png CX CY HALF` renders an oblique 3D view of a crossing
(surfaces coloured by layer, tightest z gap annotated) for eyeballing all of the above.

## Rules for all workers

- Pure functions over the dataclasses in `model.py`; no global state; no network inside tests.
- Every module gets a `pytest` file exercising it on the cached Eixample fixture
  (`tests/fixtures/eixample_overpass.json`, produced once by worker A) — others may stub inputs
  until that lands.
- Log with `logging.getLogger("twinmodel.<module>")`.
- Do NOT commit — the integrator commits on branch `ue58-twins` (subjects `TwinModel: <module>: ...`).
  Don't touch other workers' modules; if you need a field in `model.py`, add it
  (optional, with default) and note it in your final report.
