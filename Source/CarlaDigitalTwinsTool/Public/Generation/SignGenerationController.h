// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "StreetMap.h"
#include "SignDataAsset.h"
#include "PoleDataAsset.h"
#include <Carla/Road/RoadMap.h>
#include "SignGenerationController.generated.h"



UCLASS()
class CARLADIGITALTWINSTOOL_API ASignGenerationController : public AActor
{
	GENERATED_BODY()
	
public:	
	// Sets default values for this actor's properties
	ASignGenerationController(const FObjectInitializer& ObjectInitializer);


protected:
	// Called when the game starts or when spawned
	virtual void BeginPlay() override;

public:	
	// Called every frame
	virtual void Tick(float DeltaTime) override;

	UFUNCTION(BlueprintCallable, Category = "EditorUtilityWidget")
	void SignGenerationByPath(FName sign_package_path, FName pole_package_path, ESignStyle sign_style);

	UFUNCTION(Category= "SignGeneration", BlueprintCallable, CallInEditor)
	void SignGenerationForCurrentMap();

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	UStreetMap* StreetMapData = nullptr;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	FName SignPackagePath;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	FName PolePackagePath;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	ESignStyle SignStyle = ESignStyle::None;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	bool bDisplaceSignsToEdge = true;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	int MaxDisplacementIterations = 20;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	float RoadBorderPadding = 50.0f;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite, meta = (ClampMin = "0.0", ClampMax = "2.0"))
	float StepPercentOfLaneWidth = 0.33f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	TArray<AActor*> GeneratedSigns;

private:

	UFUNCTION()
	void GetSteetMapFile();

	UFUNCTION()
	void GetSignPropertyValue(FStreetMapMisc Sign, FString KeyToFind, FString& Out_KeyName, FString& Out_Value);

	bool has_spawned_sign = false;
};
