#include "Utils/GoogleStreetViewFetcher.h"
#include "HttpModule.h"
#include "ImageUtils.h"
#include "Misc/Base64.h"
#include "Kismet/KismetMathLibrary.h"
#include "Engine/StaticMeshActor.h"
#include "Materials/MaterialInstanceDynamic.h"


void UGoogleStreetViewFetcher::Initialize(FVector2D InOriginGeoCoordinates, const FString& InGoogleAPIKey)
{
    OriginGeoCoordinates = InOriginGeoCoordinates;
    GoogleAPIKey = InGoogleAPIKey;
}

void UGoogleStreetViewFetcher::RequestStreetViewImageFromActor(AActor* CameraActor)
{
    if (!CameraActor)
    {
        UE_LOG(LogTemp, Warning, TEXT("Camera actor is null."));
        return;
    }

    double OriginLat = OriginGeoCoordinates.X;
    double OriginLon = OriginGeoCoordinates.Y;

    FVector CameraLocation = CameraActor->GetActorLocation();
    FRotator CameraRotation = CameraActor->GetActorRotation();

    double metersPerDegree = 111320.0;
    double lat = OriginLat + (CameraLocation.Y / metersPerDegree);
    double lon = OriginLon + (CameraLocation.X / (metersPerDegree * FMath::Cos(FMath::DegreesToRadians(OriginLat))));
    int heading = static_cast<int>(CameraRotation.Yaw) % 360;

    FString URL = FString::Printf(
        TEXT("https://maps.googleapis.com/maps/api/streetview?size=640x480&location=%.6f,%.6f&heading=%d&pitch=0&key=%s"),
        lat, lon, heading, *GoogleAPIKey);

    UE_LOG(LogTemp, Log, TEXT("Requesting Street View image from URL: %s"), *URL);

    FHttpModule* Http = &FHttpModule::Get();
    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Request = Http->CreateRequest();
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

void UGoogleStreetViewFetcher::RenderImage(UWorld* World, AActor* CameraActor)
{
    // Spawn a plane in front of the camera
    FVector CameraLocation = CameraActor->GetActorLocation();
    FRotator CameraRotation = CameraActor->GetActorRotation();

    FVector PlaneLocation = CameraLocation + CameraRotation.Vector() * 100; // 100 units in front
    FRotator PlaneRotation = CameraRotation;

    // Create a plane actor
    AStaticMeshActor* PlaneActor = World->SpawnActor<AStaticMeshActor>(PlaneLocation, PlaneRotation);
    PlaneActor->SetActorLabel(TEXT("GoogleStreetViewRender"));
    UStaticMeshComponent* MeshComp = PlaneActor->GetStaticMeshComponent();

    // Assign built-in plane mesh
    UStaticMesh* PlaneMesh = Cast<UStaticMesh>(
        StaticLoadObject(UStaticMesh::StaticClass(), nullptr, TEXT("/Engine/BasicShapes/Plane.Plane"))
    );
    if (PlaneMesh)
    {
        MeshComp->SetStaticMesh(PlaneMesh);
        MeshComp->SetWorldScale3D(FVector(2.0f, 2.0f, 2.0f));
    }
    else
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to load plane mesh."));
    }

    // Create and apply dynamic material
    UMaterialInstanceDynamic* DynMat = MeshComp->CreateAndSetMaterialInstanceDynamic(0);
    if (DynMat && StreetViewTexture)
    {
        DynMat->SetTextureParameterValue("StreetViewTex", StreetViewTexture);
    }
}
