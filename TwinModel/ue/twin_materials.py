"""Material pools and deterministic variant selection for the twin baker.

Pure Python (no ``unreal`` import) so it is unit-testable and shared by ``bake_level.py``
(bake time) and ``repaint_materials.py`` (repaint an already baked level in place).

Why pools. Until 2026-09-02 the baker instanced exactly one CARLA material per surface kind,
so every road / pavement / kerb / ground tile of a twin used the same texture: the map read as
one endlessly repeated block. Town15 does the opposite -- it mixes four sidewalk materials, two
kerbs and several ground automaterials (survey: ``.omc/research/town15-materials-2026-09-02.md``).
Here every surface key owns a *pool* of CARLA parents; a bake enumerates the pool into
``MI_<Name>_<key>_<i>`` and every mesh picks its member from a hash of
``map name | key | tile`` (buildings: ``map name | key | osm id``).

Selection must be deterministic and stable across rebakes -- never ``random`` and never asset
iteration order -- so the same twin always bakes to the same picture and a rebake is a no-op
for content diffing. The map name is part of the seed so two twins do not land on the same
variant sequence.

Rules the pools obey (see §5 of the survey):
  * full package paths only: ``Ground/MI_LargeLandscape_Grass`` and
    ``Landscapes/MI_LargeLandscape_Grass`` are two different assets with different masters;
  * no ``MI_Road_Asphalt_A`` / ``_B`` / ``_B_Wet``: they import
    ``LevelDesign/RoadPainterAssets/RenderTexture_Town10HD``, a render target only Town10HD
    ever writes, so outside Town10 their wear mask is whatever was last cooked;
  * no ``MI_sidewalk_01`` / ``_04`` / ``_04b``: 4K (72-74 MB) legacy source textures;
  * lane markings never vary -- they have to read uniform;
  * every member sits on a weather-aware master (``M_GeneralMaster``, ``M_RoadMaster``,
    ``M_AutomaterialLandscape_*``, ``M_GenericMaterialMaster``) so rain wets the whole street,
    not half of it. ``M_VertexPaintCB`` (Town15's own ground workhorse) is weather-inert *and*
    needs vertex colours the bake does not write, so it is not in any default pool.
    Eixample has a reviewed grass-only derivative (setup_eixample_grass.py);
    this map override retains the source shader's lack of weather wetness.
"""
import zlib

GM = "/Game/Carla/Static/GenericMaterials"


def _p(rel):
    """``Folder/MI_Foo`` -> the full ``/Game/...MI_Foo.MI_Foo`` object path."""
    return "%s/%s.%s" % (GM, rel, rel.rsplit("/", 1)[-1])


# surface key (manifest ``material``) -> pool of CARLA materials to instance. Index 0 is the
# canonical look (no jitter); a repeated entry is a deliberate second, jittered instance of
# the same parent.
MATERIAL_PARENTS = {
    # all three are RoadPainter-free M_RoadMaster instances sharing T_Asphalt01_*, so the pool
    # costs almost no extra streaming
    "road": [
        _p("Roads/MI_RoadAsphalt_Town15"),   # what every twin used until now
        _p("Roads/MI_RoadAsphalt_Town11"),
        _p("Roads/MI_Road_Rural_A"),         # worn asphalt
        _p("Roads/MI_RoadAsphalt_Town15"),   # same parent, jittered tiling/saturation
    ],
    # Town15's own mix; MI_sidewalk_1_WorldPosition is world-projected (the "World Position"
    # static switch), so it ignores UVs entirely and cannot seam at a tile border
    "sidewalk": [
        _p("Sidewalk/MI_Sidewalk_Community_v2"),   # Town15's most used pavement
        _p("Sidewalk/MI_Sidewalk_Apartment"),      # what every twin used until now
        _p("Sidewalk/MI_sidewalk_1_WorldPosition"),
        _p("Sidewalk/MI_Sidewalk_Residential"),
        _p("Sidewalk/MI_Sidewalk_Skyscrapper"),
        _p("Sidewalk/MI_Sidewalk_13"),
    ],
    # kerb strips: Town15's kerb material first, the two CurbDirty atlases after it
    "curb": [
        _p("Gutters_Curbs/largeM_curb/MI_largeM_curb01"),
        _p("Gutters_Curbs/largeM_curb/MI_largeM_curb01_2"),
        _p("Gutters_Curbs/Curb/MI_CurbDirty01"),
        _p("Gutters_Curbs/Curb/MI_CurbDirty02"),
    ],
    # the raised plates' side walls. Split off ``curb`` (they shared a key until 2026-09-02) so
    # a plate edge reads differently from a real kerb -- and so the risers stay on the
    # CurbDirty atlas the riser UVs in twinmodel/export/ue.py are calibrated against
    # (CURB_TEX_V_TOP/BOTTOM sample the stone band of T_CurbDirty01).
    "riser": [
        _p("Gutters_Curbs/Curb/MI_CurbDirty01"),
        _p("Gutters_Curbs/Curb/MI_CurbDirty02"),
    ],
    # markings must read uniform across the whole map: single-member pools on purpose
    "marking_white": [_p("Roads/MI_Road_Asphalt_B_LaneMarkingWhite")],
    "marking_yellow": [_p("Roads/MI_Road_Asphalt_B_LaneMarkingYellow")],
    # verges: UV-tiled M_GeneralMaster grasses (they keep the metric Scale X/Y fix)
    "grass": [
        _p("Ground/MI_LargeLandscape_Grass"),   # what every twin used until now
        _p("Ground/MI_Grass_Cutted_Yard"),      # mown lawn
        _p("Ground/MI_Grass_Park"),
        _p("Ground/MI_Grass_Park_2"),
    ],
    # the ground slab: world-space slope/height automaterials break a 250 m tile up on their
    # own (noise + VertexNormalWS), which the flat UV-tiled grass never did. Proven on static
    # meshes by Town15 (Landscapes/MI_LargeLandscape_Grass) and Town12 (the Interurban family).
    "ground": [
        _p("Landscapes/MI_LargeLandscape_Grass"),       # 3-material auto blend
        _p("Ground/MI_LargeLandscape_Interurban"),      # 2-material grass/dirt
        _p("Ground/MI_LargeLandscape_Interurban_2"),
        _p("Ground/MI_LargeLandscape_Grass"),           # the old flat grass, kept in the mix
    ],
    # facade slabs (--buildings slabs). MI_Facade05/07 stay out: near-black glossy curtain
    # wall on the big flat twin walls (out/look_eixample/iter4)
    "building": [
        _p("Facade/MI_Facade01"), _p("Facade/MI_Facade03"), _p("Facade/MI_Brick01"),
        _p("Facade/MI_Facade11"), _p("Facade/MI_Facade10"), _p("Facade/MI_Facade12"),
        _p("Facade/MI_Brick05_Opt"),
    ],
}

# roofs when the twin has no aerial photo to texture them with (3DBAG roofs, see TEXTURED_PARENT)
MATERIAL_PARENTS["roof"] = [_p("Facade/MI_Facade05"), _p("Facade/MI_Facade11"), _p("Facade/MI_Facade08")]

# Textured twin surfaces (manifest ``textures``: 3DBAG facade styles and aerial-photo roofs, see
# tools/threedbag_buildings.py). Their MICs sit on MI_Brick01: an M_GeneralMaster instance that maps
# by the mesh UVs ("World Position"/"Triplanar" off, unlike MI_Facade01-12) and does not recolour
# ("Change Color" off -- MI_Facade01/03/11 tint their texture to flat grey with it, which is why
# the facade-pool slabs render as plain grey). The UVs written by the tool are in texture tiles,
# so the scale is 1. Being M_GeneralMaster, they keep the weather wetness.
TEXTURED_PARENT = _p("Facade/MI_Brick01")
TEXTURE_PARAMS = {"base_color": "BaseColor", "normal": "Normal", "orm": "AO / Roughness / Metallic / Emissive",
                  "windows": "Window Lights"}   # windows: the night mask, only on NIGHT_PARENT
TEXTURED_SCALARS = {"Scale X": 1.0, "Scale Y": 1.0}

# --------------------------------------------------------------------------- lit windows at night
#
# CARLA's weather pushes NightFactor (0 by day, 1 by night: a smoothstep of the sun altitude over
# +-carla.Weather.DayNightBlendDeg, 8 degrees by default) into the WeatherMaterialParameters collection
# on every weather change (Weather.cpp, AWeather::ApplyWeatherToSkyActor: "Materials gate night-only
# emissives on it"); CARLA's own M_FakeInterior windows and vehicle/traffic-light materials read it.
# The facade parent below is CARLA's M_GeneralMaster (copied, so MI_Brick01's look and the weather
# wetness stay) plus one emissive term added to its own:
#
#   lit      = saturate((Window Lit Share x NightFactor - mask.G) x 512)     a step on the window's rank
#   emissive += lit x mask.R x Window Light x lerp(Window Warm, Window Cool, mask.B)
#
# with the mask (facades.synth_window_lights) sampled at UV x WINDOW_UV_SCALE. At dusk the share
# grows with NightFactor, so windows come on one by one; by day nothing is lit.
NIGHT_DIR = "/Game/CoupledCarla/Materials"
NIGHT_MASTER = NIGHT_DIR + "/M_TwinFacadeNight.M_TwinFacadeNight"
NIGHT_PARENT = NIGHT_DIR + "/MI_TwinFacadeNight.MI_TwinFacadeNight"   # MI_Brick01's settings on it
NIGHT_NONE_TEXTURE = NIGHT_DIR + "/T_TwinWindowsNone.T_TwinWindowsNone"
NIGHT_SOURCE_MASTER = GM + "/000_Masters/M_GeneralMaster.M_GeneralMaster"
NIGHT_MPC = "/Game/Carla/Blueprints/Weather/Materials/WeatherMaterialParameters.WeatherMaterialParameters"
WINDOW_UV_SCALE = 0.25   # facades.MASK_UV_SCALE: the mask covers 4 x 4 facade texture tiles
WINDOW_TEXTURE_PARAM = "Window Lights"
WINDOW_SHARE_PARAM = "Window Lit Share"
WINDOW_NITS_PARAM = "Window Light"
WINDOW_COLOURS = {"Window Warm": (1.0, 0.70, 0.40), "Window Cool": (0.78, 0.86, 1.0)}

# Share of the windows lit while it is dark, by building use and local hour (0-23). Not measured: a
# plausible Dutch weekday, after household lighting-load and office-occupancy profiles -- homes peak in
# the evening, offices are lit in working hours and empty by 21:00 but for cleaners and stairwells,
# shop windows stay lit after closing (18:00, late opening to 21:00) into the night. coupled-sim (PR
# #142) lights a fixed 38 % of the windows; the evening values here average about that.
WINDOW_LIT_SHARE = {
    #            0     1     2     3     4     5     6     7     8     9    10    11
    "home":     [0.10, 0.06, 0.04, 0.04, 0.04, 0.06, 0.15, 0.30, 0.22, 0.12, 0.10, 0.10,
                 0.12, 0.10, 0.10, 0.12, 0.20, 0.35, 0.45, 0.50, 0.52, 0.50, 0.40, 0.25],
    "office":   [0.03, 0.03, 0.03, 0.03, 0.03, 0.04, 0.08, 0.30, 0.65, 0.75, 0.75, 0.75,
                 0.70, 0.75, 0.75, 0.70, 0.65, 0.50, 0.32, 0.20, 0.12, 0.08, 0.06, 0.04],
    "shop":     [0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.30, 0.40, 0.60, 0.90, 0.90, 0.90,
                 0.90, 0.90, 0.90, 0.90, 0.90, 0.90, 0.75, 0.65, 0.60, 0.50, 0.40, 0.30],
    "industry": [0.06, 0.06, 0.06, 0.06, 0.06, 0.08, 0.20, 0.45, 0.50, 0.50, 0.50, 0.50,
                 0.50, 0.50, 0.50, 0.45, 0.35, 0.25, 0.15, 0.10, 0.08, 0.08, 0.06, 0.06],
}
DEFAULT_WINDOW_HOUR = 20.0
# Luminance of a lit window in cd/m2 (UE emissive with the project's extended luminance range):
# a room seen through glass and curtains, brighter offices, shop windows lit for display.
WINDOW_NITS = {"home": 60.0, "office": 100.0, "shop": 250.0, "industry": 80.0}


def window_lit_share(use, hour=DEFAULT_WINDOW_HOUR):
    """Share of a use's windows lit at a local hour (fractional hours interpolate; unknown uses are
    homes)."""
    prof = WINDOW_LIT_SHARE.get(use, WINDOW_LIT_SHARE["home"])
    h = float(hour) % 24.0
    i = int(h)
    f = h - i
    return round((1.0 - f) * prof[i] + f * prof[(i + 1) % 24], 4)


def window_scalars(use, hour=DEFAULT_WINDOW_HOUR):
    """{scalar parameter: value} a textured facade MIC with a night mask gets."""
    return {WINDOW_SHARE_PARAM: window_lit_share(use, hour),
            WINDOW_NITS_PARAM: WINDOW_NITS.get(use, WINDOW_NITS["home"])}


# RoadPainter-bound instances: they must never appear in a pool (see the module docstring)
FORBIDDEN = ("MI_Road_Asphalt_A", "MI_Road_Asphalt_B", "MI_Road_Asphalt_B_Wet",
             "MI_sidewalk_01", "MI_sidewalk_04", "MI_sidewalk_04b")

# asset kind -> surface key, mirroring twinmodel.export.ue.KIND_MATERIAL. The repaint path
# reads it so a level baked before the riser split still gets the new riser pool.
KIND_KEY = {
    "drivable": "road", "parking": "road", "crossing": "road",
    "sidewalk": "sidewalk", "island": "sidewalk", "median": "sidewalk",
    "verge": "grass", "ground": "ground", "groundplane": "ground", "boundary": "ground",
    "curb": "curb", "riser": "riser",
    "marking_white": "marking_white", "marking_yellow": "marking_yellow",
    "building": "building",
}

# The bake writes metric planar UVs (1 uv unit = 1 m) while the CARLA masters are tuned for
# meshes whose UVs are roughly UE-cm sized, so the tiling parameters need ~100x the parent's
# value for the same texel density (MI_LargeLandscape_Grass ships Scale X/Y 0.005 -> 0.5,
# MI_RoadAsphalt_Town15 ships Base Scale 0.05/0.06 -> 5.0/6.0). The baker reads the parent's
# own value and multiplies, so a pool member that ships a different density keeps its own
# relative look; FALLBACK is used when the chain does not set the parameter at all.
METRIC_UV_FACTOR = 100.0
METRIC_UV_PARAMS = {
    "ground": ("Scale X", "Scale Y"),
    "grass": ("Scale X", "Scale Y"),
    "road": ("Base Scale", "Base Scale 2"),
}
METRIC_UV_FALLBACK = {"Scale X": 0.5, "Scale Y": 0.5, "Base Scale": 5.0, "Base Scale 2": 6.0}
# Sidewalks, kerbs, risers and facades deliberately keep their shipped tiling: with metric UVs
# those textures already repeat about every metre, and scaling them down made walls featureless
# (out/look_eixample/iter3). The kerb/riser UVs are hand-mapped onto a texture band anyway.

# Second variability axis: a pool member at index > 0 gets its tiling nudged, keyed by the
# master it ultimately sits on. Amplitudes are relative (0.15 = +-15 %).
#
# The jitter is applied ONLY to the parameters the baker already computes itself -- the
# metric-UV rescale above. Reading some other shipped parameter and writing it back scaled
# looked tempting but is not safe: what comes back is whichever value the override chain or
# the master default happens to hold, and writing that as an explicit override pins a value
# the material may have been getting from somewhere else. The first attempt did exactly that
# and moved the pavement's texel density on EixampleDemo. So: perturb what we own, leave
# everything else at the parent's shipped state, and let the pool carry the variety.
#
# NEVER jitter Puddles* / Raindrops* / Wetness either: those are the weather system's knobs
# (Weather.cpp pushes them through the WeatherMaterialParameters MPC).
JITTER_PARAMS = {
    "M_GeneralMaster": {"Scale X": 0.15, "Scale Y": 0.15},
    "M_RoadMaster": {"Base Scale": 0.10, "Base Scale 2": 0.10},
    "M_AutomaterialLandscape_2MatMaster": {"Scale X": 0.15, "Scale Y": 0.15},
    "M_AutomaterialLandscape_3MatMaster": {"Scale X": 0.15, "Scale Y": 0.15},
    "M_GenericMaterialMaster": {},
}


# --------------------------------------------------------------------------- selection

# Reviewed map-specific parents override a pool slot without changing tile selection.
MAP_PARENT_OVERRIDES = {
    ("EixampleDemo", "ground", index):
        "/Game/Carla/Maps/Twins/EixampleDemo/Materials/MI_TwinUrbanLawn_Dense"
    for index in range(4)
}


def parent_for(name, key, index, default):
    return MAP_PARENT_OVERRIDES.get((name, key, int(index)), default)


def pool(key):
    """The pool for a surface key (empty list for an unknown key)."""
    return list(MATERIAL_PARENTS.get(key, ()))


def mic_name(name, key, index):
    """Asset name of pool member ``index`` of ``key`` for map ``name``."""
    return "MI_%s_%s_%d" % (name, key, int(index))


def _crc(*parts):
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8"))


def variant_index(name, key, tile, n):
    """Deterministic pool index for a tile: ``crc32(name|key|tile_x|tile_y) % n``.

    ``tile`` is the manifest's ``[i, j]`` World Partition cell (tile_m = 250 m by default), so
    the variation happens at district scale -- what "different neighbourhoods" means -- rather
    than breaking up a single street.
    """
    if n <= 1:
        return 0
    tx, ty = (int(tile[0]), int(tile[1])) if tile is not None else (0, 0)
    return _crc(name, key, tx, ty) % int(n)


def building_variant_index(name, key, building_id, n):
    """Deterministic pool index for a building: ``crc32(name|key|osm id) % n``."""
    if n <= 1:
        return 0
    return _crc(name, key, building_id) % int(n)


def jitter_factors(name, key, index, master):
    """{parameter: multiplier} for pool member ``index``, from the same deterministic hash.

    Index 0 is the canonical look and is never jittered, so a one-member pool (and the first
    member of every pool) renders exactly as it did before pools existed.
    """
    if int(index) <= 0:
        return {}
    params = JITTER_PARAMS.get(master, {})
    seed = "%s|%s|%d" % (name, key, int(index))
    out = {}
    for pname, amp in sorted(params.items()):
        u = (_crc(seed, pname) % 10000) / 10000.0   # [0, 1)
        out[pname] = 1.0 + float(amp) * (2.0 * u - 1.0)
    return out


def master_name(path):
    """Short master name from a ``/Game/.../M_Foo.M_Foo`` path (for JITTER_PARAMS lookup)."""
    return path.rsplit("/", 1)[-1].split(".")[0]
