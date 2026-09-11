"""Translate a configured Unreal asset path to CARLA's runtime map identifier."""
from pathlib import PurePosixPath


def reload_map(client, level):
    name = PurePosixPath(level).name
    available = client.get_available_maps()
    matches = [m for m in available if PurePosixPath(m).name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError('Runtime map name is missing or ambiguous: '+name)
    # CARLA UE 5.8 FindMapPath matches the basename, even though editor tools use
    # the full package path. Sending /Game/... causes an invalid prefixed path.
    world = client.load_world(name)
    loaded = world.get_map().name
    normalized = loaded.lstrip('/').removeprefix('Game/')
    expected = level.removeprefix('/Game/')
    if normalized != name and normalized != expected:
        raise RuntimeError('Runtime loaded an unexpected map: '+loaded)
    return {'loaded': loaded, 'target_level': level, 'episode': world.id}
