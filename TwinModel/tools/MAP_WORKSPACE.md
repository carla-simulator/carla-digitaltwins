# Digital twin workspace

For durable projects, use `python -m twinmodel project edit PROJECT --port 8791`.
This launches the repository's unified editor with project authoring inputs,
build/apply controls, export, revision checks and shared CLI locking. See
[project CLI usage](../docs/PROJECT_CLI.md). The direct launcher below remains
available for legacy build-directory workspaces.

`tools/twin_editor.py` is the single web entry point for layout, vegetation and
furniture authoring. It uses one Leaflet map and the existing canonical layout
editor; switching tabs preserves the map view. Visibility toggles let all three
layers be reviewed together.

From `TwinModel`, launch Eixample with:

```sh
python tools/twin_editor.py out/v10_eixample eixample \
  --vegetation-output out/vegetation_eixample \
  --furniture-output out/furniture_eixample \
  --poles out/furniture_eixample/poles.json \
  --level EixampleDemo --region eixample \
  --project /absolute/path/to/CarlaUnreal.uproject \
  --engine /absolute/path/to/UnrealEditor-Cmd
```

Open **http://localhost:8791**. Set `DLSS_SDK` in the environment if required by
the local engine. Engine/project/level are optional; without them planning works
and Unreal bake buttons are disabled.

Existing `out/vegetation_<name>` and `out/furniture_<name>` plans are discovered
by default. Physical pole positions are discovered in the furniture directory's
`poles.json`, or supplied explicitly. Missing inputs are explained in the relevant
tab without preventing layout editing. Explicit output paths can initialize new
plans using the existing planners. Opening existing plans never regenerates them;
matching ground-validated furniture previews remain visible.

- **Layout:** existing Browse/Edit gestures, exact geometry annotations, undo,
  object inspection, visibility/ordering, and Save.
- **Trees & plants:** mapped trees, procedural rows, species presets, tree surrounds,
  planting zones, exclusions, placement layers, move/disable/reset, and undo.
- **Furniture:** grouped footprints/access areas, OSM anchor import, stop poles,
  shelters, banners, source-to-placement offsets, move/disable/reset, and undo.
- Both placement tabs provide searchable objects, rejected-candidate review, full
  configuration editing/export, and the existing Unreal bake commands.

Placement edits are validated and saved immediately. Invalid configurations leave
the saved config/plan unchanged. Undo histories are separate for vegetation and
furniture and last for the page session; layout keeps its existing history.

`map_workspace.py` coordinates the existing planners and bakers in one process.
`editor-planning-inputs.json` records model/pole/vegetation dependencies so stale
plans remain flagged across server restarts. Tree changes invalidate furniture;
regenerate furniture to reserve the current tree positions and surrounds. A model
change invalidates both. Changed layout annotations require **Rebuild planning
model**, followed by vegetation then furniture regeneration. The rebuild uses the
canonical build command with saved annotations, not a second geometry editor.

A planning-model rebuild does **not** bake layout geometry into Unreal. Use the
framework's map build/bake workflow for that, and refresh the physical pole export
if traffic supports move. Placement bakes use the existing ground checks against
the saved Unreal level. Reload CARLA after baking to display saved changes.

Only one Unreal placement bake may run at a time; map/plan mutations are blocked
until it finishes. Run the unified server as the owner of these output directories;
stop legacy standalone vegetation/furniture servers before editing shared files.
Legacy launchers remain available for isolated workflows, using the same planners.

Local HTTP API: `GET /api/planning`, `POST /api/planning/{vegetation,furniture}/preview`
(full configuration body), and `POST /api/planning/{vegetation,furniture}/bake`.
Canonical layout endpoints retain their paths. Browser mutations reject foreign
origins and oversized requests. No proxy servers or embedded frames are involved.
