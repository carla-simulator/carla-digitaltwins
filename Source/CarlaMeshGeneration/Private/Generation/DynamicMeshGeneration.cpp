// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Generation/DynamicMeshGeneration.h"

// Engine headers
#include "AssetRegistry/AssetRegistryModule.h"
#include "Materials/MaterialInstance.h"
#include "StaticMeshAttributes.h"
#include "RenderingThread.h"


// Carla C++ headers

// Carla plugin headers
#include "CarlaMeshGeneration.h"
#include "Paths/GenerationPathsHelper.h"

DEFINE_LOG_CATEGORY(LogCarlaDynamicMeshGeneration);
static const float OSMToCentimetersScaleFactor = 100.0f;

UStaticMesh* UDynamicMeshGeneration::CreateMeshFromPoints(
      TArray<FVector2D> Points,
      FName MeshName)
{
  //UDynamicMesh* DynamicMesh = NewObject<UDynamicMesh>();
  //FGeometryScriptPrimitiveOptions  Options;
  //DynamicMesh = UGeometryScriptLibrary_MeshPrimitiveFunctions::AppendSimpleExtrudePolygon(DynamicMesh, Options, FTransform(), Points,0,0, false);
  return nullptr;
}
