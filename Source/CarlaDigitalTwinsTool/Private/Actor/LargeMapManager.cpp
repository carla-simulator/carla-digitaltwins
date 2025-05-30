// Copyright (c) 2021 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB).
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Actor/LargeMapManager.h"

#include "Engine/WorldComposition.h"
#include "Engine/ObjectLibrary.h"
#include "Engine/EngineTypes.h"
#include "Components/PrimitiveComponent.h"
#include "Landscape.h"
#include "LandscapeHeightfieldCollisionComponent.h"
#include "LandscapeComponent.h"
#include "Kismet/GameplayStatics.h"

#include "Misc/FileHelper.h"
#include "Misc/Paths.h"

#define LARGEMAP_LOGS 1


// Sets default values
ALargeMapManager::ALargeMapManager()
{
  PrimaryActorTick.bCanEverTick = true;
  // PrimaryActorTick.TickInterval = TickInterval;
}

ALargeMapManager::~ALargeMapManager()
{
  /// Remove delegates
  // Origin rebase
  FCoreDelegates::PreWorldOriginOffset.RemoveAll(this);
  FCoreDelegates::PostWorldOriginOffset.RemoveAll(this);
  // Level added/removed from world
  FWorldDelegates::LevelRemovedFromWorld.RemoveAll(this);
  FWorldDelegates::LevelAddedToWorld.RemoveAll(this);

}

// Called when the game starts or when spawned
void ALargeMapManager::BeginPlay()
{
  Super::BeginPlay();
  RegisterTilesInWorldComposition();

  UWorld* World = GetWorld();
  /// Setup delegates
  // Origin rebase
  FCoreDelegates::PreWorldOriginOffset.AddUObject(this, &ALargeMapManager::PreWorldOriginOffset);
  FCoreDelegates::PostWorldOriginOffset.AddUObject(this, &ALargeMapManager::PostWorldOriginOffset);
  // Level added/removed from world
  FWorldDelegates::LevelAddedToWorld.AddUObject(this, &ALargeMapManager::OnLevelAddedToWorld);
  FWorldDelegates::LevelRemovedFromWorld.AddUObject(this, &ALargeMapManager::OnLevelRemovedFromWorld);


  // Setup Origin rebase settings
  UWorldComposition* WorldComposition = World->WorldComposition;
  WorldComposition->bRebaseOriginIn3DSpace = true;
  WorldComposition->RebaseOriginDistance = RebaseOriginDistance;

  LayerStreamingDistanceSquared = LayerStreamingDistance * LayerStreamingDistance;
  ActorStreamingDistanceSquared = ActorStreamingDistance * ActorStreamingDistance;
  RebaseOriginDistanceSquared = RebaseOriginDistance * RebaseOriginDistance;

  // Get spectator
  APlayerController* PlayerController = UGameplayStatics::GetPlayerController(GetWorld(), 0);
  if(PlayerController)
  {
    Spectator = PlayerController->GetPawnOrSpectator();
  }
  ConsiderSpectatorAsEgo(true);
}

void ALargeMapManager::PreWorldOriginOffset(UWorld* InWorld, FIntVector InSrcOrigin, FIntVector InDstOrigin)
{

}

void ALargeMapManager::PostWorldOriginOffset(UWorld* InWorld, FIntVector InSrcOrigin, FIntVector InDstOrigin)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::PostWorldOriginOffset);
  CurrentOriginInt = InDstOrigin;
  CurrentOriginD = FDVector(InDstOrigin);

  UWorld* World = GetWorld();
#if WITH_EDITOR
  GEngine->AddOnScreenDebugMessage(66, MsgTime, FColor::Yellow,
    FString::Printf(TEXT("Src: %s  ->  Dst: %s"), *InSrcOrigin.ToString(), *InDstOrigin.ToString()));

  // This is just to update the color of the msg with the same as the closest map
  const TArray<ULevelStreaming*>& StreamingLevels = World->GetStreamingLevels();
  FColor LevelColor = FColor::White;
  float MinDistance = 10000000.0f;
  for (const auto& TilePair : MapTiles)
  {
    const FCarlaMapTile& Tile = TilePair.Value;
    const ULevelStreaming* Level = Tile.StreamingLevel;
    FVector LevelLocation = Tile.Location;
    float Distance = FVector::Dist(LevelLocation, FVector(InDstOrigin));
    if (Distance < MinDistance)
    {
      MinDistance = Distance;
      PositonMsgColor = Level->LevelColor.ToFColor(false);
    }
  }
#endif // WITH_EDITOR
}

void ALargeMapManager::OnLevelAddedToWorld(ULevel* InLevel, UWorld* InWorld)
{


  //FDebug::DumpStackTraceToLog(ELogVerbosity::Log);
}

void ALargeMapManager::OnLevelRemovedFromWorld(ULevel* InLevel, UWorld* InWorld)
{

  //FDebug::DumpStackTraceToLog(ELogVerbosity::Log);
  FCarlaMapTile& Tile = GetCarlaMapTile(InLevel);
  Tile.TilesSpawned = false;
}

void ALargeMapManager::RegisterInitialObjects()
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::RegisterInitialObjects);
  UWorld* World = GetWorld();

}

void ALargeMapManager::OnActorDestroyed(AActor* DestroyedActor)
{


}

void ALargeMapManager::SetTile0Offset(const FVector& Offset)
{
  Tile0Offset = Offset;
}

void ALargeMapManager::SetTileSize(float Size)
{
  TileSide = Size;
}

float ALargeMapManager::GetTileSize()
{
  return TileSide;
}

FVector ALargeMapManager::GetTile0Offset()
{
  return Tile0Offset;
}

void ALargeMapManager::SetLayerStreamingDistance(float Distance)
{
  LayerStreamingDistance = Distance;
  LayerStreamingDistanceSquared =
      LayerStreamingDistance*LayerStreamingDistance;
}

void ALargeMapManager::SetActorStreamingDistance(float Distance)
{
  ActorStreamingDistance = Distance;
  ActorStreamingDistanceSquared =
      ActorStreamingDistance*ActorStreamingDistance;
}

float ALargeMapManager::GetLayerStreamingDistance() const
{
  return LayerStreamingDistance;
}

float ALargeMapManager::GetActorStreamingDistance() const
{
  return ActorStreamingDistance;
}

FTransform ALargeMapManager::GlobalToLocalTransform(const FTransform& InTransform) const
{
  return FTransform(
        InTransform.GetRotation(),
        InTransform.GetLocation() - CurrentOriginD.ToFVector(),
        InTransform.GetScale3D());
}

FVector ALargeMapManager::GlobalToLocalLocation(const FVector& InLocation) const
{
  return InLocation - CurrentOriginD.ToFVector();
}

FTransform ALargeMapManager::LocalToGlobalTransform(const FTransform& InTransform) const
{
  return FTransform(
        InTransform.GetRotation(),
        CurrentOriginD.ToFVector() + InTransform.GetLocation(),
        InTransform.GetScale3D());
}

FVector ALargeMapManager::LocalToGlobalLocation(const FVector& InLocation) const
{
  return CurrentOriginD.ToFVector() + InLocation;
}

float tick_execution_time = 0;
uint64_t num_ticks = 0;

void ALargeMapManager::Tick(float DeltaTime)
{
  Super::Tick(DeltaTime);

  // Update map tiles, load/unload based on actors to consider (heros) position
  // Also, to avoid looping over the heros again, it checks if any actor to consider has been removed
  UpdateTilesState();

  CheckIfRebaseIsNeeded();

#if WITH_EDITOR
  if (bPrintMapInfo) PrintMapInfo();
#endif // WITH_EDITOR

}

void ALargeMapManager::GenerateLargeMap() {
  GenerateMap(LargeMapTilePath);
}

void ALargeMapManager::GenerateMap(FString InAssetsPath)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::GenerateMap);

  ClearWorldAndTiles();


  // Check if it ends with '/'
  if (InAssetsPath.EndsWith(TEXT("/")))
  {
    // Remove the last character
    InAssetsPath.LeftChopInline(1, false);
  }

  AssetsPath = InAssetsPath;

  /// Retrive all the assets in the path
  TArray<FAssetData> AssetsData;
  UObjectLibrary* ObjectLibrary = UObjectLibrary::CreateLibrary(UWorld::StaticClass(), true, true);
  ObjectLibrary->LoadAssetDataFromPath(InAssetsPath);
  ObjectLibrary->GetAssetDataList(AssetsData);

  /// Generate tiles based on mesh positions
  UWorld* World = GetWorld();
  MapTiles.Reset();
  for (const FAssetData& AssetData : AssetsData)
  {
    FString TileName = AssetData.AssetName.ToString();
    if (!TileName.Contains("_Tile_"))
    {
      continue;
    }
    FString TileName_X = "";
    FString TileName_Y = "";
    size_t i = TileName.Len()-1;
    for (; i > 0; i--) {
      TCHAR character = TileName[i];
      if (character == '_') {
        break;
      }
      TileName_Y = FString::Chr(character) + TileName_Y;
    }
    i--;
    for (; i > 0; i--) {
      TCHAR character = TileName[i];
      if (character == '_') {
        break;
      }
      TileName_X = FString::Chr(character) + TileName_X;
    }
    FIntVector TileVectorID = FIntVector(FCString::Atoi(*TileName_X), FCString::Atoi(*TileName_Y), 0);
    TileID TileId = GetTileID(TileVectorID);
    LoadCarlaMapTile(InAssetsPath + "/" + AssetData.AssetName.ToString(), TileId);
  }
  ObjectLibrary->ConditionalBeginDestroy();
  GEngine->ForceGarbageCollection(true);

  ActorsToConsider.Reset();
  if (SpectatorAsEgo && Spectator)
  {
    ActorsToConsider.Add(Spectator);
  }

#if WITH_EDITOR
  DumpTilesTable();
#endif // WITH_EDITOR
}

void ALargeMapManager::GenerateMap(TArray<TPair<FString, FIntVector>> MapPathsIds)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::GenerateMap);
  ClearWorldAndTiles();

  for (TPair<FString, FIntVector>& PathId : MapPathsIds)
  {
    FIntVector& TileVectorID = PathId.Value;
    FString& Path = PathId.Key;
    TileID TileId = GetTileID(TileVectorID);
    LoadCarlaMapTile(Path, TileId);
  }
}

void ALargeMapManager::ClearWorldAndTiles()
{
  MapTiles.Empty();
}

void ALargeMapManager::RegisterTilesInWorldComposition()
{
  UWorld* World = GetWorld();
  UWorldComposition* WorldComposition = World->WorldComposition;
  World->ClearStreamingLevels();
  if (WorldComposition)
  {
    WorldComposition->TilesStreaming.Empty();
    WorldComposition->GetTilesList().Empty();

    for (auto& It : MapTiles)
    {
      ULevelStreamingDynamic* StreamingLevel = It.Value.StreamingLevel;
      World->AddStreamingLevel(StreamingLevel);
      WorldComposition->TilesStreaming.Add(StreamingLevel);
    }
  }
}


FIntVector ALargeMapManager::GetNumTilesInXY() const
{
  int32 MinX = 0;
  int32 MaxX = 0;
  int32 MinY = 0;
  int32 MaxY = 0;
  for (auto& It : MapTiles)
  {
    FIntVector TileID = GetTileVectorID(It.Key);
    MinX = (TileID.X < MinX) ? TileID.X : MinX;
    MaxX = (TileID.X > MaxX) ? TileID.X : MaxX;

    MinY = (TileID.Y < MinY) ? TileID.Y : MinY;
    MaxY = (TileID.Y > MaxY) ? TileID.Y : MaxY;
  }
  return { MaxX - MinX + 1, MaxY - MinY + 1, 0 };
}

bool ALargeMapManager::IsLevelOfTileLoaded(FIntVector InTileID) const
{
  TileID TileID = GetTileID(InTileID);

  const FCarlaMapTile* Tile = MapTiles.Find(TileID);
  if (!Tile)
  {
    if (bPrintErrors)
    {
   
    }
    return false;
  }

  const ULevelStreamingDynamic* StreamingLevel = Tile->StreamingLevel;

  return (StreamingLevel && StreamingLevel->GetLoadedLevel());
}

FIntVector ALargeMapManager::GetTileVectorID(FVector TileLocation) const
{
  FIntVector VectorId = FIntVector(
      (TileLocation -
      (Tile0Offset - FVector(0.5f*TileSide,-0.5f*TileSide, 0) + LocalTileOffset))
      / TileSide);
  VectorId.Y *= -1;
  return VectorId;
}

FIntVector ALargeMapManager::GetTileVectorID(FDVector TileLocation) const
{
  FIntVector VectorId = (
      (TileLocation -
      (Tile0Offset - FVector(0.5f*TileSide,-0.5f*TileSide, 0) + LocalTileOffset))
      / TileSide).ToFIntVector();
  VectorId.Y *= -1;
  return VectorId;
}

FIntVector ALargeMapManager::GetTileVectorID(TileID TileID) const
{
  return FIntVector{
    (int32)(TileID >> 32),
    (int32)(TileID & (int32)(~0)),
    0
  };
}

FVector ALargeMapManager::GetTileLocation(TileID TileID) const
{
  FIntVector VTileId = GetTileVectorID(TileID);
  return GetTileLocation(VTileId);
}

FVector ALargeMapManager::GetTileLocation(FIntVector TileVectorID) const
{
  TileVectorID.Y *= -1;
  return FVector(TileVectorID)* TileSide + Tile0Offset;
}

FDVector ALargeMapManager::GetTileLocationD(TileID TileID) const
{
  FIntVector VTileId = GetTileVectorID(TileID);
  return GetTileLocationD(VTileId);
}

FDVector ALargeMapManager::GetTileLocationD(FIntVector TileVectorID) const
{
  TileVectorID.Y *= -1;
  return FDVector(TileVectorID) * TileSide + Tile0Offset;
}

ALargeMapManager::TileID ALargeMapManager::GetTileID(FIntVector TileVectorID) const
{
  int64 X = ((int64)(TileVectorID.X) << 32);
  int64 Y = (int64)(TileVectorID.Y) & 0x00000000FFFFFFFF;
  return (X | Y);
}

ALargeMapManager::TileID ALargeMapManager::GetTileID(FVector TileLocation) const
{
  FIntVector TileID = GetTileVectorID(TileLocation);
  return GetTileID(TileID);
}

ALargeMapManager::TileID ALargeMapManager::GetTileID(FDVector TileLocation) const
{
  FIntVector TileID = GetTileVectorID(TileLocation);
  return GetTileID(TileID);
}

FCarlaMapTile* ALargeMapManager::GetCarlaMapTile(FVector Location)
{
  TileID TileID = GetTileID(Location);
  return GetCarlaMapTile(TileID);
}

FCarlaMapTile& ALargeMapManager::GetCarlaMapTile(ULevel* InLevel)
{
  FCarlaMapTile* Tile = nullptr;
  for (auto& It : MapTiles)
  {
    ULevelStreamingDynamic* StreamingLevel = It.Value.StreamingLevel;
    ULevel* Level = StreamingLevel->GetLoadedLevel();
    if (Level == InLevel)
    {
      Tile = &(It.Value);
      break;
    }
  }
  check(Tile);
  return *Tile;
}

FCarlaMapTile& ALargeMapManager::GetCarlaMapTile(FIntVector TileVectorID)
{
  TileID TileID = GetTileID(TileVectorID);
  FCarlaMapTile* Tile = MapTiles.Find(TileID);
  return *Tile;
}

FCarlaMapTile* ALargeMapManager::GetCarlaMapTile(TileID TileID)
{
  FCarlaMapTile* Tile = MapTiles.Find(TileID);
  return Tile;
}

FCarlaMapTile& ALargeMapManager::LoadCarlaMapTile(FString TileMapPath, TileID TileId) {
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::LoadCarlaMapTile);
  // Need to generate a new Tile
  FCarlaMapTile NewTile;
  // 1 - Calculate the Tile position
  FIntVector VTileID = GetTileVectorID(TileId);
  NewTile.Location = GetTileLocation(TileId);
  NewTile.Name = TileMapPath;

  // 3 - Generate the StreamLevel
  FVector TileLocation = NewTile.Location;
  FString TileName = NewTile.Name;
  UWorld* World = GetWorld();
  UWorldComposition* WorldComposition = World->WorldComposition;

  FString FullName = TileMapPath;
  FString PackageFileName = FullName;
  FString LongLevelPackageName = FPackageName::FilenameToLongPackageName(PackageFileName);
  FString UniqueLevelPackageName = LongLevelPackageName;

  ULevelStreamingDynamic* StreamingLevel = NewObject<ULevelStreamingDynamic>(World, *TileName);
  check(StreamingLevel);

  StreamingLevel->SetWorldAssetByPackageName(*UniqueLevelPackageName);

#if WITH_EDITOR
  if (World->IsPlayInEditor())
  {
    FWorldContext WorldContext = GEngine->GetWorldContextFromWorldChecked(World);
    StreamingLevel->RenameForPIE(WorldContext.PIEInstance);
  }
  StreamingLevel->SetShouldBeVisibleInEditor(true);
  StreamingLevel->LevelColor = FColor::MakeRandomColor();
#endif // WITH_EDITOR

  StreamingLevel->SetShouldBeLoaded(false);
  StreamingLevel->SetShouldBeVisible(false);
  StreamingLevel->bShouldBlockOnLoad = ShouldTilesBlockOnLoad;
  StreamingLevel->bInitiallyLoaded = false;
  StreamingLevel->bInitiallyVisible = false;
  StreamingLevel->LevelTransform = FTransform(TileLocation);
  StreamingLevel->PackageNameToLoad = *FullName;

  if (!FPackageName::DoesPackageExist(FullName, NULL, &PackageFileName))
  {
    
  }

  if (!FPackageName::DoesPackageExist(LongLevelPackageName, NULL, &PackageFileName))
  {

  }

  //Actual map package to load
  StreamingLevel->PackageNameToLoad = *LongLevelPackageName;

  NewTile.StreamingLevel = StreamingLevel;

  // 4 - Add it to the map
  return MapTiles.Add(TileId, NewTile);
}

void ALargeMapManager::UpdateTilesState()
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::UpdateTilesState);
  TSet<TileID> TilesToConsider;

  // Loop over ActorsToConsider to update the state of the map tiles
  // if the actor is not valid will be removed
  for (AActor* Actor : ActorsToConsider)
  {
    if (IsValid(Actor))
    {
      GetTilesToConsider(Actor, TilesToConsider);
    }
  }

  TSet<TileID> TilesToBeVisible;
  TSet<TileID> TilesToHidde;
  GetTilesThatNeedToChangeState(TilesToConsider, TilesToBeVisible, TilesToHidde);

  UpdateTileState(TilesToBeVisible, true, true, true);

  UpdateTileState(TilesToHidde, false, false, false);

  UpdateCurrentTilesLoaded(TilesToBeVisible, TilesToHidde);

}

void ALargeMapManager::CheckIfRebaseIsNeeded()
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::CheckIfRebaseIsNeeded);
  if(ActorsToConsider.Num() > 0)
  {
    UWorld* World = GetWorld();
    UWorldComposition* WorldComposition = World->WorldComposition;
    // TODO: consider multiple hero vehicles for rebasing
    AActor* ActorToConsider = ActorsToConsider[0];
    if( IsValid(ActorToConsider) )
    {
      FVector ActorLocation = ActorToConsider->GetActorLocation();
      FIntVector ILocation = FIntVector(ActorLocation.X, ActorLocation.Y, ActorLocation.Z);
      if (ActorLocation.SizeSquared() > FMath::Square(RebaseOriginDistance) )
      {
        TileID TileId = GetTileID(CurrentOriginD + ActorLocation);
        FVector NewOrigin = GetTileLocation(TileId);
        World->SetNewWorldOrigin(FIntVector(NewOrigin));
      }
    }
  }
}

void ALargeMapManager::GetTilesToConsider(const AActor* ActorToConsider,
                                          TSet<TileID>& OutTilesToConsider)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::GetTilesToConsider);
  check(ActorToConsider);
  // World location
  FDVector ActorLocation = CurrentOriginD + ActorToConsider->GetActorLocation();

  // Calculate Current Tile
  FIntVector CurrentTile = GetTileVectorID(ActorLocation);

  // Calculate tile bounds
  FDVector UpperPos = ActorLocation + FDVector(LayerStreamingDistance,LayerStreamingDistance,0);
  FDVector LowerPos = ActorLocation + FDVector(-LayerStreamingDistance,-LayerStreamingDistance,0);
  FIntVector UpperTileId = GetTileVectorID(UpperPos);
  FIntVector LowerTileId = GetTileVectorID(LowerPos);
  for (int Y = UpperTileId.Y; Y <= LowerTileId.Y; Y++)
  {
    for (int X = LowerTileId.X; X <= UpperTileId.X; X++)
    {
      // I don't check the bounds of the Tile map, if the Tile does not exist
      // I just simply discard it
      FIntVector TileToCheck = FIntVector(X, Y, 0);

      TileID TileID = GetTileID(TileToCheck);
      FCarlaMapTile* Tile = MapTiles.Find(TileID);
      if (!Tile)
      {
    
        continue; // Tile does not exist, discard
      }

        OutTilesToConsider.Add(TileID);
    }
  }
}

void ALargeMapManager::GetTilesThatNeedToChangeState(
  const TSet<TileID>& InTilesToConsider,
  TSet<TileID>& OutTilesToBeVisible,
  TSet<TileID>& OutTilesToHidde)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::GetTilesThatNeedToChangeState);
  OutTilesToBeVisible = InTilesToConsider.Difference(CurrentTilesLoaded);
  OutTilesToHidde = CurrentTilesLoaded.Difference(InTilesToConsider);
}

void ALargeMapManager::UpdateTileState(
  const TSet<TileID>& InTilesToUpdate,
  bool InShouldBlockOnLoad,
  bool InShouldBeLoaded,
  bool InShouldBeVisible)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::UpdateTileState);
  UWorld* World = GetWorld();
  UWorldComposition* WorldComposition = World->WorldComposition;

  // Gather all the locations of the levels to load
  for (const TileID TileID : InTilesToUpdate)
  {
      FCarlaMapTile* CarlaTile = MapTiles.Find(TileID);
      check(CarlaTile); // If an invalid ID reach here, we did something very wrong
      ULevelStreamingDynamic* StreamingLevel = CarlaTile->StreamingLevel;
      StreamingLevel->bShouldBlockOnLoad = InShouldBlockOnLoad;
      StreamingLevel->SetShouldBeLoaded(InShouldBeLoaded);
      StreamingLevel->SetShouldBeVisible(InShouldBeVisible);
  }
}

void ALargeMapManager::UpdateCurrentTilesLoaded(
  const TSet<TileID>& InTilesToBeVisible,
  const TSet<TileID>& InTilesToHidde)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::UpdateCurrentTilesLoaded);
  for (const TileID TileID : InTilesToHidde)
  {
    CurrentTilesLoaded.Remove(TileID);
  }

  for (const TileID TileID : InTilesToBeVisible)
  {
    CurrentTilesLoaded.Add(TileID);
  }
}

FString ALargeMapManager::GenerateTileName(TileID TileID)
{
  int32 X = (int32)(TileID >> 32);
  int32 Y = (int32)(TileID);
  return FString::Printf(TEXT("Tile_%d_%d"), X, Y);
}

FString ALargeMapManager::TileIDToString(TileID TileID)
{
  int32 X = (int32)(TileID >> 32);
  int32 Y = (int32)(TileID);
  return FString::Printf(TEXT("%d_%d"), X, Y);
}

void ALargeMapManager::DumpTilesTable() const
{
  FString FileContent = "";
  FileContent += FString::Printf(TEXT("LargeMapManager state\n"));

  FileContent += FString::Printf(TEXT("Tile:\n"));
  FileContent += FString::Printf(TEXT("ID\tName\tLocation\n"));
  for (auto& It : MapTiles)
  {
    const FCarlaMapTile& Tile = It.Value;
    FileContent += FString::Printf(TEXT("  %ld\t%s\t%s\n"), It.Key, *Tile.Name, *Tile.Location.ToString());
  }
  FileContent += FString::Printf(TEXT("\nNum generated tiles: %d\n"), MapTiles.Num());

  // Generate the map name with the assets folder name
  TArray<FString> StringArray;
  AssetsPath.ParseIntoArray(StringArray, TEXT("/"), false);

  FString FilePath = FPaths::ProjectSavedDir() + StringArray[StringArray.Num() - 1] + ".txt";
  FFileHelper::SaveStringToFile(
    FileContent,
    *FilePath,
    FFileHelper::EEncodingOptions::AutoDetect,
    &IFileManager::Get(),
    EFileWrite::FILEWRITE_Silent);
}

void ALargeMapManager::PrintMapInfo()
{
  UWorld* World = GetWorld();

  FDVector CurrentActorPosition;
  if (ActorsToConsider.Num() > 0)
    CurrentActorPosition = CurrentOriginD + ActorsToConsider[0]->GetActorLocation();

  const TArray<FLevelCollection>& WorldLevelCollections = World->GetLevelCollections();
  const FLevelCollection* LevelCollection = World->GetActiveLevelCollection();
  const TArray<ULevel*>& Levels = World->GetLevels();
  const TArray<ULevelStreaming*>& StreamingLevels = World->GetStreamingLevels();
  ULevel* CurrentLevel = World->GetCurrentLevel();

  FString Output = "";
  Output += FString::Printf(TEXT("Num levels in world composition: %d\n"), World->WorldComposition != nullptr ? World->WorldComposition->TilesStreaming.Num() : -1);
  Output += FString::Printf(TEXT("Num levels loaded: %d\n"), Levels.Num() );
  Output += FString::Printf(TEXT("Num tiles loaded: %d\n"), CurrentTilesLoaded.Num() );
  Output += FString::Printf(TEXT("Tiles loaded: [ "));
  for(TileID& TileId : CurrentTilesLoaded)
  {
    Output += FString::Printf(TEXT("%s, "), *TileIDToString(TileId));
  }
  Output += FString::Printf(TEXT("]\n"));
  GEngine->AddOnScreenDebugMessage(0, MsgTime, FColor::Cyan, Output);

  int LastMsgIndex = TilesDistMsgIndex;
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nActor Global Position: %s km"), *(FDVector(CurrentActorPosition) / (1000.0 * 100.0)).ToString()) );

  FIntVector CurrentTile = GetTileVectorID(CurrentActorPosition);
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nActor Current Tile: %d_%d"), CurrentTile.X, CurrentTile.Y ));

  LastMsgIndex = ClientLocMsgIndex;
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nOrigin: %s km"), *(FDVector(CurrentOriginInt) / (1000.0 * 100.0)).ToString()) );

  for (const AActor* Actor : ActorsToConsider)
  {
    if (IsValid(Actor))
    {
      Output = "";
      float ToKm = 1000.0f * 100.0f;
      FVector TileActorLocation = Actor->GetActorLocation();
      FDVector ClientActorLocation = CurrentOriginD + FDVector(TileActorLocation);

      Output += FString::Printf(TEXT("Local Loc: %s meters\n"), *(TileActorLocation / ToKm).ToString());
      Output += FString::Printf(TEXT("Client Loc: %s km\n"), *(ClientActorLocation / ToKm).ToString());
      Output += "---------------";
      GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, PositonMsgColor, Output);

      if (LastMsgIndex > MaxClientLocMsgIndex) break;
    }
  }
}

void ALargeMapManager::ConsiderSpectatorAsEgo(bool _SpectatorAsEgo)
{
  SpectatorAsEgo = _SpectatorAsEgo;
  if(SpectatorAsEgo && ActorsToConsider.Num() == 0 && Spectator)
  {
    // Activating the spectator in an empty world
    ActorsToConsider.Add(Spectator);
  }
  if (!SpectatorAsEgo && ActorsToConsider.Num() == 1 && ActorsToConsider.Contains(Spectator))
  {
    // Deactivating the spectator in a world with no other egos
    ActorsToConsider.Reset();
  }
}
