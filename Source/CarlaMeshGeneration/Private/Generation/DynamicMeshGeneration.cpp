// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Generation/DynamicMeshGeneration.h"
#include "GeometryScript/MeshPrimitiveFunctions.h"
#include "UDynamicMesh.h"

DEFINE_LOG_CATEGORY(LogCarlaDynamicMeshGeneration);

static const float OSMToCentimetersScaleFactor = 100.0f;

UStaticMesh* UDynamicMeshGeneration::CreateMeshFromPoints(
  const TArray<FVector2D>& Points,
  FTransform Transform,
  FName MeshName)
{
  auto Mesh = NewObject<UDynamicMesh>();
  FGeometryScriptPrimitiveOptions Options;
  UGeometryScriptLibrary_MeshPrimitiveFunctions::AppendSimpleExtrudePolygon(
    Mesh,
    Options,
    Transform,
    Points,
    0, 0, false);
  return nullptr;
}
