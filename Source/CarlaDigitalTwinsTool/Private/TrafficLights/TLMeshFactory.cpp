#include "TrafficLights/TLMeshFactory.h"

#include "Components/StaticMeshComponent.h"
#include "Logging/LogVerbosity.h"
#include "Misc/AssertionMacros.h"
#include "TrafficLights/TLBackplateDataTable.h"
#include "TrafficLights/TLLightTypeDataTable.h"
#include "TrafficLights/TLModuleDataTable.h"
#include "TrafficLights/TLPoleDataTable.h"
#include "TrafficLights/TLPoleType.h"
#include "UObject/NameTypes.h"

UDataTable* FTLMeshFactory::s_ModuleMeshTable{nullptr};
UDataTable* FTLMeshFactory::s_PoleMeshTable{nullptr};
UDataTable* FTLMeshFactory::s_LightTypeMeshTable{nullptr};
UDataTable* FTLMeshFactory::s_BackplateMeshTable{nullptr};

UDataTable* FTLMeshFactory::GetLightTypeMeshTable()
{
	if (!s_LightTypeMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/LightTypes.LightTypes")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		s_LightTypeMeshTable = Cast<UDataTable>(Loaded);
	}
	check(s_LightTypeMeshTable);
	return s_LightTypeMeshTable;
}

UDataTable* FTLMeshFactory::GetModuleMeshTable()
{
	if (!s_ModuleMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Modules.Modules")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		s_ModuleMeshTable = Cast<UDataTable>(Loaded);
	}
	check(s_ModuleMeshTable);
	return s_ModuleMeshTable;
}

UDataTable* FTLMeshFactory::GetPoleMeshTable()
{
	if (!s_PoleMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Poles.Poles")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		s_PoleMeshTable = Cast<UDataTable>(Loaded);
	}
	check(s_PoleMeshTable);
	return s_PoleMeshTable;
}

UDataTable* FTLMeshFactory::GetBackplateMeshTable()
{
	if (!s_BackplateMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/CarlaDigitalTwinsTool/Carla/Static/TrafficLight/TrafficLights2025/DataTables/Backplates.Backplates")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		s_BackplateMeshTable = Cast<UDataTable>(Loaded);
	}
	check(s_BackplateMeshTable);
	return s_BackplateMeshTable;
}

UStaticMesh* FTLMeshFactory::GetMeshForModule(const FTLHead& Head, const FTLModule& Module)
{
	UDataTable* ModuleMeshTable{GetModuleMeshTable()};
	if (!ModuleMeshTable)
	{
		UE_LOG(LogTemp, Error, TEXT("ModuleMeshFactory: ModuleMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : ModuleMeshTable->GetRowNames())
	{
		const FTLModuleRow* Row{ModuleMeshTable->FindRow<FTLModuleRow>(RowName, TEXT("GetMeshForModule"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Error, TEXT("ModuleMeshFactory: row '%s' not found"), *RowName.ToString());
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
		UE_LOG(LogTemp, Error, TEXT("ModuleMeshFactory: ModuleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : ModuleMeshTable->GetRowNames())
	{
		const FTLModuleRow* Row{ModuleMeshTable->FindRow<FTLModuleRow>(RowName, TEXT("GetMeshForModule"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Error, TEXT("ModuleMeshFactory: row '%s' not found"), *RowName.ToString());
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

UStaticMesh* FTLMeshFactory::GetBaseMeshForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> All{GetAllBaseMeshesForPole(Pole)};
	return All.Num() ? All.Last() : nullptr;
}

UStaticMesh* FTLMeshFactory::GetExtendibleMeshForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> All{GetAllExtendibleMeshesForPole(Pole)};
	return All.Num() ? All.Last() : nullptr;
}

UStaticMesh* FTLMeshFactory::GetCapMeshForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> All{GetAllCapMeshesForPole(Pole)};
	return All.Num() ? All.Last() : nullptr;
}

TArray<UStaticMesh*> FTLMeshFactory::GetAllBaseMeshesForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogTemp, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllBaseMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
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

TArray<UStaticMesh*> FTLMeshFactory::GetAllExtendibleMeshesForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> Meshes;
	UDataTable* Table{GetPoleMeshTable()};
	if (!Table)
	{
		UE_LOG(LogTemp, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllExtendibleMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
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
		UE_LOG(LogTemp, Error, TEXT("PoleMeshFactory: PoleMeshTable is null"));
		return Meshes;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		const FTLPoleRow* Row{Table->FindRow<FTLPoleRow>(RowName, TEXT("GetAllCapMeshesForPole"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
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
		UE_LOG(LogTemp, Error, TEXT("PoleMeshFactory: BackplateMeshTable is null"));
		return nullptr;
	}

	for (const FName& RowName : Table->GetRowNames())
	{
		FTLBackplateRow* Row{Table->FindRow<FTLBackplateRow>(RowName, TEXT("GetBackplateRow"))};
		if (!Row)
		{
			UE_LOG(LogTemp, Warning, TEXT("PoleMeshFactory: row '%s' not found"), *RowName.ToString());
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
	return GetBackplateRow(Head.Style) ? GetBackplateRow(Head.Style)->CornerMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateHorizontalMesh(const FTLHead& Head)
{
	return GetBackplateRow(Head.Style) ? GetBackplateRow(Head.Style)->HorizontalMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateVerticalMesh(const FTLHead& Head)
{
	return GetBackplateRow(Head.Style) ? GetBackplateRow(Head.Style)->VerticalMesh : nullptr;
}

UStaticMesh* FTLMeshFactory::GetBackplateMiddleMesh(const FTLHead& Head)
{
	return GetBackplateRow(Head.Style) ? GetBackplateRow(Head.Style)->MiddleMesh : nullptr;
}

int32 FTLMeshFactory::CountLedMaterials(UStaticMesh* Mesh)
{
	if (!IsValid(Mesh))
	{
		UE_LOG(LogTemp, Error, TEXT("CountLedSockets: Invalid Mesh"));
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

FVector2D FTLMeshFactory::GetAtlasCoordsForLightType(ETLLightType LightType)
{
	if (s_LightTypeMeshTable == nullptr)
	{
		GetLightTypeMeshTable();
		if (s_LightTypeMeshTable == nullptr)
		{
			UE_LOG(LogTemp, Error, TEXT("LightTypesTable is not set"));
			return FVector2D::ZeroVector;
		}
	}
	const UEnum* EnumPtr = StaticEnum<ETLLightType>();
	if (!EnumPtr)
	{
		return FVector2D::ZeroVector;
	}

	const FString EnumName = EnumPtr->GetNameStringByValue(static_cast<int64>(LightType));
	const FName RowName(*EnumName);

	if (const FTLLightTypeRow* Row = s_LightTypeMeshTable->FindRow<FTLLightTypeRow>(RowName, TEXT("GetAtlasCoordsForLightType")))
	{
		return Row->AtlasCoords;
	}

	return FVector2D::ZeroVector;
}
