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
	UStreetMap* StreetMapData;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	FName SignPackagePath;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	FName PolePackagePath;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	ESignStyle SignStyle;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	bool bDisplaceSignsToEdge;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite)
	int max_displacement_iterations;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite, meta = (ClampMin = "0.0", ClampMax = "2.0"))
	float distance_from_road_percent;

	UPROPERTY(Category = "SignGeneration", EditAnywhere, BlueprintReadWrite, meta = (ClampMin = "0.0", ClampMax = "2.0"))
	float step_percent_of_lane_width;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	TArray<AActor*> GeneratedSigns;
	//TMap<int32, AStaticMeshActor*> GeneratedSigns;

private:

	UFUNCTION()
	void GetSteetMapFile();

	USignDataAsset* current_data_asset;

	TArray<FVector> closest_waypoints;

	bool has_spawned_sign;
};
