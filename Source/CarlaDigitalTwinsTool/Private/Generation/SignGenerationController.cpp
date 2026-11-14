// Fill out your copyright notice in the Description page of Project Settings.


#include "Generation/SignGenerationController.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "AssetRegistry/AssetRegistryHelpers.h"
#include "Engine/StaticMeshActor.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Engine/StaticMesh.h"
#include "StaticMeshResources.h"
#include "Paths/GenerationPathsHelper.h"
#include "Carla/OpenDrive/OpenDriveParser.h"
#include "Carla/RPC/String.h"
#include "Carla/Road/element/RoadWaypoint.h"
#include <boost/optional.hpp>
#include "Generation/OpenDriveToMap.h"
#include "Kismet/KismetMathLibrary.h"
#include "DrawDebugHelpers.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Engine/Texture.h"
#include "UObject/ConstructorHelpers.h"
#include "Components/MeshComponent.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"

namespace
{
FString GetCleanLevelName(UWorld* World)
{
	if (!World)
	{
		return FString();
	}
	FString MapName = World->GetMapName();
	MapName.RemoveFromStart(World->StreamingLevelsPrefix);
	return FPackageName::GetShortName(MapName);
}

int32 GetSignCategoryPriority(ESignCategory Category)
{
	switch (Category)
	{
		case ESignCategory::Priority:    return 0;
		case ESignCategory::Prohibitory: return 1;
		case ESignCategory::SpeedLimit:  return 2;
		case ESignCategory::Warning:     return 3;
		case ESignCategory::Mandatory:   return 4;
		case ESignCategory::Information: return 5;
		case ESignCategory::Guide:       return 6;
		case ESignCategory::Others:      return 7;
		case ESignCategory::None:
		default:                         return 8;
	}
}
}

void ASignGenerationController::GetSteetMapFile()
{
	UWorld* World = GetWorld();
	if (!World)
	{
		return;
	}

	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();
	const FString LevelName = GetCleanLevelName(World);
	if (LevelName.IsEmpty())
	{
		return;
	}

	TArray<FString> CandidatePaths;
	if (UPackage* WorldPackage = World->GetOutermost())
	{
		const FString PackageFolder = FPackageName::GetLongPackagePath(WorldPackage->GetName());
		if (!PackageFolder.IsEmpty())
		{
			CandidatePaths.AddUnique(PackageFolder);
			CandidatePaths.AddUnique(PackageFolder + TEXT("/OpenDrive"));
		}
	}
	CandidatePaths.AddUnique(FString::Printf(TEXT("/Game/Carla/Maps/Twins/%s"), *LevelName));
	CandidatePaths.AddUnique(FString::Printf(TEXT("/Game/Carla/Maps/Twins/%s/OpenDrive"), *LevelName));
	CandidatePaths.AddUnique(FString::Printf(TEXT("/%s/Maps/OpenDrive"), *LevelName));
	CandidatePaths.AddUnique(FString::Printf(TEXT("/%s/OpenDrive"), *LevelName));
	if (UPackage* WorldPackage = World->GetOutermost())
	{
		const FString MountPoint = FPackageName::GetPackageMountPoint(WorldPackage->GetName()).ToString();
		if (!MountPoint.IsEmpty() && MountPoint != TEXT("None"))
		{
			CandidatePaths.AddUnique(FString::Printf(TEXT("/%s/Maps/OpenDrive"), *MountPoint));
			CandidatePaths.AddUnique(FString::Printf(TEXT("/%s/OpenDrive"), *MountPoint));
		}
	}
	CandidatePaths.AddUnique(FString::Printf(TEXT("/Game/Carla/Maps/%s/OpenDrive"), *LevelName));
	CandidatePaths.AddUnique(TEXT("/Game/Maps/OpenDrive"));

	for (const FString& CandidatePath : CandidatePaths)
	{
		TArray<FAssetData> street_map_data_assets;
		AssetRegistry.GetAssetsByPath(FName(*CandidatePath), street_map_data_assets, true);

		for (const FAssetData& asset : street_map_data_assets)
		{
			StreetMapData = Cast<UStreetMap>(asset.GetAsset());
			if (StreetMapData != nullptr)
			{
				return;
			}
		}
	}
}

// Sets default values
ASignGenerationController::ASignGenerationController(const FObjectInitializer& ObjectInitializer)
	: Super(ObjectInitializer)
{
	PrimaryActorTick.bCanEverTick = false;
	MaxDisplacementIterations = 20;
	RoadBorderPadding = 50.0f;
	StepPercentOfLaneWidth = 0.33f;
	bDisplaceSignsToEdge = true;
	has_spawned_sign = false;
}

// Called when the game starts or when spawned
void ASignGenerationController::BeginPlay()
{
	Super::BeginPlay();
	
}

// Called every frame
void ASignGenerationController::Tick(float DeltaTime)
{
	Super::Tick(DeltaTime);
}

void ASignGenerationController::SignGenerationByPath(FName sign_package_path, FName pole_package_path, ESignStyle sign_style)
{
	UWorld* World = GetWorld();
	if (!World)
	{
		return;
	}

	if (!IsValid(StreetMapData))
	{
		GetSteetMapFile();
	}
	if (!IsValid(StreetMapData))
	{
		UE_LOG(LogTemp, Warning, TEXT("SignGenerationByPath: StreetMapData not found."));
		return;
	}

	const FString FilePath = UOpenDriveToMap::ResolveLevelXodrPath(World);

	FString FileContent;
	if (!FFileHelper::LoadFileToString(FileContent, *FilePath))
	{
		UE_LOG(LogTemp, Error, TEXT("SignGenerationByPath: Failed to read OpenDRIVE file at %s"), *FilePath);
		return;
	}

	std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);
	boost::optional<carla::road::Map> current_carla_map = carla::opendrive::OpenDriveParser::Load(opendrive_xml);
	if (!current_carla_map.has_value())
	{
		UE_LOG(LogTemp, Error, TEXT("SignGenerationByPath: Failed to parse OpenDRIVE map at %s"), *FilePath);
		return;
	}

	// When creating actors again, destroy all the previously created and clear the array.
	for (AActor* entry : GeneratedSigns)
	{
		if (IsValid(entry))
		{
			entry->Destroy();
		}
	}
	GeneratedSigns.Empty();

	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();

	FString EnumName = UEnum::GetValueAsString(sign_style);
	int32 SeparatorIndex = INDEX_NONE;
	if (EnumName.FindLastChar(TEXT(':'), SeparatorIndex))
	{
		EnumName = EnumName.Mid(SeparatorIndex + 1);
	}
	FString FullPath = sign_package_path.ToString() + TEXT("/") + EnumName;
	FName FullPathName = FName(*FullPath);

	UE_LOG(LogTemp, Warning, TEXT("SignPackagePath: %s"), *FullPathName.ToString());

	PolePackagePath = pole_package_path;

	TArray<FAssetData> temp_sign;
	AssetRegistry.GetAssetsByPath(FullPathName, temp_sign, true);
	TArray<FAssetData> temp_pole;
	AssetRegistry.GetAssetsByPath(PolePackagePath, temp_pole, true);

	TArray<USignDataAsset*> sign_data;
	for (const FAssetData& asset_data : temp_sign)
	{
		USignDataAsset* temp = Cast<USignDataAsset>(asset_data.GetAsset());
		if (temp != nullptr)
		{
			sign_data.Add(temp);
		}
	}
	TArray<UPoleDataAsset*> pole_data;
	for (const FAssetData& asset_data : temp_pole)
	{
		UPoleDataAsset* temp = Cast<UPoleDataAsset>(asset_data.GetAsset());
		if (temp != nullptr)
		{
			pole_data.Add(temp);
		}
	}

	TArray<UStaticMesh*> MatchingPoleMeshes;
	for (UPoleDataAsset* PoleAsset : pole_data)
	{
		if (PoleAsset && PoleAsset->PoleMesh && PoleAsset->SignStyle == sign_style)
		{
			MatchingPoleMeshes.AddUnique(PoleAsset->PoleMesh);
		}
	}
	if (MatchingPoleMeshes.IsEmpty())
	{
		for (UPoleDataAsset* PoleAsset : pole_data)
		{
			if (PoleAsset && PoleAsset->PoleMesh)
			{
				MatchingPoleMeshes.AddUnique(PoleAsset->PoleMesh);
			}
		}
	}

	UMaterialInterface* BaseMaterial = LoadObject<UMaterialInterface>(
		nullptr,
		TEXT("/CarlaDigitalTwinsTool/Carla/Static/Signs/Materials/Atlas/MI_SignTextureAtlasSelector"));

	TMap<FString, int> spawn_name_counters;
	TMap<AActor*, ESignCategory> SpawnedSignCategories;

	static const TCHAR* PriorityKeys[] = {
		TEXT("traffic_sign"),
		TEXT("highway"),
		TEXT("maxspeed"),
		TEXT("crossing"),
		TEXT("hazard"),
		TEXT("amenity"),
	};

	for (const FStreetMapMisc& sign : StreetMapData->GetSigns())
	{
		has_spawned_sign = false;

		TArray<TPair<FString, FString>> OrderedTags;
		for (const TCHAR* Key : PriorityKeys)
		{
			if (const FString* Val = sign.Properties.Find(Key))
			{
				if (!Val->IsEmpty())
				{
					OrderedTags.Emplace(Key, *Val);
				}
			}
		}
		for (const auto& Kv : sign.Properties)
		{
			if (Kv.Value.IsEmpty())
			{
				continue;
			}
			const bool bAlreadyAdded = OrderedTags.ContainsByPredicate(
				[&Kv](const TPair<FString, FString>& Pair) { return Pair.Key == Kv.Key; });
			if (!bAlreadyAdded)
			{
				OrderedTags.Emplace(Kv.Key, Kv.Value);
			}
		}

		if (OrderedTags.IsEmpty())
		{
			continue;
		}

		USignDataAsset* MatchedAsset = nullptr;
		FString keyname;
		FString signValue;

		// 1. Exact CARLA 1.0 catalog OSM tag match ("key=value" in USignDataAsset::OsmTags)
		for (const TPair<FString, FString>& Tag : OrderedTags)
		{
			for (USignDataAsset* sign_asset : sign_data)
			{
				if (sign_asset && sign_asset->MatchesOsmTag(Tag.Key, Tag.Value))
				{
					MatchedAsset = sign_asset;
					keyname = Tag.Key;
					signValue = Tag.Value;
					break;
				}
			}
			if (MatchedAsset)
			{
				break;
			}
		}

		// 2. Legacy DataAsset fallback (SignName / asset display name substring match)
		if (!MatchedAsset)
		{
			for (const TPair<FString, FString>& Tag : OrderedTags)
			{
				if (Tag.Key != TEXT("highway") && Tag.Key != TEXT("crossing") && Tag.Key != TEXT("maxspeed"))
				{
					continue;
				}
				for (USignDataAsset* sign_asset : sign_data)
				{
					if (!sign_asset)
					{
						continue;
					}
					const bool bNameMatch = !sign_asset->SignName.IsEmpty()
						? sign_asset->SignName.Contains(Tag.Value)
						: UKismetSystemLibrary::GetDisplayName(sign_asset).Contains(Tag.Value);
					if (bNameMatch)
					{
						MatchedAsset = sign_asset;
						keyname = Tag.Key;
						signValue = Tag.Value;
						break;
					}
				}
				if (MatchedAsset)
				{
					break;
				}
			}
		}

		if (MatchedAsset)
		{
			UE_LOG(LogTemp, Log, TEXT("SignValue detected is: %s=%s"), *keyname, *signValue);

			int& counter = spawn_name_counters.FindOrAdd(signValue);
			counter++;

			FString actor_name = TEXT("TrafficSign_") + keyname + TEXT("_") + signValue + TEXT("_");
			actor_name.AppendInt(counter);

			AStaticMeshActor* temp_actor = World->SpawnActor<AStaticMeshActor>();
			if (!IsValid(temp_actor))
			{
				continue;
			}

			temp_actor->SetActorLocation(FVector(sign.Position.X, sign.Position.Y, 0.0f));
			temp_actor->SetActorLabel(actor_name);

			UStaticMesh* SelectedPoleMesh = nullptr;
			if (!MatchingPoleMeshes.IsEmpty())
			{
				const uint32 PosHash = HashCombine(
					GetTypeHash(FMath::RoundToInt(sign.Position.X)),
					GetTypeHash(FMath::RoundToInt(sign.Position.Y)));
				SelectedPoleMesh = MatchingPoleMeshes[PosHash % static_cast<uint32>(MatchingPoleMeshes.Num())];
			}

			if (MatchedAsset->SignMesh != nullptr && SelectedPoleMesh != nullptr)
			{
				UStaticMeshComponent* pole_mesh_comp = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(temp_actor);
				UStaticMeshComponent* sign_mesh_comp = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(temp_actor);
				if (pole_mesh_comp && sign_mesh_comp)
				{
					pole_mesh_comp->SetStaticMesh(SelectedPoleMesh);
					pole_mesh_comp->ComponentTags.Add(FName(TEXT("pole")));

					sign_mesh_comp->SetStaticMesh(MatchedAsset->SignMesh);

					if (MatchedAsset->Material != nullptr)
					{
						sign_mesh_comp->SetMaterial(0, MatchedAsset->Material);
					}
					else if (BaseMaterial)
					{
						UMaterialInstanceDynamic* DynamicMaterial = UMaterialInstanceDynamic::Create(BaseMaterial, temp_actor);
						if (DynamicMaterial)
						{
							DynamicMaterial->SetTextureParameterValue(TEXT("Diffuse"), MatchedAsset->Diffuse);
							DynamicMaterial->SetScalarParameterValue(TEXT("Index_X"), static_cast<float>(FMath::Max(1, MatchedAsset->Id_X)));
							DynamicMaterial->SetScalarParameterValue(TEXT("Index_Y"), static_cast<float>(FMath::Max(1, MatchedAsset->Id_Y)));
							DynamicMaterial->SetScalarParameterValue(TEXT("Scale"), MatchedAsset->Unique ? 1.0f : 4.0f);
							sign_mesh_comp->SetMaterial(0, DynamicMaterial);
						}
					}

					const FName SignSocketName(TEXT("Sign1"));
					if (pole_mesh_comp->DoesSocketExist(SignSocketName))
					{
						sign_mesh_comp->SetWorldTransform(pole_mesh_comp->GetSocketTransform(SignSocketName));
					}
					else
					{
						sign_mesh_comp->SetRelativeLocation(FVector(0.0f, 0.0f, 220.0f));
						sign_mesh_comp->SetRelativeRotation(FRotator(0.0f, -90.0f, 0.0f));
					}
					sign_mesh_comp->ComponentTags.Add(FName(TEXT("sign")));
				}
			}
			else
			{
				UE_LOG(LogTemp, Warning, TEXT("SignGenerationByPath: Missing SignMesh or PoleMesh for %s"), *actor_name);
			}

			GeneratedSigns.Add(temp_actor);
			SpawnedSignCategories.Add(temp_actor, MatchedAsset->Category);
			has_spawned_sign = true;
		}
	}

	// Moving Signals to Sidewalk
	if (bDisplaceSignsToEdge)
	{
		const int32 check_shoulder_or_driving =
			static_cast<int32_t>(carla::road::Lane::LaneType::Shoulder) |
			static_cast<int32_t>(carla::road::Lane::LaneType::Driving) |
			static_cast<int32_t>(carla::road::Lane::LaneType::Parking) |
			static_cast<int32_t>(carla::road::Lane::LaneType::Biking) |
			static_cast<int32_t>(carla::road::Lane::LaneType::Bidirectional);

		FCollisionQueryParams TraceParams(SCENE_QUERY_STAT(SignGroundSnap), true);
		TraceParams.AddIgnoredActors(GeneratedSigns);

		for (AActor* sign : GeneratedSigns)
		{
			if (!IsValid(sign))
			{
				continue;
			}

			FVector sign_location = sign->GetActorLocation();
			bool bClear = false;
			sign_location = UOpenDriveToMap::DisplaceLocationOutsideNeighboringRoadsWithMap(
				current_carla_map.get(),
				sign_location,
				StepPercentOfLaneWidth,
				RoadBorderPadding,
				FMath::Max(1, MaxDisplacementIterations),
				&bClear);
			if (!bClear)
			{
				UE_LOG(LogTemp, Warning, TEXT("SignGenerationByPath: %s remained in carriageway at %s; removing"),
					*sign->GetActorLabel(), *sign_location.ToString());
				sign->Destroy();
				continue;
			}
			sign->SetActorLocation(sign_location);

			bool bHasRoadZ = false;
			float RoadElevationCm = 0.0f;
			boost::optional<carla::road::element::Waypoint> closest_waypoint =
				current_carla_map->GetClosestWaypointOnRoad(sign_location, check_shoulder_or_driving);

			if (closest_waypoint.has_value())
			{
				carla::geom::Transform road_transform = current_carla_map->ComputeTransform(closest_waypoint.get());
				bHasRoadZ = true;
				RoadElevationCm = road_transform.location.z * 100.0f;

				std::vector<carla::road::element::Waypoint> succesor_waypoints =
					current_carla_map->GetSuccessors(closest_waypoint.get());

				if (!succesor_waypoints.empty())
				{
					carla::road::element::Waypoint next_waypoint = succesor_waypoints[0];
					carla::geom::Transform road_transform_next = current_carla_map->ComputeTransform(next_waypoint);

					const FVector Dir = FVector(road_transform.location - road_transform_next.location).GetSafeNormal();
					if (!Dir.IsNearlyZero())
					{
						FRotator rot = UKismetMathLibrary::MakeRotFromX(Dir);
						sign->SetActorRotation(rot);
					}
				}
			}

			FHitResult hit_result;
			const bool bHit = World->LineTraceSingleByChannel(
				hit_result,
				FVector(sign_location.X, sign_location.Y, 50000.0f),
				FVector(sign_location.X, sign_location.Y, -100000.0f),
				ECC_Visibility,
				TraceParams);

			if (bHit && hit_result.bBlockingHit)
			{
				sign_location.Z = hit_result.ImpactPoint.Z;
				sign->SetActorLocation(sign_location);
			}
			else if (bHasRoadZ)
			{
				sign_location.Z = RoadElevationCm;
				sign->SetActorLocation(sign_location);
			}
			else
			{
				sign->Destroy();
			}
		}

		GeneratedSigns.RemoveAll([](AActor* Actor)
		{
			return !IsValid(Actor) || Actor->IsActorBeingDestroyed();
		});
	}

	// Group generated sign actors by quantized world location (1 cm cells)
	TMap<FIntVector, TArray<AActor*>> LocationMap;

	for (AActor* Actor : GeneratedSigns)
	{
		if (!IsValid(Actor) || Actor->IsActorBeingDestroyed())
		{
			continue;
		}

		const FVector Location = Actor->GetActorLocation();
		const FIntVector Key(
			FMath::RoundToInt(Location.X),
			FMath::RoundToInt(Location.Y),
			FMath::RoundToInt(Location.Z));
		LocationMap.FindOrAdd(Key).Add(Actor);
	}

	// Stack signs sharing the same pole vertically from highest priority (top) to lowest (bottom)
	for (const auto& Elem : LocationMap)
	{
		TArray<AActor*> ActorsAtLocation = Elem.Value;
		if (ActorsAtLocation.Num() <= 1)
		{
			continue;
		}

		ActorsAtLocation.StableSort([&SpawnedSignCategories](const AActor& A, const AActor& B)
		{
			const ESignCategory CatA = SpawnedSignCategories.FindRef(&A);
			const ESignCategory CatB = SpawnedSignCategories.FindRef(&B);
			return GetSignCategoryPriority(CatA) < GetSignCategoryPriority(CatB);
		});

		AActor* FirstSignActor = ActorsAtLocation[0];
		if (!IsValid(FirstSignActor))
		{
			continue;
		}

		UActorComponent* PoleComp = FirstSignActor->FindComponentByTag(UStaticMeshComponent::StaticClass(), FName(TEXT("pole")));
		UStaticMeshComponent* PoleMesh = Cast<UStaticMeshComponent>(PoleComp);
		if (PoleMesh == nullptr)
		{
			continue;
		}

		const FName SignSocketName(TEXT("Sign1"));
		const FTransform FallbackLocal(FRotator(0.0f, -90.0f, 0.0f), FVector(0.0f, 0.0f, 220.0f));
		const FTransform SignTransform = PoleMesh->DoesSocketExist(SignSocketName)
			? PoleMesh->GetSocketTransform(SignSocketName)
			: (FallbackLocal * FirstSignActor->GetActorTransform());

		UE_LOG(LogTemp, Warning, TEXT("Found %d actors at location %s"),
			ActorsAtLocation.Num(), *FirstSignActor->GetActorLocation().ToString());

		for (int32 i = 1; i < ActorsAtLocation.Num(); i++)
		{
			AActor* CurrentActor = ActorsAtLocation[i];
			if (!IsValid(CurrentActor))
			{
				continue;
			}

			UActorComponent* SignComp = CurrentActor->FindComponentByTag(UStaticMeshComponent::StaticClass(), FName(TEXT("sign")));
			UStaticMeshComponent* SignMeshComp = Cast<UStaticMeshComponent>(SignComp);
			if (!SignMeshComp)
			{
				CurrentActor->Destroy();
				continue;
			}

			UStaticMeshComponent* SignComponent = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(FirstSignActor);
			if (SignComponent)
			{
				SignComponent->SetWorldTransform(FTransform(
					SignTransform.GetRotation(),
					FVector(
						SignTransform.GetLocation().X,
						SignTransform.GetLocation().Y,
						SignTransform.GetLocation().Z - 90.0f * static_cast<float>(i)),
					SignTransform.GetScale3D()));

				SignComponent->SetStaticMesh(SignMeshComp->GetStaticMesh());
				SignComponent->SetMaterial(0, SignMeshComp->GetMaterial(0));
				SignComponent->ComponentTags.Add(FName(TEXT("sign")));
			}

			CurrentActor->Destroy();
		}
	}

	GeneratedSigns.RemoveAll([](AActor* Actor)
	{
		return !IsValid(Actor) || Actor->IsActorBeingDestroyed();
	});
}

void ASignGenerationController::SignGenerationForCurrentMap()
{
	SignGenerationByPath(SignPackagePath, PolePackagePath, SignStyle);
}

void ASignGenerationController::GetSignPropertyValue(FStreetMapMisc Sign, FString KeyToFind, FString& Out_KeyName, FString& Out_Value)
{
	FString* FoundValue = Sign.Properties.Find(KeyToFind);

	if (FoundValue == nullptr)
	{
		Out_Value = "";
		Out_KeyName = "";
		return;
	}

	Out_Value = *FoundValue;
	Out_KeyName = KeyToFind;
}