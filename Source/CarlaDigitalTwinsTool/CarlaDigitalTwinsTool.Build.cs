// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using System.IO;
using System;
using System.Collections.Generic;

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
        var BuildPath = Path.Combine(PluginDirectory, "Build");
        var PathComparer = IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

        string[] TrackAndReadLines(string FileName)
        {
            var FilePath = Path.Combine(BuildPath, FileName);
            if (!File.Exists(FilePath))
            {
                throw new FileNotFoundException(
                    string.Format("Required CMake output '{0}' was not found. Run Setup.bat (Windows) or ./Setup.sh (Linux) first.", FilePath),
                    FilePath);
            }
            var ResolvedInfo = new FileInfo(FilePath).ResolveLinkTarget(true);
            ExternalDependencies.Add(ResolvedInfo != null ? ResolvedInfo.FullName : FilePath);
            return File.ReadAllLines(FilePath);
        }

        Console.WriteLine("Updating PublicAdditionalLibraries and RuntimeDependencies.");
        var SeenLibraries = new HashSet<string>(PathComparer);
        foreach (var Line in TrackAndReadLines("LinkLibraries.txt"))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || !SeenLibraries.Add(Trimmed))
                continue;
            if (!File.Exists(Trimmed))
                throw new FileNotFoundException(Trimmed);
            if (Trimmed.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
            {
                RuntimeDependencies.Add(Trimmed);
                if (verbose)
                    Console.WriteLine("Adding " + Trimmed + " to RuntimeDependencies");
            }
            else
            {
                PublicAdditionalLibraries.Add(Trimmed);
                if (Trimmed.EndsWith(".so", StringComparison.Ordinal) || Trimmed.Contains(".so."))
                {
                    RuntimeDependencies.Add(Trimmed);
                }
                if (verbose)
                    Console.WriteLine("Adding " + Trimmed + " to PublicAdditionalLibraries");
            }
        }

        Console.WriteLine("Updating PublicIncludePaths.");
        var SeenIncludes = new HashSet<string>(PathComparer);
        foreach (var Line in TrackAndReadLines("IncludeDirectories.txt"))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || !SeenIncludes.Add(Trimmed))
                continue;
            if (!Directory.Exists(Trimmed))
                throw new DirectoryNotFoundException(Trimmed);
            if (verbose)
                Console.WriteLine("Adding " + Trimmed + " to PublicIncludePaths");
            PublicIncludePaths.Add(Trimmed);
        }

        Console.WriteLine("Updating PublicDefinitions.");
        var SeenDefinitions = new HashSet<string>(StringComparer.Ordinal);
        foreach (var Line in TrackAndReadLines("PublicDefinitions.txt"))
        {
            var Trimmed = Line.Trim();
            if (Trimmed.Length == 0 || !SeenDefinitions.Add(Trimmed))
                continue;
            if (verbose)
                Console.WriteLine("Adding " + Trimmed + " to PublicDefinitions");
            PublicDefinitions.Add(Trimmed);
        }
    }

    public CarlaDigitalTwinsTool(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;
        bUseUnity = false;
        bEnableExceptions = true;

        if (IsWindows())
        {
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
            "Engine",
            "ProceduralMeshComponent",
            "MeshDescription",
            "RawMesh",
            "AssetTools",
            "Projects",
            }
        );


        PrivateDependencyModuleNames.AddRange(
          new string[]
          {
            "CoreUObject",
            "Slate",
            "SlateCore",
            "UnrealEd",
            "Blutility",
            "UMG",
            "EditorScriptingUtilities",
            "ImageWriteQueue",
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
            "InputCore",
            "AppFramework",
            "EngineSettings",
            "CarlaMeshGeneration",
            "StreetMapImporting",
            "StreetMapRuntime",
            "DesktopPlatform",
            "AssetRegistry",
            "ImageWrapper",
            "LevelEditor",
            "EditorSubsystem"
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
