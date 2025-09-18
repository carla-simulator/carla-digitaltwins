// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "BuildingGeneratorActor.generated.h"

class UStreetMapComponent;
class UProceduralMeshComponent;
UCLASS()
class CARLADIGITALTWINSTOOL_API ABuildingGeneratorActor : public AActor
{
	GENERATED_BODY()

public:	

	UFUNCTION(BlueprintCallable, Category = "Mesh Generation")
	UStaticMesh* GenerateTopOfBuilding(int Index, FString MapName, UMaterialInstance* MaterialInstance);

	UFUNCTION(BlueprintCallable, Category = "Mesh Generation")
	void CreatePlaneFrom2DPointsUE5(UProceduralMeshComponent* ProcMesh, UObject* Outer, const TArray<FVector2D>& Points, const FString Name, float height,UStaticMesh*& OutMesh);

	UFUNCTION(BlueprintCallable, Category = "Mesh Generation")
	void PlaceMeshesGridBetweenPoints(
		TArray<UStaticMesh*> ModuleMeshes,
		TArray<UStaticMesh*> ModuleMeshesCorner,
		int NumRows,
		float WallHeight,
		float BottomRowHeight,
		float RegularRowHeight,
		FVector StartPoint,
		FVector EndPoint,
		AActor* CurrentActor,
		TMap<UStaticMesh*, UInstancedStaticMeshComponent*> InstancedMeshes,
		UStaticMesh* CoverPlane,
		float& LengthWall
	);

	UFUNCTION(BlueprintCallable, Category = "Mesh Generation")

	void PlaceJustifiedModules(
		const FVector StartPoint,
		const FVector EndPoint,
		const TArray<UStaticMesh*>& ModulePool,
		UStaticMesh* CoverMesh,
		AActor* CurrentActor,
		TMap<UStaticMesh*, UInstancedStaticMeshComponent*> InstancedMeshes);

	UFUNCTION(BlueprintCallable)
	static UStaticMeshComponent* SpawnMeshInsidePolygonWithRotation(
		AActor* TargetActor,
		UStaticMesh* Mesh,
		const TArray<FVector2D>& Polygon,
		float BuildingHeight
	);

	UPROPERTY(VisibleAnywhere, BlueprintReadWrite, Category = "StreetMap")
	UStreetMapComponent* StreetMapComponent;
private:
	UFUNCTION()
	static bool IsPointInPolygon(const FVector2D& Point, const TArray<FVector2D>& Polygon);
	UFUNCTION()
	static bool DoLinesIntersect(const FVector2D& A1, const FVector2D& A2, const FVector2D& B1, const FVector2D& B2);
	UFUNCTION()
	static bool GetRandomPointInPolygon(const TArray<FVector2D>& Polygon, FVector2D& OutPoint);
	UFUNCTION()
	static FVector2D RotatePoint(const FVector2D& Point, float AngleDeg);
	UFUNCTION()
	static bool DoesMeshFitWithRotation(UStaticMesh* Mesh, const FVector& SpawnLocation, const TArray<FVector2D>& Polygon, float& OutRotation);
};
