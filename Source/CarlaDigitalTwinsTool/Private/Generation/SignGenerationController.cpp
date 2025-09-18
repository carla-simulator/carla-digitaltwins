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
#include "Generation/OpenDriveToMap.h"

void ASignGenerationController::GetSteetMapFile()
{
	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();
	FString LevelName = FPackageName::GetShortName(GetWorld()->GetMapName());
	FString StreetMapObjectPath = "/" + LevelName + "/Content/Maps/OpenDrive/";// + LevelName + ".osm";
	TArray<FAssetData> street_map_data_assets;
	AssetRegistry.GetAssetsByPath(FName(*StreetMapObjectPath), street_map_data_assets);

	for (FAssetData asset : street_map_data_assets)
	{
		StreetMapData = Cast<UStreetMap>(asset.GetAsset());
		if (StreetMapData != nullptr) break;
	}
}

// Sets default values
ASignGenerationController::ASignGenerationController(const FObjectInitializer& ObjectInitializer)
	: Super(ObjectInitializer)
{
 	// Set this actor to call Tick() every frame.  You can turn this off to improve performance if you don't need it.
	PrimaryActorTick.bCanEverTick = true;
	MaxDisplacementIterations = true;
	MaxDisplacementIterations = 30;
	RoadBorderPadding = 10.0f;
	StepPercentOfLaneWidth = 0.33f;
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

void ASignGenerationController::SignGenerationByPath(FName sign_package_path, FName pole_package_path)
{

	GetSteetMapFile();
	if (StreetMapData == nullptr) return;

	boost::optional<carla::road::Map> current_carla_map;

	FString FileContent;
	FString LevelName = FPackageName::GetShortName(GetWorld()->GetMapName());
	FString FilePath = UGenerationPathsHelper::GetRawMapDirectoryPath(LevelName) + "OpenDrive/" + LevelName + ".xodr";
	FFileHelper::LoadFileToString(FileContent, *FilePath);
	std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);
	//UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("UOpenDriveToMap::GenerateTile() Loading File..... "));
	current_carla_map = carla::opendrive::OpenDriveParser::Load(opendrive_xml);

	//for (const TPair<int32, AStaticMeshActor*> entry : GeneratedSigns)
	for (AActor* entry : GeneratedSigns)
	{
		//if(entry.Value != nullptr) entry.Value->Destroy();
		if (entry != nullptr) entry->Destroy();
	}

	GeneratedSigns.Empty();
	closest_waypoints.Empty();

	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();
	SignPackagePath = sign_package_path;
	PolePackagePath = pole_package_path;

	TArray<FAssetData> temp_sign;
	AssetRegistry.GetAssetsByPath(SignPackagePath, temp_sign, true);
	TArray<FAssetData> temp_pole;
	AssetRegistry.GetAssetsByPath(PolePackagePath, temp_pole, true);

	TArray<USignDataAsset*> sign_data;
	for (FAssetData asset_data : temp_sign)
	{
		USignDataAsset* temp = Cast<USignDataAsset>(asset_data.GetAsset());
		if (temp != nullptr) sign_data.Add(temp);
	}
	TArray<UPoleDataAsset*> pole_data;
	for (FAssetData asset_data : temp_pole)
	{
		UPoleDataAsset* temp = Cast<UPoleDataAsset>(asset_data.GetAsset());
		if (temp != nullptr) pole_data.Add(temp);
	}

	TMap<FString, int> spawn_name_counters;

	for (FStreetMapMisc sign : StreetMapData->GetSigns())
	{
		has_spawned_sign = false;
		//TODO: Adjust for signs not tagged under highway
		FString* current_sign_value = sign.Properties.Find("highway");
		if (current_sign_value == nullptr) current_sign_value = sign.Properties.Find("crossing");
		if (current_sign_value == nullptr) current_sign_value = sign.Properties.Find("max_speed");
		if (current_sign_value == nullptr) continue;

		for (USignDataAsset* sign_asset : sign_data)
		{
			if (has_spawned_sign) break;
			if (UKismetSystemLibrary::GetDisplayName(sign_asset).Contains(*current_sign_value))
			{
				int& counter = spawn_name_counters.FindOrAdd(*current_sign_value);
				counter++;

				FString actor_name = *current_sign_value;
				actor_name.AppendInt(counter);

				AStaticMeshActor* temp_actor = GetWorld()->SpawnActor<AStaticMeshActor>();

				carla::geom::Location cl(FVector(sign.Position.X, sign.Position.Y, 0.0f));
				//wp = GetClosestWaypoint(pos). if distance wp - pos == lane_width --> estas al borde de la carretera
				//boost::optional<Waypoint> wp = current_carla_map->GetClosestWaypointOnRoad(cl);
				FVector position = FVector(sign.Position.X, sign.Position.Y, 0.0f);
				//float dist = OpenDriveMap->DistanceToLaneBorder(current_carla_map, position);

				boost::optional<carla::road::element::Waypoint> closest_waypoint = current_carla_map->GetClosestWaypointOnRoad(cl, (int32)carla::road::Lane::LaneType::Sidewalk);
				//GetRight / GetLeft
				//CheckSignalsOnRoads
				if (false/*closest_waypoint*/)
				{


					carla::geom::Transform transform = current_carla_map->ComputeTransform(*closest_waypoint);
					double LaneWidth = current_carla_map->GetLaneWidth(*closest_waypoint);

					//FVector vector_to_waypoint = FVector(transform.location) - FVector(cl);
					//FVector vector_right_waypoint = transform.GetRightVector().ToFVector() * (LaneWidth * 0.5f * 100.0f);
					//FVector proj_to_sign = UKismetMathLibrary::ProjectVectorOnToVector(vector_to_waypoint, vector_right_waypoint);
					//FVector diff_to_border = vector_right_waypoint - proj_to_sign;

					//closest_waypoints.Add(FVector(transform.location));

					if (bDisplaceSignsToEdge)
					{
						//temp_actor->SetActorLocation(FVector(sign.Position.X, sign.Position.Y, 0.0f) - diff_to_border);
						temp_actor->SetActorLocation(FVector(transform.location));
					}
					else
					{
						temp_actor->SetActorLocation(FVector(sign.Position.X, sign.Position.Y, 0.0f));
					}
				}
				else
				{
					temp_actor->SetActorLocation(FVector(sign.Position.X, sign.Position.Y, 0.0f));
				}

				temp_actor->SetActorLabel(actor_name);

				if (sign_asset->SignMesh != nullptr && pole_data[0]->PoleMesh != nullptr)
				{
					UStaticMeshComponent* pole_mesh_comp = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(temp_actor);
					pole_mesh_comp->SetStaticMesh(pole_data[0]->PoleMesh);

					UStaticMeshComponent* sign_mesh_comp = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(temp_actor);
					sign_mesh_comp->SetStaticMesh(sign_asset->SignMesh);
					sign_mesh_comp->SetWorldTransform(pole_mesh_comp->GetSocketTransform(FName(TEXT("Sign1"))));
				}
				else
				{
					//Throw error or warning + default mesh used
				}

				//GeneratedSigns.Add(sign.OSM_ID, temp_actor);
				GeneratedSigns.Add(temp_actor);
				has_spawned_sign = true;
			}
		}
	}

	//Moving Signals to Sidewalk
	if(bDisplaceSignsToEdge)
	{

		for (AActor* sign : GeneratedSigns)
		{
			FVector sign_location = sign->GetActorLocation();
			FRotator sign_rotation = sign->GetActorRotation();
			
			sign_location = UOpenDriveToMap::DisplaceLocationOutsideNeighboringRoads(Cast<UObject>(this), sign_location, StepPercentOfLaneWidth, RoadBorderPadding);
			sign->SetActorLocation(sign_location);

			int32 check_shoulder_or_driving =
				static_cast<int32_t>(carla::road::Lane::LaneType::Shoulder) |
				static_cast<int32_t>(carla::road::Lane::LaneType::Driving);

			boost::optional<carla::road::element::Waypoint> closest_waypoint =
				current_carla_map->GetClosestWaypointOnRoad(sign_location, check_shoulder_or_driving);

			carla::geom::Transform road_transform = current_carla_map->ComputeTransform(closest_waypoint.get());
			sign->SetActorRotation(road_transform.rotation);
			
			FHitResult hit_result;

			UKismetSystemLibrary::LineTraceSingle(Cast<UObject>(this),
				FVector(sign_location.X, sign_location.Y, 50000.0f),
				FVector(sign_location.X, sign_location.Y, -100000.0f),
				ETraceTypeQuery::TraceTypeQuery1,
				true,
				GeneratedSigns,
				EDrawDebugTrace::ForDuration,
				hit_result,
				true,
				FLinearColor::Yellow, FLinearColor::Green, 3.0f);

			if (!hit_result.bBlockingHit) sign->Destroy();

			//if(hit_result.GetActor()->Tags.Contains("LandscapeToMove"))

			//sign_location = hit_result.ImpactPoint;
			//sign->SetActorLocation(sign_location);

			////Check if we should move the sign

			//if (closest_waypoint)
			//{
			//	carla::geom::Transform road_transform = current_carla_map->ComputeTransform(closest_waypoint.get());
			//	sign_rotation = road_transform.rotation;

			//	float distance_to_road = FVector(road_transform.location.ToFVector() * 100.0f - sign_location).Length();
			//	float lane_width = current_carla_map->GetLaneWidth(closest_waypoint.get());
			//	float displacement_direction = 1.0f;

			//	for (int counter = 0; counter < max_displacement_iterations; counter++)
			//	{
			//		if (displacement_direction == 0.0f) break;
			//		if (distance_to_road > (lane_width * distance_from_road_percent * 100.0f)) break;

			//		boost::optional<carla::road::element::Waypoint> right_waypoint = current_carla_map->GetRight(closest_waypoint.get());
			//		carla::road::Lane::LaneType right_lane_type = (right_waypoint) ?
			//			current_carla_map->GetLaneType(right_waypoint.get()) :
			//			carla::road::Lane::LaneType::None;

			//		boost::optional<carla::road::element::Waypoint> left_waypoint = current_carla_map->GetLeft(closest_waypoint.get());
			//		carla::road::Lane::LaneType left_lane_type = (left_waypoint) ?
			//			current_carla_map->GetLaneType(left_waypoint.get()) :
			//			carla::road::Lane::LaneType::None;

			//		if (right_lane_type != carla::road::Lane::LaneType::Driving)
			//		{
			//			displacement_direction = 1.0f;
			//		}
			//		else if (left_lane_type != carla::road::Lane::LaneType::Driving)
			//		{
			//			displacement_direction = -1.0f;
			//		}
			//		else {
			//			displacement_direction = 0.0f;
			//		}

			//		FVector displacement_diff = road_transform.GetRightVector().ToFVector() * static_cast<float>(abs(lane_width)) * 100.0f * step_percent_of_lane_width;
			//		sign_location += displacement_diff * displacement_direction;

			//		closest_waypoint = current_carla_map->GetClosestWaypointOnRoad(sign_location, check_shoulder_or_driving);
			//		road_transform = current_carla_map->ComputeTransform(closest_waypoint.get());
			//		distance_to_road = FVector(road_transform.location.ToFVector() * 100.0f - sign_location).Length();
			//		lane_width = current_carla_map->GetLaneWidth(closest_waypoint.get());
			//	}

			//	sign->SetActorLocation(sign_location);
			//	sign->SetActorRotation(sign_rotation);
			//	closest_waypoints.Add(road_transform.location.ToFVector());
			//}
		}
	}

	// We get a map of the generated signs actors with the location as the key
	TMap<FVector, TArray<AActor*>> LocationMap;

	for (AActor* Actor : GeneratedSigns)
	{
		if (!Actor) continue;

		FVector Location = Actor->GetActorLocation();

		// Use Add to group actors by location
		TArray<AActor*>& ActorList = LocationMap.FindOrAdd(Location);
		ActorList.Add(Actor);
	}

	// Get the component of the signs that share location and put it in the first sign with an offset, then destroy the duplicated sign
	for (const auto& Elem : LocationMap)
	{
		const FVector& Location = Elem.Key;
		const TArray<AActor*>& ActorsAtLocation = Elem.Value;
		AActor* FirstSignActor = ActorsAtLocation[0];

		UActorComponent* PoleComp = FirstSignActor->FindComponentByTag(UStaticMeshComponent::StaticClass(), FName("pole"));
		UStaticMeshComponent* PoleMesh = Cast<UStaticMeshComponent>(PoleComp);
		FTransform SignTransform = PoleMesh->GetSocketTransform(FName(TEXT("Sign1")));

		if (ActorsAtLocation.Num() > 1)
		{
			UE_LOG(LogTemp, Warning, TEXT("Found %d actors at location %s"),
				ActorsAtLocation.Num(), *Location.ToString());

			for (int i = 0 ; i < ActorsAtLocation.Num(); i++)
			{
				if (i == 0) continue;

				AActor* CurrentActor = ActorsAtLocation[i];
				UActorComponent* SignComp = CurrentActor->FindComponentByTag(UStaticMeshComponent::StaticClass(), FName("sign"));
				UStaticMeshComponent* SignMeshComp = Cast<UStaticMeshComponent>(SignComp);

				UStaticMeshComponent* SignComponent = UMapGenFunctionLibrary::AddStaticMeshComponentToActor(FirstSignActor);

				SignComponent->SetWorldTransform(FTransform(
					SignTransform.GetRotation(), 
					FVector(SignTransform.GetLocation().X, SignTransform.GetLocation().Y, SignTransform.GetLocation().Z * i - 90),
					SignTransform.GetScale3D()));

				SignComponent->SetStaticMesh(SignMeshComp->GetStaticMesh());
				SignComponent->SetMaterial(0, SignMeshComp->GetMaterial(0));

				CurrentActor->Destroy();

			}
		}
	}

	//for (FVector waypoint_pos : closest_waypoints)
	//{
	//	DrawDebugSphere(GetWorld(), waypoint_pos, 20.0f, 3, FColor::Red, false, 5.0f, 0.0f, 50.0f);
	//}
}

void ASignGenerationController::SignGenerationForCurrentMap()
{
	SignGenerationByPath(SignPackagePath, PolePackagePath);
}