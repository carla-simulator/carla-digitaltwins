# DigitalTwin project CLI proposal

Status: implemented and verified against a saved Unreal QA map, runtime reload, portable project exchange, and 187 regression tests. See [current commands](PROJECT_CLI.md).
Extend the existing `twinmodel` entry point.
Existing commands remain compatible while their implementations become stage adapters.

## User workflows

Create from OpenStreetMap bounds (explicit south, west, north, east ordering):

```sh
twinmodel project create maps/eixample --name eixample \
  --bbox 41.38 2.14 41.40 2.18 --profile auto
twinmodel project build maps/eixample
twinmodel project apply maps/eixample --target local
twinmodel project edit maps/eixample
```

Coordinates above are illustrative, not the existing Eixample boundary.
`create` writes project inputs; `build` acquires missing source snapshots and
generates the model and exports without starting Unreal. `apply` completes all
required stages against a configured Unreal target. `edit` opens the unified UI.

After web edits, use the same commands:

```sh
twinmodel project status maps/eixample
twinmodel project apply maps/eixample --target local --dry-run
twinmodel project apply maps/eixample --target local
```

`apply` detects changes and runs only affected stages plus their prerequisites.
It never implicitly downloads newer OSM data or reloads a running CARLA server.

Import an existing model or a complete project exported by the editor:

```sh
twinmodel project create maps/imported --from-twin /data/reviewed.twin
twinmodel project import /data/reviewed.twinproject --into maps/reviewed
twinmodel project apply maps/reviewed --target local
```

A `.twin` alone is a model snapshot, not the complete authoring project. Imported
models use snapshot mode: derive exports and placements directly from that model;
do not rerun the OSM compiler. Layout recompilation requires the original source
snapshot and build recipe. Missing provenance is reported as a capability limit,
not silently replaced with newly downloaded OSM. Model replacement is explicit:
`project model import <project> <new.twin>`; check frame/schema compatibility and
flag unresolved authoring references before accepting it as the new base.

## Durable project contract

```text
maps/eixample/
  project.json                 # versioned recipe, source mode, frame, stage settings
  sources/                     # pinned OSM/DEM/imagery or imported .twin snapshot
  authoring/
    layout.json                # canonical correction operations
    vegetation.json            # seeds, zones, exclusions, overrides
    furniture.json             # seeds, anchors, exclusions, overrides
    traffic.json               # supported traffic configuration/overrides
  build/                       # disposable models, exports, candidate plans
  state/                       # stage fingerprints and target deployment receipts
  runs/<run-id>/               # immutable input manifest, logs and reports
```

`project.json` records schema version, project ID, source snapshot hashes,
bounding box where applicable, coordinate origin/CRS, build profile, explicit
seeds, catalogue version, enabled stages and logical target map/region identity.
Paths are project-relative. Large source blobs may be content-addressed, but
portable exports must include them or explicitly declare unresolved dependencies.

Machine-local target configuration resolves engine executable, `.uproject`, full
Unreal level asset path and optional CARLA endpoint. It is separate from portable
authoring inputs; no baked-in Eixample paths, demo services or port numbers.
`project target set <project> local --engine ... --uproject ... --level ...`
registers a target without modifying Unreal.

The web UI reads/writes these authoring files through the same project service
used by the CLI. Layout Save remains explicit; successful placement edits save
their configurations immediately. Generated plans are caches, not author intent.
Saving and building use revision checks so concurrent browser/CLI writes cannot
overwrite newer edits. Runs use an immutable saved revision; unsaved browser edits
are excluded and shown clearly in the UI.

`project export <project> --out <name>.twinproject` creates a versioned archive of
the manifest, authoring files, source inputs and optional model snapshot. Exclude
machine paths and Unreal binaries. Import validates schemas, hashes, archive paths,
coordinate frames and IDs. Do not repurpose `.twin` to mean this different format.

## Commands

| Command under `twinmodel project` | Contract |
| --- | --- |
| `create PATH --bbox S W N E` / `--from-twin PATH` | Create durable project inputs |
| `adopt PATH --build-dir DIR --name NAME --vegetation DIR --furniture DIR` | Copy existing authoring/model inputs into a project without regeneration |
| `edit PATH` | Launch unified editor using project inputs |
| `status PATH [--target NAME] [--json]` | Show current, stale, missing, failed stages and reasons |
| `build PATH [--through model\|export]` | Build portable artifacts; no Unreal mutation |
| `apply PATH --target NAME [--only traffic\|vegetation\|furniture]` | Run dependency closure and save affected Unreal output |
| `run PATH --stage STAGE [--target NAME]` | Run one stage; fail with prerequisite list if inputs are stale |
| `validate PATH [--target NAME]` | Validate available artifacts; target checks only when requested |
| `source refresh PATH` | Explicitly fetch a new source revision and invalidate dependent stages |
| `export PATH --out FILE` / `import FILE --into PATH` | Transfer complete authoring projects |
| `runtime reload PATH --target NAME` | Explicitly load saved map in CARLA; separate from apply |

`build`, `apply` and `run` support `--dry-run`, `--json` and `--resume RUN_ID`.
Dry-run performs read-only planning and reports missing information; it does not
fetch data, invoke a bake, or claim ground validation that requires Unreal.
`--only` requests a subsystem, not permission to skip dependencies. If stale
geometry forces recreation of the level, expand to all enabled downstream stages
and explain why. `run --stage` provides strict manual stage control instead.

## Stage graph

```text
source snapshot + layout + build settings
                  |
                model -> validate.model -> export
                                             |
                                        unreal.geometry
                                             |
                                        unreal.traffic
                                             |
                                      occupancy.export
                                             |
                                      vegetation.plan
                                             |
                                      vegetation.bake
                                             |
                                       furniture.plan
                                             |
                                       furniture.bake
                                             |
                                       validate.target
```

Placement planners also consume the current model, authoring configuration and
catalogue. Furniture consumes ground-validated vegetation output with tree bases,
not merely unvalidated candidate positions. Physical occupancy comes from the
current target level, with layer assignments validated. Offline previews may use
cached occupancy, but label stale/absent target inputs and cannot certify a bake.
Disabled stages provide explicit empty outputs where appropriate; they must not
erase or ignore existing target obstacles silently.

Invalidation examples:

- Furniture edit: furniture plan, bake and target validation.
- Vegetation edit: vegetation plan/bake, then furniture plan/bake and validation.
- Traffic support movement: traffic bake, occupancy and both placement systems.
- Layout/source change: model and every dependent stage.
- Geometry recreation: invalidate all previous target placement receipts even if
  the candidate placement JSON happens to have identical bytes.
- Catalogue/material/rule change: invalidate affected planners or bakers through
  explicit asset and implementation version dependencies.

The existing geometry baker recreates the level. Initial implementation must
therefore treat geometry apply as full regeneration of framework-owned content,
and replay all enabled downstream stages. It must not advertise preservation of
arbitrary hand-authored actors. Keep those in a separate persistent author layer;
until that exists, refuse unmanaged target content that would be lost and report
the affected ownership boundary. Subsystem-only bakes retain regional ownership.

## Execution and failure semantics

Each adapter declares inputs, outputs, ownership, fingerprint and validation.
Fingerprints include input content, relevant recipe, implementation version,
catalogue/assets and target identity for Unreal stages. A successful process exit
alone is insufficient: verify expected outputs and report hashes. Track target
generation and saved package identity to detect external changes; when uncertain,
mark target state unknown and inspect it rather than trusting an old receipt.

Acquire project revision/run locks and a target writer lock shared by web/CLI.
One Unreal writer per target; no concurrent plan changes within a run. Publish
portable outputs atomically. Record per-stage success/failure and keep logs.
Resume only reuses completed stages whose inputs and target generation still
match. Failed downstream work leaves the run explicitly incomplete; never report
the whole map as updated. Unreal package writes are not a multi-file transaction:
initial release must report partial target updates honestly, retain recoverable
backups/checkpoints and avoid automatic runtime reload. Transactional staging of
complete replacement levels is a later feature, not an assumed guarantee.

JSON output includes schema version, run ID, authoring revision, stage states,
invalidation reasons, artifact paths and errors. Exit codes: 0 success, 1 execution
failure, 2 invalid input, 3 missing/stale prerequisite, 4 lock/revision conflict.
Rejected placement candidates are reported separately from execution failure;
existing all-rejected replacement protection remains in force.

## Implementation plan and acceptance

1. Add project schema, loader, local targets and adoption. Adopt Eixample without
   changing any existing plan, correction or saved map. Move durable settings out
   of `out/`; preserve original files and verify copied bytes.
2. Add stage registry, fingerprints, status/dry-run and adapters around existing
   `build`, `bake-export`, Unreal geometry/traffic scripts and placement planners.
   Extract reusable logic rather than calling machine-specific demo orchestration
   in `refresh-signals`; preserve its topology compatibility checks where relevant.
3. Implement apply/resume, occupancy and validated vegetation handoffs, ownership
   checks and target receipts. Do not duplicate placement algorithms.
4. Bind the unified editor to this service; add a reviewable Apply panel showing
   stage scope, progress, reports and explicit runtime reload.
5. Add portable export/import and verify on a second map with a different layout.

Acceptance cases: fresh bounds-to-level build; second unchanged apply does no
work; furniture-only edit avoids geometry rebuild; tree edit updates furniture;
layout rebuild restores every enabled placement stage; interrupted bake resumes
without accepting stale outputs; imported model builds without OSM acquisition;
web and CLI produce identical plans from identical saved inputs; moved origin or
missing override IDs raises a reviewable conflict instead of dropping user edits;
full project export/import retains edits and deterministic seeds on another machine.
