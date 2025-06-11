// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "Containers/Array.h"

#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLHeadStyle.h"
#include "TrafficLights/TLHeadAttachment.h"
#include "TrafficLights/TLHeadOrientation.h"
#include "TLHead.generated.h"

USTRUCT(BlueprintType)
struct FTLHead
{
    GENERATED_BODY()

    /** Local transform relative to parent pole */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    FTransform Transform { FTransform::Identity };

    /** Offset transform */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    FTransform Offset;

    /** Head style */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    ETLHeadStyle HeadStyle { ETLHeadStyle::NorthAmerican };

    /** Attachment type */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    ETLHeadAttachment Attachment { ETLHeadAttachment::Ring };

    /** Orientation of the head */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    ETLHeadOrientation Orientation { ETLHeadOrientation::Vertical };

    //** Has backplate */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    bool bHasBackplate { false };

    /** Modules (one or more) visible on this head */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic Light|Head")
    TArray<FTLModule> Modules;

    UPROPERTY(Transient)
    FGuid HeadID { FGuid() };

};
