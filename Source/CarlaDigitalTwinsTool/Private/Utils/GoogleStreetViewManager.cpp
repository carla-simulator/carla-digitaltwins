#include "Utils/GoogleStreetViewManager.h"
#include "Engine/World.h"

AGoogleStreetViewManager::AGoogleStreetViewManager()
{
    PrimaryActorTick.bCanEverTick = true;
}

void AGoogleStreetViewManager::Initialize(UWorld* World, FTransform CameraTransform, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey)
{
    if (!World)
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("AGoogleStreetViewManager::Initialize called with null World."));
        return;
    }

    CameraActor = World->SpawnActor<ACameraActor>(ACameraActor::StaticClass(), CameraTransform);
    if (!CameraActor)
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("AGoogleStreetViewManager::Initialize failed to spawn ACameraActor."));
        return;
    }
#if WITH_EDITOR
    CameraActor->SetActorLabel(TEXT("GoogleStreetViewCamera"));
#endif

    LastCameraLocation = CameraActor->GetActorLocation();
    LastCameraRotation = CameraActor->GetActorRotation();

    OriginGeoCoordinates = InOriginGeoCoordinates;
    GoogleAPIKey = InGoogleAPIKey;

    PrimaryActorTick.bCanEverTick = true;
    MovementThreshold = 50.0f;
    RotationThreshold = 5.0f;
}

void AGoogleStreetViewManager::BeginPlay()
{
    Super::BeginPlay();

    Fetcher = NewObject<UGoogleStreetViewFetcher>(this);
    Fetcher->Initialize(CameraActor, OriginGeoCoordinates, GoogleAPIKey);
    if (IsValid(CameraActor))
    {
        LastCameraLocation = CameraActor->GetActorLocation();
        LastCameraRotation = CameraActor->GetActorRotation();
        Fetcher->RequestGoogleStreetViewImage();
    }
}

void AGoogleStreetViewManager::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    if (!IsValid(CameraActor) || !IsValid(Fetcher))
    {
        return;
    }

    const FVector CurrentLocation = CameraActor->GetActorLocation();
    const FRotator CurrentRotation = CameraActor->GetActorRotation();

    const double DistMoved = FVector::Dist(CurrentLocation, LastCameraLocation);
    const float YawDelta = FMath::Abs(FMath::FindDeltaAngleDegrees(CurrentRotation.Yaw, LastCameraRotation.Yaw));

    if (DistMoved > MovementThreshold || YawDelta > RotationThreshold)
    {
        LastCameraLocation = CurrentLocation;
        LastCameraRotation = CurrentRotation;

        Fetcher->SetCamera(CameraActor);
        Fetcher->RequestGoogleStreetViewImage();
    }
}
