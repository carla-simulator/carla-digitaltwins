// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;

public class CarlaDigitalTwinsTool : ModuleRules
{
	public CarlaDigitalTwinsTool(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicIncludePaths.AddRange(
			new string[] {
				// ... add public include paths required here ...
			}
			);


		PrivateIncludePaths.AddRange(
			new string[] {
				// ... add other private include paths required here ...
			}
			);


		PublicDependencyModuleNames.AddRange(
			new string[]
			{
				"Core",
				"ProceduralMeshComponent",
				"MeshDescription",
				"RawMesh",
				"AssetTools"
				// ... add other public dependencies that you statically link with here ...
			}
			);


		PrivateDependencyModuleNames.AddRange(
			new string[]
			{
				"CoreUObject",
				"Engine",
				"Slate",
				"SlateCore",
				"UnrealEd",
				"Blutility",
				"UMG",
				"EditorScriptingUtilities",
				"Landscape",
				"Foliage",
				"FoliageEdit",
				"MeshMergeUtilities",
				"StaticMeshDescription",
				"PhysXVehicles",
				"Json",
				"JsonUtilities",
				"Networking",
				"Sockets",
				"HTTP",
				"RHI",
				"RenderCore",
				"MeshMergeUtilities",

				// ... add private dependencies that you statically link with here ...	
			}
			);


		DynamicallyLoadedModuleNames.AddRange(
			new string[]
			{
				// ... add any modules that your module loads dynamically here ...
			}
			);
		PublicDefinitions.Add("ASIO_NO_EXCEPTIONS");
		PublicDefinitions.Add("BOOST_NO_EXCEPTIONS");
		// PublicDefinitions.Add("LIBCARLA_NO_EXCEPTIONS");
		PublicDefinitions.Add("PUGIXML_NO_EXCEPTIONS");
		PublicDefinitions.Add("BOOST_DISABLE_ABI_HEADERS");
		PublicDefinitions.Add("BOOST_TYPE_INDEX_FORCE_NO_RTTI_COMPATIBILITY");
	}
}
