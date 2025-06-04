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

void UGoogleStreetViewFetcher::RequestGoogleStreetViewImage(AActor* CameraActor)
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

    UE_LOG(LogTemp, Log, TEXT("Requesting Google Street View image from URL: %s"), *URL);

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
            UE_LOG(LogTemp, Log, TEXT("Successfully created Google Street View texture."));
            StreetViewTexture = Texture;

            // Apply the texture to the mesh component
            if (TargetMeshComponent)
            {
                // Load the material
                UMaterialInterface* BaseMaterial = LoadObject<UMaterialInterface>(
                    nullptr,
                    TEXT("/CarlaDigitalTwinsTool/digitalTwins/Static/Utils/M_GoogleStreetViewBase.M_GoogleStreetViewBase")
                );

                if (BaseMaterial)
                {
                    UMaterialInstanceDynamic* DynMat = UMaterialInstanceDynamic::Create(BaseMaterial, this);
                    if (DynMat)
                    {
                        DynMat->SetTextureParameterValue("StreetViewTex", StreetViewTexture);
                        TargetMeshComponent->SetMaterial(0, DynMat);
                        UE_LOG(LogTemp, Log, TEXT("Applied dynamic material with Google Street View texture."));
                    }
                    else{
                        UE_LOG(LogTemp, Error, TEXT("Failed to apply dynamic material with Google Street View texture."));
                    }
                }
            }
            else{
                UE_LOG(LogTemp, Error, TEXT("Target component for Google Street View texture does not exist."));
            }
        }
        else
        {
            UE_LOG(LogTemp, Warning, TEXT("Failed to convert Street View image to texture."));
        }
    }
    else
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to retrieve Google Street View image. HTTP code: %d"),
            Response.IsValid() ? Response->GetResponseCode() : -1);
    }
}

void UGoogleStreetViewFetcher::CreateRenderingMesh(UWorld* World, AActor* CameraActor)
{
    // Spawn a plane in front of the camera
    FVector CameraLocation = CameraActor->GetActorLocation();
    FRotator CameraRotation = CameraActor->GetActorRotation();

    FVector PlaneLocation = CameraLocation + CameraRotation.Vector() * 100; // 100 units in front
    // FRotator PlaneRotation = CameraRotation;
    FVector Forward = CameraRotation.Vector(); // or CameraActor->GetActorForwardVector()
    FRotator PlaneRotation = FRotationMatrix::MakeFromX(-Forward).Rotator();
    // PlaneRotation.Yaw += 180.0f;
    // PlaneRotation.Pitch += 90.0f;
    PlaneRotation.Roll += 90.0f;
    PlaneRotation.Yaw += 270.0f;

    // Create a plane actor
    AStaticMeshActor* PlaneActor = World->SpawnActor<AStaticMeshActor>(PlaneLocation, PlaneRotation);
    PlaneActor->SetActorLabel(TEXT("GoogleStreetViewRender"));

    if (PlaneActor)
    {
        PlaneActor->SetActorLabel(TEXT("GoogleStreetViewRender"));

        UStaticMeshComponent* MeshComp = NewObject<UStaticMeshComponent>(PlaneActor);
        MeshComp->RegisterComponent();
        MeshComp->AttachToComponent(PlaneActor->GetRootComponent(), FAttachmentTransformRules::KeepRelativeTransform);
        PlaneActor->AddInstanceComponent(MeshComp);

        UStaticMesh* PlaneMesh = Cast<UStaticMesh>(
            StaticLoadObject(UStaticMesh::StaticClass(), nullptr, TEXT("/Engine/BasicShapes/Plane.Plane"))
        );

        if (PlaneMesh && PlaneMesh->IsValidLowLevel())
        {
            MeshComp->SetStaticMesh(PlaneMesh);
            MeshComp->SetWorldScale3D(FVector(3.0f));
            UE_LOG(LogTemp, Log, TEXT("Assigned static mesh to Google Street View plane."));
        }
        else
        {
            UE_LOG(LogTemp, Error, TEXT("Failed to load or assign the plane mesh."));
        }

        TargetMeshComponent = MeshComp;
    }
}
