#include "Utils/GoogleStreetViewFetcher.h"
#include "HttpModule.h"
#include "Interfaces/IHttpResponse.h"
#include "ImageUtils.h"
#include "Misc/Base64.h"
#include "Kismet/KismetMathLibrary.h"

void UGoogleStreetViewFetcher::Initialize(double InOriginLat, double InOriginLon, const FString& InGoogleApiKey)
{
    OriginLat = InOriginLat;
    OriginLon = InOriginLon;
    GoogleApiKey = InGoogleApiKey;
}

void UGoogleStreetViewFetcher::RequestStreetViewImageFromActor(AActor* CameraActor)
{
    if (!CameraActor)
    {
        UE_LOG(LogTemp, Warning, TEXT("Camera actor is null."));
        return;
    }

    FVector CameraLocation = CameraActor->GetActorLocation();
    FRotator CameraRotation = CameraActor->GetActorRotation();

    double metersPerDegree = 111320.0;
    double lat = OriginLat + (CameraLocation.Y / metersPerDegree);
    double lon = OriginLon + (CameraLocation.X / (metersPerDegree * FMath::Cos(FMath::DegreesToRadians(OriginLat))));
    int heading = static_cast<int>(CameraRotation.Yaw) % 360;

    FString URL = FString::Printf(
        TEXT("https://maps.googleapis.com/maps/api/streetview?size=640x480&location=%.6f,%.6f&heading=%d&pitch=0&key=%s"),
        lat, lon, heading, *GoogleApiKey);

    UE_LOG(LogTemp, Log, TEXT("Requesting Street View image from URL: %s"), *URL);

    FHttpModule* Http = &FHttpModule::Get();
    TSharedRef<IHttpRequest> Request = Http->CreateRequest();
    Request->SetURL(URL);
    Request->SetVerb("GET");
    Request->OnProcessRequestComplete().BindUObject(this, &UGoogleStreetViewFetcher::OnStreetViewResponseReceived);
    Request->ProcessRequest();
}

void UGoogleStreetViewFetcher::OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful)
{
    if (bWasSuccessful && Response.IsValid() && Response->GetResponseCode() == 200)
    {
        const TArray<uint8>& ImageData = Response->GetContent();
        UTexture2D* Texture = FImageUtils::ImportBufferAsTexture2D(ImageData);

        if (Texture)
        {
            UE_LOG(LogTemp, Log, TEXT("Successfully created Street View texture."));
            StreetViewTexture = Texture;
        }
        else
        {
            UE_LOG(LogTemp, Warning, TEXT("Failed to convert Street View image to texture."));
        }
    }
    else
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to retrieve Street View image. HTTP code: %d"),
            Response.IsValid() ? Response->GetResponseCode() : -1);
    }
}
