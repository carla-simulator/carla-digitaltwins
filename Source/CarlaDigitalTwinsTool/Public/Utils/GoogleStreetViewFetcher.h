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
    void Initialize(FVector2D OriginGeoCoordinates, const FString& InGoogleApiKey);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RequestStreetViewImageFromActor(AActor* CameraActor);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RenderImage(UWorld* World, AActor* CameraActor);

    UPROPERTY(BlueprintReadOnly, Category = "GoogleStreetView")
    UTexture2D* StreetViewTexture;

private:
    void OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful);

    FVector2D OriginGeoCoordinates;
    FString GoogleApiKey;
};
