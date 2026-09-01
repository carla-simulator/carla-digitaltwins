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

## Junction detection (the Eixample problem)

Cluster OSM intersection nodes that are within `JUNCTION_CLUSTER_M = 30 m` of each other *and*
connected by ways shorter than that into one `Junction`. The junction polygon is the union of the
drivable buffers of all ways inside the cluster (worker B). Roads that end at the cluster boundary
become incoming roads; for every legal in→out pair (respecting `oneway` and `restriction`
relations), worker A builds a connecting road as a cubic Hermite curve between the two lane ends,
tangent-continuous, sampled every 1 m. Its centreline must stay inside the junction polygon —
`validate` checks this.

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
line/arc quantisation. Elevation profile: piecewise cubic from the sampled z. Lanes: constant
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
  surface kind), 40–60 m junction clusters for divided arterials, NAIP + 3DEP sources.
- Selection: `twinmodel build --profile auto` → `ingest.osm.country_for_bbox` (Overpass
  `is_in`) → `profiles.choose_for_country(iso2, building_coverage)`; US picks urban when building
  footprints cover ≥ 30 % of the bbox. `--profile <name>` overrides. The chosen profile is recorded
  in `metadata["profile"]` and printed at the top of the build log.
- Lane order outward from the reference line: driving… | biking | parking | verge | sidewalk.
  `verge` → OpenDRIVE `border` lane; mesh group `verge` (grass, curb-top level).

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

A DTM has the deck removed, so elevation is handled from both sides: a bridge deck takes a
straight profile between its abutments (`cli.apply_bridge_profiles`, the whole chain of linked
bridge roads at once, abutment z = the highest DEM sample within
`ElevationRules.bridge_abutment_m` of the chain end); and the roads *under* a deck have the DEM
masked over the deck footprint and interpolated across (`road_profile_from_dem(mask=...)`, only
where the deck crosses the road's interior — a deck that reaches the road's end is the same
street continuing over the bridge). `validate` reports `grade_separation`: the minimum z gap
between driving waypoints of different layers that share an xy, which must be
`>= MIN_CLEARANCE_M` (4.5 m).

## Rules for all workers

- Pure functions over the dataclasses in `model.py`; no global state; no network inside tests.
- Every module gets a `pytest` file exercising it on the cached Eixample fixture
  (`tests/fixtures/eixample_overpass.json`, produced once by worker A) — others may stub inputs
  until that lands.
- Log with `logging.getLogger("twinmodel.<module>")`.
- Do NOT commit — the integrator commits on branch `ue58-twins` (subjects `TwinModel: <module>: ...`).
  Don't touch other workers' modules; if you need a field in `model.py`, add it
  (optional, with default) and note it in your final report.
