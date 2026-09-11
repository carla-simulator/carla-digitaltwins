"""Create/update the small PCG graph used by ProceduralVegetationTool.

Run through UnrealEditor-Cmd -run=pythonscript -script=<this file>.
Existing legacy forest/street graphs and mesh assets are not edited.
"""
import unreal

ROOT = '/Game/Carla/Blueprints/PCG/VegetationTool'
PATH = ROOT + '/PCG_TwinVegetation'
graph = unreal.load_asset(PATH)
if graph is None:
    graph = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        'PCG_TwinVegetation', ROOT, unreal.PCGGraph, unreal.PCGGraphFactory())
else:
    for node in list(graph.get_editor_property('nodes')):
        graph.remove_node(node)
source, source_settings = graph.add_node_of_type(unreal.PCGVegetationPlanSettings)
spawner, settings = graph.add_node_of_type(unreal.PCGStaticMeshSpawnerSettings)
settings.set_mesh_selector_type(unreal.PCGMeshSelectorByAttribute)
selector = settings.get_editor_property('mesh_selector_parameters')
selector.set_editor_property('attribute_name', 'Mesh')
graph.add_edge(source, 'Out', spawner, 'In')
graph.add_edge(spawner, 'Out', graph.get_output_node(), 'Out')
if not unreal.EditorAssetLibrary.save_loaded_asset(graph):
    raise RuntimeError('Could not save vegetation graph')
print('VEGETATION_GRAPH_READY', PATH, flush=True)
