# Roadside pole placement

The original pole rule used nominal lane widths plus a lateral offset. It ran
before the finished sidewalk/junction geometry existed. Chamfered corners,
widened junctions and refined outlines could therefore leave poles on asphalt.
`place_traffic_signs.py` also added a 25 cm model-convention offset after placement.

`tools/xodr_signals.py` now requires the final baked `.twin` surface model. It
finds a same-named sibling beside the OpenDRIVE file, or accepts `--twin` explicitly:

```sh
python tools/xodr_signals.py /path/to/EixampleDemo.xodr tl_signals.json \
  --twin out/v10_eixample/eixample.twin
python tools/xodr_signals.py /path/to/EixampleDemo.xodr sign_signals.json \
  --twin out/v10_eixample/eixample.twin --types 205 206 274
```

Use the model whose surfaces were baked into the level, not a newly generated
model with different geometry. The `refresh-signals` workflow uses its original
build directory for this reason. Unresolved placement stops generation before
writing the JSON; refresh restores its previous OpenDRIVE on failure.

Each JSON record retains its original OpenDRIVE transform, lane validities and
control identity. A separate `placement` contains the physical support location
in CARLA metres. Both Unreal placers require a valid placement and use it without
adding the old forward offset. For an already baked map, run
`ue/relocate_signal_poles.py --name NAME --signals all_signals.json --report report.json`
inside the editor commandlet. Generate `all_signals.json` with an empty `--types`
filter. This moves existing actors and preserves their meshes and bindings; absent
actors are reported, not generated.

Rules in `twinmodel/pole_placement.py`:

- Use existing sidewalk, island, median or verge surfaces on the road's layer.
- Exclude carriageway, parking and crossing surfaces, with 0.45 m clearance.
- Inset other support boundaries by 0.20 m; leave 0.50 m from buildings.
- Remain on the original road side, within 12 m of the anchor and 8 m along the
  reference road from its control position.
- Reserve 0.60 m between distinct supports. Solve all signs and lights together,
  in stable signal-ID order, before filtering for the requested output.
- Sample the final model's elevation plus the supporting surface height.
- Report unresolved geometry instead of placing on a road or generic ground slab.

These are configurable geometric design defaults, not regional legal clearance
rules. This pass validates support anchors; large multi-pole/gantry rigs require
footprint-aware validation before using them with these defaults. It does not
infer pedestrian access routes, optimize sign visibility, or correct signal
head orientation. Those remain separate placement concerns.

Runtime dependency: CARLA `TrafficLightManager.cpp` must adopt `AGeoTrafficSign`
actors by their nonempty `SignalId`, before its legacy 5 m proximity fallback.
Identified signs belonging to another signal must be excluded from that fallback.
Without this change, moving a support beyond 5 m can spawn a duplicate at the old
OpenDRIVE point. Traffic-light rigs already bind through their `TL_<id>` tags and
`map_logic.json`.

## EixampleDemo validation, 2026-09-10

Saved and reloaded all 200 supports (75 signs, 98 pedestrian poles, 27 vehicle
traffic lights). Before: 31 actual actor bases overlapped the finished road
surface. After: zero road overlaps, zero unsupported bases; minimum road clearance
0.44949 m (polygon/float tolerance around 0.45 m). All 27 vehicle lights retain
unique signal IDs and nonempty affected-lane lists. Runtime counts: 67 speed-limit
signs, 4 yield signs, 4 priority props, 27 vehicle lights; no duplicate signs.
OpenDRIVE and controller data are unchanged. The initial model-only audit counted
37 overlaps; actual actor anchors differ from that older model's convenience
signal positions, so the deployed-actor audit is the relevant before/after check.

Native Carla module build passed. Placement and refresh regression tests passed.
Visual comparison and runtime reports are in the workspace
`.omc/eixample-pole-placement-2026-09-10/`.
