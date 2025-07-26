// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "Engine/DataTable.h"
#include "Engine/StaticMesh.h"
#include "TrafficLights/TLBackplateDataTable.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLLightType.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"

class FTLMeshFactory
{
public:
	static UStaticMesh* GetMeshForModule(const FTLHead& Head, const FTLModule& Module);
	static TArray<UStaticMesh*> GetAllMeshesForModule(const FTLHead& Head, const FTLModule& Module);
	static UStaticMesh* GetBaseMeshForPole(const FTLPole& Pole);
	static UStaticMesh* GetExtensibleMeshForPole(const FTLPole& Pole);
	static UStaticMesh* GetCapMeshForPole(const FTLPole& Pole);
	static TArray<UStaticMesh*> GetAllBaseMeshesForPole(const FTLPole& Pole);
	static TArray<UStaticMesh*> GetAllExtensibleMeshesForPole(const FTLPole& Pole);
	static TArray<UStaticMesh*> GetAllCapMeshesForPole(const FTLPole& Pole);
	static UStaticMesh* GetBackplateCornerMesh(const FTLHead& Head);
	static UStaticMesh* GetBackplateHorizontalMesh(const FTLHead& Head);
	static UStaticMesh* GetBackplateVerticalMesh(const FTLHead& Head);
	static UStaticMesh* GetBackplateMiddleMesh(const FTLHead& Head);

	static UDataTable* GetModuleMeshTable();
	static UDataTable* GetLightTypeMeshTable();
	static UDataTable* GetPoleMeshTable();
	static UDataTable* GetBackplateMeshTable();

	static FVector2D GetAtlasCoordsForLightType(ETLLightType LightType);
	static int32 CountLedMaterials(UStaticMesh* Mesh);

private:
	static FTLBackplateRow* GetBackplateRow(ETLStyle Style);

private:
	static UDataTable* s_LightTypeMeshTable;
	static UDataTable* s_ModuleMeshTable;
	static UDataTable* s_PoleMeshTable;
	static UDataTable* s_BackplateMeshTable;
};
