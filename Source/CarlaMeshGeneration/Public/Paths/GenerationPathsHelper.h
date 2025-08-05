// Copyright (c) 2020 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "Interfaces/IPluginManager.h"
#include "HAL/PlatformFileManager.h"
#include "GenerationPathsHelper.generated.h"

UCLASS()
class CARLAMESHGENERATION_API UGenerationPathsHelper : public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public:

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetRawMapDirectoryPath(FString MapName) {
      return FPaths::ProjectPluginsDir() + MapName + "/Content/Maps/";
  }

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetMapDirectoryPath(FString MapName) {
      return "/" + MapName + "/Maps/";
  }

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetMapContentDirectoryPath(FString MapName) {
      return "/" + MapName + "/Static/";
  }

  UFUNCTION(BlueprintCallable)
  static FString GetDigitalTwinsPluginPath() {
    FString PluginPath = FPaths::ConvertRelativePathToFull(IPluginManager::Get().FindPlugin("CarlaDigitalTwinsTool")->GetBaseDir());
    return PluginPath;
  }

  UFUNCTION(BlueprintCallable)
  static void CreateDirectory(const FString& DirectoryPath)
  {
      IPlatformFile& PlatformFile = FPlatformFileManager::Get().GetPlatformFile();

      if (!PlatformFile.DirectoryExists(*DirectoryPath))
      {
          PlatformFile.CreateDirectoryTree(*DirectoryPath);
          UE_LOG(LogTemp, Log, TEXT("Created disk folder: %s"), *DirectoryPath);
      }
  }

  UFUNCTION(BlueprintCallable)
  static FString GetPythonIntermediatePath(const FString& MapName)
  {
    FString MapPath = FPaths::ConvertRelativePathToFull(UGenerationPathsHelper::GetRawMapDirectoryPath(MapName));
    FString OutPath = MapPath / TEXT("PythonIntermediate");
    UGenerationPathsHelper::CreateDirectory(OutPath);

    return OutPath;
  }

};
