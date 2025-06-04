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
    void Initialize(FVector2D OriginGeoCoordinates, const FString& InGoogleAPIKey);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void RequestGoogleStreetViewImage(AActor* CameraActor);

    UFUNCTION(BlueprintCallable, Category = "GoogleStreetView")
    void CreateRenderingMesh(UWorld* World, AActor* CameraActor);

    UPROPERTY(BlueprintReadOnly, Category = "GoogleStreetView")
    UTexture2D* StreetViewTexture;

    UPROPERTY()
    UStaticMeshComponent* TargetMeshComponent = nullptr;

private:
    void OnStreetViewResponseReceived(FHttpRequestPtr Request, FHttpResponsePtr Response, bool bWasSuccessful);

    FVector2D OriginGeoCoordinates;
    FString GoogleAPIKey;
};
