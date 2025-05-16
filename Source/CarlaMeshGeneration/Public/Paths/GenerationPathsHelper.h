// Copyright (c) 2020 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "GenerationPathsHelper.generated.h"

UCLASS()
class CARLAMESHGENERATION_API UGenerationPathsHelper : public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public:

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetRawMapDirectoryPath(FString MapName) {
      return FPaths::ProjectPluginsDir() + MapName + "/Maps/";
  }

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetMapDirectoryPath(FString MapName) {
      return "/" + MapName + "/Maps/";
  }

  UFUNCTION(BlueprintCallable, BlueprintPure)
  static FString GetMapContentDirectoryPath(FString MapName) {
      return "/" + MapName + "/Static/";
  }

};
