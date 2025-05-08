// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using System.IO;
using System;
using System.Diagnostics;

public class CarlaDigitalTwinsTool : ModuleRules
{
	private void BuildDependencies()
	{
		var Configuration = "";
		switch (Target.Configuration)
		{
      case UnrealTargetConfiguration.Debug:
				Configuration = "Debug";
				break;
      case UnrealTargetConfiguration.DebugGame:
				Configuration = "Debug";
				break;
      case UnrealTargetConfiguration.Development:
				Configuration = "RelWithDebInfo";
				break;
      case UnrealTargetConfiguration.Shipping:
				Configuration = "Release";
				break;
      case UnrealTargetConfiguration.Test:
				Configuration = "Test";
        break;
      default:
				throw new ArgumentException($"Invalid build configuration \"{Target.Configuration}\"");
    }
		var DepsPath = PluginDirectory;
    var PSI = new ProcessStartInfo();
		PSI.FileName = "cmake";
		PSI.Arguments = $" -S {DepsPath} -B Build -G Ninja -DCMAKE_BUILD_TYPE={Configuration}";
		var BuildProcess = Process.Start(PSI);
		BuildProcess.WaitForExit();
		if (BuildProcess.ExitCode != 0)
			throw new BuildException("Failed to build module dependencies.");
	}

	public CarlaDigitalTwinsTool(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;

		BuildDependencies();

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
	}
}
