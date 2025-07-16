// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

// Engine headers
#include "Components/SceneComponent.h"
#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MeshDescription.h"
#include "ProceduralMeshComponent.h"
// Carla C++ headers

// Carla plugin headers
#include "Actor/ProceduralCustomMesh.h"

#include "MapGenFunctionLibrary.generated.h"


DECLARE_LOG_CATEGORY_EXTERN(LogCarlaMapGenFunctionLibrary, Log, All);

UCLASS(BlueprintType)
class CARLAMESHGENERATION_API UMapGenFunctionLibrary : public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public:
  UFUNCTION(BlueprintCallable)
  static UStaticMesh* CreateMesh(
      const FProceduralCustomMesh& Data,
      const TArray<FProcMeshTangent>& ParamTangents,
      UMaterialInstance* MaterialInstance,
      FString MapName,
      FString FolderName,
      FName MeshName);

  static FMeshDescription BuildMeshDescriptionFromData(
      const FProceduralCustomMesh& Data,
      const TArray<FProcMeshTangent>& ParamTangents,
      UMaterialInstance* MaterialInstance );

  UFUNCTION(BlueprintCallable)
  static FVector2D GetTransversemercProjection(float lat, float lon, float lat0, float lon0);

  UFUNCTION(BlueprintCallable)
  static FVector2D InverseTransverseMercatorProjection(float x, float y, float lat0, float lon0);

  UFUNCTION(BlueprintCallable)
  static void SetThreadToSleep(float seconds);

  UFUNCTION(BlueprintCallable)
  static void FlushRenderingCommandsInBlueprint();

  UFUNCTION(BlueprintCallable)
  static void CleanupGEngine();

  UFUNCTION(BlueprintCallable)
  static UInstancedStaticMeshComponent* AddInstancedStaticMeshComponentToActor(AActor* TargetActor);

  UFUNCTION(BlueprintCallable)
  static UStaticMeshComponent* AddStaticMeshComponentToActor(AActor* TargetActor);

  UFUNCTION(BlueprintCallable)
  static USceneComponent* AddSceneComponentToActor(AActor* TargetActor);

  UFUNCTION(BlueprintCallable)
  static void SmoothVerticesDeep(
    TArray<FVector>& Vertices,
    const TArray<int32>& Indices,
    int Depth = 3,                 // Number of neighbor levels
    int NumIterations = 1,
    float SmoothingFactor = 1.0f   // Blend between original and averaged
  );

  static uint16 GetPixelG16(const TArrayView64<const uint16>& Pixels, int Width, int Height, int X, int Y)
  {
    X = FMath::Clamp(X, 0, Width - 1);
    Y = FMath::Clamp(Y, 0, Height - 1);
    return Pixels[Y * Width + X];
  }

  static float CubicHermite(float A, float B, float C, float D, float T)
  {
    float a = -0.5f*A + 1.5f*B - 1.5f*C + 0.5f*D;
    float b = A - 2.5f*B + 2.0f*C - 0.5f*D;
    float c = -0.5f*A + 0.5f*C;
    float d = B;
    return ((a * T + b) * T + c) * T + d;
  }

  static float BicubicSampleG16(const TArrayView64<const uint16>& Pixels, int Width, int Height, float X, float Y);
};
