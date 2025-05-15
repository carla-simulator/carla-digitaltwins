// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using System.IO;
using System;
using System.Diagnostics;
using System.Collections;
using System.Runtime.InteropServices;
using System.ComponentModel;

public class CarlaMeshGeneration : ModuleRules
{
  private bool IsWindows()
  {
    return (Target.Platform == UnrealTargetPlatform.Win64);
  }

  public CarlaMeshGeneration(ReadOnlyTargetRules Target) : base(Target)
  {
    PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;
    bUseUnity = false;

    if (IsWindows())
    {
      bEnableExceptions = true;
      PublicDefinitions.Add("DISABLE_WARNING_C4800=1");
    }

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
