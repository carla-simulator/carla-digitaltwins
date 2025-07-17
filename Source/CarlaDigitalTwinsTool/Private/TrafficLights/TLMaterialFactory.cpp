#include "TrafficLights/TLMaterialFactory.h"

#include "UObject/SoftObjectPath.h"

UMaterialInstanceDynamic* FMaterialFactory::GetLightMaterialInstance(UObject* Outer)
{
	static const FString Path{
		TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/TrafficLights/M_TrafficLights_Inst.M_TrafficLights_Inst")};
	TSoftObjectPtr<UMaterialInterface> SoftPtr{FSoftObjectPath(Path)};
	UMaterialInterface* Mat = SoftPtr.LoadSynchronous();
	if (!Mat)
	{
		UE_LOG(LogTemp, Error, TEXT("MaterialFactory: failed to load the material for '%d'"), *Path);
	}
	UMaterialInstanceDynamic* MID = UMaterialInstanceDynamic::Create(Mat, Outer);
	if (!MID)
	{
		UE_LOG(LogTemp, Error, TEXT("MaterialFactory: failed to create MID"));
		return nullptr;
	}

	return MID;
}
