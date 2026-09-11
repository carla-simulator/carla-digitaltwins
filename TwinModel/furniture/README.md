# Procedural furniture tool

Reusable TwinModel planning, local map authoring, UE 5.8 PCG preview and persistent
region baking. The curated palette includes bench/bin rest groups, bus shelters with their
glass assemblies, shelterless stop poles with mounted panels, and supported
banners. Every assembly is atomic. No random asset scaling.

## Use

From the CARLA source workspace (adjust paths for your checkout):

```bash
.venv-twins/bin/python carla-digitaltwins/TwinModel/tools/furniture_tool.py \
  --twin carla-digitaltwins/TwinModel/out/v10_eixample/eixample.twin \
  --output carla-digitaltwins/TwinModel/out/furniture_eixample \
  --poles carla-digitaltwins/TwinModel/out/furniture_eixample/poles.json \
  --vegetation carla-digitaltwins/TwinModel/out/vegetation_eixample/plan.json \
  --region eixample --serve \
  --level EixampleDemo \
  --engine /absolute/UnrealEngine_5/Engine/Binaries/Linux/UnrealEditor-Cmd \
  --project /absolute/carla-ue58-dev/Unreal/CarlaUnreal/CarlaUnreal.uproject
```

Open http://localhost:8795. Inspect accepted and rejected groups, change spacing,
walking width or seed, move/disable/reset a selected group, add bus-stop or banner
anchors, import observed OSM anchors, draw exclusion areas,
undo edits, regenerate, and bake. A moved group passes all the same checks. Saved
`config.json` retains author edits across sessions. The last successful bake is
retained if a replacement fails. Reload the CARLA map after saving.

The framework CLI also exposes `twinmodel furniture --twin <map.twin> --out
<plan.json> --poles <physical-poles.json> --vegetation <vegetation-plan.json>`;
optional `--config`, `--anchors`, and repeatable `--occupied-plan` reserve neighboring furniture
regions. Both entry points call `twinmodel.furniture.plan`, which can also be used
from Python. The measured catalog and defaults ship as package data.

`ue/export_furniture_occupancy.py` exports actual traffic actor positions from the
loaded world. Its convenience exporter currently labels supports as layer 0;
review layer assignments for multilevel maps. Do not use road-reference signal
coordinates as a substitute for actual physical support locations. Export with
the whole region loaded. Supply the current baked vegetation plan, including its
tree-base reservations. Additional hand-placed props and doorways need author
exclusions; they are not inferred from building footprints.

## Unreal setup and bake

Build the Carla module containing `Furniture/ProceduralFurnitureTool.{h,cpp}`.
Run `ue/setup_furniture_tool.py` once with UnrealEditor-Cmd `-run=pythonscript`.
It creates `/Game/Carla/Blueprints/PCG/FurnitureTool/PCG_TwinFurniture`, with a
CARLA Furniture Plan node feeding the standard PCG Static Mesh Spawner by `Mesh`.
This is a separate schema and actor from vegetation. Setup also enables and
saves UE 5.8 instanced-mesh usage on curated materials that lack it; otherwise
Unreal can display a default material after reloading the baked map.

`ue/bake_furniture.py --name <map> --plan <plan.json> --region <key> --report
<report.json>` loads the plan region's World Partition actors and creates or
updates `FURNITURE_<key>`. Only that actor's generated instances are replaced.
It checks complete groups, measured asset bounds, nine support probes per grounded prop,
sidewalk identity and level, and pavement height variation. Attached glass, panels
and banners inherit their parent transform with reviewed offsets; their elevated
pivots are never independently grounded. Unsupported groups
are rejected together and recorded in `validated-plan.json` and the bake report.
Other errors abort without saving. An entirely unsupported nonempty plan cannot
clear an existing region. Preview must complete with the exact expected instance
count before replacing the baked components. Native editor buttons also expose
Import Plan, Preview, Bake and Clear Generated. Save the level after editor use.

## Placement contract

- Geometry is ENU metres; PCG transforms are CARLA metres (`y=-ENU_y`), converted
  once to UE centimetres. Measured pivots are accounted for at ground projection.
- Curbs are merged, direction-normalized and stationed deterministically. Groups
  face into the sidewalk; footprints and front access areas must fit it.
- Default full-footprint road clearance: 0.45 m; crossing approach clearance: 5 m;
  inferred group interval: 45 m. These are configurable design defaults.
- The remaining sidewalk/crossing network is eroded by half the configured 1.8 m
  pedestrian width. A group cannot split or remove an existing usable component.
  This preserves existing connectivity; it does not repair existing disconnected
  sidewalks or certify accessibility compliance.
- Existing vegetation pits/bases, poles, building footprints, author exclusions,
  other supplied furniture plans, and previously accepted group access areas are
  occupied. Groups on different elevation layers are planned separately.
- Benches, bins and poles allow at most 2.5 cm ground variation by default and
  stay upright. Shelters fit a pavement plane up to 3 degrees, with at most 2.5 cm
  residual unevenness; their glass follows the exact same transform. Unreal
  rechecks the rendered ground before replacing the saved region.
- Stable group/member IDs and seeds, accepted/rejected records, no random scale.
  Changing curb geometry or candidate-step spacing can change inferred IDs.

## Extending the palette

Curate bounds, base/support points, local front direction and service access in
`twinmodel/data/furniture/catalog.json`, add a semantic group rule to the planner,
and extend the native approved mesh/kind contract. Check materials, collisions,
semantic output and ground contact in Unreal before enabling an asset.

Bus stops and banners use explicit anchors rather than density scatter. Import
observed bus stops offline, or explicitly fetch the map bbox first:

```bash
python -m twinmodel.furniture_anchors --twin <map.twin> \
  --osm-json <cached-map.json> --out <anchors.json> [--fetch]
```

The importer preserves source IDs, coordinates, names, tags and OSM node links.
`shelter=yes` selects the complete shelter/glass pair, `shelter=no` selects the
pole/panel pair, and unknown shelter types remain in the import review list.
The panel uses the framework catalogue’s VC E13 bus-stop symbol on a dedicated
mesh copy. Its +Y face points toward the road, with a 2.5 cm support-relative
offset that places the plate against the front of its pole.
The studio can load this document through **Import OSM anchors**; batch planning
accepts `--anchors`. The importer does not rebuild map geometry or change traffic.
OSM data remains attributed to OpenStreetMap contributors under ODbL 1.0.

Stop assemblies face the nearest compatible curb. The planner evaluates bounded
station fits (0, ±2, ±4 m) within 6 m of the original observation, with full geometry
checks on each fit. Blue source markers and connecting lines expose displacement
in the studio; original coordinates are never overwritten. Boarding space is
reserved against other props while remaining available to pedestrian circulation.
All source stops are reviewed even when no fit is possible. Existing mapped trees
are preserved. These are visual stop installations, not CARLA bus-routing logic.

A manual banner anchor creates the reviewed `SM_Pole05` + `SM_Banner01` assembly.
Its 4.47 m support-relative mount leaves approximately 2.51 m under the fabric;
the asset includes its crossbars. The whole overhang is checked against sidewalk,
road, crossing and obstacle envelopes. Eixample's two banner locations are proposed
decoration, explicitly marked `source=manual`, not claimed existing OSM features.

## Verification

`pytest tests/test_furniture.py tests/test_furniture_anchors.py tests/test_vegetation.py` covers deterministic
groups, reversed/split curbs, narrow sidewalks, crossings, tree bases/poles,
manual invalid moves, atomic disables, neighboring-region occupancy, elevation
layers and pedestrian-network preservation. Eixample was also baked twice and
reloaded in CARLA to verify persistent counts and inspect the actual props.
