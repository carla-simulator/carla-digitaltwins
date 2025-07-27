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
	void Bake(const FString& MapName);

	UFUNCTION(BlueprintCallable, CallInEditor, Category = "TrafficLight|JSON")
	void BuildFromJSON();

	UFUNCTION(BlueprintCallable, CallInEditor, Category = "TrafficLight|JSON")
	FString ExportToJSON() const;

	UFUNCTION(CallInEditor, BlueprintCallable, Category = "TrafficLight|Demo")
	void PlayDemoSequence();

	UFUNCTION(CallInEditor, BlueprintCallable, Category = "TrafficLight|Demo")
	void StopDemoSequence();

	void BuildFromJSONString(const FString& JSONConfig);

	UPROPERTY(EditAnywhere, Category = "TrafficLight|JSON")
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
	enum class EDemoPhase : uint8
	{
		Red,
		Green,
		AmberBlink
	};
	bool bDemoPlaying{false};
	FTimerHandle PhaseTimerHandle;
	FTimerHandle AmberBlinkTimerHandle;

	EDemoPhase CurrentPhase{EDemoPhase::Red};
	bool bAmberVisible{false};

	UPROPERTY(EditAnywhere, Category = "TrafficLight|Demo")
	float RedDuration{6.0f};

	UPROPERTY(EditAnywhere, Category = "TrafficLight|Demo")
	float GreenDuration{6.0f};

	UPROPERTY(EditAnywhere, Category = "TrafficLight|Demo")
	float AmberBlinkDuration{3.0f};

	UPROPERTY(EditAnywhere, Category = "TrafficLight|Demo")
	float AmberBlinkInterval{0.25f};

	void AdvanceDemoPhase();
	void ToggleAmberBlink();
	void EndAmberPhase();

private:
	TArray<UStaticMeshComponent*> ModuleMeshComponents;
	TArray<FTLModuleLight*> DemoLights;
};
