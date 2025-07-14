// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "StreetMap.h"
#include "SignDataAsset.h"
#include "SignGenerationController.generated.h"


UCLASS()
class CARLADIGITALTWINSTOOL_API ASignGenerationController : public AActor
{
	GENERATED_BODY()
	
public:	
	// Sets default values for this actor's properties
	ASignGenerationController();

protected:
	// Called when the game starts or when spawned
	virtual void BeginPlay() override;

public:	
	// Called every frame
	virtual void Tick(float DeltaTime) override;

	UFUNCTION(BlueprintCallable, Category = "EditorUtilityWidget")
	void SignGenerationByPath(FName package_path);

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	UStreetMap* StreetMapData;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	FName PackagePath;

	UPROPERTY(EditAnywhere, BlueprintReadWrite)
	TArray<AStaticMeshActor*> GeneratedSigns;

private:
	USignDataAsset* current_data_asset;


};
