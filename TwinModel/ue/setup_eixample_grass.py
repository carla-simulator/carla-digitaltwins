"""Create and apply Eixample's Town15-derived lawn in an Unreal Python commandlet.

Town15's vertex-painted master needs painted mesh colours. Generated twin ground
has none, so expose its MidLower layer directly. Use Town15's denser landscape
grass textures, copied as virtual textures to match this master's sampler type.
Shared Town15 assets are never modified. Run before baking EixampleDemo.
The source vertex-paint shader does not implement CARLA weather wetness.
"""
import unreal

ROOT = '/Game/Carla/Maps/Twins/EixampleDemo/Materials/'
GENERIC = '/Game/Carla/Static/GenericMaterials/'
SCALARS = {
    'MidLower - Tiling': .01,
    # Keep the rendered lawn on the generated mesh surface.
    'MidLower - Displacement Intensity': 0.0,
    'MidLower - Brightness': .35,
    'MidLower - Tint Intensity': .2,
    'MidLower - Roughness Contrast': 1.0,
    'MidLower - Specular': .03,
    'MidLower - Contrast': 1.0,
    'MidLower - Normal Flatness': .3,
}


def main():
    lib = unreal.EditorAssetLibrary
    edit = unreal.MaterialEditingLibrary

    def copy(source, name):
        path = ROOT + name
        asset = unreal.load_asset(path) if lib.does_asset_exist(path) else lib.duplicate_asset(source, path)
        assert asset, path
        return asset

    def override(mi, array, cls, name, value):
        values = [p for p in mi.get_editor_property(array) if str(p.parameter_info.name) != name]
        entry = cls()
        entry.set_editor_property('parameter_info', unreal.MaterialParameterInfo(name=name))
        entry.set_editor_property('parameter_value', value)
        mi.set_editor_property(array, values + [entry])

    master = copy(GENERIC + '000_Masters/M_VertexPaintCB', 'M_TwinUrbanLawn')
    layer = next(e for e in unreal.ObjectIterator(unreal.MaterialExpressionNamedRerouteDeclaration)
                 if e.get_outer() == master and
                 str(e.get_editor_property('name')).lower().replace(' ', '') == 'midlowerlayer')
    assert edit.connect_material_property(layer, '', unreal.MaterialProperty.MP_MATERIAL_ATTRIBUTES)
    edit.recompile_material(master)
    assert lib.save_loaded_asset(master, False)
    mi = copy(GENERIC + 'Ground/MI_VertexPaintGround01', 'MI_TwinUrbanLawn_Dense')
    edit.set_material_instance_parent(mi, master)
    for param, name in [('Diffuse', 'grass_01_d'), ('Normal', 'grass_01_n'), ('ORM', 'grass_01_ORM')]:
        tex = copy(GENERIC + 'Ground/Textures/Grass/' + name, 'T_Twin_' + name)
        tex.set_editor_property('virtual_texture_streaming', True)
        assert lib.save_loaded_asset(tex, False)
        override(mi, 'texture_parameter_values', unreal.TextureParameterValue, 'MidLower - ' + param, tex)
    for name, value in SCALARS.items():
        override(mi, 'scalar_parameter_values', unreal.ScalarParameterValue, name, value)
    override(mi, 'vector_parameter_values', unreal.VectorParameterValue,
             'MidLower - Tint', unreal.LinearColor(.015, .06, .005, 1))
    edit.update_material_instance(mi)
    assert lib.save_loaded_asset(mi, False)
    # Tile selection stays stable; all four slots share one continuous lawn.
    for index in range(4):
        path = ROOT + 'MI_EixampleDemo_ground_' + str(index)
        if not lib.does_asset_exist(path):
            continue  # First bake will create the pool instances.
        ground = unreal.load_asset(path)
        edit.set_material_instance_parent(ground, mi)
        for array in ('scalar_parameter_values', 'vector_parameter_values', 'texture_parameter_values'):
            ground.set_editor_property(array, [])
        edit.update_material_instance(ground)
        assert lib.save_loaded_asset(ground, False)
    unreal.log('Eixample lawn saved successfully')


if __name__ == '__main__':
    main()
