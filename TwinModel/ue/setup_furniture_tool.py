"""Create/update the small PCG graph used by ProceduralFurnitureTool.

Run through UnrealEditor-Cmd -run=pythonscript -script=<this file>.
Existing legacy forest/street graphs and mesh assets are not edited.
"""
import unreal
import json
from pathlib import Path

# Use the same reviewed sign catalogue as the framework's road signs. The
# legacy SM_BusStop01/atlas pairing renders a blank plate in the current content.
PANEL = '/Game/Carla/Static/TrafficSign/TwinFurniture/SM_BusStopMarker'
SOURCE = '/CarlaDigitalTwinsTool/Carla/Static/Signs/SignShapes/VertShapes/SM_VertRect_02'
MATERIAL = '/CarlaDigitalTwinsTool/Carla/Blueprints/LevelDesign/Signs/Catalog/VC/Guide/Materials/MI_bus_stop_E13'
unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(['/CarlaDigitalTwinsTool'], True)
panel = unreal.load_asset(PANEL)
if panel is None:
    panel = unreal.EditorAssetLibrary.duplicate_asset(SOURCE, PANEL)
material = unreal.load_asset(MATERIAL)
if not panel or not material:
    raise RuntimeError('Missing framework bus-stop sign catalogue assets')
panel.set_material(0, material)
if not unreal.EditorAssetLibrary.save_loaded_asset(panel):
    raise RuntimeError('Could not save bus-stop sign mesh')

# PCG uses ISMs. Some sign/pole materials were authored only for individual
# StaticMeshComponents; enable and save their UE 5.8 instance usage explicitly.
# Skip materials already compatible so unrelated shader settings remain intact.
catalog_path = Path(__file__).resolve().parents[1] / 'twinmodel/data/furniture/catalog.json'
usage = unreal.MaterialUsage.MATUSAGE_INSTANCED_STATIC_MESHES
seen = set()
for spec in json.loads(catalog_path.read_text()).values():
    mesh = unreal.load_asset(spec['path'])
    if not mesh:
        raise RuntimeError('Missing furniture mesh: '+spec['path'])
    for slot in mesh.static_materials:
        mat = slot.material_interface
        if not mat or mat.get_path_name() in seen:
            continue
        seen.add(mat.get_path_name())
        if unreal.MaterialEditingLibrary.has_material_usage(mat, usage):
            continue
        if isinstance(mat, unreal.MaterialInstanceConstant):
            unreal.MaterialEditingLibrary.set_material_usage_override(mat, usage, True, True)
            unreal.MaterialEditingLibrary.update_material_instance(mat)
        elif isinstance(mat, unreal.Material):
            unreal.MaterialEditingLibrary.set_base_material_usage(mat, usage, True)
            unreal.MaterialEditingLibrary.recompile_material(mat)
        else:
            raise RuntimeError('Unsupported furniture material: '+mat.get_path_name())
        if not unreal.EditorAssetLibrary.save_loaded_asset(mat):
            raise RuntimeError('Cannot save instancing support: '+mat.get_path_name())

ROOT = '/Game/Carla/Blueprints/PCG/FurnitureTool'
PATH = ROOT + '/PCG_TwinFurniture'
graph = unreal.load_asset(PATH)
if graph is None:
    graph = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        'PCG_TwinFurniture', ROOT, unreal.PCGGraph, unreal.PCGGraphFactory())
else:
    for node in list(graph.get_editor_property('nodes')):
        graph.remove_node(node)
source, source_settings = graph.add_node_of_type(unreal.PCGFurniturePlanSettings)
spawner, settings = graph.add_node_of_type(unreal.PCGStaticMeshSpawnerSettings)
settings.set_mesh_selector_type(unreal.PCGMeshSelectorByAttribute)
selector = settings.get_editor_property('mesh_selector_parameters')
selector.set_editor_property('attribute_name', 'Mesh')
graph.add_edge(source, 'Out', spawner, 'In')
graph.add_edge(spawner, 'Out', graph.get_output_node(), 'Out')
if not unreal.EditorAssetLibrary.save_loaded_asset(graph):
    raise RuntimeError('Could not save furniture graph')
print('FURNITURE_GRAPH_READY', PATH, flush=True)
