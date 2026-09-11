# Vegetation tool review — 2026-09-10

Follow-up implementation: [procedural vegetation tool](../vegetation/README.md).
The findings below describe the original pre-implementation audit.

Inspected local UE 5.8 C++ and loaded the actual Blueprint/PCG assets in a read-only
editor commandlet. No graphs, presets, tree meshes or map actors were changed.
This is a source/asset inspection, not a generation-performance benchmark.
Evidence: workspace `.omc/vegetation-tool-review-2026-09-10/`.

## Existing components worth retaining

The running `ue58-dev` project mounts `CarlaDigitalTwinsTool` as a **content-only**
plugin. Native implementations are in Carla/CarlaTools. The standalone
`carla-digitaltwins` and `carla-mesh-generation` source repositories are not the
native modules loaded by this server; they differ from the installed code.

- `/CarlaDigitalTwinsTool/Blueprints/Tools/VegetationSpawner/`: street-tree tool,
  `BP_SpawnVegetation`, `BPe_DigitalTwinsReader`, `PCG_VegetationSpawner`, and
  `DA_VegetationMeshes`. The PCG graph loads and has 40 nodes, data-asset reads,
  random attributes and two static-mesh spawners using mesh attributes.
- `/CarlaDigitalTwinsTool/Blueprints/Tools/ForestSpawner/`: spline/area-based
  vegetation, with Acer, pine, mixed, bushes, grass and flower presets. Its main
  graph has 98 nodes, spline sampling, differences, density operations, scale and
  rotation processing, raycast loops, and three static-mesh spawners.
- `/Game/Carla/Blueprints/PCG/ForestSpawner/`: another version of the forest tool.
  Its graph has 99 nodes and includes the custom Poisson-disc node. The digital
  twins graph uses a native spline sampler in that part of the graph instead.
  Editing one asset does not update the other.
- Native `CarlaTools/Private/OpenDriveToMap.cpp::GenerateTreePositions` creates
  `TreeSpawnPosition` marker actors from LibCarla's road-spacing helper.
  It calls that helper with its legacy default arguments.

This already provides useful asset selection, visual variation and instancing.
The missing integration is between the final TwinModel street geometry and the
placement inputs, not a lack of PCG scatter nodes.

## Findings

### Eixample has source tree data that its bake does not consume

`out/v10_eixample/eixample.twin/objects.geojson` contains 208 OSM-derived tree
records. All 208 lack species/genus; 49 have `leaf_type`. The TwinModel Unreal
export and baker do not consume these tree objects. The running EixampleDemo
reports zero vegetation environment objects through CARLA's API (not an exhaustive
inventory of untagged meshes).

68 tree points overlap the current generated drivable/parking/crossing union.
This is a disagreement between source points and generated street geometry,
not evidence that 68 real-world trees are incorrectly located. Preserve source
coordinates and flag conflicts. Do not silently move surveyed trees to make the
current road model look valid.

### Road-offset rules are insufficient for the final map

`LibCarla/source/carla/road/Map.cpp::GetTreesTransform` has newer options to measure
from the curb and stay on a sidewalk. They default to false in Map.h, and the
legacy tool's `GenerateTreePositions` call does not enable them. The helper also
prefers one outside driving-lane side, rather than producing two independent
street-tree rows. Even its improved lane-based curb logic does not validate the
finished chamfered junction polygons, access paths, pole locations or tree crowns.

### The custom sampler has concrete source defects

In the installed `CarlaTools/Private/BlueprintLibrary/PoissonDiscSampling.cpp`:

- Lines 132–133 seed from `std::random_device`, rather than the PCG seed. A forced
  regeneration is not reproducible from the graph's seed.
- Lines 200–202 add the first point to results and pending work, but never insert
  it into the neighbour grid. Later candidates can violate spacing around it.
- Distance/sample settings have no runtime validity guards before division and
  allocation; grid cell multiplication uses 32-bit integers, and storage is
  reserved for the whole bounding rectangle.
- Sampling covers the bounding rectangle, then discards outside points. Narrow or
  concave regions waste work. There is no cooperative yielding in that loop.
- `bFilterInsideSpline` is exposed but ignored: filtering is unconditional.
- Output points do not get distinct initialized seeds, and output tagged data
  does not copy the input's tags.
- Height comes from the nearest sampled boundary point, not the interior terrain.
  A later raycast can correct it; this node alone cannot.

These findings apply to the custom node. They must not be attributed to Unreal's
native spline sampler, which the mounted digital twins forest graph uses.
The variable names suggesting local/world-coordinate confusion are misleading:
UE 5.8 `UPCGSplineData::GetLocationAtAlpha` returns world space. No double-transform
bug was established in the output path.

### World raycasts need an explicit planting-surface contract

The inspected forest raycast subgraph uses complex `WorldStatic` queries with no
actor class/tag restriction, and `ignore_pcg_hits=false`. That is a broad collision
query, not a semantic test that a point belongs on soil or a designated tree pit.
The graphs do contain differences and filters; their presence alone does not
establish road, crossing, entrance and sightline guarantees. Generation order and
other PCG meshes could matter; this needs a controlled test rather than a claim
that every existing placement is wrong.

## Recommended first implementation

Use Eixample as a small street-tree pilot and retain PCG as the rendering and
instancing layer:

1. Export a vegetation plan from the **baked final TwinModel**, preserving stable
   OSM IDs, source positions and whether each point is observed or inferred.
2. Validate trunk/tree-pit footprint against roads, bicycle lanes, crossings,
   buildings, newly placed poles and pedestrian access corridors. Treat crown
   clearance separately; an overhanging crown does not imply an illegal trunk.
3. Produce accepted/rejected points with reasons. Keep source conflicts visible
   for review. Synthetic rows may fill gaps only when explicitly enabled, with
   their own provenance and deterministic spacing/seed.
4. Add a small PCG input adapter carrying position, asset path, scale, rotation,
   stable seed and provenance. Reuse the existing internal mesh-selection and
   instancing machinery. Start with a labelled visual species proxy because the
   source data do not identify species.
5. Provide preview/regenerate/bake actions and a region-scoped update so changing
   a density or preset does not rebuild the whole town or erase manual overrides.

Acceptance checks: stable results across repeated generation; no trunk/pit
intersection with excluded geometry; ground contact; bounded displacement only
for inferred points; preserved source IDs; no duplicate instances after rebake;
CARLA vegetation semantics/collision; World Partition reload; an RGB/LiDAR check;
measured generation time and steady-state cost on the same camera path.

After the pilot: unify or explicitly version the duplicate graphs; repair/test the
custom sampler if retained; profile per-asset loops and raycasts; then expand to
park/forest understory and regional presets. GPU generation should follow a
measured bottleneck and compatible node path, not precede correct planting rules.
