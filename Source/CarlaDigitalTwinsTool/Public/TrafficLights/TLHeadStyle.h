// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "TLHeadStyle.generated.h"

UENUM(BlueprintType)
enum class ETLHeadStyle : uint8
{
    NorthAmerican   UMETA(DisplayName = "North American Standard"),
    European        UMETA(DisplayName = "European Standard"),
    Asian           UMETA(DisplayName = "Asian Standard"),
    Custom          UMETA(DisplayName = "Custom"),
};
