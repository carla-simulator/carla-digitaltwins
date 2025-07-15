// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "StreetMap.h"
#include "SignDataAsset.h"

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
	void SignGenerationByPath(FName package_path);

	UFUNCTION(BlueprintCallable, CallInEditor, Category = "EditorUtilityWidget")
	void SignGenerationForCurrentMap();

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	UStreetMap* StreetMapData;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	FName PackagePath;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	bool bDisplaceSignsToEdge;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	TArray<AStaticMeshActor*> GeneratedSigns;
	//TMap<int32, AStaticMeshActor*> GeneratedSigns;

private:
	USignDataAsset* current_data_asset;

	TArray<FVector> closest_waypoints;


};
