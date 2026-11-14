// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB). This work is licensed under the
// terms of the MIT license. For a copy, see <https://opensource.org/licenses/MIT>.

#include "Generation/OpenDriveToMap.h"

#include "Carla/Geom/BoundingBox.h"
#include "Carla/Geom/Location.h"
#include "Carla/Geom/Math.h"
#include "Carla/Geom/Transform.h"
#include "Carla/Road/Junction.h"
#include "Carla/Road/Road.h"
#include "Carla/Road/RoadTypes.h"
#include "Carla/Road/element/RoadWaypoint.h"
#include "Containers/ContainersFwd.h"
#include "CoreGlobals.h"
#include "EngineUtils.h"
#include "Logging/LogMacros.h"
#include "Logging/LogVerbosity.h"
#include "Math/MathFwd.h"
#include "TrafficLights/TLModule.h"
#if ENGINE_MAJOR_VERSION < 5
#include "DesktopPlatform/Public/DesktopPlatformModule.h"
#include "DesktopPlatform/Public/IDesktopPlatform.h"
#else
#include "DesktopPlatformModule.h"
#include "IDesktopPlatform.h"
#endif
#include "Engine/LevelBounds.h"
#include "Engine/SceneCapture2D.h"
#include "FileHelpers.h"
#include "Kismet/KismetRenderingLibrary.h"
#include "KismetProceduralMeshLibrary.h"
#include "Misc/FileHelper.h"
#include "Online/CustomFileDownloader.h"
#include "Runtime/Core/Public/Async/ParallelFor.h"
#include "StaticMeshAttributes.h"
#if ENGINE_MAJOR_VERSION < 5
#include "Engine/Classes/Interfaces/Interface_CollisionDataProvider.h"
#endif
#include "Engine/AssetManager.h"
#include "Engine/TriggerBox.h"
#include "Factories/MaterialInstanceConstantFactoryNew.h"
#if ENGINE_MAJOR_VERSION < 5
#include "PhysicsCore/Public/BodySetupEnums.h"
#endif
#include "PhysicsEngine/BodySetup.h"
#include "RawMesh.h"
#if ENGINE_MAJOR_VERSION < 5
#include "AssetRegistryModule.h"
#endif
#include "EditorLevelLibrary.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "MeshDescription.h"
#include "ProceduralMeshConversion.h"
#if ENGINE_MAJOR_VERSION > 4
#include "Editor/Transactor.h"
#include "LevelEditorSubsystem.h"
#include "Subsystems/UnrealEditorSubsystem.h"
#include "WorldPartition/WorldPartitionSubsystem.h"
#endif
#include "Actor/LargeMapManager.h"
#include "Actor/ProceduralCustomMesh.h"
#include "BlueprintUtil/BlueprintUtilFunctions.h"
#include "Carla/Geom/Deformation.h"
#include "Carla/Geom/Simplification.h"
#include "Carla/OpenDrive/OpenDriveParser.h"
#include "Carla/RPC/String.h"
#include "Carla/Road/element/RoadInfoSignal.h"
#include "CarlaDigitalTwinsTool.h"
#include "ContentBrowserModule.h"
#include "DrawDebugHelpers.h"
#include "EditorLevelUtils.h"
#include "GameFramework/Actor.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "Kismet/GameplayStatics.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Math/Vector.h"
#include "Paths/GenerationPathsHelper.h"
#include "StreetMapActor.h"

#if WITH_EDITOR
#include "Carla/Road/Controller.h"
#include "Carla/Road/InformationSet.h"
#include "Carla/Road/Signal.h"
#include "Carla/Road/SignalType.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLPole.h"
#include "TrafficLights/TrafficLightActor.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Components/SplineComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Editor.h"
#include "Engine/Engine.h"
#include "Engine/Texture2D.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Engine/World.h"
#include "HAL/PlatformFileManager.h"
#include "HighResScreenshot.h"
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "IMeshMergeUtilities.h"
#include "ImageUtils.h"
#include "Interfaces/IPluginManager.h"
#include "Materials/MaterialInterface.h"
#include "MeshMergeModule.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Misc/ScopeExit.h"
#include "Modules/ModuleManager.h"
#include "RHICommandList.h"
#include "RenderUtils.h"
#include "Runtime/ImageWriteQueue/Public/ImagePixelData.h"
#include "Runtime/ImageWriteQueue/Public/ImageWriteQueue.h"
#include "Runtime/ImageWriteQueue/Public/ImageWriteTask.h"
#include "UObject/ConstructorHelpers.h"
#include "UObject/GCObjectScopeGuard.h"
#include "UObject/SoftObjectPath.h"
#include "UObject/StrongObjectPtr.h"
#include "Utils/GeometryImporter.h"

namespace
{
void SaveCurrentEditorLevel()
{
#if ENGINE_MAJOR_VERSION >= 5
	if (GEditor)
	{
		if (ULevelEditorSubsystem* LevelEditorSubsystem = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>())
		{
			LevelEditorSubsystem->SaveCurrentLevel();
		}
	}
#else
	UEditorLevelLibrary::SaveCurrentLevel();
#endif
}

void LoadEditorLevel(const FString& LevelPath)
{
#if ENGINE_MAJOR_VERSION >= 5
	if (GEditor)
	{
		if (ULevelEditorSubsystem* LevelEditorSubsystem = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>())
		{
			LevelEditorSubsystem->LoadLevel(LevelPath);
		}
	}
#else
	UEditorLevelLibrary::LoadLevel(LevelPath);
#endif
}

void SaveGeneratedPackagesHeadlessSafe(UWorld* World, bool bSaveLevel = true)
{
	const bool bHasPersistentMapPackage =
		bSaveLevel && World && World->GetOutermost() &&
		!World->GetOutermost()->GetName().StartsWith(TEXT("/Temp/"));
	FEditorFileUtils::SaveDirtyPackages(
		/*bPromptUserToSave=*/false,
		/*bSaveMapPackages=*/bHasPersistentMapPackage,
		/*bSaveContentPackages=*/true,
		/*bFastSave=*/false,
		/*bNotifyNoPackagesSaved=*/false,
		/*bCanBeDeclined=*/false);
	if (bHasPersistentMapPackage)
	{
		SaveCurrentEditorLevel();
	}
}
} // namespace

struct FTerrainMeshData
{
	int32 MeshIndex = 0;
	FVector2D Offset = FVector2D::ZeroVector;
	TArray<FVector> Vertices;
	TArray<int32> Triangles;
	TArray<FVector2D> UVs;
	TArray<FVector> Normals;
	TArray<FProcMeshTangent> Tangents;
};

UOpenDriveToMap::UOpenDriveToMap()
{
	bRoadsFinished = false;
	bHasStarted = false;
	bMapLoaded = false;
#ifdef _WIN32
	PythonBinPath = TEXT("python");
#else
	PythonBinPath = TEXT("/usr/bin/python3");
#endif
}

UOpenDriveToMap::~UOpenDriveToMap()
{
}

FString LaneTypeToFString(carla::road::Lane::LaneType LaneType)
{
	switch (LaneType)
	{
		case carla::road::Lane::LaneType::Driving:
			return FString("Driving");
		case carla::road::Lane::LaneType::Stop:
			return FString("Stop");
		case carla::road::Lane::LaneType::Shoulder:
			return FString("Shoulder");
		case carla::road::Lane::LaneType::Biking:
			return FString("Biking");
		case carla::road::Lane::LaneType::Sidewalk:
			return FString("Sidewalk");
		case carla::road::Lane::LaneType::Border:
			return FString("Border");
		case carla::road::Lane::LaneType::Restricted:
			return FString("Restricted");
		case carla::road::Lane::LaneType::Parking:
			return FString("Parking");
		case carla::road::Lane::LaneType::Bidirectional:
			return FString("Bidirectional");
		case carla::road::Lane::LaneType::Median:
			return FString("Median");
		case carla::road::Lane::LaneType::Special1:
			return FString("Special1");
		case carla::road::Lane::LaneType::Special2:
			return FString("Special2");
		case carla::road::Lane::LaneType::Special3:
			return FString("Special3");
		case carla::road::Lane::LaneType::RoadWorks:
			return FString("RoadWorks");
		case carla::road::Lane::LaneType::Tram:
			return FString("Tram");
		case carla::road::Lane::LaneType::Rail:
			return FString("Rail");
		case carla::road::Lane::LaneType::Entry:
			return FString("Entry");
		case carla::road::Lane::LaneType::Exit:
			return FString("Exit");
		case carla::road::Lane::LaneType::OffRamp:
			return FString("OffRamp");
		case carla::road::Lane::LaneType::OnRamp:
			return FString("OnRamp");
		case carla::road::Lane::LaneType::Any:
			return FString("Any");
		default:
			break;
	}

	return FString("Empty");
}

void UOpenDriveToMap::ConvertOSMInOpenDrive()
{
	ON_SCOPE_EXIT
	{
		if (IsRooted() && !HasAnyFlags(RF_ClassDefaultObject))
		{
			RemoveFromRoot();
		}
	};

	FGCObjectScopeGuard GCGuard(this);

	if (!IsValid(FileDownloader))
	{
		FileDownloader = NewObject<UCustomFileDownloader>(this);
	}

	FilePath = FPaths::Combine(
		UGenerationPathsHelper::GetRawMapDirectoryPath(MapName),
		TEXT("OpenDrive"),
		MapName + TEXT(".osm"));
	FileDownloader->ConvertOSMInOpenDrive(FilePath, OriginGeoCoordinates.X, OriginGeoCoordinates.Y, OpenDriveGenParams);
	FilePath.RemoveFromEnd(TEXT(".osm"), ESearchCase::Type::IgnoreCase);
	FilePath += TEXT(".xodr");

	if (!FPaths::FileExists(FilePath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ConvertOSMInOpenDrive: Expected OpenDRIVE output not found at %s"), *FilePath);
		return;
	}

	DownloadFinished();
	SaveGeneratedPackagesHeadlessSafe(GetEditorWorld(), false);
	LoadMap();
}

void UOpenDriveToMap::CreateMap()
{
	if (MapName.IsEmpty())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Map Name Is Empty"));
		return;
	}

	if (!Url.IsEmpty())
	{
		if (!IsValid(FileDownloader))
		{
			FileDownloader = NewObject<UCustomFileDownloader>(this);
		}

		FileDownloader->ResultFileName = MapName;
		FileDownloader->Url = Url;

		if (!HasAnyFlags(RF_ClassDefaultObject) && !IsRooted())
		{
			AddToRoot();
		}
		FileDownloader->DownloadDelegate.BindUObject(this, &UOpenDriveToMap::ConvertOSMInOpenDrive);
		FileDownloader->StartDownload();
	}
	else if (LocalFilePath.EndsWith(TEXT(".xodr"), ESearchCase::IgnoreCase))
	{
		ImportXODR();
	}
	else if (LocalFilePath.EndsWith(TEXT(".osm"), ESearchCase::IgnoreCase))
	{
		ImportOSM();
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("URL and Local FilePath are Empty or unsupported. URL: %s  Local FilePath: %s"), *Url,
			*LocalFilePath);
	}
}

void UOpenDriveToMap::CreateTerrain(const int NumberOfTerrainX, const int NumberOfTerrainY, const float MeshGridResolution)
{
	const int32 GridRes = FMath::RoundToInt(MeshGridResolution);
	if (NumberOfTerrainX <= 0 || NumberOfTerrainY <= 0 || GridRes <= 0)
	{
		return;
	}

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	const float TileSizeX = TileSize / static_cast<float>(NumberOfTerrainX);
	const float TileSizeY = TileSize / static_cast<float>(NumberOfTerrainY);
	const FVector MinBox(MinPosition.X, MaxPosition.Y, 0.0f);

	const int32 TotalTiles = NumberOfTerrainX * NumberOfTerrainY;
	TArray<FTerrainMeshData> AllMeshData;
	AllMeshData.SetNum(TotalTiles);

	ParallelFor(TotalTiles, [&](int32 Index)
	{
		const int32 x = Index % NumberOfTerrainX;
		const int32 y = Index / NumberOfTerrainX;

		const FVector2D Offset(MinBox.X + x * TileSizeX, MinBox.Y + y * TileSizeY);

		FTerrainMeshData& MeshData = AllMeshData[Index];
		MeshData.MeshIndex = Index;
		MeshData.Offset = Offset;

		const int32 VertsX = GridRes + 1;
		const int32 VertsY = GridRes + 1;
		const float StepX = TileSizeX / static_cast<float>(GridRes);
		const float StepY = TileSizeY / static_cast<float>(GridRes);

		TArray<FVector>& Vertices = MeshData.Vertices;
		TArray<int32>& Triangles = MeshData.Triangles;
		TArray<FVector2D>& UVs = MeshData.UVs;

		Vertices.Reserve(VertsX * VertsY);
		UVs.Reserve(VertsX * VertsY);
		Triangles.Reserve((VertsX - 1) * (VertsY - 1) * 6);

		for (int32 iy = 0; iy < VertsY; ++iy)
		{
			const float Y = iy * StepY;
			const float VCoord = static_cast<float>(iy) / static_cast<float>(GridRes);
			for (int32 ix = 0; ix < VertsX; ++ix)
			{
				const float X = ix * StepX;
				const float Height = GetHeightForLandscape(FVector(Offset.X + X, Offset.Y + Y, 0.0f));
				Vertices.Add(FVector(X, Y, Height));
				UVs.Add(FVector2D(static_cast<float>(ix) / static_cast<float>(GridRes), VCoord));
			}
		}

		for (int32 iy = 0; iy < VertsY - 1; ++iy)
		{
			for (int32 ix = 0; ix < VertsX - 1; ++ix)
			{
				const int32 i0 = ix + iy * VertsX;
				const int32 i1 = (ix + 1) + iy * VertsX;
				const int32 i2 = ix + (iy + 1) * VertsX;
				const int32 i3 = (ix + 1) + (iy + 1) * VertsX;

				Triangles.Add(i0);
				Triangles.Add(i2);
				Triangles.Add(i1);

				Triangles.Add(i3);
				Triangles.Add(i1);
				Triangles.Add(i2);
			}
		}
	});

	UMaterialInstance* DuplicatedLandscapeMaterial = DefaultLandscapeMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultLandscapeMaterial, MapName))
		: nullptr;
	const FString TileSuffix = GetStringForCurrentTile();

	for (FTerrainMeshData& MeshData : AllMeshData)
	{
		TArray<FVector> Normals;
		TArray<FProcMeshTangent> Tangents;

		UKismetProceduralMeshLibrary::CalculateTangentsForMesh(
			MeshData.Vertices, MeshData.Triangles, MeshData.UVs, Normals, Tangents);

		FProceduralCustomMesh ProcMeshData;
		ProcMeshData.Vertices = MoveTemp(MeshData.Vertices);
		ProcMeshData.Triangles = MoveTemp(MeshData.Triangles);
		ProcMeshData.Normals = MoveTemp(Normals);
		ProcMeshData.UV0 = MoveTemp(MeshData.UVs);

		UStaticMesh* StaticMesh = UMapGenFunctionLibrary::CreateMesh(ProcMeshData, Tangents, DuplicatedLandscapeMaterial, MapName,
			"Terrain", FName(*FString::Printf(TEXT("SM_LandscapeMesh_%d%s"), MeshData.MeshIndex, *TileSuffix)));

		if (!StaticMesh)
		{
			continue;
		}

		AStaticMeshActor* Actor = World->SpawnActor<AStaticMeshActor>(
			AStaticMeshActor::StaticClass(), FVector(MeshData.Offset.X, MeshData.Offset.Y, 0.0f), FRotator::ZeroRotator);
		if (!Actor)
		{
			continue;
		}

		UStaticMeshComponent* MeshComp = Actor->GetStaticMeshComponent();
		if (!MeshComp)
		{
			continue;
		}

		MeshComp->SetStaticMesh(StaticMesh);
		MeshComp->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
		MeshComp->SetCollisionObjectType(ECC_WorldStatic);
		MeshComp->SetCollisionResponseToAllChannels(ECR_Block);
		MeshComp->SetMobility(EComponentMobility::Static);

		MeshComp->SetGenerateOverlapEvents(true);
		MeshComp->bReturnMaterialOnMove = true;
		Actor->SetActorLabel(FString::Printf(TEXT("LandscapeActor_%d%s"), MeshData.MeshIndex, *TileSuffix));
		Actor->Tags.Add("LandscapeToMove");
		MeshComp->CastShadow = false;

#if ENGINE_MAJOR_VERSION > 4
		Actor->SetIsSpatiallyLoaded(true);
#endif
		Landscapes.Add(Actor);
	}
}

void UOpenDriveToMap::CreateTerrainMesh(
	const int MeshIndex, const FVector2D Offset, const int TileSizeX, const int TileSizeY, const float MeshResolution)
{
	const int32 GridRes = FMath::RoundToInt(MeshResolution);
	if (GridRes <= 0 || TileSizeX <= 0 || TileSizeY <= 0)
	{
		return;
	}

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	AStaticMeshActor* MeshActor = World->SpawnActor<AStaticMeshActor>();
	if (!MeshActor)
	{
		return;
	}
	MeshActor->SetActorLocation(FVector(Offset.X, Offset.Y, 0.0f));
	UStaticMeshComponent* Mesh = MeshActor->GetStaticMeshComponent();
	if (!Mesh)
	{
		MeshActor->Destroy();
		return;
	}

	const int32 VerticesInLineX = GridRes + 1;
	const int32 VerticesInLineY = GridRes + 1;
	const float SpaceBetweenVerticesX = static_cast<float>(TileSizeX) / static_cast<float>(GridRes);
	const float SpaceBetweenVerticesY = static_cast<float>(TileSizeY) / static_cast<float>(GridRes);

	TArray<FVector> Vertices;
	TArray<int32> Triangles;
	TArray<FVector> Normals;
	TArray<FProcMeshTangent> Tangents;
	TArray<FVector2D> UVs;

	Vertices.Reserve(VerticesInLineX * VerticesInLineY);
	UVs.Reserve(VerticesInLineX * VerticesInLineY);
	Triangles.Reserve((VerticesInLineX - 1) * (VerticesInLineY - 1) * 6);

	for (int32 i = 0; i < VerticesInLineX; i++)
	{
		const float X = i * SpaceBetweenVerticesX;
		for (int32 j = 0; j < VerticesInLineY; j++)
		{
			const float Y = j * SpaceBetweenVerticesY;
			const float HeightValue = GetHeightForLandscape(FVector(Offset.X + X, Offset.Y + Y, 0.0f));
			Vertices.Add(FVector(X, Y, HeightValue));
			UVs.Add(FVector2D(i, j));
		}
	}

	for (int32 i = 0; i < VerticesInLineX - 1; i++)
	{
		for (int32 j = 0; j < VerticesInLineY - 1; j++)
		{
			Triangles.Add(j + (i * VerticesInLineY));
			Triangles.Add((j + 1) + (i * VerticesInLineY));
			Triangles.Add(j + ((i + 1) * VerticesInLineY));

			Triangles.Add((j + 1) + (i * VerticesInLineY));
			Triangles.Add((j + 1) + ((i + 1) * VerticesInLineY));
			Triangles.Add(j + ((i + 1) * VerticesInLineY));
		}
	}

	UKismetProceduralMeshLibrary::CalculateTangentsForMesh(Vertices, Triangles, UVs, Normals, Tangents);

	FProceduralCustomMesh MeshData;
	MeshData.Vertices = MoveTemp(Vertices);
	MeshData.Triangles = MoveTemp(Triangles);
	MeshData.Normals = MoveTemp(Normals);
	MeshData.UV0 = MoveTemp(UVs);

	UMaterialInstance* DuplicatedLandscapeMaterial = DefaultLandscapeMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultLandscapeMaterial, MapName))
		: nullptr;

	const FString TileSuffix = GetStringForCurrentTile();
	UStaticMesh* MeshToSet = UMapGenFunctionLibrary::CreateMesh(MeshData, Tangents, DuplicatedLandscapeMaterial, MapName, "Terrain",
		FName(*FString::Printf(TEXT("SM_LandscapeMesh%d%s"), MeshIndex, *TileSuffix)));
	Mesh->SetStaticMesh(MeshToSet);
	Mesh->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
	Mesh->SetCollisionObjectType(ECC_WorldStatic);
	Mesh->SetCollisionResponseToAllChannels(ECR_Block);
	Mesh->SetMobility(EComponentMobility::Static);
	Mesh->SetGenerateOverlapEvents(true);
	Mesh->bReturnMaterialOnMove = true;

	MeshActor->SetActorLabel(FString::Printf(TEXT("SM_LandscapeActor%d%s"), MeshIndex, *TileSuffix));
	MeshActor->Tags.Add(FName("LandscapeToMove"));
#if ENGINE_MAJOR_VERSION > 4
	MeshActor->SetIsSpatiallyLoaded(true);
#endif
	Mesh->CastShadow = false;
	Landscapes.Add(MeshActor);
}

AActor* UOpenDriveToMap::SpawnActorWithCheckNoCollisions(UClass* ActorClassToSpawn, FTransform Transform)
{
	UWorld* World = GetEditorWorld();
	if (!World || !ActorClassToSpawn)
	{
		return nullptr;
	}
	FActorSpawnParameters SpawnParameters;
	SpawnParameters.bNoFail = true;
	SpawnParameters.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;

	return World->SpawnActor<AActor>(ActorClassToSpawn, Transform, SpawnParameters);
}

void UOpenDriveToMap::GenerateTileStandalone()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateTileStandalone Function called"));

	GenerateTile();
	SaveGeneratedPackagesHeadlessSafe(GetEditorWorld(), true);
}

void UOpenDriveToMap::GenerateTile()
{
	FGCObjectScopeGuard GCGuard(this);

	if (FilePath.IsEmpty())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("UOpenDriveToMap::GenerateTile(): Empty FilePath"));
		return;
	}

	if (!CarlaMap.has_value())
	{
		FString FileContent;
		if (!FFileHelper::LoadFileToString(FileContent, *FilePath))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("UOpenDriveToMap::GenerateTile(): Failed to load %s"), *FilePath);
			return;
		}
		std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("UOpenDriveToMap::GenerateTile() Loading File..... "));
		CarlaMap = carla::opendrive::OpenDriveParser::Load(opendrive_xml);
	}

	if (!CarlaMap.has_value())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Invalid Map"));
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Valid Map loaded"));
		MapName = FPaths::GetCleanFilename(FilePath);
		MapName.RemoveFromEnd(".xodr", ESearchCase::Type::IgnoreCase);
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("MapName %s"), *MapName);

#if ENGINE_MAJOR_VERSION < 5
		UEditorLevelLibrary::LoadLevel(*BaseLevelName);
		AActor* QueryActor = UGameplayStatics::GetActorOfClass(GetEditorWorld(), ALargeMapManager::StaticClass());
		if (QueryActor != nullptr)
		{
			ALargeMapManager* LmManager = Cast<ALargeMapManager>(QueryActor);
			LmManager->GenerateMap_Editor();
			NumTilesInXY = LmManager->GetNumTilesInXY();
			TileSize = LmManager->GetTileSize();
			Tile0Offset = LmManager->GetTile0Offset();
			UEditorLevelLibrary::SaveCurrentLevel();
			FCarlaMapTile& CarlaTile = LmManager->GetCarlaMapTile(CurrentTilesInXY);

			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Current Tile is %s"), *(CurrentTilesInXY.ToString()));
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("NumTilesInXY is %s"), *(NumTilesInXY.ToString()));
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("TileSize is %f"), (TileSize));
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Tile0Offset is %s"), *(Tile0Offset.ToString()));
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Tile Name is %s"), *(CarlaTile.Name));

			UEditorLevelLibrary::LoadLevel(CarlaTile.Name);
#endif
			WorldOriginPosition = FVector(0, 0, 0);
			WorldEndPosition = FVector(UMapGenFunctionLibrary::GetTransversemercProjection(FinalGeoCoordinates.X,
										   FinalGeoCoordinates.Y, OriginGeoCoordinates.X, OriginGeoCoordinates.Y),
				0);

			if (TileSize <= KINDA_SMALL_NUMBER)
			{
				const float ProjectedSpan = FMath::Max(FMath::Abs(WorldEndPosition.X), FMath::Abs(WorldEndPosition.Y));
				TileSize = (ProjectedSpan > KINDA_SMALL_NUMBER) ? ProjectedSpan : 200000.0f;
			}
			MinPosition = FVector(CurrentTilesInXY.X * TileSize, CurrentTilesInXY.Y * -TileSize, 0.0f);
			MaxPosition = FVector((CurrentTilesInXY.X + 1.0f) * TileSize, (CurrentTilesInXY.Y + 1.0f) * -TileSize, 0.0f);

			GenerateAll(CarlaMap, MinPosition, MaxPosition);

			bHasStarted = true;
			bRoadsFinished = true;
			bMapLoaded = true;
			bTileFinished = false;
#if ENGINE_MAJOR_VERSION < 5
		}
		else
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Largemapmanager not found "));
		}
#endif
		SaveGeneratedPackagesHeadlessSafe(GetEditorWorld(), true);
	}
}

bool UOpenDriveToMap::GoNextTile()
{
	if (NumTilesInXY.X <= 0 || NumTilesInXY.Y <= 0)
	{
		return false;
	}
	CurrentTilesInXY.X++;
	if (CurrentTilesInXY.X >= NumTilesInXY.X)
	{
		CurrentTilesInXY.X = 0;
		CurrentTilesInXY.Y++;
		if (CurrentTilesInXY.Y >= NumTilesInXY.Y)
		{
			return false;
		}
	}
	return true;
}

void UOpenDriveToMap::ReturnToMainLevel()
{
	FEditorFileUtils::SaveDirtyPackages(false, true, true, false, false, false, nullptr);
	LoadEditorLevel(BaseLevelName);
}

void UOpenDriveToMap::CorrectPositionForAllActorsInCurrentTile()
{
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}
	TArray<AActor*> FoundActors;
	UGameplayStatics::GetAllActorsOfClass(World, AActor::StaticClass(), FoundActors);
	for (AActor* Current : FoundActors)
	{
		if (!IsValid(Current))
		{
			continue;
		}
		Current->AddActorWorldOffset(-MinPosition, false);
		if (AStaticMeshActor* MeshActor = Cast<AStaticMeshActor>(Current))
		{
			if (UStaticMeshComponent* MeshComp = MeshActor->GetStaticMeshComponent())
			{
				if (UStaticMesh* StaticMesh = MeshComp->GetStaticMesh())
				{
					StaticMesh->ClearFlags(RF_Standalone);
				}
			}
		}
	}
	CollectGarbage(GARBAGE_COLLECTION_KEEPFLAGS);
	if (GEngine)
	{
		GEngine->PerformGarbageCollectionAndCleanupActors();
	}
}

FString UOpenDriveToMap::GetStringForCurrentTile()
{
	return FString("_X_") + FString::FromInt(CurrentTilesInXY.X) + FString("_Y_") + FString::FromInt(CurrentTilesInXY.Y);
}

AActor* UOpenDriveToMap::SpawnActorInEditorWorld(UClass* Class, FVector Location, FRotator Rotation)
{
	UWorld* World = GetEditorWorld();
	if (!World || !Class)
	{
		return nullptr;
	}
	return World->SpawnActor<AActor>(Class, Location, Rotation);
}

void UOpenDriveToMap::OpenFileDialog()
{
	void* ParentWindowPtr = nullptr;
	if (FSlateApplication::IsInitialized())
	{
		TSharedPtr<SWindow> TopWindow = FSlateApplication::Get().GetActiveTopLevelWindow();
		if (TopWindow.IsValid() && TopWindow->GetNativeWindow().IsValid())
		{
			ParentWindowPtr = TopWindow->GetNativeWindow()->GetOSWindowHandle();
		}
	}

	TArray<FString> OutFileNames;
	IDesktopPlatform* DesktopPlatform = FDesktopPlatformModule::Get();
	if (DesktopPlatform)
	{
		DesktopPlatform->OpenFileDialog(
			ParentWindowPtr, TEXT("Select xodr file"), FPaths::ProjectDir(), FString(), TEXT("*.xodr"), 0, OutFileNames);
	}
	for (const FString& CurrentString : OutFileNames)
	{
		FilePath = CurrentString;
		UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("FileObtained %s"), *CurrentString);
	}
}

UWorld* UOpenDriveToMap::GetEditorWorld()
{
	if (!GEditor)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("GEditor is null"));
		return nullptr;
	}

	UUnrealEditorSubsystem* UnrealEditorSubsystem = GEditor->GetEditorSubsystem<UUnrealEditorSubsystem>();
	if (UnrealEditorSubsystem)
	{
		return UnrealEditorSubsystem->GetEditorWorld();
	}
	UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Not Editor subsystem found"));
	return nullptr;
}

UWorld* UOpenDriveToMap::GetGameWorld()
{
	if (!GEditor)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("GEditor is null"));
		return nullptr;
	}

	UUnrealEditorSubsystem* UnrealEditorSubsystem = GEditor->GetEditorSubsystem<UUnrealEditorSubsystem>();
	if (UnrealEditorSubsystem)
	{
		return UnrealEditorSubsystem->GetGameWorld();
	}
	UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Not Editor subsystem found"));
	return nullptr;
}

void UOpenDriveToMap::LoadMap()
{
	FGCObjectScopeGuard GCGuard(this);

	if (FilePath.IsEmpty())
	{
		return;
	}

	FilePath = FPaths::ConvertRelativePathToFull(FilePath);

	FString FileContent;
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::LoadMap(): File to load %s"), *FilePath);
	if (!FFileHelper::LoadFileToString(FileContent, *FilePath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("UOpenDriveToMap::LoadMap(): Failed to read %s"), *FilePath);
		return;
	}
	std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);
	CarlaMap = carla::opendrive::OpenDriveParser::Load(opendrive_xml);

	if (!CarlaMap.has_value())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Invalid Map"));
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Valid Map loaded"));
		MapName = FPaths::GetCleanFilename(FilePath);
		MapName.RemoveFromEnd(".xodr", ESearchCase::Type::IgnoreCase);
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("MapName %s"), *MapName);

		ToolInstances.Empty();
		GeneratedSplines.Empty();
		Landscapes.Empty();

		UWorld* World = GetEditorWorld();
		AActor* QueryActor = World ? UGameplayStatics::GetActorOfClass(World, ALargeMapManager::StaticClass()) : nullptr;
#if ENGINE_MAJOR_VERSION < 5
		if (QueryActor != nullptr)
		{
			ALargeMapManager* LargeMapManager = Cast<ALargeMapManager>(QueryActor);
			NumTilesInXY = LargeMapManager->GetNumTilesInXY();
			TileSize = LargeMapManager->GetTileSize();
			Tile0Offset = LargeMapManager->GetTile0Offset();
			CurrentTilesInXY = FIntVector(0, 0, 0);
			BaseLevelName = LargeMapManager->LargeMapTilePath + "/" + LargeMapManager->LargeMapName;
			do
			{
				GenerateTileStandalone();
			} while (GoNextTile());
			ReturnToMainLevel();
		}
#else
		if (!World)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("LoadMap: Editor world is null"));
			return;
		}

		StreetMapActorReference = Cast<AStreetMapActor>(UGameplayStatics::GetActorOfClass(World, AStreetMapActor::StaticClass()));
		if (!IsValid(StreetMapActorReference))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("StreetMapActorReference is not valid"));
		}

		if (DefaultHeightmap)
		{
			HeightmapCopy = DefaultHeightmap->GetCPUCopy();
			if (HeightmapCopy.IsValid())
			{
				HeightmapPixels = HeightmapCopy->AsG16();
				HeightmapWidth = HeightmapCopy->GetWidth();
				HeightmapHeight = HeightmapCopy->GetHeight();
			}
		}

		for (int32 Idx = 0; Idx < ToolsClasses.Num(); ++Idx)
		{
			const TSubclassOf<UDGTImplementable>& ToolClass = ToolsClasses[Idx];
			if (!ToolClass)
			{
				UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("ToolToInstantiate is null at index %d"), Idx);
				continue;
			}

			UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Instantiating tool of class: %s"), *ToolClass->GetName());

			UDGTImplementable* Tool = NewObject<UDGTImplementable>(this, ToolClass);
			if (!Tool)
			{
				UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to instantiate tool of class: %s"), *ToolClass->GetName());
				continue;
			}

			ToolInstances.Add(Tool);
			UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Tool instance created: %s"), *Tool->GetName());
		}

		if (ALargeMapManager* LargeMapManager = Cast<ALargeMapManager>(QueryActor))
		{
			if (TileSize <= KINDA_SMALL_NUMBER)
			{
				TileSize = LargeMapManager->GetTileSize();
				Tile0Offset = LargeMapManager->GetTile0Offset();
			}
			const FIntVector LmTiles = LargeMapManager->GetNumTilesInXY();
			if (LmTiles.X > 0 && LmTiles.Y > 0)
			{
				NumTilesInXY = LmTiles;
			}
		}
		NumTilesInXY.X = FMath::Max(1, NumTilesInXY.X);
		NumTilesInXY.Y = FMath::Max(1, NumTilesInXY.Y);

		CurrentTilesInXY = FIntVector::ZeroValue;
		do
		{
			GenerateTileStandalone();
		} while (GoNextTile());

		World = GetEditorWorld();
		if (IsValid(StreetMapActorReference))
		{
			GeneratedSplines.Append(StreetMapActorReference->SpawnTaggedTerrainSplines());
		}
		else
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("StreetMapActorReference is not valid"));
		}

		GenerateCurbSplinesFromRoadRenders();

		for (UDGTImplementable* Tool : ToolInstances)
		{
			if (Tool && World)
			{
				Tool->RunUtilityFunction(World, this);
				Tool->RunUtilityFunctionWithSplinesParamters(World, this, GeneratedSplines);
			}
		}

		SaveGeneratedPackagesHeadlessSafe(World, true);
#endif
		Landscapes.Empty();
	}
}

void UOpenDriveToMap::GenerateCurbSplinesFromRoadRenders()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Generating road renders and splines for curbs"));

	CurrentTilesInXY.X = 0;
	CurrentTilesInXY.Y = 0;

	do
	{
		MinPosition = FVector(CurrentTilesInXY.X * TileSize, CurrentTilesInXY.Y * -TileSize, 0.0f);
		MaxPosition = FVector((CurrentTilesInXY.X + 1.0f) * TileSize, (CurrentTilesInXY.Y + 1.0f) * -TileSize, 0.0f);

		RenderRoadToTexture(MinPosition, MaxPosition);

	} while (GoNextTile());

	GenerateCurbSplines();
}

TArray<AActor*> UOpenDriveToMap::GenerateMiscActors(float Offset, FVector MinLocation, FVector MaxLocation)
{
	TArray<AActor*> Returning;
	if (!CarlaMap.has_value())
	{
		return Returning;
	}
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return Returning;
	}

	carla::geom::Vector3D CarlaMinLocation(MinLocation.X / 100, MinLocation.Y / 100, MinLocation.Z / 100);
	carla::geom::Vector3D CarlaMaxLocation(MaxLocation.X / 100, MaxLocation.Y / 100, MaxLocation.Z / 100);

	std::vector<std::pair<carla::geom::Transform, std::string>> Locations =
		CarlaMap->GetTreesTransform(CarlaMinLocation, CarlaMaxLocation, DistanceBetweenTrees, DistanceFromRoadEdge, Offset);
	int32 i = 0;
	for (auto& cl : Locations)
	{
		const FVector scale{1.0f, 1.0f, 1.0f};
		cl.first.location.z = GetHeight(cl.first.location.x, cl.first.location.y) / 100.0f;
		FTransform NewTransform(FRotator(cl.first.rotation), FVector(cl.first.location), scale);

		NewTransform = GetSnappedPosition(NewTransform);

		AActor* Spawner = World->SpawnActor<AStaticMeshActor>(
			AStaticMeshActor::StaticClass(), NewTransform.GetLocation(), NewTransform.Rotator());
		if (!Spawner)
		{
			continue;
		}
		Spawner->Tags.Add(FName("MiscSpawnPosition"));
		Spawner->Tags.Add(FName(cl.second.c_str()));
		Spawner->SetActorLabel("MiscSpawnPosition" + FString::FromInt(i) + GetStringForCurrentTile());
#if ENGINE_MAJOR_VERSION > 4
		Spawner->SetIsSpatiallyLoaded(true);
#endif
		++i;
		Returning.Add(Spawner);
	}
	return Returning;
}

void UOpenDriveToMap::GenerateAll(const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation)
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateAll() Generating Roads..... "));
	GenerateRoadMesh(ParamCarlaMap, MinLocation, MaxLocation);

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateAll() Generating Lane Marks..... "));
	GenerateLaneMarks(ParamCarlaMap, MinLocation, MaxLocation);

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateAll() Generating Terrain..... "));
	CreateTerrain(5, 5, 64);

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateAll() Generating Traffic Lights..... "));
	GenerateTrafficLights(ParamCarlaMap, MinLocation, MaxLocation);

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("UOpenDriveToMap::GenerateAll() Generating Misc stuff..... "));
	GenerationFinished(MinLocation, MaxLocation);

#if PLATFORM_LINUX
	if (bUseMitsuba)
	{
		MitsubaMeshOptimization();
	}
#endif
}

void UOpenDriveToMap::GenerateRoadMesh(
	const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation)
{
	if (!ParamCarlaMap.has_value())
	{
		return;
	}
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	opg_parameters.vertex_distance = 0.5f;
	opg_parameters.vertex_width_resolution = 8.0f;
#if ENGINE_MAJOR_VERSION < 5
	opg_parameters.simplification_percentage = 50.0f;
#else
	opg_parameters.simplification_percentage = 0.0f;
#endif
	double start = FPlatformTime::Seconds();

	carla::geom::Vector3D CarlaMinLocation(MinLocation.X / 100, MinLocation.Y / 100, MinLocation.Z / 100);
	carla::geom::Vector3D CarlaMaxLocation(MaxLocation.X / 100, MaxLocation.Y / 100, MaxLocation.Z / 100);
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT(" Generating roads between %s  and %s"), *(CarlaMinLocation.ToFVector().ToString()),
		*(CarlaMaxLocation.ToFVector().ToString()));
	const auto Meshes = ParamCarlaMap->GenerateOrderedChunkedMeshInLocations(opg_parameters, CarlaMinLocation, CarlaMaxLocation);
	double end = FPlatformTime::Seconds();
	UE_LOG(LogCarlaDigitalTwinsTool, Log,
		TEXT(" GenerateOrderedChunkedMesh code executed in %f seconds. Simplification percentage is %f"), end - start,
		opg_parameters.simplification_percentage);

	start = FPlatformTime::Seconds();

	struct FPreparedMeshData
	{
		FProceduralCustomMesh MeshData;
		FVector MeshCentroid = FVector::ZeroVector;
		carla::road::Lane::LaneType LaneType = carla::road::Lane::LaneType::None;
		int32 SortOrder = 0;
		int32 Index = 0;
	};

	TArray<FPreparedMeshData> PreparedMeshes;
	FCriticalSection Mutex;
	int32 BaseOrder = 0;

	for (const auto& PairMap : Meshes)
	{
		const auto& LaneType = PairMap.first;
		const auto& MeshList = PairMap.second;
		const int32 GroupBase = BaseOrder;
		BaseOrder += static_cast<int32>(MeshList.size());

		ParallelFor(static_cast<int32>(MeshList.size()),
			[&](int32 i)
			{
				const auto& Mesh = MeshList[i];
				if (!Mesh || !Mesh->IsValid() || Mesh->GetVertices().empty())
				{
					return;
				}

				auto& Vertices = Mesh->GetVertices();

				if (LaneType == carla::road::Lane::LaneType::Driving)
				{
					for (auto& Vertex : Vertices)
					{
						FVector FV = Vertex.ToFVector();
						Vertex.z +=
							GetHeight(Vertex.x * 100.0f, Vertex.y * 100.0f, DistanceToLaneBorder(ParamCarlaMap, FV) > 65.0f) /
							100.0f;
					}
#if ENGINE_MAJOR_VERSION < 5
					carla::geom::Simplification Simplify(0.15);
					Simplify.Simplificate(Mesh);
#endif
				}
				else
				{
					for (auto& Vertex : Vertices)
					{
						Vertex.z += (GetHeight(Vertex.x * 100.0f, Vertex.y * 100.0f, false) + 15.0f) / 100.0f;
					}
				}

				if (Vertices.empty())
				{
					return;
				}

				FVector Centroid(0.0f);
				for (const auto& V : Vertices)
				{
					Centroid += V.ToFVector();
				}
				Centroid /= static_cast<float>(Vertices.size());

				for (auto& V : Vertices)
				{
					V.x -= Centroid.X;
					V.y -= Centroid.Y;
					V.z -= Centroid.Z;
				}

				FPreparedMeshData Data;
				Data.MeshData = *Mesh;
				Data.MeshCentroid = Centroid;
				Data.LaneType = LaneType;
				Data.SortOrder = GroupBase + i;

				{
					FScopeLock Lock(&Mutex);
					PreparedMeshes.Add(MoveTemp(Data));
				}
			});
	}

	PreparedMeshes.Sort([](const FPreparedMeshData& A, const FPreparedMeshData& B)
	{
		return A.SortOrder < B.SortOrder;
	});
	for (int32 Idx = 0; Idx < PreparedMeshes.Num(); ++Idx)
	{
		PreparedMeshes[Idx].Index = Idx;
	}

	UMaterialInstance* DuplicatedRoadMaterial = DefaultRoadMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultRoadMaterial, MapName))
		: nullptr;
	UMaterialInstance* DuplicatedSidewalkMaterial = DefaultSidewalksMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultSidewalksMaterial, MapName))
		: DuplicatedRoadMaterial;
	const FString TileSuffix = GetStringForCurrentTile();

	for (FPreparedMeshData& Entry : PreparedMeshes)
	{
		const FVector& Centroid = Entry.MeshCentroid;
		const int32 Index = Entry.Index;
		const carla::road::Lane::LaneType LaneType = Entry.LaneType;

		TArray<FProcMeshTangent> Tangents;
		UKismetProceduralMeshLibrary::CalculateTangentsForMesh(
			Entry.MeshData.Vertices, Entry.MeshData.Triangles, Entry.MeshData.UV0, Entry.MeshData.Normals, Tangents);

		AStaticMeshActor* TempActor = World->SpawnActor<AStaticMeshActor>();
		if (!TempActor)
		{
			continue;
		}
		UStaticMeshComponent* StaticMeshComponent = TempActor->GetStaticMeshComponent();
		if (!StaticMeshComponent)
		{
			TempActor->Destroy();
			continue;
		}

		StaticMeshComponent->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
		StaticMeshComponent->SetCollisionObjectType(ECC_WorldStatic);
		StaticMeshComponent->SetCollisionResponseToAllChannels(ECR_Block);
		StaticMeshComponent->SetMobility(EComponentMobility::Static);

		StaticMeshComponent->SetGenerateOverlapEvents(true);
		StaticMeshComponent->bReturnMaterialOnMove = true;

		UStaticMesh* FinalMesh = nullptr;

		if (LaneType == carla::road::Lane::LaneType::Driving)
		{
			if (DuplicatedRoadMaterial)
			{
				StaticMeshComponent->SetMaterial(0, DuplicatedRoadMaterial);
			}
			StaticMeshComponent->CastShadow = false;
			TempActor->SetActorLabel(FString::Printf(TEXT("SM_DrivingLane_%d"), Index));

			FinalMesh = UMapGenFunctionLibrary::CreateMesh(Entry.MeshData, Tangents, DuplicatedRoadMaterial, MapName, "DrivingLane",
				FName(*FString::Printf(TEXT("SM_DrivingLaneMesh%d%s"), Index, *TileSuffix)));
		}
		else
		{
			const FString LaneFolder = LaneTypeToFString(LaneType);
			UMaterialInstance* NonDrivingMaterial = (LaneType == carla::road::Lane::LaneType::Sidewalk ||
													 LaneType == carla::road::Lane::LaneType::Shoulder ||
													 LaneType == carla::road::Lane::LaneType::Border)
				? DuplicatedSidewalkMaterial
				: DuplicatedRoadMaterial;

			if (NonDrivingMaterial)
			{
				StaticMeshComponent->SetMaterial(0, NonDrivingMaterial);
			}
			StaticMeshComponent->CastShadow = false;
			TempActor->SetActorLabel(FString::Printf(TEXT("SM_Lane_%d"), Index));

			FinalMesh = UMapGenFunctionLibrary::CreateMesh(Entry.MeshData, Tangents, NonDrivingMaterial, MapName, LaneFolder,
				FName(*FString::Printf(TEXT("SM_%sMesh%d%s"), *LaneFolder, Index, *TileSuffix)));
		}

		StaticMeshComponent->SetStaticMesh(FinalMesh);
		TempActor->SetActorLocation(Centroid * 100.0f);
		TempActor->Tags.Add(FName("RoadLane"));
		TempActor->SetActorEnableCollision(true);

#if ENGINE_MAJOR_VERSION > 4
		TempActor->SetIsSpatiallyLoaded(true);
#endif
	}

	end = FPlatformTime::Seconds();
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Mesh spawning and translation code executed in %f seconds."), end - start);
}

void UOpenDriveToMap::GenerateLaneMarks(
	const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation)
{
	if (!ParamCarlaMap.has_value())
	{
		return;
	}
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	opg_parameters.vertex_distance = 0.5f;
	opg_parameters.vertex_width_resolution = 8.0f;
	opg_parameters.simplification_percentage = 15.0f;
	std::vector<std::string> lanemarkinfo;
	carla::geom::Vector3D CarlaMinLocation(MinLocation.X / 100, MinLocation.Y / 100, MinLocation.Z / 100);
	carla::geom::Vector3D CarlaMaxLocation(MaxLocation.X / 100, MaxLocation.Y / 100, MaxLocation.Z / 100);
	auto MarkingMeshes = ParamCarlaMap->GenerateLineMarkings(opg_parameters, CarlaMinLocation, CarlaMaxLocation, lanemarkinfo);

	UMaterialInstance* DuplicatedLaneMarksYellowMaterial = DefaultLaneMarksYellowMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultLaneMarksYellowMaterial, MapName))
		: nullptr;
	UMaterialInstance* DuplicatedLaneMarksWhiteMaterial = DefaultLaneMarksWhiteMaterial
		? Cast<UMaterialInstance>(UBlueprintUtilFunctions::CopyAssetToPlugin(DefaultLaneMarksWhiteMaterial, MapName))
		: nullptr;
	const FString TileSuffix = GetStringForCurrentTile();

	constexpr float MinMarkSpacingCm = 250.0f;
	constexpr float MinMarkSpacingSq = MinMarkSpacingCm * MinMarkSpacingCm;
	TMap<FIntVector, TArray<FVector>> SpatialGrid;

	int32 meshindex = 0;
	for (size_t index = 0; index < MarkingMeshes.size(); ++index)
	{
		const auto& Mesh = MarkingMeshes[index];
		if (!Mesh || Mesh->GetVertices().empty() || !Mesh->IsValid())
		{
			continue;
		}

		FVector MeshCentroid = FVector::ZeroVector;
		for (auto& Vertex : Mesh->GetVertices())
		{
			FVector VertexFVector = Vertex.ToFVector();
			Vertex.z +=
				GetHeight(Vertex.x * 100.0f, Vertex.y * 100.0f, DistanceToLaneBorder(ParamCarlaMap, VertexFVector) > 65.0f) /
					100.0f +
				0.01f;
			MeshCentroid += Vertex.ToFVector();
		}

		MeshCentroid /= static_cast<float>(Mesh->GetVertices().size());

		for (auto& Vertex : Mesh->GetVertices())
		{
			Vertex.x -= MeshCentroid.X;
			Vertex.y -= MeshCentroid.Y;
			Vertex.z -= MeshCentroid.Z;
		}

		const FVector WorldPos = MeshCentroid * 100.0f;
		const FIntVector Cell(
			FMath::FloorToInt(WorldPos.X / MinMarkSpacingCm),
			FMath::FloorToInt(WorldPos.Y / MinMarkSpacingCm),
			FMath::FloorToInt(WorldPos.Z / MinMarkSpacingCm));

		bool bTooClose = false;
		for (int32 dz = -1; dz <= 1 && !bTooClose; ++dz)
		{
			for (int32 dy = -1; dy <= 1 && !bTooClose; ++dy)
			{
				for (int32 dx = -1; dx <= 1; ++dx)
				{
					if (const TArray<FVector>* NeighborBucket = SpatialGrid.Find(Cell + FIntVector(dx, dy, dz)))
					{
						for (const FVector& ExistingPos : *NeighborBucket)
						{
							if (FVector::DistSquared(WorldPos, ExistingPos) < MinMarkSpacingSq)
							{
								bTooClose = true;
								break;
							}
						}
					}
				}
			}
		}

		if (bTooClose)
		{
			continue;
		}

		AStaticMeshActor* TempActor = World->SpawnActor<AStaticMeshActor>();
		if (!TempActor)
		{
			continue;
		}
		UStaticMeshComponent* StaticMeshComponent = TempActor->GetStaticMeshComponent();
		if (!StaticMeshComponent)
		{
			TempActor->Destroy();
			continue;
		}
		TempActor->SetActorLabel(FString::Printf(TEXT("SM_LaneMark_%d%s"), meshindex, *TileSuffix));
		StaticMeshComponent->CastShadow = false;

		const std::string& MarkInfo = (index < lanemarkinfo.size()) ? lanemarkinfo[index] : std::string();
		const bool bIsYellow = (MarkInfo.find("yellow") != std::string::npos);
		UMaterialInstance* SelectedMarkMaterial = bIsYellow
			? DuplicatedLaneMarksYellowMaterial
			: DuplicatedLaneMarksWhiteMaterial;

		if (SelectedMarkMaterial)
		{
			StaticMeshComponent->SetMaterial(0, SelectedMarkMaterial);
		}

		const FProceduralCustomMesh MeshData = *Mesh;
		TArray<FVector> Normals;
		TArray<FProcMeshTangent> Tangents;
		UKismetProceduralMeshLibrary::CalculateTangentsForMesh(
			MeshData.Vertices, MeshData.Triangles, MeshData.UV0, Normals, Tangents);

		UStaticMesh* MeshToSet = UMapGenFunctionLibrary::CreateMesh(MeshData, Tangents, SelectedMarkMaterial, MapName,
			"LaneMark", FName(*FString::Printf(TEXT("SM_LaneMarkMesh%d%s"), meshindex, *TileSuffix)));
		StaticMeshComponent->SetStaticMesh(MeshToSet);

		TempActor->SetActorLocation(WorldPos);
		if (!MarkInfo.empty())
		{
			TempActor->Tags.Add(*FString(UTF8_TO_TCHAR(MarkInfo.c_str())));
		}
		TempActor->Tags.Add(FName("RoadLane"));
#if ENGINE_MAJOR_VERSION > 4
		TempActor->SetIsSpatiallyLoaded(true);
#endif
		SpatialGrid.FindOrAdd(Cell).Add(WorldPos);
		meshindex++;
		TempActor->SetActorEnableCollision(false);
		StaticMeshComponent->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	}
}

void UOpenDriveToMap::GenerateTreePositions(
	const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation)
{
	if (!ParamCarlaMap.has_value())
	{
		return;
	}
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	carla::geom::Vector3D CarlaMinLocation(MinLocation.X / 100, MinLocation.Y / 100, MinLocation.Z / 100);
	carla::geom::Vector3D CarlaMaxLocation(MaxLocation.X / 100, MaxLocation.Y / 100, MaxLocation.Z / 100);

	std::vector<std::pair<carla::geom::Transform, std::string>> Locations =
		ParamCarlaMap->GetTreesTransform(CarlaMinLocation, CarlaMaxLocation, DistanceBetweenTrees, DistanceFromRoadEdge);
	int32 i = 0;
	for (auto& cl : Locations)
	{
		const FVector scale{1.0f, 1.0f, 1.0f};
		cl.first.location.z = GetHeight(cl.first.location.x, cl.first.location.y) / 100.0f;
		FTransform NewTransform(FRotator(cl.first.rotation), FVector(cl.first.location), scale);
		NewTransform = GetSnappedPosition(NewTransform);

		AActor* Spawner = World->SpawnActor<AStaticMeshActor>(
			AStaticMeshActor::StaticClass(), NewTransform.GetLocation(), NewTransform.Rotator());
		if (!Spawner)
		{
			continue;
		}

		Spawner->Tags.Add(FName("TreeSpawnPosition"));
		Spawner->Tags.Add(FName(cl.second.c_str()));
		Spawner->SetActorLabel("TreeSpawnPosition" + FString::FromInt(i) + GetStringForCurrentTile());
#if ENGINE_MAJOR_VERSION > 4
		Spawner->SetIsSpatiallyLoaded(true);
#endif
		++i;
	}
}

void UOpenDriveToMap::GenerateTrafficLights(
	const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation)
{
	if (!ParamCarlaMap)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("GenerateTrafficLights: Map is not valid, skipping."));
		return;
	}
	UWorld* EditorWorld = GetEditorWorld();
	if (!EditorWorld)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("GenerateTrafficLights: EditorWorld is null, skipping."));
		return;
	}

	const FString BPPath{TEXT("/CarlaDigitalTwinsTool/Blueprints/TrafficLight/BP_TrafficLightActor.BP_TrafficLightActor_C")};
	const carla::geom::Vector3D CarlaMinLocation(MinLocation.X / 100.0, MinLocation.Y / 100.0, MinLocation.Z / 100.0);
	const carla::geom::Vector3D CarlaMaxLocation(MaxLocation.X / 100.0, MaxLocation.Y / 100.0, MaxLocation.Z / 100.0);
	const double MinX{FMath::Min(CarlaMinLocation.x, CarlaMaxLocation.x)};
	const double MaxX{FMath::Max(CarlaMinLocation.x, CarlaMaxLocation.x)};
	const double MinY{FMath::Min(CarlaMinLocation.y, CarlaMaxLocation.y)};
	const double MaxY{FMath::Max(CarlaMinLocation.y, CarlaMaxLocation.y)};

	auto InChunk2D = [&](const carla::geom::Vector3D& Position) -> bool
	{ return (Position.x >= MinX && Position.x <= MaxX && Position.y >= MinY && Position.y <= MaxY); };

	auto MakeStableLabel = [&](const carla::road::Signal& S) -> FString
	{
		const FString Sid{UTF8_TO_TCHAR(S.GetSignalId().c_str())};
		const FString Key{FString::Printf(TEXT("%s|%s"), *MapName, *Sid)};
		const uint32 Hash{FCrc::StrCrc32(*Key)};
		return FString::Printf(TEXT("TrafficLight_%08X"), Hash);
	};

	UClass* TrafficLightBPClass{LoadObject<UClass>(nullptr, *BPPath)};
	if (!IsValid(TrafficLightBPClass))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Unable to load BP in %s"), *BPPath);
		return;
	}

	const FString Rel{TEXT("Carla/Static/TrafficLight/TrafficLights2025/Presets/Default.json")};
	FString JsonAbs;
	FString CachedDefaultJSON;
	bool bHasCachedDefaultJSON = false;
	if (TryResolveContentFileAnywhere(Rel, JsonAbs))
	{
		bHasCachedDefaultJSON = FFileHelper::LoadFileToString(CachedDefaultJSON, *JsonAbs);
		if (!bHasCachedDefaultJSON)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Failed to read JSON file at %s. Using backup."), *JsonAbs);
		}
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning,
			TEXT("Default traffic light JSON not found under any Content: %s. Using backup."), *Rel);
	}

	std::vector<const carla::road::element::RoadInfoSignal*> Signals{ParamCarlaMap->GetAllSignalReferences()};

	TSet<const carla::road::Signal*> SeenSignals;

	for (const carla::road::element::RoadInfoSignal* Info : Signals)
	{
		if (!Info)
		{
			continue;
		}

		const carla::road::Signal* Signal = Info->GetSignal();
		if (!Signal)
		{
			continue;
		}

		if (!carla::road::SignalType::IsTrafficLight(Signal->GetType()))
		{
			continue;
		}

		if (SeenSignals.Contains(Signal))
		{
			continue;
		}

		const carla::road::RoadId SignalRoadId = Signal->GetRoadId();

		carla::geom::Transform SignalTransform = Signal->GetTransform();
		SignalTransform.rotation.yaw += 90.0f;

		SignalTransform.location = SignalTransform.location +
			carla::geom::Location(SignalTransform.GetForwardVector() * 0.25f);

		if (!InChunk2D(SignalTransform.location))
		{
			continue;
		}

		SeenSignals.Add(Signal);
		const FString Label{MakeStableLabel(*Signal)};
		ATrafficLightActor* TL{EditorWorld->SpawnActorDeferred<ATrafficLightActor>(
			TrafficLightBPClass, SignalTransform, nullptr, nullptr, ESpawnActorCollisionHandlingMethod::DontSpawnIfColliding)};
		if (!IsValid(TL))
		{
			continue;
		}

		if (bHasCachedDefaultJSON)
		{
			TL->BuildFromJSONString(CachedDefaultJSON);
		}
		else
		{
			TL->PopulateDefault();
		}

		TL->FinishSpawning(SignalTransform);
		if (!IsValid(TL))
		{
			continue;
		}

		carla::road::JuncId JunctionId = ParamCarlaMap->GetJunctionId(SignalRoadId);
		TL->SignalID = FString(UTF8_TO_TCHAR(Signal->GetSignalId().c_str()));

		const auto& Controllers = Signal->GetControllers();
		if (!Controllers.empty())
		{
			const std::string& ControllerId = *Controllers.begin();
			TL->TrafficLightGroupID = FString(UTF8_TO_TCHAR(ControllerId.c_str()));
			if (JunctionId < 0)
			{
				const auto& MapControllers = ParamCarlaMap->GetControllers();
				const auto CtlIt = MapControllers.find(ControllerId);
				if (CtlIt != MapControllers.end() && CtlIt->second && !CtlIt->second->GetJunctions().empty())
				{
					JunctionId = *CtlIt->second->GetJunctions().begin();
				}
			}
		}
		TL->JunctionID = JunctionId;

		const std::vector<carla::road::LaneValidity>& Validities = Info->GetValidities();
		const carla::road::SignalOrientation SignalOrientation = Signal->GetOrientation();

		for (FTLPole& Pole : TL->Poles)
		{
			for (FTLHead& Head : Pole.Heads)
			{
				for (FTLModule& Module : Head.Modules)
				{
					Module.LaneIds.Empty();

					for (const auto& Validity : Validities)
					{
						const carla::road::LaneId MinLane = FMath::Min(Validity._from_lane, Validity._to_lane);
						const carla::road::LaneId MaxLane = FMath::Max(Validity._from_lane, Validity._to_lane);
						for (carla::road::LaneId LaneId = MinLane; LaneId <= MaxLane; ++LaneId)
						{
							if (LaneId != 0)
							{
								bool bShouldAddLane = false;

								switch (SignalOrientation)
								{
									case carla::road::SignalOrientation::Positive:
										bShouldAddLane = (LaneId < 0);
										break;
									case carla::road::SignalOrientation::Negative:
										bShouldAddLane = (LaneId > 0);
										break;
									case carla::road::SignalOrientation::Both:
										bShouldAddLane = true;
										break;
								}

								if (bShouldAddLane)
								{
									Module.LaneIds.AddUnique(LaneId);
								}
							}
						}
					}
				}
			}
		}

		TL->Build();
		TL->Bake(MapName, Label);

		UE_LOG(LogCarlaDigitalTwinsTool, Verbose,
			TEXT("Baked traffic light: Signal %s -> Actor '%s'"),
			UTF8_TO_TCHAR(Signal->GetSignalId().c_str()),
			*Label);

		TL->Destroy();
	}
}

float UOpenDriveToMap::GetHeight(float PosX, float PosY, bool bDrivingLane)
{
	if (DefaultHeightmap && HeightmapCopy.IsValid() && HeightmapPixels.Num() > 0)
	{
		const int32 TextureSizeX = HeightmapCopy->GetWidth();
		const int32 TextureSizeY = HeightmapCopy->GetHeight();
		if (TextureSizeX > 0 && TextureSizeY > 0)
		{
			const float SpanX = WorldEndPosition.X - WorldOriginPosition.X;
			const float SpanY = WorldEndPosition.Y - WorldOriginPosition.Y;

			float NormalizedX = FMath::IsNearlyZero(SpanX) ? 0.0f : (PosX - WorldOriginPosition.X) / SpanX;
			float NormalizedY = FMath::IsNearlyZero(SpanY) ? 0.0f : (PosY - WorldOriginPosition.Y) / SpanY;

			NormalizedX = FMath::Clamp(NormalizedX, 0.0f, 1.0f);
			NormalizedY = FMath::Clamp(NormalizedY, 0.0f, 1.0f);

			const float TexX = NormalizedX * static_cast<float>(TextureSizeX);
			const float TexY = NormalizedY * static_cast<float>(TextureSizeY);

			const float SmoothedValue = UMapGenFunctionLibrary::BicubicSampleG16(
				HeightmapPixels, TextureSizeX, TextureSizeY, TexX, TexY);
			const float LandscapeHeight = SmoothedValue * (MaxHeight - MinHeight) + MinHeight;

			if (bDrivingLane)
			{
				return LandscapeHeight - carla::geom::deformation::GetBumpDeformation(PosX, PosY);
			}
			return LandscapeHeight - 5.0f;
		}
	}

	const float DefZ = carla::geom::deformation::GetZPosInDeformation(PosX, PosY) * 0.7f;
	if (bDrivingLane)
	{
		return DefZ - carla::geom::deformation::GetBumpDeformation(PosX, PosY);
	}
	return DefZ;
}

FTransform UOpenDriveToMap::GetSnappedPosition(FTransform Origin)
{
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return Origin;
	}

	FTransform ToReturn = Origin;
	const FVector OriginLoc = Origin.GetLocation();
	const FVector Start(OriginLoc.X, OriginLoc.Y, FMath::Max(OriginLoc.Z, MaxHeight) + 10000.0f);
	const FVector End(OriginLoc.X, OriginLoc.Y, FMath::Min(OriginLoc.Z, MinHeight) - 10000.0f);
	FHitResult HitResult;
	FCollisionQueryParams CollisionQuery;
	CollisionQuery.bTraceComplex = true;
	FCollisionResponseParams CollisionParams;

	if (World->LineTraceSingleByChannel(
			HitResult, Start, End, ECollisionChannel::ECC_WorldStatic, CollisionQuery, CollisionParams))
	{
		const FString HitName = HitResult.GetActor() ? HitResult.GetActor()->GetName() : TEXT("Unknown");
		UE_LOG(LogCarlaDigitalTwinsTool, Verbose, TEXT("Hit Actor %s at location %s"), *HitName,
			*HitResult.Location.ToString());
		ToReturn.SetLocation(HitResult.Location);
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("No Hit Actor at location %s"), *Start.ToString());
		ToReturn.SetLocation(FVector(Start.X, Start.Y, GetHeight(Start.X, Start.Y, false)));
	}
	return ToReturn;
}

float UOpenDriveToMap::GetHeightForLandscape(FVector Origin)
{
	return GetHeight(Origin.X, Origin.Y, false) - 2.0f;
}

float UOpenDriveToMap::DistanceToLaneBorder(
	const boost::optional<carla::road::Map>& ParamCarlaMap, const FVector& location, int32_t lane_type) const
{
	if (!ParamCarlaMap.has_value())
	{
		return 100000.0f;
	}
	carla::geom::Location cl(location);
	auto wp = ParamCarlaMap->GetClosestWaypointOnRoad(cl, lane_type);
	if (wp)
	{
		carla::geom::Transform ct = ParamCarlaMap->ComputeTransform(*wp);
		double LaneWidth = ParamCarlaMap->GetLaneWidth(*wp);
		return cl.Distance(ct.location) - LaneWidth;
	}
	return 100000.0f;
}

bool UOpenDriveToMap::IsInRoad(const boost::optional<carla::road::Map>& ParamCarlaMap, const FVector& location) const
{
	if (!ParamCarlaMap.has_value())
	{
		return false;
	}
	const int32_t RoadMask =
		(static_cast<int32_t>(carla::road::Lane::LaneType::Sidewalk) - 1) &
		~static_cast<int32_t>(carla::road::Lane::LaneType::None);
	return ParamCarlaMap->GetWaypoint(location, RoadMask).has_value();
}

void UOpenDriveToMap::ImportXODR()
{
	IPlatformFile& FileManager = FPlatformFileManager::Get().GetPlatformFile();
	const FString MyFileDestination = FPaths::Combine(
		UGenerationPathsHelper::GetRawMapDirectoryPath(MapName),
		TEXT("OpenDrive"),
		MapName + TEXT(".xodr"));
	FileManager.CreateDirectoryTree(*FPaths::GetPath(MyFileDestination));

	if (FileManager.CopyFile(*MyFileDestination, *LocalFilePath, EPlatformFileRead::None, EPlatformFileWrite::None))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Verbose, TEXT("FilePaths: File Copied!"));
		FilePath = MyFileDestination;
		LoadMap();
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FilePaths local xodr file not copied: %s -> %s"), *LocalFilePath, *MyFileDestination);
	}
}

void UOpenDriveToMap::ImportOSM()
{
	IPlatformFile& FileManager = FPlatformFileManager::Get().GetPlatformFile();
	const FString MyFileDestination = FPaths::Combine(
		UGenerationPathsHelper::GetRawMapDirectoryPath(MapName),
		TEXT("OpenDrive"),
		MapName + TEXT(".osm"));
	FileManager.CreateDirectoryTree(*FPaths::GetPath(MyFileDestination));

	if (FileManager.CopyFile(*MyFileDestination, *LocalFilePath, EPlatformFileRead::None, EPlatformFileWrite::None))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Verbose, TEXT("FilePaths: File Copied!"));
		ConvertOSMInOpenDrive();
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FilePaths local osm file not copied: %s -> %s"), *LocalFilePath, *MyFileDestination);
	}
}

void UOpenDriveToMap::MoveActorsToSubLevels(TArray<AActor*> ActorsToMove)
{
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}
	AActor* QueryActor = UGameplayStatics::GetActorOfClass(World, ALargeMapManager::StaticClass());

	if (QueryActor != nullptr)
	{
		ALargeMapManager* LmManager = Cast<ALargeMapManager>(QueryActor);
		if (LmManager)
		{
			SaveGeneratedPackagesHeadlessSafe(World, true);
			UOpenDriveToMap::MoveActorsToSubLevelWithLargeMap(ActorsToMove, LmManager);
		}
	}
}

void UOpenDriveToMap::MoveActorsToSubLevelWithLargeMap(TArray<AActor*> Actors, ALargeMapManager* LargeMapManager)
{
	if (!LargeMapManager)
	{
		return;
	}
	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	TMap<FCarlaMapTile*, TArray<AActor*>> ActorsToMove;
	for (AActor* Actor : Actors)
	{
		if (!IsValid(Actor))
		{
			continue;
		}
		UHierarchicalInstancedStaticMeshComponent* Component = Cast<UHierarchicalInstancedStaticMeshComponent>(
			Actor->GetComponentByClass(UHierarchicalInstancedStaticMeshComponent::StaticClass()));
		FVector ActorLocation = Actor->GetActorLocation();
		const int32 InstanceCount = Component ? Component->GetInstanceCount() : 0;
		if (Component && InstanceCount > 0)
		{
			ActorLocation = FVector::ZeroVector;
			for (int32 i = 0; i < InstanceCount; ++i)
			{
				FTransform Transform;
				Component->GetInstanceTransform(i, Transform, true);
				ActorLocation += Transform.GetTranslation();
			}
			ActorLocation /= static_cast<float>(InstanceCount);
		}
		UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Actor at location %s"), *ActorLocation.ToString());
		FCarlaMapTile* Tile = LargeMapManager->GetCarlaMapTile(ActorLocation);
		if (!Tile)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Error: actor %s in location %s is outside the map"), *Actor->GetName(),
				*ActorLocation.ToString());
			continue;
		}

		if (Component && InstanceCount > 0)
		{
			UpdateInstancedMeshCoordinates(Component, Tile->Location);
		}
		else
		{
			UpdateGenericActorCoordinates(Actor, Tile->Location);
		}
		ActorsToMove.FindOrAdd(Tile).Add(Actor);
	}

	for (auto& Element : ActorsToMove)
	{
		FCarlaMapTile* Tile = Element.Key;
		TArray<AActor*>& ActorList = Element.Value;
		if (!Tile || ActorList.IsEmpty())
		{
			continue;
		}

		if (ULevelStreamingDynamic* StreamingLevel = Tile->StreamingLevel)
		{
			StreamingLevel->bShouldBlockOnLoad = true;
			StreamingLevel->SetShouldBeVisible(true);
			StreamingLevel->SetShouldBeLoaded(true);
		}
		ULevelStreaming* Level =
			UEditorLevelUtils::AddLevelToWorld(World, *Tile->Name, ULevelStreamingDynamic::StaticClass(), FTransform());
		if (!Level)
		{
			continue;
		}
		int32 MovedActors = UEditorLevelUtils::MoveActorsToLevel(ActorList, Level, false, false);
		UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Moved %d actors"), MovedActors);
		FEditorFileUtils::SaveDirtyPackages(false, true, true, false, false, false, nullptr);
		if (ULevel* LoadedLevel = Level->GetLoadedLevel())
		{
			UEditorLevelUtils::RemoveLevelFromWorld(LoadedLevel);
		}
	}

	if (GEngine)
	{
		GEngine->PerformGarbageCollectionAndCleanupActors();
	}
	FText TransResetText(FText::FromString("Clean up after Move actors to sublevels"));
	if (GEditor && GEditor->Trans)
	{
		GEditor->Trans->Reset(TransResetText);
		GEditor->Cleanse(true, true, TransResetText);
	}
}

void UOpenDriveToMap::UpdateInstancedMeshCoordinates(UHierarchicalInstancedStaticMeshComponent* Component, FVector TileOrigin)
{
	if (!Component)
	{
		return;
	}
	const int32 InstanceCount = Component->GetInstanceCount();
	if (InstanceCount <= 0)
	{
		return;
	}
	TArray<FTransform> NewTransforms;
	NewTransforms.Reserve(InstanceCount);
	for (int32 i = 0; i < InstanceCount; ++i)
	{
		FTransform Transform;
		Component->GetInstanceTransform(i, Transform, true);
		Transform.AddToTranslation(-TileOrigin);
		NewTransforms.Add(Transform);
	}
	Component->BatchUpdateInstancesTransforms(0, NewTransforms, true, true, true);
}

void UOpenDriveToMap::UpdateGenericActorCoordinates(AActor* Actor, FVector TileOrigin)
{
	if (!Actor)
	{
		return;
	}
	FVector LocalLocation = Actor->GetActorLocation() - TileOrigin;
	Actor->SetActorLocation(LocalLocation);
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("%s New location %s"), *Actor->GetName(), *LocalLocation.ToString());
}

void UOpenDriveToMap::UnloadWorldPartitionRegion(const FBox& RegionBox)
{
	if (!GEngine)
	{
		return;
	}
	UWorld* World = GEngine->GetWorldFromContextObject(this, EGetWorldErrorMode::LogAndReturnNull);
	if (World)
	{
		UWorldPartitionSubsystem* WorldPartitionSubsystem = World->GetSubsystem<UWorldPartitionSubsystem>();
		(void)WorldPartitionSubsystem;
		(void)RegionBox;
	}
}

void UOpenDriveToMap::RenderRoadToTexture(FVector MinLocation, FVector MaxLocation)
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Render road for curbs generation"));

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	FBox Bounds(EForceInit::ForceInitToZero);
	Bounds += MinLocation;
	Bounds += MaxLocation;

	const FVector2D Extent = FVector2D(Bounds.Max) - FVector2D(Bounds.Min);
	if (Extent.X <= KINDA_SMALL_NUMBER || Extent.Y <= KINDA_SMALL_NUMBER)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("RenderRoadToTexture: Degenerate bounds extent (%f, %f), skipping."), Extent.X, Extent.Y);
		return;
	}

	FString RoadLabel = TEXT("DrivingLane");
#if PLATFORM_LINUX
	if (bUseMitsuba)
	{
		RoadLabel = TEXT("UpdatedRoad");
	}
#endif

	TArray<AActor*> HiddenActors;
	{
		TArray<AActor*> Actors;
		UGameplayStatics::GetAllActorsOfClass(World, AActor::StaticClass(), Actors);
		HiddenActors.Reserve(Actors.Num());
		for (AActor* Actor : Actors)
		{
			if (!IsValid(Actor))
			{
				continue;
			}
			const FString Name = Actor->GetActorLabel();
			if (!Name.Contains(RoadLabel, ESearchCase::CaseSensitive))
			{
				Actor->SetActorHiddenInGame(true);
				HiddenActors.Add(Actor);
			}
		}
	}
	ON_SCOPE_EXIT
	{
		for (AActor* HiddenActor : HiddenActors)
		{
			if (IsValid(HiddenActor))
			{
				HiddenActor->SetActorHiddenInGame(false);
			}
		}
	};

	const FVector Center = Bounds.GetCenter();
	const double RenderTargetScale = UE_CM_TO_M * 8.0;
	const FVector2D RenderTargetExtent = Extent * RenderTargetScale;
	const FIntPoint RenderTargetSize(
		FMath::Clamp(FMath::RoundToInt(RenderTargetExtent.X), 1, 8192),
		FMath::Clamp(FMath::RoundToInt(RenderTargetExtent.Y), 1, 8192));

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("RenderTargetSize: %i, %i"), RenderTargetSize.X, RenderTargetSize.Y);

	TStrongObjectPtr<UTextureRenderTarget2D> RenderTarget(NewObject<UTextureRenderTarget2D>());
	if (!RenderTarget.IsValid())
	{
		return;
	}
	RenderTarget->ClearColor = FLinearColor::Black;
	RenderTarget->OverrideFormat = PF_FloatRGB;
	RenderTarget->bForceLinearGamma = true;
	RenderTarget->SizeX = RenderTargetSize.X;
	RenderTarget->SizeY = RenderTargetSize.Y;
	RenderTarget->UpdateResource();

	FActorSpawnParameters ActorSpawnParameters;
	ActorSpawnParameters.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	ActorSpawnParameters.Name = MakeUniqueObjectName(World->PersistentLevel, ASceneCapture2D::StaticClass(), FName(*(TEXT("Camera") + GetStringForCurrentTile())));

	ASceneCapture2D* Camera = World->SpawnActor<ASceneCapture2D>(ASceneCapture2D::StaticClass(), ActorSpawnParameters);
	if (!IsValid(Camera))
	{
		return;
	}
	ON_SCOPE_EXIT
	{
		if (IsValid(Camera))
		{
			Camera->Destroy();
		}
	};

	USceneCaptureComponent2D* CaptureComponent = Camera->GetCaptureComponent2D();
	if (!CaptureComponent)
	{
		return;
	}
	CaptureComponent->ProjectionType = ECameraProjectionMode::Orthographic;
	CaptureComponent->OrthoWidth = Extent.X;
	CaptureComponent->CaptureSource = ESceneCaptureSource::SCS_BaseColor;
	CaptureComponent->bCaptureEveryFrame = false;
	CaptureComponent->bCaptureOnMovement = false;
	CaptureComponent->TextureTarget = RenderTarget.Get();

	CaptureComponent->ShowFlags.SetLumenGlobalIllumination(false);
	CaptureComponent->ShowFlags.SetLumenReflections(false);
	CaptureComponent->ShowFlags.SetGlobalIllumination(false);
	CaptureComponent->ShowFlags.SetScreenSpaceReflections(false);
	CaptureComponent->ShowFlags.SetDistanceFieldAO(false);
	CaptureComponent->ShowFlags.SetTemporalAA(false);
	CaptureComponent->ShowFlags.SetMotionBlur(false);
	CaptureComponent->ShowFlags.SetBloom(false);
	CaptureComponent->ShowFlags.SetVolumetricFog(false);
	CaptureComponent->ShowFlags.SetDynamicShadows(false);

	const double CameraZ = FMath::Max<double>(Bounds.Max.Z, MaxHeight) + 10000.0;
	const FVector Location(Center.X, Center.Y, CameraZ);
	const FQuat Rotation = FRotationMatrix::MakeFromXZ((Center - Location).GetSafeNormal(), FVector::YAxisVector).ToQuat();

	Camera->SetActorLocation(Location);
	Camera->SetActorRotation(Rotation);

	CaptureComponent->CaptureScene();

	FTextureRenderTargetResource* RTResource = RenderTarget->GameThread_GetRenderTargetResource();
	if (!RTResource)
	{
		return;
	}

	TArray<FColor> Pixels;
	if (!RTResource->ReadPixels(Pixels) || Pixels.Num() != RenderTargetSize.X * RenderTargetSize.Y)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("RenderRoadToTexture: Failed to read pixels from RenderTarget."));
		return;
	}

	const FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);
	FPlatformFileManager::Get().GetPlatformFile().CreateDirectoryTree(*OutPath);
	const FString ImagePath = OutPath / (TEXT("road_render") + GetStringForCurrentTile() + TEXT(".png"));

	auto ImageTask = MakeUnique<FImageWriteTask>();
	auto PixelData = MakeUnique<TImagePixelData<FColor>>(RenderTargetSize);
	PixelData->Pixels = MoveTemp(Pixels);
	ImageTask->PixelData = MoveTemp(PixelData);

	ImageTask->Filename = ImagePath;
	ImageTask->Format = EImageFormat::PNG;
	ImageTask->CompressionQuality = static_cast<int32>(EImageCompressionQuality::Default);
	ImageTask->bOverwriteFile = true;
	ImageTask->PixelPreProcessors.Add(TAsyncAlphaWrite<FColor>(255));

	auto& HighResScreenshotConfig = GetHighResScreenshotConfig();
	if (HighResScreenshotConfig.ImageWriteQueue)
	{
		TFuture<bool> Task = HighResScreenshotConfig.ImageWriteQueue->Enqueue(MoveTemp(ImageTask));
		Task.Wait();
	}
}

void UOpenDriveToMap::GenerateCurbSplines()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Create splines for curbs generation"));

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	const FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);

	RunPythonMergeTiles();
	RunPythonRoadEdges();

	const FString JsonPath = OutPath / TEXT("contours.json");

	MinPosition = FVector(0.0f, 0.0f, 0.0f);
	MaxPosition = FVector(NumTilesInXY.X * TileSize, NumTilesInXY.Y * -TileSize, 0.0f);

	FBox Bounds(EForceInit::ForceInitToZero);
	Bounds += MinPosition;
	Bounds += MaxPosition;

	const FVector Center = Bounds.GetCenter();
	const FVector2D Extent = FVector2D(Bounds.Max) - FVector2D(Bounds.Min);
	if (Extent.X <= KINDA_SMALL_NUMBER || Extent.Y <= KINDA_SMALL_NUMBER)
	{
		return;
	}
	const double RenderTargetScale = UE_CM_TO_M * 8.0;
	const FVector2D RenderTargetExtent = Extent * RenderTargetScale;
	const FIntPoint RenderTargetSize(
		FMath::Max(1, FMath::RoundToInt(RenderTargetExtent.X)),
		FMath::Max(1, FMath::RoundToInt(RenderTargetExtent.Y)));

	TArray<USplineComponent*> RoadSplines =
		UGeometryImporter::CreateSplinesFromJson(World, JsonPath, FVector2D(Center.X, Center.Y), Extent, RenderTargetSize);

	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Number of road splines: %i"), RoadSplines.Num());

	// Project spline points to the floor
	for (USplineComponent* Spline : RoadSplines)
	{
		if (!IsValid(Spline))
		{
			continue;
		}
		const int32 NumPoints = Spline->GetNumberOfSplinePoints();
		for (int32 i = 0; i < NumPoints; ++i)
		{
			FVector Pos = Spline->GetLocationAtSplinePoint(i, ESplineCoordinateSpace::Local);
			Pos.Z = GetHeight(Pos.X, Pos.Y, false);
			Spline->SetLocationAtSplinePoint(i, Pos, ESplineCoordinateSpace::Local, false);
		}
		Spline->UpdateSpline();
	}

	GeneratedSplines.Append(RoadSplines);
}

void UOpenDriveToMap::RunPythonScript(FString ScriptPath, FString Args)
{
	if (!FPaths::FileExists(ScriptPath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("RunPythonScript: Script not found at %s"), *ScriptPath);
		return;
	}

	const FString PythonExe = PythonBinPath;

	void* ReadPipe = nullptr;
	void* WritePipe = nullptr;
	if (!FPlatformProcess::CreatePipe(ReadPipe, WritePipe))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("RunPythonScript: Failed to create stdout pipe."));
		return;
	}

	FProcHandle ProcHandle = FPlatformProcess::CreateProc(
		*PythonExe,
		*Args,
		false,    // bLaunchDetached
		true,     // bLaunchHidden
		true,     // bLaunchReallyHidden
		nullptr,
		0,
		nullptr,
		WritePipe, // PipeWriteChild (stdout/stderr)
		nullptr);  // PipeReadChild (stdin)

	if (!ProcHandle.IsValid())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to launch Python executable '%s' for script '%s'."), *PythonExe, *ScriptPath);
		FPlatformProcess::ClosePipe(ReadPipe, WritePipe);
		return;
	}

	constexpr double TimeoutSeconds = 600.0;
	const double StartTime = FPlatformTime::Seconds();
	FString Output;
	while (FPlatformProcess::IsProcRunning(ProcHandle))
	{
		Output += FPlatformProcess::ReadPipe(ReadPipe);
		if ((FPlatformTime::Seconds() - StartTime) > TimeoutSeconds)
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Python script '%s' timed out after %.0f seconds; terminating."), *ScriptPath, TimeoutSeconds);
			FPlatformProcess::TerminateProc(ProcHandle, true);
			break;
		}
		FPlatformProcess::Sleep(0.01f);
	}

	Output += FPlatformProcess::ReadPipe(ReadPipe);

	int32 ReturnCode = 0;
	FPlatformProcess::GetProcReturnCode(ProcHandle, &ReturnCode);
	FPlatformProcess::CloseProc(ProcHandle);
	FPlatformProcess::ClosePipe(ReadPipe, WritePipe);

	if (ReturnCode != 0)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Python script '%s' exited with code %d:\n%s"), *ScriptPath, ReturnCode, *Output);
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Display, TEXT("Python Output:\n%s"), *Output);
	}
}

void UOpenDriveToMap::RunPythonMergeTiles()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Running Python merge rendered tiles script..."));

	FString PluginPath = UGenerationPathsHelper::GetDigitalTwinsPluginPath();
	FString ScriptPath = PluginPath / TEXT("Content/Python/merge_images.py");
	FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);

	FString Args;
	Args += FString::Printf(TEXT("\"%s\" "), *ScriptPath);
	Args += FString::Printf(TEXT("--folder_path=\"%s\" "), *OutPath);

	RunPythonScript(ScriptPath, Args);
}

void UOpenDriveToMap::RunPythonRoadEdges()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Running Python road edges extraction script..."));

	FString PluginPath = UGenerationPathsHelper::GetDigitalTwinsPluginPath();
	FString ScriptPath = PluginPath / TEXT("Content/Python/road_edge_detection.py");
	FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);

	FString Args;
	Args += FString::Printf(TEXT("\"%s\" "), *ScriptPath);
	Args += FString::Printf(TEXT("--folder_path=\"%s\" "), *OutPath);

	RunPythonScript(ScriptPath, Args);
}

TArray<FRoadSignInfo> UOpenDriveToMap::GetAllRoadSignsInfo()
{
	TArray<FRoadSignInfo> RoadSigns;
	if (!CarlaMap.has_value())
	{
		return RoadSigns;
	}

	const carla::road::Map& Map = CarlaMap.value();
	auto Signals = Map.GetAllSignalReferences();

	for (size_t i = 0; i < Signals.size(); ++i)
	{
		const auto& SignalRef = Signals[i];
		if (!SignalRef)
		{
			continue;
		}

		const auto* Signal = SignalRef->GetSignal();
		if (!Signal)
		{
			continue;
		}

		FRoadSignInfo Info;
		Info.SignId = FString(UTF8_TO_TCHAR(SignalRef->GetSignalId().c_str()));
		Info.RoadId = FString::FromInt(SignalRef->GetRoadId());
		Info.S = SignalRef->GetS();
		Info.T = SignalRef->GetT();

		auto signalOrientation = SignalRef->GetOrientation();
		switch (signalOrientation)
		{
			case carla::road::SignalOrientation::Positive:
				Info.Orientation = TEXT("Positive");
				break;

			case carla::road::SignalOrientation::Negative:
				Info.Orientation = TEXT("Negative");
				break;

			case carla::road::SignalOrientation::Both:
				Info.Orientation = TEXT("Both");
				break;

			default:
				Info.Orientation = TEXT("Unknown");
				break;
		}

		const auto SignalTransform = Signal->GetTransform();
		Info.Type = FString(UTF8_TO_TCHAR(Signal->GetType().c_str()));
		Info.Subtype = FString(UTF8_TO_TCHAR(Signal->GetSubtype().c_str()));
		Info.Name = FString(UTF8_TO_TCHAR(Signal->GetName().c_str()));
		Info.Value = Signal->GetValue();
		Info.Unit = FString(UTF8_TO_TCHAR(Signal->GetUnit().c_str()));
		Info.Country = FString(UTF8_TO_TCHAR(Signal->GetCountry().c_str()));
		Info.bIsDynamic = Signal->GetDynamic();
		Info.Transform = SignalTransform;

		RoadSigns.Add(Info);
	}

	return RoadSigns;
}

void UOpenDriveToMap::ExportStaticMeshToOBJ(UStaticMesh* StaticMesh, const FString& OutputPath)
{
	UE_LOG(LogTemp, Log, TEXT("Exporting mesh as OBJ to path: %s"), *OutputPath);

	if (!StaticMesh || !StaticMesh->GetRenderData() || StaticMesh->GetRenderData()->LODResources.Num() == 0)
	{
		UE_LOG(LogTemp, Warning, TEXT("StaticMesh is null or has no LOD render data."));
		return;
	}

	const FStaticMeshLODResources& LOD = StaticMesh->GetRenderData()->LODResources[0];

	const FPositionVertexBuffer& PositionVertexBuffer = LOD.VertexBuffers.PositionVertexBuffer;
	const FStaticMeshVertexBuffer& VertexBuffer = LOD.VertexBuffers.StaticMeshVertexBuffer;
	const int32 VertexCount = static_cast<int32>(PositionVertexBuffer.GetNumVertices());
	const FIndexArrayView Indices = LOD.IndexBuffer.GetArrayView();
	const int32 NumTriangles = Indices.Num() / 3;

	FString ObjData;
	ObjData.Reserve(VertexCount * 96 + NumTriangles * 48 + 128);
	ObjData += FString::Printf(TEXT("# Exported OBJ from %s\n"), *StaticMesh->GetName());

	for (int32 i = 0; i < VertexCount; ++i)
	{
		FVector Pos = FVector(PositionVertexBuffer.VertexPosition(i));
		Pos.Z = GetHeight(Pos.X, Pos.Y) / 100.0f;
		ObjData += FString::Printf(TEXT("v %f %f %f\n"), Pos.X, Pos.Z, Pos.Y);
	}

	const int32 NormalCount = static_cast<int32>(VertexBuffer.GetNumVertices());
	for (int32 i = 0; i < VertexCount; ++i)
	{
		const FVector Normal = (i < NormalCount) ? FVector(VertexBuffer.VertexTangentZ(i)) : FVector::UpVector;
		ObjData += FString::Printf(TEXT("vn %f %f %f\n"), Normal.X, Normal.Z, Normal.Y);
	}

	const bool bHasUVs = (VertexBuffer.GetNumTexCoords() > 0 && NormalCount >= VertexCount);
	for (int32 i = 0; i < VertexCount; ++i)
	{
		const FVector2D UV = bHasUVs ? FVector2D(VertexBuffer.GetVertexUV(i, 0)) : FVector2D::ZeroVector;
		ObjData += FString::Printf(TEXT("vt %f %f\n"), UV.X, 1.0f - UV.Y);
	}

	for (int32 i = 0; i < NumTriangles; ++i)
	{
		const int32 i0 = Indices[i * 3 + 0] + 1;
		const int32 i1 = Indices[i * 3 + 1] + 1;
		const int32 i2 = Indices[i * 3 + 2] + 1;
		ObjData += FString::Printf(TEXT("f %d/%d/%d %d/%d/%d %d/%d/%d\n"), i0, i0, i0, i1, i1, i1, i2, i2, i2);
	}

	FPlatformFileManager::Get().GetPlatformFile().CreateDirectoryTree(*FPaths::GetPath(OutputPath));
	if (FFileHelper::SaveStringToFile(ObjData, *OutputPath))
	{
		UE_LOG(LogTemp, Log, TEXT("Successfully exported to OBJ: %s"), *OutputPath);
	}
	else
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to save OBJ file: %s"), *OutputPath);
	}
}

void UOpenDriveToMap::MergeRoads()
{
	UE_LOG(LogTemp, Log, TEXT("Merging roads to single mesh..."));

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	TArray<AActor*> FoundActors;
	UGameplayStatics::GetAllActorsOfClass(World, AStaticMeshActor::StaticClass(), FoundActors);

	TArray<UPrimitiveComponent*> ComponentsToMerge;

	for (AActor* Actor : FoundActors)
	{
		if (IsValid(Actor) && Actor->GetActorLabel().StartsWith(TEXT("SM_DrivingLane_")))
		{
			if (UStaticMeshComponent* SMC =
					Cast<UStaticMeshComponent>(Actor->GetComponentByClass(UStaticMeshComponent::StaticClass())))
			{
				ComponentsToMerge.Add(SMC);
			}
		}
	}

	if (ComponentsToMerge.Num() == 0)
	{
		UE_LOG(LogTemp, Warning, TEXT("No components to merge"));
		return;
	}

	FMeshMergingSettings MergeSettings;
	MergeSettings.bMergeMaterials = true;
#if ENGINE_MAJOR_VERSION > 5 || (ENGINE_MAJOR_VERSION == 5 && ENGINE_MINOR_VERSION >= 8)
	MergeSettings.PivotType = EMeshMergePivotType::WorldOrigin;
#else
	MergeSettings.bPivotPointAtZero = true;
#endif

	const FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);
	const FString AssetName = TEXT("MergedRoad");
	const FString SafeMapSegment = MapName.IsEmpty() ? TEXT("Default") : FPaths::MakeValidFileName(MapName);
	const FString PackageName = FString::Printf(TEXT("/Game/ImportedMeshes/%s/%s"), *SafeMapSegment, *AssetName);
	UPackage* Package = CreatePackage(*PackageName);

	IMeshMergeUtilities& MeshMergeUtilities =
		FModuleManager::LoadModuleChecked<IMeshMergeModule>("MeshMergeUtilities").GetUtilities();

	TArray<UObject*> AssetsToSync;
	FVector MergedActorLocation = FVector::ZeroVector;

	MeshMergeUtilities.MergeComponentsToStaticMesh(ComponentsToMerge, World, MergeSettings,
		nullptr,
		Package,
		AssetName, AssetsToSync, MergedActorLocation,
		1.0f,
		false
	);

	UE_LOG(LogTemp, Log, TEXT("Merged mesh saved to package: %s"), *PackageName);

	for (UObject* Asset : AssetsToSync)
	{
		if (UStaticMesh* MergedMesh = Cast<UStaticMesh>(Asset))
		{
			const FString ObjPath = OutPath / FString::Printf(TEXT("MergedRoad_%.4f_%.4f.obj"), WorldEndPosition.X, WorldEndPosition.Y);
			ExportStaticMeshToOBJ(MergedMesh, ObjPath);
			break;
		}
	}
}

void UOpenDriveToMap::RunPythonRoadSegmentation()
{
	FString PluginPath = UGenerationPathsHelper::GetDigitalTwinsPluginPath();
	FString ScriptPath = PluginPath / TEXT("Content/Python/road_segmentation.py");
	FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);

	FString Args;
	Args += FString::Printf(TEXT("\"%s\" "), *ScriptPath);
	Args += FString::Printf(TEXT("--lon_min=%.8f "), OriginGeoCoordinates.Y);
	Args += FString::Printf(TEXT("--lat_min=%.8f "), OriginGeoCoordinates.X);
	Args += FString::Printf(TEXT("--lon_max=%.8f "), FinalGeoCoordinates.Y);
	Args += FString::Printf(TEXT("--lat_max=%.8f "), FinalGeoCoordinates.X);
	Args += FString::Printf(TEXT("--output_path=\"%s\" "), *OutPath);

	RunPythonScript(ScriptPath, Args);
}

void UOpenDriveToMap::RunPythonMitsubaOptimization()
{
	FString PluginPath = UGenerationPathsHelper::GetDigitalTwinsPluginPath();
	FString ScriptPath = PluginPath / TEXT("Content/Python/mitsuba_road_refiner.py");
	FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);

	FString Args;
	Args += FString::Printf(TEXT("\"%s\" "), *ScriptPath);
	Args += FString::Printf(TEXT("--folder_path=\"%s\" "), *OutPath);

	RunPythonScript(ScriptPath, Args);
}

void UOpenDriveToMap::MitsubaMeshOptimization()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Running road mesh optimization with Mitsuba..."));

	UWorld* World = GetEditorWorld();
	if (!World)
	{
		return;
	}

	MergeRoads();
	RunPythonRoadSegmentation();
	RunPythonMitsubaOptimization();

	FString OutPath = UGenerationPathsHelper::GetPythonIntermediatePath(MapName);
	FString ObjPath = OutPath / TEXT("updated_road.obj");
	UGeometryImporter::ImportObj(ObjPath, World, DefaultRoadMaterial);

	// Hide DrivingLane actors
	TArray<AActor*> Actors;
	UGameplayStatics::GetAllActorsOfClass(World, AActor::StaticClass(), Actors);
	for (AActor* Actor : Actors)
	{
		if (!IsValid(Actor))
		{
			continue;
		}
		const FString Name = Actor->GetActorLabel();
		if (Name.Contains(TEXT("DrivingLane"), ESearchCase::CaseSensitive))
		{
			Actor->SetActorHiddenInGame(true);
			Actor->SetIsTemporarilyHiddenInEditor(true);
		}
	}
}
bool UOpenDriveToMap::TryResolveContentFileAnywhere(const FString& RelativePathUnderContent, FString& OutAbsPath)
{
	if (RelativePathUnderContent.IsEmpty())
	{
		return false;
	}

	IPluginManager& PM{IPluginManager::Get()};
	const TArray<TSharedRef<IPlugin>> Plugins{PM.GetEnabledPlugins()};
	for (const TSharedRef<IPlugin>& Plugin : Plugins)
	{
		if (!Plugin->CanContainContent())
		{
			continue;
		}

		const FString Candidate{FPaths::Combine(Plugin->GetContentDir(), RelativePathUnderContent)};
		if (FPaths::FileExists(Candidate))
		{
			OutAbsPath = Candidate;
			return true;
		}
	}

	const FString ProjectCandidate{FPaths::Combine(FPaths::ProjectContentDir(), RelativePathUnderContent)};
	if (FPaths::FileExists(ProjectCandidate))
	{
		OutAbsPath = ProjectCandidate;
		return true;
	}

	return false;
}
#endif

FString UOpenDriveToMap::ResolveLevelXodrPath(const UWorld* World)
{
	if (!World)
	{
		return FString();
	}

	FString MapShortName = World->GetMapName();
	MapShortName.RemoveFromStart(World->StreamingLevelsPrefix);
	const FString LevelName = FPackageName::GetShortName(MapShortName);
	if (LevelName.IsEmpty())
	{
		return FString();
	}

	TArray<FString> Candidates;
	if (UPackage* WorldPackage = World->GetOutermost())
	{
		FString MapFilePath;
		if (FPackageName::TryConvertLongPackageNameToFilename(
				WorldPackage->GetName(), MapFilePath, FPackageName::GetMapPackageExtension()))
		{
			const FString MapDir = FPaths::GetPath(MapFilePath);
			Candidates.AddUnique(FPaths::Combine(MapDir, TEXT("OpenDrive"), LevelName + TEXT(".xodr")));
			Candidates.AddUnique(FPaths::Combine(MapDir, LevelName + TEXT(".xodr")));
		}
	}
	Candidates.AddUnique(FPaths::Combine(
		FPaths::ProjectContentDir(), TEXT("Carla/Maps/Twins"), LevelName, TEXT("OpenDrive"), LevelName + TEXT(".xodr")));
	Candidates.AddUnique(FPaths::Combine(
		FPaths::ProjectContentDir(), TEXT("Carla/Maps/OpenDrive"), LevelName + TEXT(".xodr")));
	const FString LegacyPath = FPaths::Combine(
		UGenerationPathsHelper::GetRawMapDirectoryPath(LevelName), TEXT("OpenDrive"), LevelName + TEXT(".xodr"));
	Candidates.AddUnique(LegacyPath);

	for (const FString& Candidate : Candidates)
	{
		if (FPaths::FileExists(Candidate))
		{
			return Candidate;
		}
	}
	return LegacyPath;
}

FVector UOpenDriveToMap::DisplaceLocationOutsideNeighboringRoadsWithMap(
	const carla::road::Map& Map,
	FVector InLocation,
	float SteppingPercentage,
	float RoadLimitPadding,
	int32 MaxIterations,
	bool* bOutIsClear)
{
	using carla::road::Lane;
	using carla::road::element::Waypoint;

	(void)SteppingPercentage;

	const int32 CarriagewayLanes =
		static_cast<int32>(Lane::LaneType::Driving) |
		static_cast<int32>(Lane::LaneType::Shoulder) |
		static_cast<int32>(Lane::LaneType::Parking) |
		static_cast<int32>(Lane::LaneType::Biking) |
		static_cast<int32>(Lane::LaneType::Bidirectional);
	const double Padding = FMath::Max(RoadLimitPadding, 0.0f) * 1e-2; // cm -> m

	carla::geom::Location Position(InLocation); // cm -> m
	bool bClear = false;
	const int32 ClampedIterations = FMath::Max(MaxIterations, 0);

	for (int32 Iteration = 0; Iteration <= ClampedIterations; ++Iteration)
	{
		boost::optional<Waypoint> Closest = Map.GetClosestWaypointOnRoad(Position, CarriagewayLanes);
		if (!Closest.has_value())
		{
			bClear = true;
			break;
		}

		// Snap Z to the lane elevation first so 3D nearest-waypoint lookup works on bridges/hills.
		Position.z = Map.ComputeTransform(Closest.get()).location.z;
		Closest = Map.GetClosestWaypointOnRoad(Position, CarriagewayLanes);
		if (!Closest.has_value())
		{
			bClear = true;
			break;
		}

		const carla::geom::Transform LaneTransform = Map.ComputeTransform(Closest.get());
		const double Distance = carla::geom::Math::Distance2D(LaneTransform.location, Position);
		if (Distance >= FMath::Abs(Map.GetLaneWidth(Closest.get())) * 0.5 + Padding)
		{
			bClear = true;
			break;
		}
		if (Iteration == ClampedIterations)
		{
			break;
		}

		// Walk outward to the outermost carriageway lane on this side of the road.
		Waypoint Outer = Closest.get();
		for (int32 Step = 0; Step < 16 && Outer.lane_id != 0; ++Step)
		{
			const boost::optional<Waypoint> Next = Map.GetRight(Outer);
			if (!Next.has_value() || (static_cast<int32>(Map.GetLaneType(Next.get())) & CarriagewayLanes) == 0)
			{
				break;
			}
			Outer = Next.get();
		}

		const carla::geom::Transform OuterTransform = Map.ComputeTransform(Outer);
		const carla::geom::Vector3D Outward = OuterTransform.GetRightVector();
		const float Offset = static_cast<float>(FMath::Abs(Map.GetLaneWidth(Outer)) * 0.5 + Padding + 0.01);
		Position = carla::geom::Location(
			static_cast<float>(OuterTransform.location.x + Outward.x * Offset),
			static_cast<float>(OuterTransform.location.y + Outward.y * Offset),
			static_cast<float>(OuterTransform.location.z));
	}

	if (bOutIsClear != nullptr)
	{
		*bOutIsClear = bClear;
	}
	return FVector(Position.x * 100.0f, Position.y * 100.0f, InLocation.Z);
}

FVector UOpenDriveToMap::DisplaceLocationOutsideNeighboringRoads(
	const UObject* WorldContextObject,
	FVector InLocation,
	float SteppingPercentage,
	float RoadLimitPadding)
{
	if (!GEngine)
	{
		return InLocation;
	}
	UWorld* World = GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::LogAndReturnNull);
	if (!World)
	{
		return InLocation;
	}
	const FString file_path = ResolveLevelXodrPath(World);
	if (file_path.IsEmpty())
	{
		return InLocation;
	}

	FString FileContent;
	if (!FFileHelper::LoadFileToString(FileContent, *file_path))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("DisplaceLocationOutsideNeighboringRoads: Failed to load %s"), *file_path);
		return InLocation;
	}
	std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);

	boost::optional<carla::road::Map> current_carla_map = carla::opendrive::OpenDriveParser::Load(opendrive_xml);
	if (!current_carla_map.has_value())
	{
		return InLocation;
	}

	return DisplaceLocationOutsideNeighboringRoadsWithMap(
		current_carla_map.get(),
		InLocation,
		SteppingPercentage,
		RoadLimitPadding,
		10);
}
