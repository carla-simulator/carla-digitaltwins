// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "SignDataAsset.generated.h"

/**
 * 
 */
UCLASS()
class CARLADIGITALTWINSTOOL_API USignDataAsset : public UPrimaryDataAsset
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	UStaticMesh* SignMesh;
	
	
};
