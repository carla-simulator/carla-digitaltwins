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


// Sets default values
ASignGenerationController::ASignGenerationController(const FObjectInitializer& ObjectInitializer)
	: Super(ObjectInitializer)
{
 	// Set this actor to call Tick() every frame.  You can turn this off to improve performance if you don't need it.
	PrimaryActorTick.bCanEverTick = true;
	bDisplaceSignsToEdge = true;
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

void ASignGenerationController::SignGenerationByPath(FName package_path)
{

	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();
	FString LevelName = FPackageName::GetShortName(GetWorld()->GetMapName());
	FString StreetMapObjectPath = UGenerationPathsHelper::GetMapDirectoryPath(LevelName) + "OpenDrive/" + LevelName + ".osm";
	//AssetData street_map_data_asset = AssetRegistry.GetAssetByObjectPath(StreetMapObjectPath);
	//StreetMapData = Cast<UStreetMap>(street_map_data_asset.GetAsset());
	//const FSoftObjectPath DefaultItemPath(StreetMapObjectPath);
	//StreetMapData = Cast<UStreetMap>(DefaultItemPath.TryLoad());

	if (StreetMapData == nullptr) return;

	boost::optional<carla::road::Map> current_carla_map;

	FString FileContent;
	FString FilePath = UGenerationPathsHelper::GetRawMapDirectoryPath(LevelName) + "OpenDrive/" + LevelName + ".xodr";
	FFileHelper::LoadFileToString(FileContent, *FilePath);
	std::string opendrive_xml = carla::rpc::FromLongFString(FileContent);
	//UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("UOpenDriveToMap::GenerateTile() Loading File..... "));
	current_carla_map = carla::opendrive::OpenDriveParser::Load(opendrive_xml);

	//for (const TPair<int32, AStaticMeshActor*> entry : GeneratedSigns)
	for (AStaticMeshActor* entry : GeneratedSigns)
	{
		//if(entry.Value != nullptr) entry.Value->Destroy();
		if(entry != nullptr) entry->Destroy();
	}

	GeneratedSigns.Empty();
	closest_waypoints.Empty();

	PackagePath = package_path;
	TArray<FAssetData> temp_sign;
	AssetRegistry.GetAssetsByPath(PackagePath, temp_sign, true);

	TArray<USignDataAsset*> sign_data;
	for (FAssetData asset_data : temp_sign)
	{
		USignDataAsset* temp = Cast<USignDataAsset>(asset_data.GetAsset());
		if (temp != nullptr) sign_data.Add(temp);
	}

	TMap<FString, int> spawn_name_counters;

	for (FStreetMapMisc sign : StreetMapData->GetSigns())
	{

		//TODO: Adjust for signs not tagged under highway
		FString* current_sign_value = sign.Properties.Find("highway");
		if (current_sign_value == nullptr) continue;

		for (USignDataAsset* sign_asset : sign_data)
		{
			
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
				if (closest_waypoint)
				{


					carla::geom::Transform transform = current_carla_map->ComputeTransform(*closest_waypoint);
					double LaneWidth = current_carla_map->GetLaneWidth(*closest_waypoint);

					//FVector vector_to_waypoint = FVector(transform.location) - FVector(cl);
					//FVector vector_right_waypoint = transform.GetRightVector().ToFVector() * (LaneWidth * 0.5f * 100.0f);
					//FVector proj_to_sign = UKismetMathLibrary::ProjectVectorOnToVector(vector_to_waypoint, vector_right_waypoint);
					//FVector diff_to_border = vector_right_waypoint - proj_to_sign;
					
					closest_waypoints.Add(FVector(transform.location));

					if(bDisplaceSignsToEdge)
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

				if(sign_asset->SignMesh != nullptr)
				{
					temp_actor->GetStaticMeshComponent()->SetStaticMesh(sign_asset->SignMesh);
				} 
				else
				{
					//Throw error or warning + default mesh used
				}

				//GeneratedSigns.Add(sign.OSM_ID, temp_actor);
				GeneratedSigns.Add(temp_actor);

			}
		}
	}

	//for (FVector waypoint_pos : closest_waypoints)
	//{
	//	DrawDebugSphere(GetWorld(), waypoint_pos, 20.0f, 1, FColor::Red, false, 5.0f, 0.0f, 100.0f);
	//}
}

void ASignGenerationController::SignGenerationForCurrentMap()
{

	//StreetMapData = ;
	//SignGenerationByPath("/Script/StreetMapRuntime.StreetMap'/FINAL_TEST/Maps/OpenDrive/FINAL_TEST.FINAL_TEST'")
}