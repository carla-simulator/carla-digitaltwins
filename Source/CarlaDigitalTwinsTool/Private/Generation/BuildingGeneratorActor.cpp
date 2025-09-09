// Fill out your copyright notice in the Description page of Project Settings.


#include "Generation/BuildingGeneratorActor.h"

#include "StreetMapActor.h"
#include "StreetMapRuntime.h"
#include "StreetMapComponent.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Kismet/KismetMathLibrary.h"

UStaticMesh* ABuildingGeneratorActor::GenerateTopOfBuilding(int Index, FString MapName, UMaterialInstance* MaterialInstance)
{
	return StreetMapComponent->GenerateTopOfBuilding(Index, MapName, MaterialInstance);
}

bool ABuildingGeneratorActor::IsPointInPolygon(const FVector2D& Point, const TArray<FVector2D>& Polygon)
{
    bool bInside = false;
    int NumPoints = Polygon.Num();

    for (int i = 0, j = NumPoints - 1; i < NumPoints; j = i++)
    {
        if (((Polygon[i].Y > Point.Y) != (Polygon[j].Y > Point.Y)) &&
            (Point.X < (Polygon[j].X - Polygon[i].X) * (Point.Y - Polygon[i].Y) / (Polygon[j].Y - Polygon[i].Y) + Polygon[i].X))
        {
            bInside = !bInside;
        }
    }
    return bInside;
}

// ----------------------------
// Line segment intersection
// ----------------------------
bool ABuildingGeneratorActor::DoLinesIntersect(const FVector2D& A1, const FVector2D& A2, const FVector2D& B1, const FVector2D& B2)
{
    auto Cross = [](const FVector2D& P1, const FVector2D& P2) {
        return P1.X * P2.Y - P1.Y * P2.X;
        };

    FVector2D R = A2 - A1;
    FVector2D S = B2 - B1;
    float Den = Cross(R, S);

    if (FMath::IsNearlyZero(Den)) return false; // Parallel

    float T = Cross(B1 - A1, S) / Den;
    float U = Cross(B1 - A1, R) / Den;

    return (T >= 0 && T <= 1 && U >= 0 && U <= 1);
}

// ----------------------------
// Random point inside polygon
// ----------------------------
bool ABuildingGeneratorActor::GetRandomPointInPolygon(const TArray<FVector2D>& Polygon, FVector2D& OutPoint)
{
    if (Polygon.Num() < 3) return false;

    FVector2D Min(FLT_MAX, FLT_MAX), Max(-FLT_MAX, -FLT_MAX);
    for (const FVector2D& P : Polygon)
    {
        Min.X = FMath::Min(Min.X, P.X);
        Min.Y = FMath::Min(Min.Y, P.Y);
        Max.X = FMath::Max(Max.X, P.X);
        Max.Y = FMath::Max(Max.Y, P.Y);
    }

    for (int Attempts = 0; Attempts < 1000; Attempts++)
    {
        float X = FMath::FRandRange(Min.X, Max.X);
        float Y = FMath::FRandRange(Min.Y, Max.Y);
        FVector2D Candidate(X, Y);

        if (IsPointInPolygon(Candidate, Polygon))
        {
            OutPoint = Candidate;
            return true;
        }
    }

    return false; // failed to find point
}

// ----------------------------
// Rotate point around origin
// ----------------------------
FVector2D ABuildingGeneratorActor::RotatePoint(const FVector2D& Point, float AngleDeg)
{
    float Rad = FMath::DegreesToRadians(AngleDeg);
    float CosA = FMath::Cos(Rad);
    float SinA = FMath::Sin(Rad);

    return FVector2D(
        Point.X * CosA - Point.Y * SinA,
        Point.X * SinA + Point.Y * CosA
    );
}

// ----------------------------
// Fit check with rotation
// ----------------------------
bool ABuildingGeneratorActor::DoesMeshFitWithRotation(UStaticMesh* Mesh, const FVector& SpawnLocation, const TArray<FVector2D>& Polygon, float& OutRotation)
{
    if (!Mesh) return false;

    FBoxSphereBounds Bounds = Mesh->GetBounds();
    FVector Extent = Bounds.BoxExtent;

    // Local corners in XY plane
    TArray<FVector2D> LocalCorners = {
        FVector2D(Extent.X,  Extent.Y),
        FVector2D(Extent.X, -Extent.Y),
        FVector2D(-Extent.X, -Extent.Y),
        FVector2D(-Extent.X,  Extent.Y)
    };

    FVector2D Center2D(SpawnLocation.X, SpawnLocation.Y);

    // Try multiple rotations (every 15 degrees for example)
    for (float Angle = 0; Angle < 180.f; Angle += 15.f)
    {
        TArray<FVector2D> RotatedCorners;
        for (const FVector2D& Corner : LocalCorners)
        {
            RotatedCorners.Add(RotatePoint(Corner, Angle) + Center2D);
        }

        // 1. All corners inside polygon
        bool bAllInside = true;
        for (const FVector2D& Corner : RotatedCorners)
        {
            if (!IsPointInPolygon(Corner, Polygon))
            {
                bAllInside = false;
                break;
            }
        }
        if (!bAllInside) continue;

        // 2. Check for intersections
        TArray<TPair<FVector2D, FVector2D>> BoxEdges = {
            {RotatedCorners[0], RotatedCorners[1]},
            {RotatedCorners[1], RotatedCorners[2]},
            {RotatedCorners[2], RotatedCorners[3]},
            {RotatedCorners[3], RotatedCorners[0]}
        };

        bool bIntersects = false;
        for (int i = 0; i < Polygon.Num(); i++)
        {
            FVector2D P1 = Polygon[i];
            FVector2D P2 = Polygon[(i + 1) % Polygon.Num()];

            for (const auto& Edge : BoxEdges)
            {
                if (DoLinesIntersect(Edge.Key, Edge.Value, P1, P2))
                {
                    bIntersects = true;
                    break;
                }
            }
            if (bIntersects) break;
        }

        if (!bIntersects)
        {
            OutRotation = Angle;
            return true; // Found a valid rotation
        }
    }

    return false; // No fit
}

// ----------------------------
// Main entry point
// ----------------------------
UStaticMeshComponent* ABuildingGeneratorActor::SpawnMeshInsidePolygonWithRotation(
    AActor* TargetActor,
    UStaticMesh* Mesh,
    const TArray<FVector2D>& Polygon,
    float BuildingHeight)
{
    if (!TargetActor || !Mesh || Polygon.Num() < 3) return nullptr;

    // Find random point inside polygon
    FVector2D RandomPoint;
    if (!ABuildingGeneratorActor::GetRandomPointInPolygon(Polygon, RandomPoint))
        return nullptr;

    FVector SpawnLocation(RandomPoint.X, RandomPoint.Y, BuildingHeight); // Example Z

    float BestRotation = 0.f;
    if (!ABuildingGeneratorActor::DoesMeshFitWithRotation(Mesh, SpawnLocation, Polygon, BestRotation))
        return nullptr;

    if (!IsValid(TargetActor))
    {
        UE_LOG(LogTemp, Warning, TEXT("Invalid building actor!"));
        return nullptr;
    }

    if (!Mesh)
    {
        UE_LOG(LogTemp, Warning, TEXT("Mesh is null!"));
        return nullptr;
    }

    USceneComponent* Root = TargetActor->GetRootComponent();
    if (!Root)
    {
        UE_LOG(LogTemp, Warning, TEXT("%s has no RootComponent, skipping."), *TargetActor->GetName());
        return nullptr;
    }

    // Create a new static mesh component
    UStaticMeshComponent* MeshComp = NewObject<UStaticMeshComponent>(TargetActor);

    if (MeshComp && Mesh)
    {
        UStaticMesh* LoadedMesh = Mesh;

        LoadedMesh->ConditionalPostLoad();  // Force full load
        MeshComp->SetStaticMesh(LoadedMesh);
        MeshComp->SetWorldLocation(SpawnLocation);
        MeshComp->SetWorldRotation(FRotator(0.f, BestRotation, 0.f));

        // Attach to actor root
        MeshComp->AttachToComponent(TargetActor->GetRootComponent(), FAttachmentTransformRules::KeepWorldTransform);

        // Register so it becomes active in the scene
        MeshComp->RegisterComponent();

        return MeshComp;
    }

    return nullptr;
}
