// Copyright (c) 2021 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Commandlet/GenerateTileCommandlet.h"

#include <fstream>
#include "Generation/OpenDriveToMap.h"
#include "Misc/Paths.h"

#if WITH_EDITOR
#include "FileHelpers.h"
#endif
#include "UObject/GCObjectScopeGuard.h"
#include "UObject/SoftObjectPath.h"

DEFINE_LOG_CATEGORY(LogCarlaToolsMapGenerateTileCommandlet);


UGenerateTileCommandlet::UGenerateTileCommandlet()
{
}

UGenerateTileCommandlet::UGenerateTileCommandlet(const FObjectInitializer& Initializer)
  : Super(Initializer)
{
}

#if WITH_EDITOR

int32 UGenerateTileCommandlet::Main(const FString &Params)
{
  UE_LOG(LogCarlaToolsMapGenerateTileCommandlet, Log, TEXT("UGenerateTileCommandlet::Main Arguments %s"), *Params);
  TArray<FString> Tokens;
  TArray<FString> Switches;
  TMap<FString, FString> ParamsMap;

  ParseCommandLine(*Params, Tokens, Switches, ParamsMap);

  const FString LogFilePath = FPaths::Combine(FPaths::ProjectSavedDir(), TEXT("Logs"), TEXT("CommandletParameters.txt"));
  std::ofstream file(TCHAR_TO_UTF8(*LogFilePath), std::ios::app);
  if (file.is_open())
  {
    file << TCHAR_TO_UTF8(*Params) << '\n';
    for (const FString& Token : Tokens)
    {
      file << " Tokens " << TCHAR_TO_UTF8(*Token) << '\n';
    }
    for (const FString& Switch : Switches)
    {
      file << " SWITCHES " << TCHAR_TO_UTF8(*Switch) << '\n';
    }
    for (const TPair<FString, FString>& PairMap : ParamsMap)
    {
      file << "Key: " << TCHAR_TO_UTF8(*PairMap.Key) << " Value: " << TCHAR_TO_UTF8(*PairMap.Value) << '\n';
    }
  }

  const TCHAR* RequiredKeys[] = {
    TEXT("FilePath"),
    TEXT("BaseLevelName"),
    TEXT("GeoCoordsX"),
    TEXT("GeoCoordsY"),
    TEXT("CTileX"),
    TEXT("CTileY")
  };
  for (const TCHAR* Key : RequiredKeys)
  {
    if (!ParamsMap.Contains(Key))
    {
      UE_LOG(LogCarlaToolsMapGenerateTileCommandlet, Error, TEXT("Missing required commandlet parameter: -%s=..."), Key);
      return -1;
    }
  }

  // Resolve lazily rather than in the CDO constructor so Editor/commandlet startup
  // does not log CDO load errors when the optional content repository is absent.
  if (OpenDriveClass == nullptr)
  {
    static const TCHAR* CandidateClassPaths[] = {
      TEXT("/CarlaDigitalTwinsTool/digitalTwins/BP_OpenDriveToMap.BP_OpenDriveToMap_C"),
      TEXT("/CarlaDigitalTwinsTool/BP_OpenDriveToMap.BP_OpenDriveToMap_C"),
    };
    for (const TCHAR* ClassPath : CandidateClassPaths)
    {
      OpenDriveClass = FSoftClassPath(ClassPath).TryLoadClass<UOpenDriveToMap>();
      if (OpenDriveClass != nullptr)
      {
        break;
      }
    }
    if (OpenDriveClass == nullptr)
    {
      OpenDriveClass = UOpenDriveToMap::StaticClass();
    }
  }
  OpenDriveMap = NewObject<UOpenDriveToMap>(this, OpenDriveClass);
  if (OpenDriveMap == nullptr)
  {
    UE_LOG(LogCarlaToolsMapGenerateTileCommandlet, Error, TEXT("OpenDrive object is not properly spawned"));
    return -1;
  }

  FGCObjectScopeGuard OpenDriveGuard(OpenDriveMap);
  OpenDriveMap->FilePath = ParamsMap[TEXT("FilePath")];
  OpenDriveMap->BaseLevelName = ParamsMap[TEXT("BaseLevelName")];
  OpenDriveMap->OriginGeoCoordinates = FVector2D(
      FCString::Atod(*ParamsMap[TEXT("GeoCoordsX")]),
      FCString::Atod(*ParamsMap[TEXT("GeoCoordsY")]));
  OpenDriveMap->CurrentTilesInXY = FIntVector(
      FCString::Atoi(*ParamsMap[TEXT("CTileX")]),
      FCString::Atoi(*ParamsMap[TEXT("CTileY")]),
      0);
  OpenDriveMap->GenerateTile();

  return 0;
}

#endif
