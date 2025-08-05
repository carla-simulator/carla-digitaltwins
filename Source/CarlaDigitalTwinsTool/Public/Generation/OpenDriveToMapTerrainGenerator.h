// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB). This work is licensed under the terms of the MIT license. For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include <Carla/Road/RoadMap.h>
#include "OpenDriveToMapTerrainGenerator.generated.h"


DECLARE_LOG_CATEGORY_EXTERN(LogCarlaDigitalTwinsToolTerrainGeneration, Log, All);

class UOpenDriveToMap;
UCLASS(Blueprintable, BlueprintType)
class CARLADIGITALTWINSTOOL_API UOpenDriveToMapTerrainGenerator : public UBlueprintFunctionLibrary
{
  GENERATED_BODY()
public: 
  static void GenerateTerrainsFromTypes(UOpenDriveToMap* OpenDriveToMap,  const boost::optional<carla::road::Map>& ParamCarlaMap, FVector MinLocation, FVector MaxLocation, const TArray<FString>& ExcludedTypes);
};
