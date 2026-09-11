# Traffic-light assembly selection

TwinModel already invokes CarlaTools' native Traffic Light Tool, builds its JSON
presets and bakes the result. The European selector previously returned `eu_pole`
for every through signal, irrespective of the number of controlled lanes.

Pass the **preset directory** to `place_traffic_lights.py --rig ue/rigs --style eu`.
The normal refresh workflow already does this. Passing a single preset explicitly
continues to use that preset. `--rig-map` overrides individual signals.

European through-signal defaults:

- One lane: `eu_pole`, one head.
- Two or more lanes with usable lane geometry: `eu_mast_2head` is a template;
  the fitter generates one overhead head per controlled lane, plus a low repeater.
  Every head keeps the same original signal phase.
- Unsuitable/missing mast geometry: a repeater pole, with the geometric rejection
  recorded in the placement report. An explicitly requested unsuitable mast fails.

`tools/xodr_signals.py` exports controlled driving-lane centers and widths.
`traffic_rigs.py` projects their positions into the validated support's local
frame, places one overhead head at every controlled lane center, and sizes the
arm with a 0.5 m end allowance. The support stays in its approved sidewalk position.
The generator uses existing European black pole and lamp assets from the
TrafficLights2025 tables. Native mesh bounds confirm the horizontal section runs
along +X; translating its origin supports either road side without turning heads.

Mast limits: 20 m reach, 12 m longitudinal lane/support offset, all controlled
lanes on one side of the support, and at least 5.5 m between the lowest lamp and
the highest controlled lane. These are geometric defaults, not a reconstruction
of surveyed Barcelona signal assemblies. Tree/building occlusion and structural
engineering require additional review. Pedestrian and arrow presets retain their
existing selection; this change does not add pedestrian phase control.

All presets and geometry resolve before actor deletion. Generated mesh counts
must match the requested assets. A partial build prevents saving the level.
Signal IDs, controller membership, timing and head bindings remain explicit in
`map_logic.json`; additional heads never introduce independent signal movements.

## Eixample validation, 2026-09-10 (coverage revision)

The first mast fitter mistakenly retained a fixed two-head template. It now
clones a head for every controlled lane, including five-lane approaches; partial
or duplicate lane geometry fails validation. The 20 m art-layout limit accommodates
the widest Eixample approaches (18.77 m maximum generated reach). It is not a
structural design certification.

`EU_DENSE.junction.infer_wide_crossing_signals` enables a source-data fallback in
`twinmodel/signal_coverage.py`. It upgrades an intersection only when every
incoming road has a synthetic fallback control, every approach has at least two
driving lanes, and approach directions cross. Explicit source controls, narrow
streets, continuations and gores remain unchanged. Each inferred incoming approach
gets a separate controller/stage. IDs and validated support anchors are retained;
obsolete synthetic signs are removed when replaced with lights.

This covers Aragó/Pau Claris (j7), Aragó/Roger de Llúria (j10), and
Pau Claris/Diputació (j15). These are inferred simulation controls, not surveyed
signal plans. Six signals and six controllers are added; the original 27 vehicle
signal identities and timings are preserved. Road geometry and lane links are
unchanged. Source-model tags and the coverage report record the inference.

- 33 vehicle signals: 11 simple poles and 22 masts, with 106 vehicle heads.
- Each controlled lane has a head; multilane assemblies also have a low repeater.
- 98 pedestrian props retained. No new pedestrian phase behaviour is inferred.
- All 106 heads passed forced red/green lamp checks; normal cycling was restored.
- All 33 API lights have affected lanes. The three new junctions were observed
  through a full cycle: every approach reached red and green, with no conflicting
  green pair across 66 samples.
- 131 light/sign actors replaced; all other 934 external actor packages remain
  byte-identical, including the 483 vegetation/tree-surround instances.
- Tests cover 2/3/5-lane head placement, source-control preservation, narrow roads,
  continuation/merge exclusions, idempotence and independent inferred stages.
- Workspace evidence: `.omc/signal-coverage-2026-09-10/`.

## Independent lane phases (2026-09-11)

EU_DENSE now enables `junction.lane_signal_phases`. `lane_signals.py` splits each
vehicle signal's validity range into lane-scoped signals and gives each its own
controller. The first lane retains the old ID; additional IDs have a stable
`_lane_mN`/`_lane_pN` suffix. Each junction runs one vehicle lane green at a time.
This conservative generated schedule is configurable through CARLA's existing
traffic-light API; it is not a surveyed Barcelona phase plan. Disabling the profile
option for a fresh build retains approach-level phases.

`rig_anchor` metadata keeps these logical signals on their original physical
assembly. OpenDRIVE exports it as signal `userData`; signal refresh preserves it.
The landmark exporter places only the anchor, groups its child lanes, and passes
per-lane `signal_id` values into the rig fitter. Every overhead head binds to its
own signal. The low repeater shares the anchor lane's phase. Missing bindings fail
before level mutation instead of silently reverting to synchronized heads.

The existing CARLA extension from c2f54185b splits the baked assembly into one
runtime light per signal and attaches only that signal's head meshes. No native
server extension or API change is required.

Eixample now has **84 runtime vehicle signals on 33 physical vehicle assemblies**,
with the same 106 heads and 98 pedestrian props. All support locations are
unchanged. Every API light affects exactly its specified lane. A full accelerated
cycle exercised all 84 signals with no simultaneous green within a junction;
configured timing was restored afterward. Traffic Manager drove a vehicle through
the green lane while stopping another in the adjacent red lane. An isolated middle-lane green test
verified exactly one green head while the other 105 heads were red.

Evidence: `.omc/lane-phases-2026-09-11/` in the workspace. A fresh fixture build
and signal-refresh graft are covered by `tests/test_lane_signals.py`; legacy
approach-stage tests explicitly disable lane phases to retain coverage of that
separate mode.


### Pedestrian connection

`connect_pedestrian_signals` assigns paired pedestrian heads to one exclusive
`ped_<junction>` controller per signalized junction. It also recovers missing
controller assignments from the head's junction metadata. Vehicle lane phases
retain their identities. `place_traffic_lights.py` now retains pedestrian entries
in map_logic.json and validates that their controllers contain no vehicle signals.

CARLA's MapLogicParser adopts these baked heads as API traffic lights, using
OpenDRIVE type 1000002 to select a two-state green/red cycle and omit vehicle
trigger boxes. WALK lasts 10 seconds. After WALK, all lights remain red for
`ceil(longest crossing width / 1.2) + 2` seconds before vehicles resume. This is a
conservative exclusive pedestrian phase, so vehicle waits increase. These timings
are simulation defaults, not a reconstruction of Barcelona's actual signal plans.

Eixample has 98 connected pedestrian heads across 10 pedestrian stages, plus the
existing 84 vehicle signals. Clients can distinguish the heads by looking up
their OpenDRIVE landmark type (1000002); the usual get/set state and group APIs
work. Standard walker AI does not obey these lights yet.

Runtime verification exercised all 182 lights through complete accelerated cycles,
checked paired-head synchronization and absence of pedestrian/vehicle green
conflicts, then restored normal timing. Every pedestrian actor has no vehicle
stop waypoints. Tests cover exclusive grouping, missing assignments, idempotence,
clearance timing and unchanged vehicle timing.
