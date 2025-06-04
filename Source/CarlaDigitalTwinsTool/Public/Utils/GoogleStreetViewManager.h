#pragma once

#include "CoreMinimal.h"
#include "Camera/CameraActor.h"
#include "GoogleStreetViewManager.generated.h"

UCLASS()
class CARLADIGITALTWINSTOOL_API AGoogleStreetViewManager : public AActor
{
    GENERATED_BODY()

public:
    virtual void BeginPlay() override;

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void Initialize(UWorld* World, FTransform CameraTransform, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey);

    UPROPERTY(EditAnywhere)
    ACameraActor* CameraActor;

    UPROPERTY(EditAnywhere)
    FVector2D OriginGeoCoordinates;

    UPROPERTY(EditAnywhere)
    FString GoogleAPIKey;
};
