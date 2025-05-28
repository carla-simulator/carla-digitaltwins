#pragma once

#include "Kismet/BlueprintFunctionLibrary.h"
#include "GeometryImporter.generated.h"

UCLASS(Blueprintable, BlueprintType)
class CARLADIGITALTWINSTOOL_API UGeometryImporter : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:

    UFUNCTION(BlueprintCallable, Category = "MyPlugin")
    TArray<FVector2D> ReadCSVCoordinates(FString path, FVector2D OriginGeoCoordinates);
};
