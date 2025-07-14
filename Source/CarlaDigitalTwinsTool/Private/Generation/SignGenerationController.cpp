// Fill out your copyright notice in the Description page of Project Settings.


#include "Generation/SignGenerationController.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "AssetRegistry/AssetRegistryHelpers.h"
#include "Engine/StaticMeshActor.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Engine/StaticMesh.h"
#include "StaticMeshResources.h"



// Sets default values
ASignGenerationController::ASignGenerationController()
{
 	// Set this actor to call Tick() every frame.  You can turn this off to improve performance if you don't need it.
	PrimaryActorTick.bCanEverTick = true;

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

	PackagePath = package_path;

	for (AStaticMeshActor* actor : GeneratedSigns)
	{
		actor->Destroy();
	}
	
	GeneratedSigns.Empty();

	TArray<FAssetData> sign_data;

	FAssetRegistryModule& AssetRegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");

	IAssetRegistry& AssetRegistry = AssetRegistryModule.Get();

	const bool bRecursive = true;
	AssetRegistry.GetAssetsByPath(PackagePath, sign_data, bRecursive);


	//UAssetRegistryHelpers::GetAssetRegistry()->GetAssetsByPath(PackagePath, sign_data, false, false);

	for (FStreetMapSign sign : StreetMapData->GetSigns()) 
	{
		for (FAssetData sign_asset : sign_data)
		{
			current_data_asset = Cast<USignDataAsset>(sign_asset.GetAsset());
			if (current_data_asset == nullptr) continue;

			if (UKismetSystemLibrary::GetDisplayName(sign_asset.GetAsset()).Contains(sign.SignValue))
			{
				AStaticMeshActor* temp_actor =
					GetWorld()->SpawnActor<AStaticMeshActor>();
						/*FName(TEXT("StaticMeshActor")),
						FVector(sign.Position.X, sign.Position.Y, 0.0f));*/

				temp_actor->SetActorLocation(FVector(sign.Position.X, sign.Position.Y, 0.0f));
				temp_actor->GetStaticMeshComponent()->SetStaticMesh(current_data_asset->SignMesh);
				GeneratedSigns.Add(temp_actor);

			}
		}
	}
}

