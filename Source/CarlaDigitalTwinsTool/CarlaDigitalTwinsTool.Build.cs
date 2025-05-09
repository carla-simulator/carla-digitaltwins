// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using System.IO;
using System;
using System.Diagnostics;
using System.Collections;
using System.Runtime.InteropServices;
using EpicGames.Core;
using System.ComponentModel;

public class CarlaDigitalTwinsTool : ModuleRules
{
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

	private string GetVCVars() // Windows only.
	{
		var Root = Environment.GetEnvironmentVariable("PROGRAMFILES");
		if (Root == null)
			throw new DirectoryNotFoundException();
		Root = Path.Combine(Root, "Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build");
		if (!Path.Exists(Root))
			throw new DirectoryNotFoundException(Root);
		Root = Path.Combine(Root, "vcvars64.bat");
		if (!Path.Exists(Root))
			throw new DirectoryNotFoundException(Root);
		return Root;
	}

	private string ArrayToCMakeList(string[] elements)
	{
		string result = "";
		if (elements.Length == 0)
			return result;
		if (elements.Length == 1)
			return elements[0];
		foreach (var element in elements)
		{
			result += element;
			result += ";";
		}
		return result;
	}

	private void BuildBoost()
	{
		var verbose = true;

		Console.WriteLine("Building CMake dependencies.");

		var BoostComponents = new string[]
		{
			"asio",
			"iterator",
			"date_time",
			"geometry",
			"container",
			"variant2",
			"gil",
		};
		var BoostComponentsList = ArrayToCMakeList(BoostComponents);
		var CurrentProcess = Process.GetCurrentProcess();
		var DepsPath = PluginDirectory;
		var Configuration = GetCMakeConfigurationName();
		var BuildPath = Path.Combine(DepsPath, "Build-" + Configuration);
		var PSI = new ProcessStartInfo();
		PSI.WorkingDirectory = DepsPath;
		var IsWindows = RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
		if (IsWindows)
		{
			PSI.FileName = Environment.GetEnvironmentVariable("comspec");
			PSI.Arguments = string.Format(
				"/k \"\"{0}\" && cmake -S . -B {1} -G Ninja -DCMAKE_BUILD_TYPE={2} -DBOOST_COMPONENTS={3}\"",
				GetVCVars(),
				BuildPath,
				Configuration,
				BoostComponentsList);
			// Workaround for process during generate project files never exiting.
			PSI.RedirectStandardOutput = false;
			PSI.RedirectStandardError = false;
		}
		else
		{
			PSI.FileName = "cmake";
			PSI.Arguments = string.Format(
				" -S . -B Build-{0} -G Ninja -DCMAKE_BUILD_TYPE={1} -DBOOST_COMPONENTS={2}",
				Target.Configuration,
				Configuration,
				BoostComponentsList);
		}
		
		var BuildProcess = new Process();
		BuildProcess.StartInfo = PSI;
		Console.WriteLine(string.Format("Running {0} {1}", PSI.FileName, PSI.Arguments));
		BuildProcess.Start();
		if (IsWindows) // Workaround for the reason mentioned above:
		{
			string Text;
			Action<TextReader> Callback = async (Reader) => {
				while ((Text = await Reader.ReadLineAsync()) != null)
					Console.WriteLine(Text);
			};
			try
      {
        Callback(BuildProcess.StandardOutput);
        Callback(BuildProcess.StandardError);
      }
			catch (Exception)
			{
			}
		}
		BuildProcess.WaitForExit();
		if (BuildProcess.ExitCode != 0)
		{
			throw new BuildException(string.Format(
				"Failed to build module dependencies (Exit code = {0}).",
				BuildProcess.ExitCode));
		}

		Console.WriteLine("Updating PublicAdditionalLibraries.");
		foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "LinkLibraries.txt")))
		{
			var Trimmed = Line.Trim();
			if (Trimmed.Length == 0)
				continue;
			if (!Path.Exists(Trimmed))
				throw new FileNotFoundException(Trimmed);
			if (verbose)
				Console.WriteLine("Adding " + Trimmed + " to PublicAdditionalLibraries");
			PublicAdditionalLibraries.Add(Trimmed);
		}

		Console.WriteLine("Updating PublicIncludePaths.");
		foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "IncludeDirectories.txt")))
		{
			var Trimmed = Line.Trim();
			if (Trimmed.Length == 0)
				continue;
			if (!Path.Exists(Trimmed))
				throw new FileNotFoundException(Trimmed);
			if (verbose)
				Console.WriteLine("Adding " + Trimmed + " to PublicIncludePaths");
			PublicIncludePaths.Add(Trimmed);
		}

		Console.WriteLine("Updating PublicDefinitions.");
		foreach (var Line in File.ReadAllLines(Path.Combine(BuildPath, "PublicDefinitions.txt")))
		{
			var Trimmed = Line.Trim();
			if (Trimmed.Length == 0)
				continue;
			if (verbose)
				Console.WriteLine("Adding " + Trimmed + " to PublicDefinitions");
			PublicDefinitions.Add(Trimmed);
		}
	}

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
	}
}
