#pragma once

#include "CoreMinimal.h"
#include "UObject/NoExportTypes.h"
#include "Engine/Texture2D.h"
#include "HttpModule.h"
#include "Interfaces/IHttpRequest.h"
#include "Interfaces/IHttpResponse.h"
#include "GoogleStreetViewFetcher.generated.h"

DECLARE_LOG_CATEGORY_EXTERN(LogGoogleStreetView, Log, All);

UCLASS(Blueprintable)
class CARLADIGITALTWINSTOOL_API UGoogleStreetViewFetcher : public UObject
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void Initialize(ACameraActor* InCameraActor, FVector2D OriginGeoCoordinates, const FString& InGoogleAPIKey);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void SetCamera(ACameraActor* InCameraActor);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RequestGoogleStreetViewImage();

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void ApplyCameraTexture();

    virtual void BeginDestroy() override;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    TObjectPtr<ACameraActor> CameraActor = nullptr;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    TObjectPtr<UTexture2D> StreetViewTexture = nullptr;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    TObjectPtr<UStaticMeshComponent> TargetMeshComponent = nullptr;

    UPROPERTY()
    TObjectPtr<UMaterialInstanceDynamic> StreetViewPostProcessMaterial = nullptr;

private:
    void OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful);

    TSharedPtr<IHttpRequest, ESPMode::ThreadSafe> ActiveRequest;
    FVector2D OriginGeoCoordinates = FVector2D::ZeroVector;
    FString GoogleAPIKey;
};
