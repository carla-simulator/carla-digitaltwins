# Procedural vegetation tool

Author tree rows and planting beds from the **final baked TwinModel surfaces**, review each placement, then generate and bake CARLA's internal vegetation meshes through UE PCG. The planner is independent of Unreal; the native adapter and graph require the CARLA UE5.8 build.

## Start

From the CARLA_SOURCE workspace (Python needs TwinModel's dependencies, including Shapely):

```bash
.venv-twins/bin/python carla-digitaltwins/TwinModel/tools/vegetation_tool.py \
  --twin carla-digitaltwins/TwinModel/out/v10_eixample/eixample.twin \
  --output carla-digitaltwins/TwinModel/out/vegetation_eixample \
  --poles .omc/eixample-pole-placement-2026-09-10/placements.json \
  --serve --level EixampleDemo --region eixample \
  --project carla-ue58-dev/Unreal/CarlaUnreal/CarlaUnreal.uproject \
  --engine UnrealEngine_5/Engine/Binaries/Linux/UnrealEditor-Cmd
```

Open http://localhost:8794. Omit `--serve` for a deterministic CLI plan. Omit engine/project/level for planning without baking. Supply actual validated pole placements when the model has traffic controls; OpenDRIVE reference points are not necessarily physical pole locations.

`examples/eixample.json` contains the tested Eixample streetscape configuration;
pass its path with `--config` to reproduce that authoring setup. It includes a
explicit exclusions where the current rendered road differs from the
TwinModel planting polygons, plus recorded disabled candidates from rendered
surface checks. It enables mature street rows and low-shrub beds in three areas.
This example is specific to the v10 Eixample input.

For a fresh content checkout, run `TwinModel/ue/setup_vegetation_tool.py` through UnrealEditor-Cmd's Python commandlet once to create `/Game/Carla/Blueprints/PCG/VegetationTool/PCG_TwinVegetation`. Build the Carla module containing `AProceduralVegetationTool` first.

## Author and bake

1. Inspect mapped trees and rejection reasons. Source OSM coordinates remain in the plan. Enable inferred street rows when desired; they are off by default.
2. Select a preset and draw a polygon for trees, shrubs, or grass. Finish the polygon to regenerate. Grass and shrubs require explicit zones. Draw exclusions around entrances or other areas not represented in the map data.
3. Select a tree to disable it or move it explicitly. Manual placements receive the same clearance checks. Undo restores the preceding configuration. Select an area to delete it.
4. Adjust the seed, spacing, or density and regenerate. `config.json` stores edits, `plan.json` stores accepted transforms plus the full review/rejection list. Restarting restores saved configuration.
5. Bake saves the map and its external actor packages. The commandlet loads spatial actors in the plan region, validates asset footprints, projects onto actual sidewalk/terrain collision, runs PCG's Static Mesh Spawner, checks the instance count, and copies the result into persistent instanced mesh components.
6. Reload the map in CARLA to see the saved result. An already running game does not automatically reload edited content.

Use a stable region name: rebaking replaces that region's owned components. Separate region names are independent; author nonoverlapping regions to avoid duplicate vegetation. Existing unrelated foliage is not automatically removed. Do not simultaneously edit/save the same map from another editor process.

## Rules and coordinates

Configuration polygons and manual positions use **local ENU metres** (TwinModel coordinates). Plan `x,y,z` use **CARLA metres** (`y = -ENU_y`); the native importer converts to Unreal centimetres. Seeds, mesh choice, scale, and rotation derive from stable candidate IDs. Adding unrelated candidates does not reshuffle existing variations, though new neighboring plants can change spacing acceptance.

The planner checks planting-pit support, road/cycle-lane clearance, crossings, building/crown clearance, poles, exclusions, local sidewalk pedestrian width, plant spacing, elevation layer, and slope. Native import rejects missing meshes, undersized mesh footprints, duplicate IDs, missing ground, and unexpected surfaces before replacing the actor's imported points. Bake requires a complete PCG preview. Failed imports do not save over the existing bake.

Presets in `defaults.json` include small deciduous trees, mature street trees, pine, shrubs, and individual grass clumps. Sizes are conservative bounds for the supplied meshes. When adding assets, measure their bounds and update crown/pit radii; the importer checks them. Asset selection is a visual proxy, not an identification of botanical species. All current presets explicitly flag this.

These rules do not infer entrances, underground services, full intersection sight triangles, or a connected pedestrian navigation network. Use exclusions and manual review for those constraints. Crown overlap between neighboring trees can be intentional; spacing controls center separation. New town inputs must contain final support surfaces and suitable elevation data; baking currently targets maps under `/Game/Carla/Maps/Twins/<name>/<name>` with the standard sidewalk/terrain asset folders.

## Implementation and checks

- `twinmodel/vegetation.py`: deterministic candidate generation and validation.
- `tools/vegetation_tool.py` and `.html`: local authoring UI and bake orchestration.
- `ue/setup_vegetation_tool.py`, `ue/bake_vegetation.py`: graph setup and automated bake.
- Active native code: `carla-ue58-dev/Unreal/CarlaUnreal/Plugins/Carla/Source/Carla/Vegetation/ProceduralVegetationTool.*`.

```bash
.venv-twins/bin/python -m pytest carla-digitaltwins/TwinModel/tests/test_vegetation.py -q
```

The legacy forest and street-spawner graphs remain available; this tool uses its own native plan adapter feeding UE's standard PCG Static Mesh Spawner.

## Sidewalk tree bases

Sidewalk trees now include the existing `SM_TreeBase02` stone-and-soil surround.
The surround is a companion PCG instance owned by the same tree ID and region,
so preview, replacement and clearing keep the tree/base pair together. Trees
on natural ground do not receive a surround. The UI reports plants and surrounds
separately; native bake instance counts include both.

`sidewalk_footing` in the configuration defines its asset, fixed scale (0.8),
conservative radius (1.12 m before scaling), embedding depth (-0.02 m), and the
measured asset soil height (0.14744362 m before scaling). Tree growth variation
does not resize the base. Set this configuration entry to `null` to disable it.
The supported asset has a raised soil surface: it covers the underlying paving
without requiring a cutout or coplanar surfaces. It is not a flush excavated pit.

The complete base footprint participates in road, pole, building, pedestrian and
plant spacing checks. During import, the base follows the sidewalk normal; eight
edge traces require continuous sidewalk support within 1 cm of that plane. Its
bottom embeds below the pavement while the upright trunk sits at the soil height.
Unsupported/uneven locations fail import, preserving the previous saved bake.
The Eixample example records such exclusions in its overrides, with reasons shown
when inspected. Source tree coordinates remain available in the review.

Bases use CARLA's Static semantic class; trees retain Vegetation. Existing art
assets are reused without modification.

## Verified Eixample pilot (2026-09-10)

The initial pipeline pilot baked 148 instances (98 trees, 1 shrub, 49 grass clumps),
and retained exactly 148 after a second bake and World Partition reload. All
148 instance origins passed runtime ground-contact traces; the maximum measured
gap was below 0.001 cm. RGB and semantic cameras rendered vegetation, and semantic
LiDAR returned vegetation hits. All 200 validated pole positions and 27 traffic
lights remained present. Native compilation, 17 planner tests, and browser tests
for editing, undo, zones, exclusions, and invalid settings passed.

Evidence is in workspace `.omc/vegetation-tool-2026-09-10/`. These are pilot checks,
not a guarantee for every town or asset collection. The native mesh footprint
and planting-surface guards must pass for each new plan.

The revised streetscape example addresses the pilot's sparse appearance: it
enables inferred curb rows with the mature-tree preset and uses compact shrubs
in three planting areas. Before adding sidewalk bases, it contained 230 trees,
76 shrubs and 8 grass clumps.
The same road-facing camera shows rows with substantial canopy instead of
isolated saplings. Seven candidates conflicting with rendered road geometry
remain explicitly disabled; nearby conflicts are covered by exclusions.
Composition evidence: workspace `.omc/vegetation-composition-2026-09-10/`.

With base footprints and rendered-edge checks enabled, the current example has
205 trees (194 with stone surrounds), 76 shrubs and 8 grass clumps: 483 total
plant/base instances. The new footing tests bring the planner suite to 20 tests.
