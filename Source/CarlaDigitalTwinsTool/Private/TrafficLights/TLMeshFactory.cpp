#include "TrafficLights/TLMeshFactory.h"

#include "CarlaDigitalTwinsTool.h"
#include "Components/StaticMeshComponent.h"
#include "CoreGlobals.h"
#include "Logging/LogVerbosity.h"
#include "Math/MathFwd.h"
#include "Misc/AssertionMacros.h"
#include "TrafficLights/TLBackplateDataTable.h"
#include "TrafficLights/TLLightTypeDataTable.h"
#include "TrafficLights/TLModuleDataTable.h"
#include "TrafficLights/TLPoleDataTable.h"
#include "TrafficLights/TLPoleType.h"
#include "UObject/NameTypes.h"

TWeakObjectPtr<UDataTable> FTLMeshFactory::s_ModuleMeshTable{};
TWeakObjectPtr<UDataTable> FTLMeshFactory::s_PoleMeshTable{};
TWeakObjectPtr<UDataTable> FTLMeshFactory::s_LightTypeMeshTable{};
TWeakObjectPtr<UDataTable> FTLMeshFactory::s_BackplateMeshTable{};

UDataTable* FTLMeshFactory::GetLightTypeMeshTable()
{
	if (!s_LightTypeMeshTable.IsValid())
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/LightTypes.LightTypes")};
		s_LightTypeMeshTable = LoadObject<UDataTable>(nullptr, Path);
		if (!s_LightTypeMeshTable.IsValid())
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FTLMeshFactory: Failed to load LightTypes DataTable at '%s'"), Path);
			return nullptr;
		}
	}
	return s_LightTypeMeshTable.Get();
}

UDataTable* FTLMeshFactory::GetModuleMeshTable()
{
	if (!s_ModuleMeshTable.IsValid())
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Modules.Modules")};
		s_ModuleMeshTable = LoadObject<UDataTable>(nullptr, Path);
		if (!s_ModuleMeshTable.IsValid())
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FTLMeshFactory: Failed to load Modules DataTable at '%s'"), Path);
			return nullptr;
		}
	}
	return s_ModuleMeshTable.Get();
}

UDataTable* FTLMeshFactory::GetPoleMeshTable()
{
	if (!s_PoleMeshTable.IsValid())
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Poles.Poles")};
		s_PoleMeshTable = LoadObject<UDataTable>(nullptr, Path);
		if (!s_PoleMeshTable.IsValid())
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FTLMeshFactory: Failed to load Poles DataTable at '%s'"), Path);
			return nullptr;
		}
	}
	return s_PoleMeshTable.Get();
}

UDataTable* FTLMeshFactory::GetBackplateMeshTable()
{
	if (!s_BackplateMeshTable.IsValid())
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Backplates.Backplates")};
		s_BackplateMeshTable = LoadObject<UDataTable>(nullptr, Path);
		if (!s_BackplateMeshTable.IsValid())
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FTLMeshFactory: Failed to load Backplates DataTable at '%s'"), Path);
			return nullptr;
		}
	}
	return s_BackplateMeshTable.Get();
}

UStaticMesh* FTLMeshFactory::GetBaseMeshByName(const FString& Name)
{
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetBaseMeshByName"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}
		if (Row->PoleType == ETLPoleType::Base && IsValid(Row->Mesh) && Row->Mesh->GetName() == Name)
		{
			return Row->Mesh;
		}
	}
	return nullptr;
}

UStaticMesh* FTLMeshFactory::GetExtensibleMeshByName(const FString& Name)
{
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetExtensibleMeshByName"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}
		if (Row->PoleType == ETLPoleType::Extensible && IsValid(Row->Mesh) && Row->Mesh->GetName() == Name)
		{
			return Row->Mesh;
		}
	}
	return nullptr;
}

UStaticMesh* FTLMeshFactory::GetCapMeshByName(const FString& Name)
{
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetCapMeshByName"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}
		if (Row->PoleType == ETLPoleType::Cap && IsValid(Row->Mesh) && Row->Mesh->GetName() == Name)
		{
			return Row->Mesh;
		}
	}
	return nullptr;
}

UStaticMesh* FTLMeshFactory::GetMeshForModule(const FTLHead& Head, const FTLModule& Module)
{
	UDataTable* ModuleMeshTable{GetModuleMeshTable()};
	if (!ModuleMeshTable)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ModuleMeshFactory: ModuleMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : ModuleMeshTable->GetRowNames())
	{
		const FTLModuleRow* Row{ModuleMeshTable->FindRow<FTLModuleRow>(RowName, TEXT("GetMeshForModule"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ModuleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}
		if (Row->Style == Head.Style && Row->Orientation == Head.Orientation && Row->bHasVisor == Module.bHasVisor &&
			IsValid(Row->Mesh))
		{
			return Row->Mesh;
		}
	}

	return nullptr;
}

TArray<UStaticMesh*> FTLMeshFactory::GetAllMeshesForModule(const FTLHead& Head, const FTLModule& Module)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* ModuleMeshTable{GetModuleMeshTable()};
	if (!ModuleMeshTable)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ModuleMeshFactory: ModuleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : ModuleMeshTable->GetRowNames())
	{
		const FTLModuleRow* Row{ModuleMeshTable->FindRow<FTLModuleRow>(RowName, TEXT("GetMeshForModule"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ModuleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}
		if (Row->Style == Head.Style && Row->Orientation == Head.Orientation && Row->bHasVisor == Module.bHasVisor &&
			IsValid(Row->Mesh))
		{
			Meshes.Add(Row->Mesh);
		}
	}

	return Meshes;
}

TArray<UStaticMesh*> FTLMeshFactory::GetAllBaseMeshesForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllBaseMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && Row->PoleType == ETLPoleType::Base &&
			IsValid(Row->Mesh))
		{
			Meshes.Add(Row->Mesh);
		}
	}
	return Meshes;
}

TArray<UStaticMesh*> FTLMeshFactory::GetAllExtensibleMeshesForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllExtensibleMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && Row->PoleType == ETLPoleType::Extensible &&
			IsValid(Row->Mesh))
		{
			Meshes.Add(Row->Mesh);
		}
	}
	return Meshes;
}

TArray<UStaticMesh*> FTLMeshFactory::GetAllCapMeshesForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllCapMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && Row->PoleType == ETLPoleType::Cap &&
			IsValid(Row->Mesh))
		{
			Meshes.Add(Row->Mesh);
		}
	}
	return Meshes;
}

FTLBackplateRow* FTLMeshFactory::GetBackplateRow(ETLStyle Style)
{
	UDataTable* Table{GetBackplateMeshTable()};
	if (!Table)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("PoleMeshFactory: BackplateMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		FTLBackplateRow* Row{Table->FindRow<FTLBackplateRow>(RowName, TEXT("GetBackplateRow"))};
		if (!Row)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
			continue;
		}

		if (Row->Style == Style)
		{
			return Row;
		}
	}
	return nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateCornerMesh(const FTLHead& Head)
{
	const FTLBackplateRow* Row = GetBackplateRow(Head.Style);
	return Row ? Row->CornerMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateHorizontalMesh(const FTLHead& Head)
{
	const FTLBackplateRow* Row = GetBackplateRow(Head.Style);
	return Row ? Row->HorizontalMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateVerticalMesh(const FTLHead& Head)
{
	const FTLBackplateRow* Row = GetBackplateRow(Head.Style);
	return Row ? Row->VerticalMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateMiddleMesh(const FTLHead& Head)
{
	const FTLBackplateRow* Row = GetBackplateRow(Head.Style);
	return Row ? Row->MiddleMesh : nullptr;
}

int32 FTLMeshFactory::CountLedMaterials(UStaticMesh* Mesh)
{
	if (!IsValid(Mesh))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("CountLedSockets: Invalid Mesh"));
		return 0;
	}

	int32 Count{0};
	const TArray<FStaticMaterial>& StaticMaterials{Mesh->GetStaticMaterials()};
	for (const FStaticMaterial& Material : StaticMaterials)
	{
		if (Material.MaterialSlotName.ToString().StartsWith(TEXT("led_")))
		{
			++Count;
		}
	}
	return Count;
}

FIntPoint FTLMeshFactory::GetAtlasCoordsForLightType(ETLLightType LightType)
{
	UDataTable* LightTable = GetLightTypeMeshTable();
	if (!IsValid(LightTable))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("LightTypesTable is not set"));
		return FIntPoint::ZeroValue;
	}
	const UEnum* EnumPtr{StaticEnum<ETLLightType>()};
	if (!EnumPtr)
	{
		return FIntPoint::ZeroValue;
	}

	const FString EnumName{EnumPtr->GetNameStringByValue(static_cast<int64>(LightType))};
	const FName RowName(*EnumName);
	if (const FTLLightTypeRow* Row = LightTable->FindRow<FTLLightTypeRow>(RowName, TEXT("GetAtlasCoordsForLightType")))
	{
		return Row->AtlasCoords;
	}
	return FIntPoint::ZeroValue;
}
