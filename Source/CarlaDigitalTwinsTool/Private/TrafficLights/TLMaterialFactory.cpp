#include "TrafficLights/TLMaterialFactory.h"

#include "CarlaDigitalTwinsTool.h"
#include "UObject/Object.h"
#include "UObject/WeakObjectPtr.h"

UMaterialInterface* FMaterialFactory::GetBaseLightMaterial()
{
	constexpr TCHAR const* BaseMatPath =
		TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/TrafficLights/"
			 "M_TrafficLights_Inst.M_TrafficLights_Inst");

	static TWeakObjectPtr<UMaterialInterface> CachedBaseMat;
	if (!CachedBaseMat.IsValid())
	{
		CachedBaseMat = LoadObject<UMaterialInterface>(nullptr, BaseMatPath);
	}

	UMaterialInterface* BaseMat = CachedBaseMat.Get();
	if (!IsValid(BaseMat))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("MaterialFactory: failed to load base material at '%s'"), BaseMatPath);
		return nullptr;
	}

	return BaseMat;
}

UMaterialInstanceDynamic* FMaterialFactory::CreateLightMaterialInstanceDynamic(UStaticMeshComponent* Comp, const FName& SlotName)
{
	if (!IsValid(Comp))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("CreateLightMaterialInstanceDynamic: Invalid StaticMeshComponent"));
		return nullptr;
	}
	const int32 MatIndex{Comp->GetMaterialIndex(SlotName)};
	if (MatIndex == INDEX_NONE)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("CreateLightMID: slot '%s' not found on component '%s'"),
			*SlotName.ToString(), *Comp->GetName());
		return nullptr;
	}

	UMaterialInterface* BaseMat{GetBaseLightMaterial()};
	if (!IsValid(BaseMat))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("CreateLightMID: base material invalid"));
		return nullptr;
	}

	UMaterialInstanceDynamic* MID{Comp->CreateDynamicMaterialInstance(MatIndex, BaseMat)};
	if (!IsValid(MID))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("CreateLightMID: failed to create MID for slot '%s'"), *SlotName.ToString());
		return nullptr;
	}
	return MID;
}
