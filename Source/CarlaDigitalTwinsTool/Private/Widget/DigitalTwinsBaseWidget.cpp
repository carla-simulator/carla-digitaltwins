// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "DigitalTwinsBaseWidget.h"
#include "Generation/OpenDriveToMap.h"
#include "CarlaDigitalTwinsTool.h"

static UOpenDriveToMap* OpenDriveToMapObject = nullptr;

UOpenDriveToMap* UDigitalTwinsBaseWidget::InitializeOpenDriveToMap(TSubclassOf<UOpenDriveToMap> BaseClass){
  if( OpenDriveToMapObject == nullptr && BaseClass != nullptr ){
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Creating New Object") );
    OpenDriveToMapObject = NewObject<UOpenDriveToMap>(this, BaseClass);
  }
  return OpenDriveToMapObject;
}

UOpenDriveToMap* UDigitalTwinsBaseWidget::GetOpenDriveToMap(){
  return OpenDriveToMapObject;
}

void UDigitalTwinsBaseWidget::SetOpenDriveToMap(UOpenDriveToMap* ToSet){
  OpenDriveToMapObject = ToSet;
}

void UDigitalTwinsBaseWidget::DestroyOpenDriveToMap(){
  OpenDriveToMapObject->ConditionalBeginDestroy();
  OpenDriveToMapObject = nullptr;
}
