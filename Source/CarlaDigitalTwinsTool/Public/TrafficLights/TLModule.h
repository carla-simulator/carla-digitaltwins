// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "TrafficLights/TLLightType.h"
#include "TLModule.generated.h"

USTRUCT(BlueprintType)
struct FTLModule
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Module")
    UStaticMesh* ModuleMesh;

    /** Local transform relative to parent head */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Module")
    FTransform RelativeTransform { FTransform::Identity };

    /** Offset transform */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Module")
    FTransform Offset;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Module")
    ETLLightType LightType { ETLLightType::Red };

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Module")
    bool bHasVisor { false };

    UPROPERTY(Transient)
    FGuid ModuleID { FGuid() };
};
