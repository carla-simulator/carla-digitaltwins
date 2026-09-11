# Procedural street furniture: implementation proposal

Local repository and asset audit, 2026-09-11. This is an implementation design,
not an installed tool or a changed map. Eixample is the first proposed pilot.

## Architecture to reuse

The vegetation pipeline already separates deterministic planning against final
TwinModel surfaces from UE PCG rendering and transactional region replacement:

- `twinmodel/vegetation.py`: stable IDs/seeds, ENU geometry, accepted/rejected
  records, source provenance, overrides and exclusion zones.
- `tools/vegetation_tool.py` and `.html`: map review, selection, undo,
  configuration persistence, preview and bake orchestration.
- `ue/setup_vegetation_tool.py`: a custom plan node feeding UE's standard PCG
  Static Mesh Spawner through the `Mesh` attribute.
- Active native implementation: `carla-ue58-dev/Unreal/CarlaUnreal/Plugins/Carla/`
  `Source/Carla/Vegetation/ProceduralVegetationTool.{h,cpp}`. Import validates
  assets/support before replacing points; bake preserves the previous result
  until the new generation succeeds.

Reuse this architecture, but do not pass furniture through the existing
vegetation schema. That importer explicitly requires vegetation asset paths,
plant kinds and circular pit/crown radii. Furniture needs oriented footprints,
access envelopes, assemblies and mounting metadata. A small dedicated
`PCG_TwinDecoration` graph and adapter can initially follow the same pattern.
Factor shared orchestration later without destabilizing the vegetation tool.
PCG consumes validated points; random scatter cannot establish street semantics.

## Available inputs and important gaps

The inspected `out/v10_eixample/eixample.twin` contains 211 final surfaces
(49 sidewalk, 84 crossing, 76 ground, 2 drivable), 79 curb records and 208
point objects, all trees. There are no furnishing, bus-stop or entrance point
objects. `twinmodel/ingest/osm.py` queries tree nodes but does not currently query
these amenities. Final surfaces, rather than raw OSM street widths, are the
placement authority; rendered support validation must still catch discrepancies.

Read accepted vegetation plans including tree surrounds and actual physical
traffic-rig/sign placements as occupied space. OpenDRIVE signal reference
positions are not physical pole positions. Preserve elevation layers throughout.
Building footprints do not identify entrances, emergency exits or shop access.
Provide explicit author exclusion/access polygons until those inputs exist.

Extend ingestion later for observed `amenity=bench`, `amenity=waste_basket`,
`highway=bus_stop`, `public_transport=platform`, entrance nodes and relevant
street-light/support anchors. Preserve source IDs and original coordinates;
show geometry conflicts rather than silently moving observed infrastructure.

## Verified asset families

These packages exist in the active content checkout. The companion
`asset-audit.json`, when present, records read-only Unreal load/class/bounds
measurements. Package names alone do not establish art quality, mounting points,
front direction or collision suitability; curate those before enabling a preset.
All paths below have `/Game/Carla/Static/` as their prefix.

| Family | Candidate packages | Required review |
| --- | --- | --- |
| Benches | `Static/SM_Bench01`, `Static/SM_Bench02`, `Static/SM_Bench_Plant`, `Static/Town_C/Props/SM_Bench_03` | Seat-facing axis, feet, sitting/access space; planter bench is an assembly footprint |
| Small bins | `Static/SM_TrashCan01`, `Static/SM_TrashCan02`, `Static/SM_TrashCan03`, `Static/SM_Trashcan05`, `Static/Town_C/Props/SM_Trashcan06`, `Other/SM_TrashCan02` | Actual size, opening/service side, ground versus mounting requirements |
| Bus shelters | `Static/Materials/BusStop/SM_BusStop` plus `SM_BusStop_Glasses`; `Static/Materials/BusStop02/SM_BusStop02` plus `SM_BusStop02_Glass` | Structural/glass companion transforms, opening, platform and boarding area |
| Bus-stop panel | `TrafficSign/TrafficSignPanel/Panel_NoConnectors/SM_BusStop01` | Panel requires a compatible support; it is not a shelter |
| Banners | `Other/Banner/SM_Banner01` through `SM_Banner08`, `SM_BannerIndUAB_01` through `_03` | Mount type, authored support/attachment, branding/region fit |
| Bollards | `Static/SM_Bollard_Concrete`, `Static/SM_Bollard_Grey`, `Static/SM_Bollard_Green` | Use only at deliberate access boundaries; retain gaps |

There are additional legacy benches, dumpsters, recycling bins and litter.
Do not mix legacy and current variants indiscriminately or populate every
available asset. A coherent Eixample palette should initially select one bench
family and one small-bin family. No random scale variation for manufactured
objects; variants and restrained orientation differences provide variation.

## Placement rules

The numeric values below are configurable pilot design defaults, not claims of
regulatory compliance. Full accessibility requires reviewed local requirements
and continuous route validation, not merely distance-to-road tests.

1. **Construct curb furniture bands.** Use high-side sidewalk curb sections on
   the correct elevation layer, clip out crossing approaches, corner sight areas,
   entrances and user exclusions, then orient objects along the local curb
   tangent. Determine the normal by testing sidewalk support on each side;
   do not assume line winding. Start with 0.4 m minimum clearance from the
   *entire object footprint* to the curb/road, adjustable per asset and context.
   Fragmented curb IDs must not create repeated groups at every fragment end.
2. **Reserve pedestrian routes first.** Maintain a proposed 1.8 m continuous
   clear route through the block and to crossings/entrances. Require oriented
   footprints and functional envelopes to avoid it. A local free rectangle like
   the vegetation test is insufficient: test connectivity and minimum width of
   the remaining walkable polygon after all proposed groups and existing
   obstacles, preferably with clearance-aware paths between access anchors.
3. **Compose small functional groups.** A rest pocket can have one or two
   benches with a nearby bin and existing tree shade, where width permits.
   Reserve the whole assembly atomically. Keep bench fronts and bin service
   access open. Do not place bins inside tree surrounds. Space inferred rest
   pockets by useful block intervals (initially 40–70 m), not individual random
   points; allow empty narrow streets. A waste-service cluster uses a dedicated
   zone and different assets, rather than replacing pedestrian litter bins.
4. **Protect crossing and signal visibility.** Initially exclude candidates
   within 5 m of crossing polygons and explicit junction sight polygons; this
   conservative preview filter is not a complete sight-distance model. Final
   tall-object validation needs driver eye-to-signal/crossing visibility against
   lane approach geometry and the full 3D asset envelope. Account for doorways,
   cycle tracks, parking door zones, hydrants and maintenance access when known.
5. **Make transit placement semantic.** A bus-stop anchor identifies road side,
   served lane/direction and platform. Reserve boarding/queue/access space and
   the bus approach/departure zone before selecting a shelter. Reject a shelter
   when the platform is too narrow; an explicitly reviewed panel-only stop may
   remain. Never infer a functional stop from density. With current Eixample
   data, bus stops require author anchors or an ingestion extension. Decorative
   shelters do not create CARLA bus-routing behavior.
6. **Mount banners to real supports.** Require a reviewed pole/wall anchor and
   an asset-defined attachment transform, or a verified self-supporting complete
   asset. Validate height and swept clearance, compatible support dimensions,
   orientation and signal occlusion. Do not put a panel at ground level or attach
   banners automatically to traffic-light poles. Until mounting metadata exists,
   banners stay unavailable for automatic generation.
7. **Validate actual ground and geometry.** Use oriented XY footprints derived
   from measured meshes plus reviewed support points, service/sitting envelopes
   and assembly extents. Trace feet/corners onto eligible sidewalk/terrain at
   matching elevation, ignoring the tool's prior instances. Reject mixed levels,
   gaps, excessive support height differences or unexpected road/building hits.
   Use measured pivot-to-foot transforms so props neither float nor bury their
   legs. Upright bins and shelters must not inherit arbitrary slope tilt.
8. **Keep one shared occupancy view.** Include existing manually placed props,
   trees and surrounds, poles, signs, signal masts and all accepted groups.
   Road, cycle, crossing, support, collision and access checks apply equally to
   manual placements. Report why a group failed and preserve the last good bake.

## Proposed plan and asset contracts

Version a `twin-decoration-plan/1` document with input hashes, region, seed,
coordinate convention, palette version and every candidate/rejection. Each group
has a stable ID, provenance (`observed`, `inferred`, `manual`), anchor, surface
layer, members, occupied/functional polygons and dependencies. Each member has
asset path, transform, stable seed, semantic class, support points and optional
mount parent/attachment. Store author overrides by ID, not array position.
Use ENU metres in geometry and explicit CARLA-metres transforms (`y=-ENU_y`);
the adapter converts to UE centimetres once. No hidden snapping of source data.

An asset catalog records class, local bounds, feet, front axis, scale=1,
footprint, functional envelopes, supported surface/mount type, collision policy,
semantic class and optional assembly children. Keep physical collision separate
from visual glass/cloth. PCG instancing is suitable for passive repeated meshes;
interactive doors, animated supports or behavior-bearing assets need an explicit
actor-spawning path. Verify CARLA RGB/depth/semantic/LiDAR behavior after baking.

Use `group-ID/member-ID` seeds so unrelated additions do not reshuffle variation.
Changing a region replaces only that tool-owned region; failed validation does
not delete the old result. Validate cross-region overlaps and ownership instead
of assuming independent plans cannot collide. Preview can show rejected footprints,
reserved walking strips and mount links. Expose palette, group frequency,
clearances, seed, author anchors/exclusions, disable/move/lock, undo and bake.

## First implementation and acceptance

Implement benches plus small bins on a selected Eixample block first. Deliver
asset catalog with measured pivots/feet, a pure-Python grouped planner and tests,
review UI using existing map interactions, a small native decoration adapter and
PCG graph, and transactional region bake. Defer bus shelters and banners until
semantic anchors and mounting/assembly metadata are reviewed. This keeps the first
visible result useful without inventing transit infrastructure.

Test narrow sidewalks, chamfered corners, split/reversed curbs, crossing access,
existing tree bases, grouped placement failure, rotated footprints, bridge layers,
manual invalid placements and seeded regeneration. Confirm connected pedestrian
access, no road overlaps, real foot contact, correct facing and materials in a
street-level preview; bake twice and reload World Partition to detect duplicates.
Check semantic/LiDAR output and generation/runtime cost on the same camera path.
Success is coherent useful streetscape groups with open walking space, not a
maximum prop count. No native compilation, deployment or map changes were made
as part of this investigation.

## Measured pilot assets

All 14 selected packages loaded as `StaticMesh` in a read-only Unreal Python
commandlet. Bounds are local-axis dimensions in metres, not rotated world
footprints or support-point measurements. Full precision bounds/materials are in
`asset-audit.json`. No assets were saved.

- Bench01: 1.79 × 0.64 × 1.00 m; Bench02: 2.40 × 0.68 × 0.78 m.
- Bench_Plant: 4.17 × 1.39 × 1.28 m, with an off-center pivot (local X minimum
  −3.28 m). Centering its collision reservation on the actor would be incorrect.
- TrashCan01: 0.54 × 0.37 × 1.21 m; TrashCan02: 0.79 × 0.67 × 1.06 m;
  TrashCan03: 0.76 × 0.76 × 0.99 m.
- BusStop: 1.89 × 3.88 × 2.74 m; BusStop02: 3.97 × 1.33 × 2.29 m.
  Their long axes differ. Glass packages have elevated local minima and must
  retain assembly-relative transforms rather than being individually grounded.
- Banner01: 0.21 × 3.04 × 2.00 m, local Z minimum −1.95 m;
  BannerIndUAB_01: 0.10 × 1.27 × 3.04 m, local Z minimum −3.00 m.
  Their top-biased pivots demonstrate why a generic ground scatter would fail.
- BusStop01 sign panel: 0.52 × 0.01 × 0.76 m; centered vertically around its
  origin, requiring a mounting transform.

The load/bounds audit does not validate collision, LOD quality or visual fronts.
Those are explicit first-implementation checks, not completed findings.
