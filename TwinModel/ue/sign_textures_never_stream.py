"""Mark the sign atlases (and the shared ORM) as never-streamed.

The atlas selector material samples a 4x4 cell of a 4096^2 atlas through a texture *parameter*; the
texture streamer has no streaming data for the per-sign material instances (the master's data names
the default atlas only) and falls back to "the whole texture covers the plate", so a 60 cm plate
seen from 8 m keeps the atlas at 256^2 -> a 64 px cell. `ListTextures` showed
"4096x4096 ..., 256x256 (45 KB) ... T_VC_ProhibitorySignAtlas_01" while 67 plates were on screen.
Never-streaming the ~11 MB atlases that a level actually references is cheap and fixes every user of
the material (placer, museum, editor tool).

    UnrealEditor-Cmd <uproject> -run=pythonscript -script="/abs/ue/sign_textures_never_stream.py [--revert]"
"""
import sys

import unreal

ROOT = "/CarlaDigitalTwinsTool/Carla/Static/Signs/DataAssetsTextures"
EXTRA = [ROOT + "/Textures/T_signs_orm"]


def main(argv):
    revert = "--revert" in argv
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous([ROOT], True)
    paths = [p.split(".")[0] for p in unreal.EditorAssetLibrary.list_assets(ROOT + "/SignsAtlases", recursive=True, include_folder=False)]
    paths = sorted(set(paths) | set(EXTRA))
    changed, same, missing = [], [], []
    for p in paths:
        tex = unreal.EditorAssetLibrary.load_asset(p)
        if tex is None or not isinstance(tex, unreal.Texture2D):
            missing.append(p)
            continue
        want = not revert
        before = bool(tex.get_editor_property("never_stream"))
        if before == want:
            same.append(p)
            continue
        tex.set_editor_property("never_stream", want)
        ok = unreal.EditorAssetLibrary.save_loaded_asset(tex, only_if_is_dirty=False)
        changed.append((p, ok))
        unreal.log("[never_stream] %s never_stream %s -> %s saved=%s" % (p, before, want, ok))
    unreal.log("[never_stream] done: %d changed, %d already, %d missing" % (len(changed), len(same), len(missing)))
    for p in missing:
        unreal.log_warning("[never_stream] missing " + p)
    return 1 if missing or any(not ok for _, ok in changed) else 0


if __name__ == "__main__":
    rc = main(sys.argv[1:])
    if rc:
        unreal.log_error("[never_stream] exit %d" % rc)
