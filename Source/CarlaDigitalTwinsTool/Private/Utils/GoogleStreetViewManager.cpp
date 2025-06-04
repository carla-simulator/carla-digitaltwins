#include "Utils/GoogleStreetViewManager.h"
#include "Utils/GoogleStreetViewFetcher.h"


void AGoogleStreetViewManager::Initialize(UWorld* World, FTransform CameraTransform, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey)
{
    CameraActor = World->SpawnActor<ACameraActor>(ACameraActor::StaticClass(), CameraTransform);
    CameraActor->SetActorLabel(TEXT("GoogleStreetViewCamera"));

    OriginGeoCoordinates = InOriginGeoCoordinates;
    GoogleAPIKey = InGoogleAPIKey;
}

void AGoogleStreetViewManager::BeginPlay()
{
    Super::BeginPlay();

    UGoogleStreetViewFetcher* Fetcher = NewObject<UGoogleStreetViewFetcher>();
    Fetcher->AddToRoot();

    Fetcher->Initialize(OriginGeoCoordinates, GoogleAPIKey);
    Fetcher->CreateRenderingMesh(GetWorld(), CameraActor);
    Fetcher->RequestGoogleStreetViewImage(CameraActor);
}
