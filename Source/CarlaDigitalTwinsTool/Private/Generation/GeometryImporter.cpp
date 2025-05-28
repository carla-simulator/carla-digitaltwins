#include "Misc/FileHelper.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "Generation/GeometryImporter.h"
#include "Engine/Engine.h"

TArray<FVector2D> UGeometryImporter::ReadCSVCoordinates(FString path, FVector2D OriginGeoCoordinates)
{
    UE_LOG(LogTemp, Warning, TEXT("Reading latlon coordinates"));

    TArray<FVector2D> Coordinates;

    FString PluginPath = FPaths::ConvertRelativePathToFull(FPaths::ProjectPluginsDir() / TEXT("carla-digitaltwins"));
    FString FilePositionsPath = PluginPath / path;
    FString FileContent;

    if (FFileHelper::LoadFileToString(FileContent, *FilePositionsPath))
    {
        TArray<FString> Lines;
        FileContent.ParseIntoArrayLines(Lines);

        for (int32 i = 0; i < Lines.Num(); ++i)
        {
            FString Line = Lines[i];
            TArray<FString> Columns;
            Line.ParseIntoArray(Columns, TEXT(","), true);

            if (Columns.Num() >= 2)
            {
                float X = FCString::Atof(*Columns[0]);
                float Y = FCString::Atof(*Columns[1]);
                FVector2D Pos = UMapGenFunctionLibrary::GetTransversemercProjection( Y, X, OriginGeoCoordinates.X, OriginGeoCoordinates.Y );
                Coordinates.Add(Pos);
            }
        }
    }
    else
    {
        UE_LOG(LogTemp, Warning, TEXT("Failed to read file at: %s"), *FilePositionsPath);
    }

    return Coordinates;
}
