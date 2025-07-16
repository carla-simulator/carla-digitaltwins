// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

// Engine headers
#include "CarlaMeshGeneration.h"
#include "CoreMinimal.h"
#include "Containers/ArrayView.h"
#include "DynamicMeshGeneration.generated.h"

DECLARE_LOG_CATEGORY_EXTERN(LogCarlaDynamicMeshGeneration, Log, All);

UCLASS(BlueprintType)
class CARLAMESHGENERATION_API UDynamicMeshGeneration :
  public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public:

  UFUNCTION(BlueprintCallable)
  static UStaticMesh* CreateMeshFromPoints(
    const TArray<FVector2D>& Points,
    FTransform Transform,
    FName MeshName);

};
