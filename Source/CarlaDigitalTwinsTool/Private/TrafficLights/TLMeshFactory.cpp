#include "TrafficLights/TLMeshFactory.h"

#include "Components/StaticMeshComponent.h"
#include "Logging/LogVerbosity.h"
#include "TrafficLights/TLBackplateDataTable.h"
#include "TrafficLights/TLModuleDataTable.h"
#include "TrafficLights/TLPoleDataTable.h"
#include "UObject/NameTypes.h"

UDataTable* FTLMeshFactory::ModuleMeshTable{nullptr};
UDataTable* FTLMeshFactory::PoleMeshTable{nullptr};
UDataTable* FTLMeshFactory::LightTypeMeshTable{nullptr};
UDataTable* FTLMeshFactory::BackplateMeshTable{nullptr};

UDataTable* FTLMeshFactory::GetLightTypeMeshTable()
{
	if (!LightTypeMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/Game/Carla/Static/TrafficLight/TrafficLights2025/"
				 "DataTables/LightTypes.LightTypes")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		LightTypeMeshTable = Cast<UDataTable>(Loaded);
	}
	return LightTypeMeshTable;
}

UDataTable* FTLMeshFactory::GetModuleMeshTable()
{
	if (!ModuleMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/Game/Carla/Static/TrafficLight/TrafficLights2025/DataTables/"
				 "Modules.Modules")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		ModuleMeshTable = Cast<UDataTable>(Loaded);
	}
	return ModuleMeshTable;
}

UDataTable* FTLMeshFactory::GetPoleMeshTable()
{
	if (!PoleMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/Game/Carla/Static/TrafficLight/TrafficLights2025/DataTables/"
				 "Poles.Poles")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		PoleMeshTable = Cast<UDataTable>(Loaded);
	}
	return PoleMeshTable;
}

UDataTable* FTLMeshFactory::GetBackplateMeshTable()
{
	if (!BackplateMeshTable)
	{
		constexpr TCHAR const* Path{
			TEXT("/Game/Carla/Static/TrafficLight/TrafficLights2025/DataTables/"
				 "Backplates.Backplates")};
		UObject* Loaded = StaticLoadObject(UDataTable::StaticClass(), nullptr, Path);
		BackplateMeshTable = Cast<UDataTable>(Loaded);
	}
	return BackplateMeshTable;
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
	return All.Num() ? All[0] : nullptr;
}

UStaticMesh* FTLMeshFactory::GetExtendibleMeshForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> All{GetAllExtendibleMeshesForPole(Pole)};
	return All.Num() ? All[0] : nullptr;
}

UStaticMesh* FTLMeshFactory::GetCapMeshForPole(const FTLPole& Pole)
{
	TArray<UStaticMesh*> All{GetAllCapMeshesForPole(Pole)};
	return All.Num() ? All[0] : nullptr;
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

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && IsValid(Row->BaseMesh))
		{
			Meshes.Add(Row->BaseMesh);
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

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && IsValid(Row->ExtendibleMesh))
		{
			Meshes.Add(Row->ExtendibleMesh);
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

		if (Row->Style == Pole.Style && Row->Orientation == Pole.Orientation && IsValid(Row->CapMesh))
		{
			Meshes.Add(Row->CapMesh);
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
