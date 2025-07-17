// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Materials/MaterialInstanceDynamic.h"
#include "UObject/Object.h"

class FMaterialFactory
{
public:
	static UMaterialInstanceDynamic* GetLightMaterialInstance(UObject* Outer);
};
