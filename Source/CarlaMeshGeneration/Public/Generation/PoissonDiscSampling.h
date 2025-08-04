// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

// Engine headers
#include "PCGSettings.h"
#include "Metadata/PCGAttributePropertySelector.h"
#include "PoissonDiscSampling.generated.h"

/**
 * Various fractal noises that can be used to filter points
 */
UCLASS(BlueprintType, ClassGroup = (Procedural))
class UPCGPoissonDiscSamplingSettings : public UPCGSettings
{
  GENERATED_BODY()
public:

  UPROPERTY(BlueprintReadWrite, EditAnywhere, Category = Settings)
  FVector2D Extent = FVector2D(100.0F, 100.0F);

  UPROPERTY(BlueprintReadWrite, EditAnywhere, Category = Settings)
  float MinDistance = 1.0F;

  UPROPERTY(BlueprintReadWrite, EditAnywhere, Category = Settings)
  int32 MaxRetries = 32;

};

class FPCGPoissonDiscSampling : public IPCGElement
{
protected:

	virtual bool ExecuteInternal(FPCGContext* Context) const override;

	virtual EPCGElementExecutionLoopMode ExecutionLoopMode(
        const UPCGSettings* Settings) const override
    {
        return EPCGElementExecutionLoopMode::SinglePrimaryPin;
    }
};
