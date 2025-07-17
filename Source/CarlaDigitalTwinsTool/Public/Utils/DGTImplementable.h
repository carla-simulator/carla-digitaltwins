// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB). This work is licensed under the terms of the MIT license. For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "EditorUtilityObject.h"
#include "DGTImplementable.generated.h"

UCLASS(Blueprintable, BlueprintType)
class CARLADIGITALTWINSTOOL_API UDGTImplementable : public UEditorUtilityObject
{
  GENERATED_BODY()

public:

  UFUNCTION(BlueprintImplementableEvent, Category = "CarlaDigitalTwinsTool")
  void RunUtilityFunction(UWorld* World);
};
