// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "Engine/StaticMesh.h"
#include "Engine/Texture2D.h"
#include "Materials/MaterialInterface.h"
#include "DataTables/TreeTableRow.h"
#include "SignDataAsset.generated.h"

UENUM(BlueprintType)
enum class ESignCategory : uint8
{
	Priority    UMETA(DisplayName = "Priority"),
	Mandatory   UMETA(DisplayName = "Mandatory"),
	Prohibitory UMETA(DisplayName = "Prohibitory"),
	Warning     UMETA(DisplayName = "Warning"),
	Information UMETA(DisplayName = "Information"),
	Guide       UMETA(DisplayName = "Guide"),
	Others      UMETA(DisplayName = "Others"),
	SpeedLimit  UMETA(DisplayName = "SpeedLimit"),
	None        UMETA(DisplayName = "None")
};

/**
 * One traffic sign of the catalog: a plate mesh plus the texture/material that paints it.
 * Atlas cells (Id_X, Id_Y) are 1-based (1..4, top-left = (1, 1)).
 */
UCLASS(BlueprintType)
class CARLADIGITALTWINSTOOL_API USignDataAsset : public UPrimaryDataAsset
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign")
	TObjectPtr<UStaticMesh> SignMesh = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign")
	bool Unique = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign")
	TObjectPtr<UTexture2D> Diffuse = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign", meta = (ClampMin = "1", ClampMax = "4"))
	int Id_X = 1;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign", meta = (ClampMin = "1", ClampMax = "4"))
	int Id_Y = 1;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Sign")
	TObjectPtr<UMaterialInterface> Material = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	ESignStyle Style = ESignStyle::None;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	ESignCategory Category = ESignCategory::None;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	FString SignName;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	FString Description;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	FString Meaning;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	FString XodrType;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	FString XodrSubtype;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Catalog")
	TArray<FString> OsmTags;

	UFUNCTION(BlueprintCallable, Category = "Catalog")
	bool MatchesOsmTag(const FString& Key, const FString& Value) const
	{
		return OsmTags.Contains(Key + TEXT("=") + Value);
	}

	UFUNCTION(BlueprintCallable, Category = "Catalog")
	bool MatchesXodrSignal(const FString& Type, const FString& Subtype) const
	{
		if (XodrType.IsEmpty() || XodrType != Type)
		{
			return false;
		}
		return XodrSubtype.IsEmpty() || XodrSubtype == TEXT("-1") || XodrSubtype == Subtype;
	}
};
