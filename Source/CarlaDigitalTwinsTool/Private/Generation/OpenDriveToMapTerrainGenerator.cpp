// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB). This work is licensed under the terms of the MIT license. For a copy, see <https://opensource.org/licenses/MIT>.

#include "Generation/OpenDriveToMapTerrainGenerator.h"
#include "Generation/OpenDriveToMap.h"
#include "StreetMapActor.h"
#include "StreetMapComponent.h"
#include "DynamicMeshActor.h"
#include "DynamicMesh/DynamicMesh3.h"
#include "GeometryScript/MeshPrimitiveFunctions.h"
#include "GeometryScript/MeshAssetFunctions.h"
#include "GeometryScript/MeshRemeshFunctions.h"
#include "GeometryScript/MeshSubdivideFunctions.h"
#include "Paths/GenerationPathsHelper.h"
#include "Kismet/GameplayStatics.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "Misc/PackageName.h"
#include "UObject/SavePackage.h"
#include "Engine/StaticMeshActor.h"
#include "FileHelpers.h"

DEFINE_LOG_CATEGORY(LogCarlaDigitalTwinsToolTerrainGeneration);


void UOpenDriveToMapTerrainGenerator::GenerateTerrainsFromTypes(
  UOpenDriveToMap* OpenDriveToMap,
  const boost::optional<carla::road::Map>& ParamCarlaMap,
  FVector MinLocation, FVector MaxLocation, 
  const TArray<FString>& ExcludedTerrainTypes)
{
  UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Log, TEXT("Generating terrains from types..."));
  if (!ParamCarlaMap)
  {
    UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("No Carla map provided for terrain generation."));
    return;
  }

  if(!IsValid(OpenDriveToMap->StreetMapActorReference))
  {
    OpenDriveToMap->StreetMapActorReference = Cast<AStreetMapActor>(UGameplayStatics::GetActorOfClass(OpenDriveToMap->GetEditorWorld(), AStreetMapActor::StaticClass()));
  }
  if( !IsValid(OpenDriveToMap->StreetMapActorReference) )
  {
    UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("StreetMapActorReference is not valid") );
    return;
  }
  
  UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Log, TEXT("Generating numbers of terrains: %d"), OpenDriveToMap->StreetMapActorReference->GetStreetMapComponent()->GetStreetMap()->GetTerrains().Num());
  uint32 TerrainIndex = 0;
  for (const FStreetMapTerrain& CurrentTerrain : OpenDriveToMap->StreetMapActorReference->GetStreetMapComponent()->GetStreetMap()->GetTerrains() )
  {
    if( CurrentTerrain.TerrainType.IsEmpty() )
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Log, TEXT("Terrain type is empty for terrain %d"), TerrainIndex);
      ++TerrainIndex;
      continue;
    }
    FString CurrentTerrainType = CurrentTerrain.TerrainType.ToLower();
    if(ExcludedTerrainTypes.Contains(CurrentTerrainType))
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Log, TEXT("Excluded terrain type: %d"), TerrainIndex);
      ++TerrainIndex;
      continue;
    }
    TArray<FVector2D> TerrainVertices = CurrentTerrain.RoadPoints;
    if( TerrainVertices.Num() < 3 )
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Warning, TEXT("Terrain vertices are less than 3 for terrain %d"), TerrainIndex);
      ++TerrainIndex;
      continue;
    }
    FVector2D InitialVertex = TerrainVertices[0];
    bool bIsInRange = (InitialVertex.X > MinLocation.X && InitialVertex.Y < MinLocation.Y &&
      InitialVertex.X < MaxLocation.X && InitialVertex.Y > MaxLocation.Y);
    if( !bIsInRange )
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Warning, TEXT("Terrain %d is outside the specified bounds"), TerrainIndex);
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Warning, TEXT("Terrain Bounds: Min(%f, %f), Max(%f, %f)"), 
        MinLocation.X, MinLocation.Y, MaxLocation.X, MaxLocation.Y);
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Warning, TEXT("Terrain Initial Vertex: (%f, %f)"), InitialVertex.X, InitialVertex.Y);
      ++TerrainIndex;
      continue;
    }

    UDynamicMesh* DynamicMesh = NewObject<UDynamicMesh>();
    FGeometryScriptPrimitiveOptions ExtrudeOptions;
    ExtrudeOptions.bFlipOrientation = false;
    FTransform Transform;

    UGeometryScriptLibrary_MeshPrimitiveFunctions::AppendSimpleExtrudePolygon(
        DynamicMesh,
        ExtrudeOptions,
        Transform,
        CurrentTerrain.RoadPoints,
        100.0f,  // Extrude height
        1.0f,    // Wall thickness
        true
    );

    UGeometryScriptLibrary_MeshSubdivideFunctions::ApplyUniformTessellation(
      DynamicMesh,
      3,
      nullptr  // Debug
    );

    FGeometryScriptRemeshOptions RemeshOptions;
		FGeometryScriptUniformRemeshOptions UniformOptions;
    UGeometryScriptLibrary_RemeshingFunctions::ApplyUniformRemesh(
      DynamicMesh,
      RemeshOptions,  // Target edge length
      UniformOptions,  // Smoothing factor
      nullptr  // Debug
    );
    DynamicMesh->EditMesh([&,OpenDriveToMap, InitialVertex](FDynamicMesh3& Mesh)
    {
      for (int32 VertexID : Mesh.VertexIndicesItr())
      {
        FVector3d Position = Mesh.GetVertex(VertexID);

        // Assuming GetHeight(double X, double Y) is a method in your class
        double Height = OpenDriveToMap->GetHeight(Position.X, Position.Y, false);

        Mesh.SetVertex(VertexID, FVector3d(Position.X - InitialVertex.X, Position.Y - InitialVertex.Y, Height));
      }
    });
    
    
    FString AssetPath = UGenerationPathsHelper::GetMapContentDirectoryPath(OpenDriveToMap->MapName) + "Terrains/";
    FString CleanAssetPath = AssetPath;
    FString MeshName = FString::Printf(TEXT("SM_%s_%d"), *CurrentTerrain.TerrainType, TerrainIndex);
    FString PackageName = CleanAssetPath / MeshName;
    FString UniquePackageName;
    if (!FPackageName::TryConvertFilenameToLongPackageName(PackageName, UniquePackageName))
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Invalid package name: %s"), *PackageName);
      ++TerrainIndex;
      continue;
    }
    // Step 3: Create the package
    UPackage* Package = CreatePackage(*UniquePackageName);
    if (!Package)
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Failed to create package for mesh at: %s"), *UniquePackageName);
      ++TerrainIndex;
      continue;
    }

    UStaticMesh* NewStaticMesh = NewObject<UStaticMesh>(
      Package,
      FName(*MeshName),
      RF_Public | RF_Standalone
    );

    if (!NewStaticMesh)
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Failed to create StaticMesh asset %s"), *MeshName );
      ++TerrainIndex;
      continue;
    }

    FGeometryScriptCopyMeshToAssetOptions CopyOptions;
    FGeometryScriptMeshWriteLOD TargetLOD;
    TargetLOD.LODIndex = 0;

    EGeometryScriptOutcomePins Outcome;
    UGeometryScriptLibrary_StaticMeshFunctions::CopyMeshToStaticMesh(
      DynamicMesh,
      NewStaticMesh,
      CopyOptions,
      TargetLOD,
      Outcome,
      nullptr  // Debug
    );

    if (Outcome != EGeometryScriptOutcomePins::Success)
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Failed to copy mesh into StaticMesh %s"), *MeshName);
      ++TerrainIndex;
      continue;
    }

    if (!NewStaticMesh)
    {
        UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Failed to create StaticMesh from DynamicMesh %s"), *MeshName);
        ++TerrainIndex;
        continue;
    }

    // Step 5: Register and save asset
    FAssetRegistryModule::AssetCreated(NewStaticMesh);
    Package->MarkPackageDirty();

    FString PackageFileName = FPackageName::LongPackageNameToFilename(UniquePackageName, FPackageName::GetAssetPackageExtension());
    FSavePackageArgs SaveArgs;
    SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
    SaveArgs.SaveFlags = SAVE_None;
    UPackage::SavePackage(Package, NewStaticMesh, *PackageFileName, SaveArgs);


    AStaticMeshActor* NewActor = OpenDriveToMap->GetEditorWorld()->SpawnActor<AStaticMeshActor>();
    if (!NewActor)
    {
      UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Error, TEXT("Failed to spawn StaticMeshActor for terrain %d"), TerrainIndex);
      ++TerrainIndex;
      continue;
    }
    NewActor->SetActorLabel(FString::Printf(TEXT("Terrain_%s_%d"), *CurrentTerrain.TerrainType, TerrainIndex));
    NewActor->GetStaticMeshComponent()->SetStaticMesh(NewStaticMesh);
    NewActor->SetActorLocation(FVector(InitialVertex.X, InitialVertex.Y, 0.0f));
  }
  UEditorLoadingAndSavingUtils::SaveDirtyPackages(true, true);
  UE_LOG(LogCarlaDigitalTwinsToolTerrainGeneration, Log, TEXT("Terrain generation completed."));
}
