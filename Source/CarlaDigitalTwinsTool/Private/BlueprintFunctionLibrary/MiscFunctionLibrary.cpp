// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.


#include "BlueprintFunctionLibrary/MiscFunctionLibrary.h"
#include "GeneralProjectSettings.h"

FString UMiscFunctionLibrary::GetProjectName()
{
  const UGeneralProjectSettings* ProjectSettings = GetDefault<UGeneralProjectSettings>();
  return ProjectSettings->ProjectName;
}
