#pragma once

#include "CoreMinimal.h"
#include "UObject/NoExportTypes.h"
#include "Engine/Texture2D.h"
#include "GoogleStreetViewFetcher.generated.h"

UCLASS(Blueprintable)
class CARLADIGITALTWINSTOOL_API UGoogleStreetViewFetcher : public UObject
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void Initialize(double InOriginLat, double InOriginLon, const FString& InGoogleApiKey);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RequestStreetViewImageFromActor(AActor* CameraActor);

    UPROPERTY(BlueprintReadOnly, Category = "GoogleStreetView")
    UTexture2D* StreetViewTexture;

private:
    void OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful);

    double OriginLat;
    double OriginLon;
    FString GoogleApiKey;
};
