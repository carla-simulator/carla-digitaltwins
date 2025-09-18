// Fill out your copyright notice in the Description page of Project Settings.


#include "Generation/BuildingGeneratorActor.h"

#include "StreetMapActor.h"
#include "StreetMapRuntime.h"
#include "StreetMapComponent.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Kismet/KismetMathLibrary.h"
#include "RawMesh.h"
#include "CoreMinimal.h"
#include "ProceduralMeshComponent.h"
#include "KismetProceduralMeshLibrary.h"

#include "CompGeom/PolygonTriangulation.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "StaticMeshAttributes.h"
#include "Factories/Factory.h"
#include "PackageTools.h"
#include "MeshDescription.h"
#include "StaticMeshOperations.h"

#include "BodySetupEnums.h"
#include "ProceduralMeshConversion.h"
#include "Engine/StaticMeshSourceData.h"
#include "Misc/PackageName.h"
#include "Widgets/Text/STextBlock.h"
#include "Widgets/Input/SButton.h"
#include "IAssetTools.h"
#include "AssetToolsModule.h"
#include "DetailLayoutBuilder.h"
#include "DetailWidgetRow.h"
#include "EditorDirectories.h"
#include "PhysicsEngine/BodySetup.h"
#include "Dialogs/DlgPickAssetPath.h"
#include "AssetRegistry/AssetRegistryModule.h"

#include "Components/InstancedStaticMeshComponent.h"
#include "GameFramework/Actor.h"


UStaticMesh* ABuildingGeneratorActor::GenerateTopOfBuilding(int Index, FString MapName, UMaterialInstance* MaterialInstance)
{
	return StreetMapComponent->GenerateTopOfBuilding(Index, MapName, MaterialInstance);
}
// ---------- Robust helper math utilities (2D) ----------
static double SignedArea2D(const TArray<FVector2D>& Points)
{
    double Area = 0.0;
    const int32 N = Points.Num();
    for (int32 i = 0; i < N; ++i)
    {
        const FVector2D& A = Points[i];
        const FVector2D& B = Points[(i + 1) % N];
        Area += (double)A.X * (double)B.Y - (double)B.X * (double)A.Y;
    }
    return Area * 0.5;
}

// Cross product (B - A) x (C - A) — use this for convexity tests
static double Cross2D_A_BC(const FVector2D& A, const FVector2D& B, const FVector2D& C)
{
    double ux = (double)B.X - (double)A.X;
    double uy = (double)B.Y - (double)A.Y;
    double vx = (double)C.X - (double)A.X;
    double vy = (double)C.Y - (double)A.Y;
    return ux * vy - uy * vx;
}

// Strict barycentric point-in-triangle using double precision.
// Returns true only if P is *strictly* inside triangle ABC (not on edges).
static bool PointInTriangle_Strict(const FVector2D& P, const FVector2D& A, const FVector2D& B, const FVector2D& C)
{
    const double ax = A.X, ay = A.Y;
    const double bx = B.X, by = B.Y;
    const double cx = C.X, cy = C.Y;
    const double px = P.X, py = P.Y;

    const double v0x = cx - ax, v0y = cy - ay;
    const double v1x = bx - ax, v1y = by - ay;
    const double v2x = px - ax, v2y = py - ay;

    const double dot00 = v0x * v0x + v0y * v0y;
    const double dot01 = v0x * v1x + v0y * v1y;
    const double dot02 = v0x * v2x + v0y * v2y;
    const double dot11 = v1x * v1x + v1y * v1y;
    const double dot12 = v1x * v2x + v1y * v2y;

    const double denom = dot00 * dot11 - dot01 * dot01;
    const double EPS = 1e-12;
    if (FMath::Abs((float)denom) < (float)EPS) return false; // degenerate triangle

    const double invDenom = 1.0 / denom;
    const double u = (dot11 * dot02 - dot01 * dot12) * invDenom;
    const double v = (dot00 * dot12 - dot01 * dot02) * invDenom;

    // strict inside (exclude edges) to avoid degenerates; tweak EPS if needed.
    const double EDGE_EPS = 1e-9;
    return (u > EDGE_EPS) && (v > EDGE_EPS) && ((u + v) < (1.0 - EDGE_EPS));
}

void TriangulatePolygon(const TArray<FVector2D>& Points, TArray<int32>& Triangles)
{
    Triangles.Reset();
    const int32 n = Points.Num();
    if (n < 3) return;

    // Setup vertex index list
    TArray<int32> V;
    V.SetNum(n);

    // Orientation check
    if (SignedArea2D(Points) > 0.0)
    {
        for (int32 i = 0; i < n; i++) V[i] = i; // CCW
    }
    else
    {
        for (int32 i = 0; i < n; i++) V[i] = (n - 1) - i; // CW
    }

    int nv = n;
    int guard = 2 * nv;
    int v = nv - 1;

    while (nv > 2)
    {
        if (--guard < 0)
        {
            UE_LOG(LogTemp, Error, TEXT("TriangulatePolygon failed - non-simple polygon?"));
            return;
        }

        int u = v; if (u >= nv) u = 0;
        v = u + 1; if (v >= nv) v = 0;
        int w = v + 1; if (w >= nv) w = 0;

        const FVector2D& A = Points[V[u]];
        const FVector2D& B = Points[V[v]];
        const FVector2D& C = Points[V[w]];

        // Convex check
        if (Cross2D_A_BC(A, B, C) <= 0) continue;

        // Ear check
        bool isEar = true;
        for (int p = 0; p < nv; p++)
        {
            if (p == u || p == v || p == w) continue;
            if (PointInTriangle_Strict(Points[V[p]], A, B, C))
            {
                isEar = false;
                break;
            }
        }

        if (isEar)
        {
            Triangles.Add(V[u]);
            Triangles.Add(V[v]);
            Triangles.Add(V[w]);

            V.RemoveAt(v);
            nv--;
            guard = 2 * nv;
        }
    }
}



UStaticMesh* ConvertProcMeshToStaticMesh(UProceduralMeshComponent* ProcMesh)
{
    // Find first selected ProcMeshComp
    UProceduralMeshComponent* ProcMeshComp = ProcMesh;
    if (ProcMeshComp != nullptr)
    {
        FString NewNameSuggestion = FString(TEXT("ProcMesh"));
        FString DefaultPath;
        const FString DefaultDirectory = FEditorDirectories::Get().GetLastDirectory(ELastDirectory::NEW_ASSET);
        FPackageName::TryConvertFilenameToLongPackageName(DefaultDirectory, DefaultPath);

        if (DefaultPath.IsEmpty())
        {
            DefaultPath = TEXT("/Game/Meshes");
        }

        FString PackageName = DefaultPath / NewNameSuggestion;
        FString Name;
        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>("AssetTools");
        AssetToolsModule.Get().CreateUniqueAssetName(PackageName, TEXT(""), PackageName, Name);

        TSharedPtr<SDlgPickAssetPath> PickAssetPathWidget =
            SNew(SDlgPickAssetPath)
            .Title(FText::FromString("ConvertToStaticMeshPickName"))
            .DefaultAssetPath(FText::FromString(PackageName));

        if (PickAssetPathWidget->ShowModal() == EAppReturnType::Ok)
        {
            // Get the full name of where we want to create the physics asset.
            FString UserPackageName = PickAssetPathWidget->GetFullAssetPath().ToString();
            FName MeshName(*FPackageName::GetLongPackageAssetName(UserPackageName));

            // Check if the user inputed a valid asset name, if they did not, give it the generated default name
            if (MeshName == NAME_None)
            {
                // Use the defaults that were already generated.
                UserPackageName = PackageName;
                MeshName = *Name;
            }


            FMeshDescription MeshDescription = BuildMeshDescription(ProcMeshComp);

            // If we got some valid data.
            if (MeshDescription.Polygons().Num() > 0)
            {
                // Then find/create it.
                UPackage* Package = CreatePackage(*UserPackageName);
                check(Package);

                // Create StaticMesh object
                UStaticMesh* StaticMesh = NewObject<UStaticMesh>(Package, MeshName, RF_Public | RF_Standalone);
                StaticMesh->InitResources();

                StaticMesh->SetLightingGuid();

                // Add source to new StaticMesh
                FStaticMeshSourceModel& SrcModel = StaticMesh->AddSourceModel();
                SrcModel.BuildSettings.bRecomputeNormals = false;
                SrcModel.BuildSettings.bRecomputeTangents = false;
                SrcModel.BuildSettings.bRemoveDegenerates = false;
                SrcModel.BuildSettings.bUseHighPrecisionTangentBasis = false;
                SrcModel.BuildSettings.bUseFullPrecisionUVs = false;
                SrcModel.BuildSettings.bGenerateLightmapUVs = true;
                SrcModel.BuildSettings.SrcLightmapIndex = 0;
                SrcModel.BuildSettings.DstLightmapIndex = 1;
                StaticMesh->CreateMeshDescription(0, MoveTemp(MeshDescription));
                StaticMesh->CommitMeshDescription(0);

                //// SIMPLE COLLISION
                if (!ProcMeshComp->bUseComplexAsSimpleCollision)
                {
                    StaticMesh->CreateBodySetup();
                    UBodySetup* NewBodySetup = StaticMesh->GetBodySetup();
                    NewBodySetup->BodySetupGuid = FGuid::NewGuid();
                    NewBodySetup->AggGeom.ConvexElems = ProcMeshComp->ProcMeshBodySetup->AggGeom.ConvexElems;
                    NewBodySetup->bGenerateMirroredCollision = false;
                    NewBodySetup->bDoubleSidedGeometry = true;
                    NewBodySetup->CollisionTraceFlag = CTF_UseDefault;
                    NewBodySetup->CreatePhysicsMeshes();
                }

                //// MATERIALS
                TSet<UMaterialInterface*> UniqueMaterials;
                const int32 NumSections = ProcMeshComp->GetNumSections();
                for (int32 SectionIdx = 0; SectionIdx < NumSections; SectionIdx++)
                {
                    FProcMeshSection* ProcSection =
                        ProcMeshComp->GetProcMeshSection(SectionIdx);
                    UMaterialInterface* Material = ProcMeshComp->GetMaterial(SectionIdx);
                    UniqueMaterials.Add(Material);
                }
                // Copy materials to new mesh
                for (auto* Material : UniqueMaterials)
                {
                    StaticMesh->GetStaticMaterials().Add(FStaticMaterial(Material));
                }

                //Set the Imported version before calling the build
                StaticMesh->ImportVersion = EImportStaticMeshVersion::LastVersion;

                // Build mesh from source
                StaticMesh->Build(false);
                StaticMesh->PostEditChange();

                // Notify asset registry of new asset
                FAssetRegistryModule::AssetCreated(StaticMesh);

                return StaticMesh;
            }
        }
    }
    return nullptr;
}


void ABuildingGeneratorActor::CreatePlaneFrom2DPointsUE5(UProceduralMeshComponent* ProcMesh, UObject* Outer, const TArray<FVector2D>& Points, const FString Name, float Height, UStaticMesh*& OutMesh)
{
    if (!ProcMesh || Points.Num() < 3)
    {
        UE_LOG(LogTemp, Warning, TEXT("Invalid input for mesh generation"));
        return;
    }

    // Convert 2D points to 3D vertices (Z = 0)
    TArray<FVector> Vertices;
    for (const FVector2D& Point : Points)
    {
        Vertices.Add(FVector(Point.X, Point.Y, Height));
    }

    // Triangles array
    TArray<int32> Triangles;
    TriangulatePolygon(Points, Triangles);

    // Normals (all facing up)
    TArray<FVector> Normals;
    Normals.Init(FVector::UpVector, Vertices.Num());

    // UVs (simple planar mapping)
    TArray<FVector2D> UV0;
    for (const FVector& Vert : Vertices)
    {
        UV0.Add(FVector2D(Vert.X, Vert.Y));
    }

    // Tangents
    TArray<FProcMeshTangent> Tangents;
    Tangents.Init(FProcMeshTangent(1, 0, 0), Vertices.Num());

    // Vertex colors
    TArray<FColor> VertexColors;
    VertexColors.Init(FColor::White, Vertices.Num());

    ProcMesh->CreateMeshSection(0, Vertices, Triangles, Normals, UV0, VertexColors, Tangents, true);

#if WITH_EDITOR
    OutMesh = ConvertProcMeshToStaticMesh(ProcMesh);
    //SaveStaticMeshAsset(OutMesh, Name);
#endif
}

float GetMeshWidth(UStaticMesh* Mesh)
{
    if (!Mesh) return 0.f;
    FBoxSphereBounds Bounds = Mesh->GetBounds();
    return Bounds.BoxExtent.X * 2.0f; // Full width (X dimension)
}

UInstancedStaticMeshComponent* GetOrCreateInstancedStaticMesh(
    AActor* CurrentActor,
    UStaticMesh* Mesh,
    TMap<UStaticMesh*, UInstancedStaticMeshComponent*>& InstancedMeshes
)
{
    if (!CurrentActor || !Mesh)
    {
        return nullptr;
    }

    // Check if there is already an instanced static mesh component for this mesh
    if (UInstancedStaticMeshComponent** FoundComponent = InstancedMeshes.Find(Mesh))
    {
        return *FoundComponent;
    }

    // Create a new instanced static mesh component
    UInstancedStaticMeshComponent* NewInstancedMesh = NewObject<UInstancedStaticMeshComponent>(CurrentActor);
    if (!NewInstancedMesh)
    {
        return nullptr;
    }

    NewInstancedMesh->RegisterComponent();  // Register with the world
    NewInstancedMesh->SetStaticMesh(Mesh);
    NewInstancedMesh->AttachToComponent(CurrentActor->GetRootComponent(), FAttachmentTransformRules::KeepRelativeTransform);

    // Optional: You can set relative transform or scale here
    NewInstancedMesh->SetRelativeScale3D(FVector(200.0f));

    // Add it to the actor and the map
    CurrentActor->AddInstanceComponent(NewInstancedMesh);
    InstancedMeshes.Add(Mesh, NewInstancedMesh);

    return NewInstancedMesh;
}


void ABuildingGeneratorActor::PlaceMeshesGridBetweenPoints(
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
    )
{
    FVector WallVector = EndPoint - StartPoint;
    float WallLength = WallVector.Size();

    // ✅ Guard against zero-length wall (degenerate segment)
    if (WallLength < KINDA_SMALL_NUMBER)
    {
        UE_LOG(LogTemp, Warning, TEXT("Skipping degenerate wall segment between %s and %s"),
            *StartPoint.ToString(), *EndPoint.ToString());
        LengthWall = 0.f;
        return;
    }

    FVector WallDir = WallVector / WallLength; // safe normalize

    // --- FIX: Ensure consistent direction regardless of CW/CCW polygon ---
    FVector2D A(StartPoint.X, StartPoint.Y);
    FVector2D B(EndPoint.X, EndPoint.Y);
    float CrossVal = FVector2D::CrossProduct(B - A, FVector2D(WallDir.X, WallDir.Y));
    if (CrossVal < 0.f)
    {
        Swap(StartPoint, EndPoint);
        WallVector = EndPoint - StartPoint;
        WallLength = WallVector.Size();

        if (WallLength < KINDA_SMALL_NUMBER) return; // re-check after swap
        WallDir = WallVector / WallLength;
    }

    LengthWall = WallLength;

    for (int Row = 0; Row < NumRows; Row++)
    {
        float DistanceCovered = 0.f;

        // Pick which mesh set to use 
        TArray<UStaticMesh*>& MeshPool = ModuleMeshes;

        float RowHeight = (Row == 0) ? BottomRowHeight : RegularRowHeight;
        float ZOffset = (Row == 0) ? 0.f : BottomRowHeight + (Row - 1) * RegularRowHeight;

        while (DistanceCovered < WallLength)
        {
            // Pick random mesh 
            UStaticMesh* PickedMesh = MeshPool[FMath::RandRange(0, MeshPool.Num() - 1)];
            float ModuleWidth = GetMeshWidth(PickedMesh);

            float Remaining = WallLength - DistanceCovered;

            // ✅ Guard against negative or tiny Remaining values
            if (Remaining < 0.0001)
                break;

            if (ModuleWidth <= Remaining)
            {
                FVector Pos = StartPoint + WallDir * DistanceCovered + FVector(0.f, 0.f, ZOffset);
                FTransform InstanceTransform(FRotator::ZeroRotator, Pos);

                UInstancedStaticMeshComponent* ModuleMesh =
                    GetOrCreateInstancedStaticMesh(CurrentActor, PickedMesh, InstancedMeshes);
                if (ModuleMesh)
                {
                    ModuleMesh->AddInstance(InstanceTransform);
                }

                DistanceCovered += ModuleWidth;
            }
            else
            {
                FVector Pos = StartPoint + WallDir * DistanceCovered + FVector(0.f, 0.f, ZOffset);
                FTransform InstanceTransform(FRotator::ZeroRotator, Pos);

                FVector Scale = FVector(Remaining / 100, 1.f, RowHeight / 100);
                InstanceTransform.SetScale3D(Scale);

                UInstancedStaticMeshComponent* CoverPlaneMesh =
                    GetOrCreateInstancedStaticMesh(CurrentActor, CoverPlane, InstancedMeshes);
                if (CoverPlaneMesh)
                {
                    CoverPlaneMesh->AddInstance(InstanceTransform);
                }

                DistanceCovered = WallLength; // end this row
            }
        }
    }
}

void ABuildingGeneratorActor::PlaceJustifiedModules(
    const FVector StartPoint,
    const FVector EndPoint,
    const TArray<UStaticMesh*>& ModulePool,
    UStaticMesh* CoverMesh,
    AActor* CurrentActor,
    TMap<UStaticMesh*, UInstancedStaticMeshComponent*> InstancedMeshes)
{
    if (ModulePool.Num() == 0 || !CoverMesh) return;

    // --- Compute wall vector ---
    FVector WallVector = EndPoint - StartPoint;
    float WallLength = WallVector.Size();
    if (WallLength < KINDA_SMALL_NUMBER) return;

    FVector WallDir = WallVector.GetSafeNormal();

    // --- Precompute a sequence of meshes that fit ---
    TArray<UStaticMesh*> PickedModules;
    TArray<float> Widths;
    float TotalModulesLength = 0.0f;

    while (true)
    {
        UStaticMesh* Candidate = ModulePool[FMath::RandRange(0, ModulePool.Num() - 1)];
        if (!Candidate) break;

        float Width = Candidate->GetBounds().BoxExtent.X * 2.0f;

        if (TotalModulesLength + Width > WallLength)
            break; // stop, next would overflow

        PickedModules.Add(Candidate);
        Widths.Add(Width);
        TotalModulesLength += Width;
    }

    // If no module fits, just cover the entire span
    if (PickedModules.Num() == 0)
    {
        FVector CoverLocation = StartPoint + WallDir * (WallLength * 0.5f);
        float CoverMeshWidth = CoverMesh->GetBounds().BoxExtent.X * 2.0f;
        FVector CoverScale(WallLength / CoverMeshWidth, 1.0f, 1.0f);

        FTransform CoverTransform(WallDir.Rotation(), CoverLocation, CoverScale);
        if (UInstancedStaticMeshComponent* CoverISM = GetOrCreateInstancedStaticMesh(CurrentActor, CoverMesh, InstancedMeshes))
        {
            CoverISM->AddInstance(CoverTransform);
            CoverISM->SetWorldScale3D(FVector(1, 1, 1));
        }
        return;
    }

    // --- Center the picked sequence ---
    float Remaining = WallLength - TotalModulesLength;
    float StartOffset = Remaining * 0.5f;
    float CurrentPos = 0.0f; // we'll track relative to StartOffset separately

    // --- Cover at start ---
    if (StartOffset > KINDA_SMALL_NUMBER)
    {
        FVector CoverLocation = StartPoint + WallDir * (StartOffset * 0.5f);
        float CoverMeshWidth = CoverMesh->GetBounds().BoxExtent.X * 2.0f;
        FVector CoverScale(StartOffset / CoverMeshWidth, 1.0f, 1.0f);

        FTransform CoverTransform(WallDir.Rotation(), CoverLocation, CoverScale);
        if (UInstancedStaticMeshComponent* CoverISM = GetOrCreateInstancedStaticMesh(CurrentActor, CoverMesh, InstancedMeshes))
        {
            CoverISM->AddInstance(CoverTransform);
            CoverISM->SetWorldScale3D(FVector(1, 1, 1));
        }
    }

    // --- Place modular meshes sequentially ---
    for (int32 i = 0; i < PickedModules.Num(); i++)
    {
        UStaticMesh* Module = PickedModules[i];
        float Width = Widths[i];

        FVector ModuleLocation = StartPoint + WallDir * (StartOffset + CurrentPos + Width * 0.5f);
        FTransform ModuleTransform(WallDir.Rotation(), ModuleLocation, FVector(1, 1, 1));

        if (UInstancedStaticMeshComponent* ModuleISM = GetOrCreateInstancedStaticMesh(CurrentActor, Module, InstancedMeshes))
        {
            ModuleISM->AddInstance(ModuleTransform);
            ModuleISM->SetWorldScale3D(FVector(1, 1, 1));
        }

        CurrentPos += Width;
    }

    // --- Cover at end ---
    float EndGap = WallLength - (StartOffset + CurrentPos);
    if (EndGap > KINDA_SMALL_NUMBER)
    {
        FVector CoverLocation = StartPoint + WallDir * (StartOffset + CurrentPos + EndGap * 0.5f);
        float CoverMeshWidth = CoverMesh->GetBounds().BoxExtent.X * 2.0f;
        FVector CoverScale(EndGap / CoverMeshWidth, 1.0f, 1.0f);

        FTransform CoverTransform(WallDir.Rotation(), CoverLocation, CoverScale);
        if (UInstancedStaticMeshComponent* CoverISM = GetOrCreateInstancedStaticMesh(CurrentActor, CoverMesh, InstancedMeshes))
        {
            CoverISM->AddInstance(CoverTransform);
            CoverISM->SetWorldScale3D(FVector(1, 1, 1));
        }
    }
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
