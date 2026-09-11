# DigitalTwin project CLI and web app

Run from `TwinModel/` using the framework Python environment and matching CARLA
Python wheel. An editable install (`pip install -e TwinModel` from the repo root)
also provides the `twinmodel` entry point. Web assets and Unreal scripts are part
of this repository; Unreal apply requires the compatible CARLA UE 5.8 checkout
with native vegetation/furniture tools.

## Create, edit and apply

```sh
python -m twinmodel project create projects/mycity --bbox SOUTH WEST NORTH EAST
python -m twinmodel project build projects/mycity
python -m twinmodel project target set projects/mycity local \
  --engine /absolute/path/to/UnrealEditor-Cmd \
  --uproject /absolute/path/to/CarlaUnreal.uproject \
  --level /Game/Carla/Maps/Twins/MyCity/MyCity --host localhost --port 2000
python -m twinmodel project edit projects/mycity --target local --port 8791
python -m twinmodel project apply projects/mycity --target local --dry-run
python -m twinmodel project apply projects/mycity --target local
python -m twinmodel project runtime reload projects/mycity --target local
```

Bounds order is **south, west, north, east**. Creation does not fetch. First build
acquires and pins OSM, DEM and imagery. Creation flags `--no-dem`, `--no-imagery`
and `--no-refine` disable unwanted inputs/stages. `--profile auto` is resolved and
pinned during acquisition. Unavailable requested sources fail explicitly.
Subsequent builds use pinned inputs offline.

The project web app combines Layout, Trees & plants, Furniture, and project
build/apply controls. Layout Save persists corrections; valid placement edits save
their configurations immediately and regenerate previews. CLI and UI share those
inputs and planners. Revision checks reject stale browser saves; project locks
serialize writes. Preview metadata lives in `state/`, outside generated artifacts.

Apply updates the saved Unreal level. It does not reload CARLA. The separate
reload command/web button loads the saved level and resets its runtime actors.

## Stages

```text
model → export → validate.model → unreal.geometry → unreal.traffic
 → occupancy.export → vegetation.plan → vegetation.bake
 → furniture.plan → furniture.bake → validate.target
```

Geometry rebuilds restore downstream placements. Traffic support changes update
dependent plants/furniture. Vegetation changes regenerate furniture around the
ground-validated trees and surrounds. Furniture changes leave geometry, traffic
and vegetation current. Unchanged applies skip all stages.
The recipe's `enabled` booleans control traffic, vegetation and furniture. Disabled
placement components emit explicit empty plans and clear only their owned regions,
retaining authoring settings for re-enabling. Traffic cannot be disabled while the
model still contains traffic controls; reconcile those layout inputs first.

```sh
python -m twinmodel project status projects/mycity --target local
python -m twinmodel project build projects/mycity --through model
python -m twinmodel project apply projects/mycity --target local --only furniture
python -m twinmodel project run projects/mycity --target local --stage furniture.bake
python -m twinmodel project validate projects/mycity
python -m twinmodel project validate projects/mycity --target local
```

`--only traffic|vegetation|furniture` still repairs affected dependencies.
`run --stage` refuses stale prerequisites. Model validation uses canonical
CARLA/OpenDRIVE checks; target validation reopens the saved level and compares
actual placement counts with bake reports. Physical support layers are resolved
against model surfaces and height; ambiguous supports stop for review.

Build/apply dry-run does not fetch, build or mutate Unreal. It lists stages and
detectable blockers, without claiming unperformed geometry/ground validation.

## Existing maps and source revisions

```sh
python -m twinmodel project adopt projects/eixample --name eixample \
  --build-dir out/v10_eixample --corrections data/corrections/eixample.json \
  --vegetation out/vegetation_eixample --furniture out/furniture_eixample
python -m twinmodel project create projects/imported --from-twin /data/map.twin
python -m twinmodel project source import-recipe projects/eixample \
  --build-dir out/v10_eixample --cache data
python -m twinmodel project source refresh projects/mycity
```

Adoption copies existing inputs and placement evidence without changing originals.
It starts in snapshot mode: `.twin` is a model directory, not the complete project.
A snapshot exports/bakes without OSM; layout recompilation needs its source recipe.
Recipe import copies original local OSM/DEM/imagery inputs and fails if they are
missing. Explicit source refresh fetches a new revision and retains the previous
one under `runs/`.

`project model import PROJECT NEW.twin` replaces a snapshot base. Changed origins,
existing layout corrections and unresolved placement overrides require reconciliation
instead of silently shifting/discarding edits. New projects record their ENU origin;
bounds changes that shift it are rejected.

## Ownership and recovery

New levels become project-owned after geometry creation. Existing levels require
matching ownership. Inspect and adopt a legacy generated level explicitly:

```sh
python -m twinmodel project target adopt projects/mycity local --dry-run
python -m twinmodel project target adopt projects/mycity local
python -m twinmodel project apply projects/mycity --target local --resume RUN_ID
python -m twinmodel project target restore projects/mycity local \
  --run RUN_ID --stage furniture.bake --dry-run
python -m twinmodel project target restore projects/mycity local \
  --run RUN_ID --stage furniture.bake
```

Adoption compares deployed OpenDRIVE and inventories loaded actors. Unrecognized
actors prevent replacement. Inspection writes a report; `--dry-run` grants no
ownership. Adoption records ownership, not successful stage receipts. Subsequent
apply regenerates the level. Hand edits to generated actor properties are not
authoring inputs and are not preserved by regeneration.

Unreal writers are locked per `.uproject`. Target fingerprints include map assets,
World Partition actors/objects and generated meshes. Mutating stages checkpoint
owned content and receipts. Failures remain explicit partial runs. Resume reuses
verified stages; restore verifies hashes and retains the displaced target first.
Shared plugin/catalogue setup assets are outside map checkpoints: this is not a
transaction over every file in the Unreal project. Portable build failures retain
the preceding published output and previous generations remain in run directories.

## Portable exchange and files

```sh
python -m twinmodel project export projects/mycity --out mycity.twinproject
python -m twinmodel project import mycity.twinproject --into projects/copied
```

The web Export project link uses the same archive format. Archives retain recipe,
source inputs, authoring settings and seeds; exclude local target settings, Unreal
binaries and adoption logs. Import validates manifests, hashes, paths and schemas.
Configure a destination target and install its matching asset/plugin dependencies.
When available, a model snapshot is included as an editor review baseline. Pending
annotations can therefore be opened after import even if they do not compile yet.
The baseline is not certified as a successful build; apply still rebuilds from the
saved authoring inputs. Placement tabs become available after the first target
apply supplies physical occupancy, without restarting the editor.

```text
project.json    project identity, source recipe and coordinate frame
sources/        pinned source data or imported model
authoring/      layout, vegetation, furniture and traffic inputs
build/          generated model, exports, previews and target-stage artifacts
state/          local targets, locks, fingerprints and receipts
runs/<id>/      input manifests, logs, reports and recovery checkpoints
```

Preserve recipe, sources and authoring. Generated output/local state are ignored
by the project `.gitignore`. CLI results are JSON; execution logs are separate.
Exit codes: 0 success, 1 execution failure, 2 invalid input, 3 prerequisite error,
4 write/revision conflict.

Eixample retains its existing placements. Its imported legacy layout includes
unresolved road/lane bindings and source footprints. Rebuilding reports those
diagnostics and preserves the preceding model; reconcile the annotations before
applying changed layout geometry. Project export preserves them for review.

## Distribute to a Linux CARLA binary

A `.twinproject` carries editable inputs. A cooked content pack carries the saved
Unreal map and referenced assets for a compatible CARLA binary:

```sh
python -m twinmodel project package projects/mycity --target local \
  --name MyCityPack --version 1.0.0 \
  --base /releases/carla-0.10.0-Linux-release-metadata.tar.gz --out dist/linux
```

The target checkout supplies `Util/ContentPacks/carla_pack.py`; its engine performs
a Shipping/Linux DLC cook against the specified release. The command requires
current successful build/apply receipts. To intentionally distribute a previously
saved map while authoring edits remain pending, add `--saved-target`: the report
records the pending stages and does not claim those edits are in the package.
`--dry-run` performs preflight without cooking or writing. `--require-nav` fails
when pedestrian navigation is absent; navigation generation is not performed by
packaging. Use a dedicated pack name and a new version for each distribution.

The command checks OpenDRIVE and traffic JSON, serializes project/target writes,
verifies archive integrity and exact runtime sidecar bytes, and checks that the
saved target did not change during cooking. It publishes the archive plus a JSON
report with SHA-256, base release, target signature, source revision, and feature
availability. Failures retain logs under `runs/package-<id>/`.

The packer places each map's `map_logic.json` beside its own OpenDRIVE in a
map-specific directory. This preserves custom lane/pedestrian phases and avoids
collisions when a pack contains multiple maps. Re-registering a map removes its
obsolete managed traffic sidecars.

On the receiving machine:

```sh
python3 carla_pack.py install MyCityPack-1.0.0-carla-0.10.0-Linux.tar.gz \
  --server /path/to/carla-package
# Start/restart CARLA, then client.load_world('MyCity').
```

The recipient needs the matching base release and a binary built with our native
DigitalTwin extensions; a content-only pack cannot add C++ classes to an older
executable. Validate a release in a packaged server, including map discovery,
OpenDRIVE, traffic phases, vegetation/furniture and a camera capture.
