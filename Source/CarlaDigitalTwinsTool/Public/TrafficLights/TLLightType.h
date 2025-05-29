// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "TLLightType.generated.h"

UENUM(BlueprintType)
enum class ETLLightType : uint8
{
    Red            UMETA(DisplayName = "Solid Color (Red)"),
    Yellow         UMETA(DisplayName = "Solid Color (Yellow)"),
    Green          UMETA(DisplayName = "Solid Color (Green)"),
    ArrowLeft      UMETA(DisplayName = "Arrow Left"),
    ArrowRight     UMETA(DisplayName = "Arrow Right"),
    ArrowStraight  UMETA(DisplayName = "Arrow Straight"),
    Timer          UMETA(DisplayName = "Timer"),
    Pedestrian     UMETA(DisplayName = "Pedestrian"),
    PedestrianStop UMETA(DisplayName = "Pedestrian Stop"),
    Off            UMETA(DisplayName = "Off")
};
