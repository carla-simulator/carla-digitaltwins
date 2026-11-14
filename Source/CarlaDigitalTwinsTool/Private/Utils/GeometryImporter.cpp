#include "Utils/GeometryImporter.h"
#include "Misc/FileHelper.h"
#include "Engine/Engine.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "Misc/Paths.h"
#include "Json.h"
#include "JsonUtilities.h"

#if WITH_EDITOR
#include "EditorAssetLibrary.h"
#include "AssetToolsModule.h"
#include "Factories/FbxFactory.h"
#include "AutomatedAssetImportData.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Components/StaticMeshComponent.h"
#include "Kismet/GameplayStatics.h"
#include "Modules/ModuleManager.h"
#include "Editor.h"
#include "UObject/StrongObjectPtr.h"
#endif

DEFINE_LOG_CATEGORY(LogGeometryImporter);

TArray<FVector2D> UGeometryImporter::ReadCSVCoordinates(FString Path, FVector2D OriginGeoCoordinates)
{
    TArray<FVector2D> Coordinates;
    FString FileContent;

    if (FFileHelper::LoadFileToString(FileContent, *Path))
    {
        TArray<FString> Lines;
        FileContent.ParseIntoArrayLines(Lines);
        Coordinates.Reserve(Lines.Num());

        for (int32 i = 0; i < Lines.Num(); ++i)
        {
            TArray<FString> Columns;
            Lines[i].ParseIntoArray(Columns, TEXT(","), true);

            if (Columns.Num() >= 2)
            {
                double X = 0.0;
                double Y = 0.0;
                const FString Col0 = Columns[0].TrimStartAndEnd();
                const FString Col1 = Columns[1].TrimStartAndEnd();
                if (!LexTryParseString(X, *Col0) || !LexTryParseString(Y, *Col1))
                {
                    continue;
                }
                const FVector2D Pos = UMapGenFunctionLibrary::GetTransversemercProjection(
                    Y, X, OriginGeoCoordinates.X, OriginGeoCoordinates.Y);
                Coordinates.Add(Pos);
            }
        }
    }
    else
    {
        UE_LOG(LogGeometryImporter, Warning, TEXT("Failed to read file at: %s"), *Path);
    }

    return Coordinates;
}

USplineComponent* UGeometryImporter::CreateSpline(UWorld* World, const TArray<FVector>& Points, const FString SplineName)
{
    if (!World || Points.Num() < 2)
    {
        UE_LOG(LogGeometryImporter, Warning, TEXT("Invalid world pointer or insufficient spline points (%d)"), Points.Num());
        return nullptr;
    }

    AActor* SplineActor = World->SpawnActor<AActor>(AActor::StaticClass(), FTransform::Identity);
    if (!SplineActor)
    {
        UE_LOG(LogGeometryImporter, Warning, TEXT("Spline actor not created"));
        return nullptr;
    }
#if WITH_EDITOR
    SplineActor->SetActorLabel(SplineName);
#endif
    SplineActor->Tags.Add(FName("curb"));

    USplineComponent* Spline = NewObject<USplineComponent>(SplineActor);
    Spline->ClearSplinePoints(false);
    Spline->RegisterComponent();
    Spline->SetMobility(EComponentMobility::Static);
    SplineActor->SetRootComponent(Spline);

    for (int32 i = 0; i < Points.Num(); ++i)
    {
        Spline->AddSplinePoint(Points[i], ESplineCoordinateSpace::World, false);
    }

    const int32 Num = Spline->GetNumberOfSplinePoints();
    for (int32 i = 0; i < Num; ++i)
    {
        Spline->SetSplinePointType(i, ESplinePointType::Curve, false);
    }
    Spline->SetClosedLoop(true, false);

    const float TangentScale = 0.25f;
    for (int32 i = 0; i < Num; ++i)
    {
        const FVector Current = Spline->GetLocationAtSplinePoint(i, ESplineCoordinateSpace::Local);
        const FVector Prev = Spline->GetLocationAtSplinePoint((i - 1 + Num) % Num, ESplineCoordinateSpace::Local);
        const FVector Next = Spline->GetLocationAtSplinePoint((i + 1) % Num, ESplineCoordinateSpace::Local);
        const double D1 = FVector::Distance(Current, Prev);
        const double D2 = FVector::Distance(Current, Next);
        const double Clamped = FMath::Min(D1, D2);

        const FVector Dir = (Next - Prev) * 0.5;
        const double DesiredLength = FMath::Min(Dir.Size() * TangentScale, Clamped);
        const FVector Tangent = Dir.GetSafeNormal() * DesiredLength;

        Spline->SetTangentsAtSplinePoint(i, Tangent, Tangent, ESplineCoordinateSpace::Local, false);
    }

    Spline->UpdateSpline();

    return Spline;
}

TArray<USplineComponent*> UGeometryImporter::ImportGeoJsonPolygonsToSplines(UWorld* World, const FString& GeoJsonFilePath, const FVector2D OriginGeoCoordinates)
{
    UE_LOG(LogGeometryImporter, Log, TEXT("Importing geojson and creating splines from file: %s"), *GeoJsonFilePath);

    TArray<USplineComponent*> CreatedSplines;
    if (!World)
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("ImportGeoJsonPolygonsToSplines: World is null."));
        return CreatedSplines;
    }

    FString JsonString;
    if (!FFileHelper::LoadFileToString(JsonString, *GeoJsonFilePath))
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("Failed to load GeoJSON file: %s"), *GeoJsonFilePath);
        return CreatedSplines;
    }

    TSharedPtr<FJsonObject> JsonParsed;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonString);

    if (!FJsonSerializer::Deserialize(Reader, JsonParsed) || !JsonParsed.IsValid())
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("Failed to parse GeoJSON file: %s"), *GeoJsonFilePath);
        return CreatedSplines;
    }

    const TArray<TSharedPtr<FJsonValue>>* Features = nullptr;
    if (!JsonParsed->TryGetArrayField(TEXT("features"), Features) || !Features)
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("No 'features' array found in GeoJSON."));
        return CreatedSplines;
    }

    auto SpawnPolygonOuterRing = [&](const TArray<TSharedPtr<FJsonValue>>& PolygonRings, int32& InOutIndex)
    {
        if (PolygonRings.Num() == 0 || !PolygonRings[0].IsValid())
        {
            return;
        }
        const TArray<TSharedPtr<FJsonValue>>* OuterRing = nullptr;
        if (!PolygonRings[0]->TryGetArray(OuterRing) || !OuterRing)
        {
            return;
        }

        TArray<FVector> Points;
        Points.Reserve(OuterRing->Num());
        for (const TSharedPtr<FJsonValue>& Coord : *OuterRing)
        {
            const TArray<TSharedPtr<FJsonValue>>* CoordArray = nullptr;
            if (!Coord.IsValid() || !Coord->TryGetArray(CoordArray) || !CoordArray || CoordArray->Num() < 2)
            {
                continue;
            }
            double Lon = 0.0;
            double Lat = 0.0;
            if (!(*CoordArray)[0].IsValid() || !(*CoordArray)[1].IsValid() ||
                !(*CoordArray)[0]->TryGetNumber(Lon) || !(*CoordArray)[1]->TryGetNumber(Lat))
            {
                continue;
            }
            const FVector2D Pos2D = UMapGenFunctionLibrary::GetTransversemercProjection(
                Lat, Lon, OriginGeoCoordinates.X, OriginGeoCoordinates.Y);
            Points.Add(FVector(Pos2D.X, Pos2D.Y, 0.0f));
        }

        if (Points.Num() > 1 && Points[0].Equals(Points.Last(), 0.01f))
        {
            Points.Pop();
        }

        if (Points.Num() >= 2)
        {
            const FString SplineName = FString::Printf(TEXT("Spline_%d"), InOutIndex++);
            if (USplineComponent* Spline = CreateSpline(World, Points, SplineName))
            {
                CreatedSplines.Add(Spline);
            }
        }
    };

    int32 SplineIdx = 0;
    for (const TSharedPtr<FJsonValue>& FeatureValue : *Features)
    {
        if (!FeatureValue.IsValid())
        {
            continue;
        }
        const TSharedPtr<FJsonObject> FeatureObj = FeatureValue->AsObject();
        if (!FeatureObj.IsValid())
        {
            continue;
        }
        const TSharedPtr<FJsonObject>* GeometryPtr = nullptr;
        if (!FeatureObj->TryGetObjectField(TEXT("geometry"), GeometryPtr) || !GeometryPtr || !(*GeometryPtr).IsValid())
        {
            continue;
        }
        const TSharedPtr<FJsonObject>& Geometry = *GeometryPtr;

        FString GeometryType;
        if (!Geometry->TryGetStringField(TEXT("type"), GeometryType))
        {
            continue;
        }

        const TArray<TSharedPtr<FJsonValue>>* Coordinates = nullptr;
        if (!Geometry->TryGetArrayField(TEXT("coordinates"), Coordinates) || !Coordinates || Coordinates->Num() == 0)
        {
            continue;
        }

        if (GeometryType == TEXT("Polygon"))
        {
            SpawnPolygonOuterRing(*Coordinates, SplineIdx);
        }
        else if (GeometryType == TEXT("MultiPolygon"))
        {
            for (const TSharedPtr<FJsonValue>& PolyVal : *Coordinates)
            {
                const TArray<TSharedPtr<FJsonValue>>* PolyRings = nullptr;
                if (PolyVal.IsValid() && PolyVal->TryGetArray(PolyRings) && PolyRings)
                {
                    SpawnPolygonOuterRing(*PolyRings, SplineIdx);
                }
            }
        }
    }

    return CreatedSplines;
}

TArray<USplineComponent*> UGeometryImporter::CreateSplinesFromJson(
    UWorld* World,
    const FString& JsonFilePath,
    FVector2D Center,
    FVector2D Extent,
    FIntPoint RenderTargetSize)
{
    TArray<USplineComponent*> CreatedSplines;
    if (!World || RenderTargetSize.X <= 0 || RenderTargetSize.Y <= 0)
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("CreateSplinesFromJson: Invalid World or RenderTargetSize (%d x %d)"),
            RenderTargetSize.X, RenderTargetSize.Y);
        return CreatedSplines;
    }

    FString JsonString;
    if (!FFileHelper::LoadFileToString(JsonString, *JsonFilePath))
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("Failed to load JSON from: %s"), *JsonFilePath);
        return CreatedSplines;
    }

    TSharedPtr<FJsonValue> RootValue;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonString);
    if (!FJsonSerializer::Deserialize(Reader, RootValue) || !RootValue.IsValid())
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("Failed to parse JSON: %s"), *JsonFilePath);
        return CreatedSplines;
    }

    const TArray<TSharedPtr<FJsonValue>>* ContourArray = nullptr;
    if (!RootValue->TryGetArray(ContourArray) || !ContourArray)
    {
        return CreatedSplines;
    }

    const float ZOffset = 0.0f;
    const FVector2D RTSize2D(RenderTargetSize);

    for (int32 ContourIdx = 0; ContourIdx < ContourArray->Num(); ++ContourIdx)
    {
        const TArray<TSharedPtr<FJsonValue>>* PointList = nullptr;
        if (!(*ContourArray)[ContourIdx].IsValid() || !(*ContourArray)[ContourIdx]->TryGetArray(PointList) || !PointList)
        {
            continue;
        }

        TArray<FVector> Points;
        Points.Reserve(PointList->Num());

        for (const TSharedPtr<FJsonValue>& PointVal : *PointList)
        {
            const TArray<TSharedPtr<FJsonValue>>* XY = nullptr;
            if (!PointVal.IsValid() || !PointVal->TryGetArray(XY) || !XY || XY->Num() < 2)
            {
                continue;
            }
            double X = 0.0;
            double Y = 0.0;
            if (!(*XY)[0].IsValid() || !(*XY)[1].IsValid() ||
                !((*XY)[0]->TryGetNumber(X) && (*XY)[1]->TryGetNumber(Y)))
            {
                continue;
            }
            FVector2D UV(X, Y);
            UV /= RTSize2D;
            UV.Y = 1.0f - UV.Y;
            UV -= FVector2D(0.5f, 0.5f);
            UV.X = -UV.X;
            const FVector2D P = Center + UV * Extent;
            Points.Add(FVector(P.X, P.Y, ZOffset));
        }

        if (Points.Num() >= 2)
        {
            const FString Name = FString::Printf(TEXT("Spline_%d"), ContourIdx);
            if (USplineComponent* Spline = CreateSpline(World, Points, Name))
            {
                CreatedSplines.Add(Spline);
            }
        }
    }
    return CreatedSplines;
}

void UGeometryImporter::ImportObj(const FString& ObjFilePath, UWorld* World, UMaterialInterface* Material)
{
#if WITH_EDITOR
    if (!World)
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("ImportObj: World is null."));
        return;
    }
    if (!FPaths::FileExists(ObjFilePath))
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("File does not exist: %s"), *ObjFilePath);
        return;
    }

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>("AssetTools");

    TStrongObjectPtr<UFbxFactory> ObjFactory(NewObject<UFbxFactory>());
    TStrongObjectPtr<UAutomatedAssetImportData> ImportData(NewObject<UAutomatedAssetImportData>());
    ImportData->Factory = ObjFactory.Get();
    ImportData->Filenames.Add(ObjFilePath);
    ImportData->DestinationPath = TEXT("/Game/ImportedMeshes");
    ImportData->bReplaceExisting = true;

    TArray<UObject*> ImportedAssets = AssetToolsModule.Get().ImportAssetsAutomated(ImportData.Get());

    if (ImportedAssets.Num() > 0)
    {
        if (UStaticMesh* ImportedMesh = Cast<UStaticMesh>(ImportedAssets[0]))
        {
            FActorSpawnParameters SpawnParams;
            const FVector SpawnLocation(0.0f, 0.0f, 0.0f);
            AStaticMeshActor* MeshActor = World->SpawnActor<AStaticMeshActor>(SpawnLocation, FRotator::ZeroRotator, SpawnParams);

            if (MeshActor)
            {
                MeshActor->SetActorLabel(TEXT("UpdatedRoad"));
                if (UStaticMeshComponent* MeshComp = MeshActor->GetStaticMeshComponent())
                {
                    MeshComp->SetStaticMesh(ImportedMesh);
                    if (Material)
                    {
                        MeshComp->SetMaterial(0, Material);
                    }
                    else
                    {
                        UE_LOG(LogGeometryImporter, Warning, TEXT("Material not provided for imported OBJ mesh."));
                    }
                }
                UE_LOG(LogGeometryImporter, Log, TEXT("Imported and spawned mesh: %s"), *ImportedMesh->GetName());
            }
        }
        else
        {
            UE_LOG(LogGeometryImporter, Error, TEXT("Imported object is not a static mesh."));
        }
    }
    else
    {
        UE_LOG(LogGeometryImporter, Error, TEXT("No assets imported from file: %s"), *ObjFilePath);
    }
#endif
}
