#include "Utils/GoogleStreetViewFetcher.h"
#include "HttpModule.h"
#include "ImageUtils.h"
#include "Misc/Base64.h"
#include "Kismet/KismetMathLibrary.h"
#include "Engine/StaticMeshActor.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Camera/CameraActor.h"
#include "Camera/CameraComponent.h"
#include "Generation/MapGenFunctionLibrary.h"

DEFINE_LOG_CATEGORY(LogGoogleStreetView);

void UGoogleStreetViewFetcher::Initialize(ACameraActor* InCameraActor, FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey)
{
    CameraActor = InCameraActor;
    OriginGeoCoordinates = InOriginGeoCoordinates;
    GoogleAPIKey = InGoogleAPIKey;
}

void UGoogleStreetViewFetcher::SetCamera(ACameraActor* InCameraActor)
{
    CameraActor = InCameraActor;
}

void UGoogleStreetViewFetcher::BeginDestroy()
{
    if (ActiveRequest.IsValid())
    {
        ActiveRequest->OnProcessRequestComplete().Unbind();
        ActiveRequest->CancelRequest();
        ActiveRequest.Reset();
    }
    Super::BeginDestroy();
}

void UGoogleStreetViewFetcher::RequestGoogleStreetViewImage()
{
    if (!IsValid(CameraActor))
    {
        UE_LOG(LogGoogleStreetView, Warning, TEXT("Camera actor is null."));
        return;
    }
    if (GoogleAPIKey.IsEmpty())
    {
        UE_LOG(LogGoogleStreetView, Warning, TEXT("GoogleAPIKey is empty; skipping Street View request."));
        return;
    }

    if (ActiveRequest.IsValid())
    {
        ActiveRequest->OnProcessRequestComplete().Unbind();
        ActiveRequest->CancelRequest();
        ActiveRequest.Reset();
    }

    const double OriginLat = OriginGeoCoordinates.X;
    const double OriginLon = OriginGeoCoordinates.Y;

    const FVector CameraLocation = CameraActor->GetActorLocation();
    const FRotator CameraRotation = CameraActor->GetActorRotation();

    const FVector2D LatLon = UMapGenFunctionLibrary::InverseTransverseMercatorProjection(
        CameraLocation.X, CameraLocation.Y, OriginLat, OriginLon);
    const double Lat = LatLon.X;
    const double Lon = LatLon.Y;
    const int32 Heading = FMath::RoundToInt(FRotator::ClampAxis(CameraRotation.Yaw)) % 360;

    const FString URL = FString::Printf(
        TEXT("https://maps.googleapis.com/maps/api/streetview?size=640x480&location=%.6f,%.6f&heading=%d&pitch=0&key=%s"),
        Lat, Lon, Heading, *GoogleAPIKey);

    UE_LOG(LogGoogleStreetView, Log,
        TEXT("Requesting Google Street View image (lat=%.6f, lon=%.6f, heading=%d, key=REDACTED)"),
        Lat, Lon, Heading);

    FHttpModule& Http = FHttpModule::Get();
    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Request = Http.CreateRequest();
    Request->SetURL(URL);
    Request->SetVerb(TEXT("GET"));
    Request->OnProcessRequestComplete().BindUObject(this, &UGoogleStreetViewFetcher::OnStreetViewResponseReceived);
    ActiveRequest = Request;
    if (!Request->ProcessRequest())
    {
        ActiveRequest.Reset();
        UE_LOG(LogGoogleStreetView, Error, TEXT("Failed to dispatch Google Street View HTTP request."));
    }
}

void UGoogleStreetViewFetcher::OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful)
{
    if (ActiveRequest == Request)
    {
        ActiveRequest.Reset();
    }

    if (bWasSuccessful && Response.IsValid() && Response->GetResponseCode() == 200)
    {
        const TArray<uint8>& ImageData = Response->GetContent();
        UTexture2D* Texture = FImageUtils::ImportBufferAsTexture2D(ImageData);

        if (Texture)
        {
            UE_LOG(LogGoogleStreetView, Log, TEXT("Successfully created Google Street View texture."));
            StreetViewTexture = Texture;

            if (IsValid(CameraActor))
            {
                ApplyCameraTexture();
            }
            else
            {
                UE_LOG(LogGoogleStreetView, Error, TEXT("No Camera Actor available."));
            }
        }
        else
        {
            UE_LOG(LogGoogleStreetView, Warning, TEXT("Failed to convert Google Street View image to texture."));
        }
    }
    else
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("Failed to retrieve Google Street View image. HTTP code: %d"),
            Response.IsValid() ? Response->GetResponseCode() : -1);
    }
}

void UGoogleStreetViewFetcher::ApplyCameraTexture()
{
    if (!StreetViewPostProcessMaterial)
    {
        UMaterialInterface* BasePPMat = LoadObject<UMaterialInterface>(
            nullptr,
            TEXT("/CarlaDigitalTwinsTool/digitalTwins/Static/Utils/M_GoogleStreetViewPost.M_GoogleStreetViewPost"));
        if (BasePPMat)
        {
            StreetViewPostProcessMaterial = UMaterialInstanceDynamic::Create(BasePPMat, this);
        }
    }

    if (!StreetViewPostProcessMaterial)
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("Failed to load or create M_GoogleStreetViewPost material."));
        return;
    }

    if (StreetViewTexture)
    {
        StreetViewPostProcessMaterial->SetTextureParameterValue(TEXT("StreetViewTex"), StreetViewTexture);
    }
    else
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("No StreetViewTexture."));
        return;
    }

    UCameraComponent* Cam = IsValid(CameraActor) ? CameraActor->FindComponentByClass<UCameraComponent>() : nullptr;
    if (Cam)
    {
        bool bAlreadyAdded = false;
        for (FWeightedBlendable& Existing : Cam->PostProcessSettings.WeightedBlendables.Array)
        {
            if (Existing.Object == StreetViewPostProcessMaterial)
            {
                Existing.Weight = 1.0f;
                bAlreadyAdded = true;
                break;
            }
        }
        if (!bAlreadyAdded)
        {
            FWeightedBlendable Blendable;
            Blendable.Object = StreetViewPostProcessMaterial;
            Blendable.Weight = 1.0f;
            Cam->PostProcessSettings.WeightedBlendables.Array.Add(Blendable);
        }
    }
    else
    {
        UE_LOG(LogGoogleStreetView, Error, TEXT("No camera actor or camera component."));
    }
}
