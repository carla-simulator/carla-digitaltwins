// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

// Engine headers
#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MiscFunctionLibrary.generated.h"


UCLASS(BlueprintType)
class CARLADIGITALTWINSTOOL_API UMiscFunctionLibrary : public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public:
  UFUNCTION(BlueprintCallable)
  static FString GetProjectName();
};
