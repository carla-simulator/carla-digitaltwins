#pragma once

#include "CoreMinimal.h"
#include "UObject/NoExportTypes.h"
#include "Engine/Texture2D.h"
#include "HttpModule.h"
#include "Interfaces/IHttpRequest.h"
#include "Interfaces/IHttpResponse.h"
#include "GoogleStreetViewFetcher.generated.h"

UCLASS(Blueprintable)
class CARLADIGITALTWINSTOOL_API UGoogleStreetViewFetcher : public UObject
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void Initialize(ACameraActor* InCameraActor, FVector2D OriginGeoCoordinates, const FString& InGoogleAPIKey);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RequestGoogleStreetViewImage();

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void ApplyCameraTexture();

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    ACameraActor* CameraActor;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    UTexture2D* StreetViewTexture;

    UPROPERTY(EditAnywhere, Category = "GoogleStreetView")
    UStaticMeshComponent* TargetMeshComponent = nullptr;

private:
    void OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful);

    FVector2D OriginGeoCoordinates;
    FString GoogleAPIKey;
};
