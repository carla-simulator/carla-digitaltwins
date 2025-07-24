// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "CoreMinimal.h"
#include "Misc/FileHelper.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLLightType.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"
#include "UObject/ObjectMacros.h"

#include "TrafficLightActor.generated.h"

UCLASS(BlueprintType, Blueprintable)
class CARLADIGITALTWINSTOOL_API ATrafficLightActor : public AActor
{
	GENERATED_BODY()

public:
	ATrafficLightActor();

	virtual void OnConstruction(const FTransform& Transform) override;

	void Build();

	UFUNCTION(BlueprintCallable, CallInEditor, Category = "TrafficLight")
	void BuildFromJSON();

	UPROPERTY(EditAnywhere, Category = "TrafficLight")
	FFilePath JSONFile;

	UPROPERTY(EditAnywhere, Category = "TrafficLight")
	TArray<FTLPole> Poles;

private:
	USceneComponent* AddRootPole(USceneComponent* Parent, FTLPole& Pole);
	UStaticMeshComponent* AddPoleBase(USceneComponent* Parent, FTLPole& Pole);
	UStaticMeshComponent* AddPoleExtensible(USceneComponent* Parent, FTLPole& Pole);
	UStaticMeshComponent* AddPoleCap(USceneComponent* Parent, FTLPole& Pole);
	USceneComponent* AddHead(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head);
	UStaticMeshComponent* AddModule(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head, FTLModule& ModuleData);
	void AddBackplate(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head);
	FVector2D GetAtlasCoordsForLightType(ETLLightType LightType) const;
	void RebuildModuleChain(FTLHead& Head);
	void Clear();

private:
	TArray<UStaticMeshComponent*> ModuleMeshComponents;
};
