#include "Utils/GoogleStreetViewManager.h"

void AGoogleStreetViewManager::Initialize(UWorld* World, FTransform CameraTransform, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey)
{
    GoogleCamera = World->SpawnActor<ACameraActor>(ACameraActor::StaticClass(), CameraTransform);
    GoogleCamera->SetActorLabel(TEXT("GoogleStreetViewCamera"));

    EditorCamera = World->SpawnActor<ACameraActor>(ACameraActor::StaticClass(), CameraTransform);
    EditorCamera->SetActorLabel(TEXT("UnrealCamera"));

    LastCameraLocation = GoogleCamera->GetActorLocation();
    LastCameraRotation = GoogleCamera->GetActorRotation();

    OriginGeoCoordinates = InOriginGeoCoordinates;
    GoogleAPIKey = InGoogleAPIKey;

    PrimaryActorTick.bCanEverTick = true;
    MovementThreshold = 50.f;
    RotationThreshold = 5.0f;
}

void AGoogleStreetViewManager::BeginPlay()
{
    Super::BeginPlay();

    Fetcher = NewObject<UGoogleStreetViewFetcher>();
    Fetcher->AddToRoot();

    Fetcher->Initialize(GoogleCamera, OriginGeoCoordinates, GoogleAPIKey);
    Fetcher->RequestGoogleStreetViewImage();
}

void AGoogleStreetViewManager::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    if (!GoogleCamera || !Fetcher) return;

    FVector CurrentLocation = EditorCamera->GetActorLocation();
    FRotator CurrentRotation = EditorCamera->GetActorRotation();

    if (FVector::Dist(CurrentLocation, LastCameraLocation) > MovementThreshold ||
        FMath::Abs(CurrentRotation.Yaw - LastCameraRotation.Yaw) > RotationThreshold)
    {
        LastCameraLocation = CurrentLocation;
        LastCameraRotation = CurrentRotation;

        GoogleCamera->SetActorLocationAndRotation(CurrentLocation,CurrentRotation);

        Fetcher->SetCamera(GoogleCamera);
        Fetcher->RequestGoogleStreetViewImage();
    }
}
