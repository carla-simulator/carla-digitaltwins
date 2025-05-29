// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using System.IO;
using System;
using System.Diagnostics;
using System.Collections;
using System.Runtime.InteropServices;
using System.ComponentModel;

public class CarlaDigitalTwinsTool : ModuleRules
{
    private bool IsWindows()
    {
        return (Target.Platform == UnrealTargetPlatform.Win64);
    }

    private string GetCMakeConfigurationName()
    {
        switch (Target.Configuration)
        {
            case UnrealTargetConfiguration.Debug:
                return "Debug";
            case UnrealTargetConfiguration.DebugGame:
                return "Debug";
            case UnrealTargetConfiguration.Development:
                return "RelWithDebInfo";
            case UnrealTargetConfiguration.Shipping:
                return "Release";
            case UnrealTargetConfiguration.Test:
                return "Test";
            default:
                throw new ArgumentException(string.Format("Invalid build configuration \"{0}\"", Target.Configuration));
        }
    }

    private void BuildBoost()
    {
        var verbose = false;
        var CurrentProcess = Process.GetCurrentProcess();
        var BuildPath = Path.Combine(PluginDirectory, "Build");

        Console.WriteLine("Updating PublicAdditionalLibraries and RuntimeDependencies.");
        var Last = "";
        foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "LinkLibraries.txt")))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || Trimmed == Last)
                continue;
            if (!File.Exists(Trimmed))
                throw new FileNotFoundException(Trimmed);
            if (Trimmed.EndsWith(".dll"))
            {
                RuntimeDependencies.Add(Trimmed);
                if (verbose)
                    Console.WriteLine("Adding " + Trimmed + " to RuntimeDependencies");
            }
            else
            {
                PublicAdditionalLibraries.Add(Trimmed);
                if (verbose)
                    Console.WriteLine("Adding " + Trimmed + " to PublicAdditionalLibraries");
            }
            Last = Trimmed;
        }

        Console.WriteLine("Updating PublicIncludePaths.");
        foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "IncludeDirectories.txt")))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || Trimmed == Last)
                continue;
            if (!Directory.Exists(Trimmed))
                throw new FileNotFoundException(Trimmed);
            if (verbose)
                Console.WriteLine("Adding " + Trimmed + " to PublicIncludePaths");
            PublicIncludePaths.Add(Trimmed);
            Last = Trimmed;
        }

        Console.WriteLine("Updating PublicDefinitions.");
        foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "PublicDefinitions.txt")))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || Trimmed == Last)
                continue;
            if (verbose)
                Console.WriteLine("Adding " + Trimmed + " to PublicDefinitions");
            PublicDefinitions.Add(Trimmed);
            Last = Trimmed;
        }
    }

    public CarlaDigitalTwinsTool(ReadOnlyTargetRules Target) : base(Target)
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
        "CarlaMeshGeneration",
        "StreetMapImporting",
        "StreetMapRuntime",
        "LevelEditor",
        "InputCore"
        "EngineSettings"
              // ... add private dependencies that you statically link with here ...
          }
          );

        if (Target.Version.MajorVersion < 5)
        {
            PrivateDependencyModuleNames.Add("PhysXVehicles");
        }

        DynamicallyLoadedModuleNames.AddRange(
          new string[]
          {
              // ... add any modules that your module loads dynamically here ...
                }
        );

        BuildBoost();
        PublicDefinitions.Add("ASIO_NO_EXCEPTIONS");
        PublicDefinitions.Add("BOOST_NO_EXCEPTIONS");
        PublicDefinitions.Add("PUGIXML_NO_EXCEPTIONS");
        PublicDefinitions.Add("BOOST_DISABLE_ABI_HEADERS");
        // PublicDefinitions.Add("BOOST_TYPE_INDEX_FORCE_NO_RTTI_COMPATIBILITY");
        if (IsWindows())
        {
            PrivateDefinitions.Add("_USE_MATH_DEFINES");
        }
    }
}
