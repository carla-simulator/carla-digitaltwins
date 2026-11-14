#pragma once

#include "CoreMinimal.h"
#include "Camera/CameraActor.h"
#include "Utils/GoogleStreetViewFetcher.h"
#include "GoogleStreetViewManager.generated.h"

UCLASS()
class CARLADIGITALTWINSTOOL_API AGoogleStreetViewManager : public AActor
{
    GENERATED_BODY()

public:
    AGoogleStreetViewManager();

    virtual void BeginPlay() override;

    virtual void Tick(float DeltaTime) override;

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void Initialize(UWorld* World, FTransform CameraTransform, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey);

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    TObjectPtr<ACameraActor> CameraActor = nullptr;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    FVector2D OriginGeoCoordinates = FVector2D::ZeroVector;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    FString GoogleAPIKey;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    TObjectPtr<UGoogleStreetViewFetcher> Fetcher = nullptr;

    FVector LastCameraLocation = FVector::ZeroVector;

    FRotator LastCameraRotation = FRotator::ZeroRotator;

    float MovementThreshold = 50.0f;

    float RotationThreshold = 5.0f;
};
