// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Widget/DigitalTwinsBaseWidget.h"
#include "Generation/OpenDriveToMap.h"
#include "Misc/FileHelper.h"
#include "ContentBrowserModule.h"
#include "IContentBrowserSingleton.h"
#include "Misc/Paths.h"
#include "Misc/PackageName.h"
#include "CarlaDigitalTwinsTool.h"
#include "HAL/PlatformFileManager.h"
#include "UObject/StrongObjectPtr.h"

namespace
{
// TStrongObjectPtr roots the shared UOpenDriveToMap instance across GC passes
// after OpenDriveToMap calls RemoveFromRoot(), avoiding dangling/collected state
// before DestroyOpenDriveToMap() explicitly releases it.
TStrongObjectPtr<UOpenDriveToMap> SharedOpenDriveToMap;
}

UOpenDriveToMap* UDigitalTwinsBaseWidget::InitializeOpenDriveToMap(TSubclassOf<UOpenDriveToMap> BaseClass)
{
  if (!SharedOpenDriveToMap.IsValid() && BaseClass != nullptr)
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Creating UOpenDriveToMap instance"));
    UOpenDriveToMap* Created = NewObject<UOpenDriveToMap>(GetTransientPackage(), BaseClass);
    OpenDriveToMapInstance = Created;
    SharedOpenDriveToMap.Reset(Created);
  }
  else if (SharedOpenDriveToMap.IsValid())
  {
    OpenDriveToMapInstance = SharedOpenDriveToMap.Get();
  }
  return OpenDriveToMapInstance;
}

UOpenDriveToMap* UDigitalTwinsBaseWidget::GetOpenDriveToMap()
{
  if (!IsValid(OpenDriveToMapInstance) && SharedOpenDriveToMap.IsValid())
  {
    OpenDriveToMapInstance = SharedOpenDriveToMap.Get();
  }
  return OpenDriveToMapInstance;
}

void UDigitalTwinsBaseWidget::SetOpenDriveToMap(UOpenDriveToMap* ToSet)
{
  OpenDriveToMapInstance = ToSet;
  SharedOpenDriveToMap.Reset(ToSet);
}

void UDigitalTwinsBaseWidget::DestroyOpenDriveToMap()
{
  UOpenDriveToMap* Existing = SharedOpenDriveToMap.Get();
  if (!Existing)
  {
    Existing = OpenDriveToMapInstance;
  }
  OpenDriveToMapInstance = nullptr;
  SharedOpenDriveToMap.Reset();
  if (IsValid(Existing) && !Existing->IsRooted())
  {
    Existing->MarkAsGarbage();
  }
}

void UDigitalTwinsBaseWidget::CreatePlugin(FString PluginName)
{
  PluginName.TrimStartAndEndInline();
  if (PluginName.IsEmpty() || PluginName.Contains(TEXT("..")) || PluginName.Contains(TEXT("/")) || PluginName.Contains(TEXT("\\")))
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("CreatePlugin: Invalid plugin name \"%s\""), *PluginName);
    return;
  }

  const FString PluginsDir = FPaths::ProjectPluginsDir();
  const FString PluginDir = FPaths::Combine(PluginsDir, PluginName);
  const FString ContentDir = FPaths::Combine(PluginDir, TEXT("Content"));
  const FString UPluginFile = FPaths::Combine(PluginDir, PluginName + TEXT(".uplugin"));

  IPlatformFile& PlatformFile = FPlatformFileManager::Get().GetPlatformFile();
  if (!PlatformFile.CreateDirectoryTree(*ContentDir))
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("CreatePlugin: Failed to create directory \"%s\""), *ContentDir);
    return;
  }

  const FString EscapedName = PluginName.ReplaceCharWithEscapedChar();
  const FString UPluginContent = FString::Printf(
      TEXT("{\n")
      TEXT("  \"FileVersion\": 3,\n")
      TEXT("  \"Version\": 1,\n")
      TEXT("  \"VersionName\": \"1.0\",\n")
      TEXT("  \"FriendlyName\": \"%s\",\n")
      TEXT("  \"Description\": \"A content-only plugin generated in C++.\",\n")
      TEXT("  \"Category\": \"Content\",\n")
      TEXT("  \"CreatedBy\": \"AutoGenerator\",\n")
      TEXT("  \"CreatedByURL\": \"\",\n")
      TEXT("  \"DocsURL\": \"\",\n")
      TEXT("  \"CanContainContent\": true,\n")
      TEXT("  \"IsBetaVersion\": false,\n")
      TEXT("  \"IsExperimentalVersion\": false,\n")
      TEXT("  \"EnabledByDefault\": true,\n")
      TEXT("  \"Installed\": false\n")
      TEXT("}"),
      *EscapedName
  );

  if (FFileHelper::SaveStringToFile(UPluginContent, *UPluginFile, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Successfully created content-only plugin: %s"), *PluginName);
  }
  else
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to create plugin file for: %s"), *PluginName);
    return;
  }

  const FString MountPoint = FString::Printf(TEXT("/%s/"), *PluginName);
  const FString VirtualPath = FString::Printf(TEXT("/%s"), *PluginName);
  FPackageName::RegisterMountPoint(MountPoint, ContentDir);
  TArray<FString> PathsToOpen = { VirtualPath };

  FContentBrowserModule& ContentBrowserModule = FModuleManager::LoadModuleChecked<FContentBrowserModule>("ContentBrowser");
  ContentBrowserModule.Get().SyncBrowserToFolders(PathsToOpen);
}
