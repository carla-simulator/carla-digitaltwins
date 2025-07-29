#include "TrafficLights/TLMaterialFactory.h"

UMaterialInstanceDynamic* FMaterialFactory::GetLightMaterialInstance(UObject* Outer)
{
	static UMaterialInterface* BaseMat{Cast<UMaterialInterface>(StaticLoadObject(UMaterialInterface::StaticClass(), nullptr,
		TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/TrafficLights/"
			 "M_TrafficLights_Inst.M_TrafficLights_Inst")))};
	if (!IsValid(BaseMat))
	{
		UE_LOG(LogTemp, Error, TEXT("MaterialFactory: failed to load base material at '%s'"),
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/TrafficLights/"
				 "M_TrafficLights_Inst.M_TrafficLights_Inst"));
		return nullptr;
	}

	UMaterialInstanceDynamic* MID{UMaterialInstanceDynamic::Create(BaseMat, Outer)};
	if (!IsValid(MID))
	{
		UE_LOG(LogTemp, Error, TEXT("MaterialFactory: failed to create dynamic material instance"));
		return nullptr;
	}
	return MID;
}
