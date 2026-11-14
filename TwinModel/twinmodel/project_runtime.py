"""Translate a configured Unreal asset path to CARLA's runtime map identifier."""
from pathlib import PurePosixPath


def reload_map(client, level):
    if not isinstance(level, str) or not level.startswith('/Game/'):
        raise ValueError('Target level must be a /Game/ package path')
    name = PurePosixPath(level).name
    if not name:
        raise ValueError('Target level has an empty map name')
    expected = level.removeprefix('/Game/')
    available = client.get_available_maps()
    matches = [m for m in available if PurePosixPath(m).name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError('Runtime map name is missing or ambiguous: '+name)
    cand_norm = matches[0].lstrip('/').removeprefix('Game/')
    if '/' in cand_norm and cand_norm.casefold() != expected.casefold():
        raise ValueError(f'Available runtime map {matches[0]} does not match configured {level}')
    # CARLA UE 5.8 FindMapPath matches the basename, even though editor tools use
    # the full package path. Sending /Game/... causes an invalid prefixed path.
    world = client.load_world(name)
    loaded = world.get_map().name
    normalized = loaded.lstrip('/').removeprefix('Game/')
    if normalized != name and normalized != expected:
        raise RuntimeError('Runtime loaded an unexpected map: '+loaded)
    return {'loaded': loaded, 'target_level': level, 'episode': world.id}
