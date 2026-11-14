// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.


#include "BlueprintUtil/BlueprintUtilFunctions.h"
#include "GeneralProjectSettings.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "AssetToolsModule.h"
#include "Engine/AssetManager.h"
#include "IAssetTools.h"
#include "ObjectTools.h"
#include "UObject/UObjectGlobals.h"
#include "EditorAssetLibrary.h"
#include "FileHelpers.h"
#include "WorldPartition/WorldPartitionBlueprintLibrary.h"
#include "WorldPartition/WorldPartitionSubsystem.h"
#include "Components/SplineComponent.h"

DEFINE_LOG_CATEGORY(LogDigitalTwinsToolBlueprintUtil);

FString UBlueprintUtilFunctions::GetProjectName()
{
  const UGeneralProjectSettings* ProjectSettings = GetDefault<UGeneralProjectSettings>();
  return ProjectSettings ? ProjectSettings->ProjectName : FString();
}

UObject* UBlueprintUtilFunctions::CopyAssetToPlugin(UObject* SourceObject, FString PluginName)
{
#if WITH_EDITOR
  while (PluginName.RemoveFromStart(TEXT("/"))) {}
  while (PluginName.RemoveFromEnd(TEXT("/"))) {}
  if (!SourceObject || PluginName.IsEmpty())
  {
    return nullptr;
  }

  const FString SourcePath = SourceObject->GetPathName();
  const FString SourceAssetName = SourceObject->GetName();

  const FString TargetMountPrefix = FString::Printf(TEXT("/%s/"), *PluginName);
  if (SourcePath.StartsWith(TargetMountPrefix) || SourcePath.StartsWith(TEXT("/Engine/")) || SourcePath.StartsWith(TEXT("/Script/")))
  {
    return SourceObject;
  }

  // CARLA 1.0 digital-twin maps live under /Game/Carla/Maps/Twins/<Name> without a mounted
  // /<Name>/ plugin. Keep the source asset reference intact instead of failing DuplicateAsset.
  FString MountFilename;
  if (!FPackageName::TryConvertLongPackageNameToFilename(TargetMountPrefix, MountFilename))
  {
    return SourceObject;
  }

  const FString SourceFolderPath = FPaths::GetPath(SourcePath);
  const FString RootPath = TEXT("/CarlaDigitalTwinsTool");
  FString RelativePath;
  if (SourceFolderPath.Equals(RootPath))
  {
    RelativePath.Empty();
  }
  else if (SourceFolderPath.StartsWith(RootPath + TEXT("/")))
  {
    RelativePath = SourceFolderPath.Mid(RootPath.Len() + 1);
  }
  else
  {
    FString Stripped = SourceFolderPath;
    Stripped.RemoveFromStart(TEXT("/"));
    int32 SlashIdx = INDEX_NONE;
    if (Stripped.FindChar(TEXT('/'), SlashIdx))
    {
      RelativePath = Stripped.Mid(SlashIdx + 1);
    }
  }

  const FString DestinationPath = RelativePath.IsEmpty()
      ? FString::Printf(TEXT("/%s"), *PluginName)
      : FString::Printf(TEXT("/%s/%s"), *PluginName, *RelativePath);
  const FString TargetAssetPath = FString::Printf(TEXT("%s/%s"), *DestinationPath, *SourceAssetName);

  if (UEditorAssetLibrary::DoesAssetExist(TargetAssetPath))
  {
    UObject* ExistingAsset = UEditorAssetLibrary::LoadAsset(TargetAssetPath);
    if (ExistingAsset)
    {
      return ExistingAsset;
    }
    UE_LOG(LogDigitalTwinsToolBlueprintUtil, Error, TEXT("Invalid Asset: %s"), *TargetAssetPath);
    return nullptr;
  }

  FAssetToolsModule& AssetToolsModule = FAssetToolsModule::GetModule();
  UObject* DuplicatedAsset = AssetToolsModule.Get().DuplicateAsset(SourceAssetName, DestinationPath, SourceObject);

  if (!DuplicatedAsset)
  {
    UE_LOG(LogDigitalTwinsToolBlueprintUtil, Error, TEXT("Failed to duplicate asset: %s -> %s"), *SourcePath, *DestinationPath);
    return nullptr;
  }

  const FString DuplicatedPath = DuplicatedAsset->GetPathName();
  if (!UEditorAssetLibrary::SaveAsset(DuplicatedPath, false))
  {
    UE_LOG(LogDigitalTwinsToolBlueprintUtil, Warning, TEXT("Duplicated asset created but not saved: %s"), *DuplicatedPath);
  }

  TSet<UObject*> SubObjects;
  TQueue<UObject*> Pending;
  Pending.Enqueue(DuplicatedAsset);

  while (!Pending.IsEmpty())
  {
    UObject* CurrentObject = nullptr;
    Pending.Dequeue(CurrentObject);

    if (!CurrentObject || SubObjects.Contains(CurrentObject))
    {
      continue;
    }

    SubObjects.Add(CurrentObject);

    TArray<UObject*> FoundRefs;
    FReferenceFinder ReferenceCollector(FoundRefs, nullptr, false, true, true);
    ReferenceCollector.FindReferences(CurrentObject);

    for (UObject* Ref : FoundRefs)
    {
      if (Ref && !SubObjects.Contains(Ref) && Ref->IsAsset() &&
          Ref->GetPathName().StartsWith(TEXT("/CarlaDigitalTwinsTool/")))
      {
        Pending.Enqueue(Ref);
      }
    }
  }

  TMap<UObject*, UObject*> ReplacementMap;
  TSet<UObject*> ObjectsToReplaceWithin;
  ObjectsToReplaceWithin.Add(DuplicatedAsset);

  for (UObject* RefObj : SubObjects)
  {
    if (!RefObj || RefObj == DuplicatedAsset)
    {
      continue;
    }

    const FString RefObjPath = RefObj->GetPathName();
    if (!RefObjPath.StartsWith(TEXT("/CarlaDigitalTwinsTool/")))
    {
      continue;
    }

    const FString RefFolderPath = FPaths::GetPath(RefObjPath);
    FString RefRelativePath;
    if (RefFolderPath.StartsWith(RootPath + TEXT("/")))
    {
      RefRelativePath = RefFolderPath.Mid(RootPath.Len() + 1);
    }

    const FString RefDestinationPath = RefRelativePath.IsEmpty()
        ? FString::Printf(TEXT("/%s"), *PluginName)
        : FString::Printf(TEXT("/%s/%s"), *PluginName, *RefRelativePath);
    const FString TargetRefObjPath = FString::Printf(TEXT("%s/%s"), *RefDestinationPath, *RefObj->GetName());

    if (!UEditorAssetLibrary::DoesAssetExist(TargetRefObjPath))
    {
      AssetToolsModule.Get().DuplicateAsset(RefObj->GetName(), RefDestinationPath, RefObj);
    }

    UObject* LoadedDuplicatedRefObject = UEditorAssetLibrary::LoadAsset(TargetRefObjPath);
    if (LoadedDuplicatedRefObject)
    {
      ReplacementMap.Add(RefObj, LoadedDuplicatedRefObject);
      ObjectsToReplaceWithin.Add(LoadedDuplicatedRefObject);
    }
  }

  for (const auto& Pair : ReplacementMap)
  {
    UObject* Original = Pair.Key;
    UObject* Replacement = Pair.Value;

    TArray<UObject*> ObjectsToReplace = {Original};
    ObjectTools::ForceReplaceReferences(Replacement, ObjectsToReplace, ObjectsToReplaceWithin);
  }

  if (ReplacementMap.Num() > 0)
  {
    FEditorFileUtils::SaveDirtyPackages(false, false, true, false, false, false);
  }

  return DuplicatedAsset;

#else
  return nullptr;
#endif
}

void UBlueprintUtilFunctions::UnloadRegionWorldPartition(
    UWorld* World,
    const FBox& RegionBounds,
    const FString& RegionName)
{
  TArray<FActorDesc> Descs;

  TArray<FGuid> ToUnload;
  ToUnload.Reserve(Descs.Num());

  for (const FActorDesc& Desc : Descs)
  {
    UClass* ActorClass = Cast<UClass>(Desc.Class.TryLoad());
    if (!ActorClass)
    {
      ToUnload.Add(Desc.Guid);
      continue;
    }

    AActor* CDO = Cast<AActor>(ActorClass->GetDefaultObject());
    if (CDO && CDO->FindComponentByClass<USplineComponent>())
    {
      continue;
    }

    ToUnload.Add(Desc.Guid);
  }
}
