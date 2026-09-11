from types import SimpleNamespace
import pytest
from twinmodel.project_runtime import reload_map


def test_runtime_uses_basename_and_verifies_loaded_asset():
    called = []
    world = SimpleNamespace(id=9, get_map=lambda:SimpleNamespace(name='Carla/Maps/Twins/Test/Test'))
    client = SimpleNamespace(get_available_maps=lambda:['Test'],
        load_world=lambda name:(called.append(name) or world))
    assert reload_map(client,'/Game/Carla/Maps/Twins/Test/Test')['episode'] == 9
    assert called == ['Test']


def test_runtime_rejects_ambiguous_map_before_loading():
    client = SimpleNamespace(get_available_maps=lambda:['/Game/A/Test','/Game/B/Test'],
                             load_world=lambda name:pytest.fail('Must not load ambiguous map'))
    with pytest.raises(ValueError, match='ambiguous'):
        reload_map(client,'/Game/A/Test')
