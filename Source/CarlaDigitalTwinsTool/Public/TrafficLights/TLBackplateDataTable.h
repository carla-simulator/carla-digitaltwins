// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Components/StaticMeshComponent.h"
#include "Engine/DataTable.h"
#include "TrafficLights/TLStyle.h"
#include "UObject/ObjectMacros.h"

#include "TLBackplateDataTable.generated.h"

USTRUCT(BlueprintType)
struct FTLBackplateRow : public FTableRowBase
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Traffic Light|Backplate")
	ETLStyle Style;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Traffic Light|Backplate")
	UStaticMesh* CornerMesh;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Traffic Light|Backplate")
	UStaticMesh* VerticalMesh;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Traffic Light|Backplate")
	UStaticMesh* HorizontalMesh;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Traffic Light|Backplate")
	UStaticMesh* MiddleMesh;
};
