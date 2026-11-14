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
  UWorldComposition* WorldComposition = World ? World->WorldComposition : nullptr;
  if (WorldComposition)
  {
    WorldComposition->bRebaseOriginIn3DSpace = true;
    WorldComposition->RebaseOriginDistance = RebaseOriginDistance;
  }

  LayerStreamingDistanceSquared = LayerStreamingDistance * LayerStreamingDistance;
  ActorStreamingDistanceSquared = ActorStreamingDistance * ActorStreamingDistance;
  RebaseOriginDistanceSquared = RebaseOriginDistance * RebaseOriginDistance;

  // Get spectator
  APlayerController* PlayerController = UGameplayStatics::GetPlayerController(World, 0);
  if (PlayerController)
  {
    Spectator = PlayerController->GetPawnOrSpectator();
  }
  ConsiderSpectatorAsEgo(true);
}

void ALargeMapManager::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
  FCoreDelegates::PreWorldOriginOffset.RemoveAll(this);
  FCoreDelegates::PostWorldOriginOffset.RemoveAll(this);
  FWorldDelegates::LevelRemovedFromWorld.RemoveAll(this);
  FWorldDelegates::LevelAddedToWorld.RemoveAll(this);
  Super::EndPlay(EndPlayReason);
}

void ALargeMapManager::PreWorldOriginOffset(UWorld* InWorld, FIntVector InSrcOrigin, FIntVector InDstOrigin)
{

}

void ALargeMapManager::PostWorldOriginOffset(UWorld* InWorld, FIntVector InSrcOrigin, FIntVector InDstOrigin)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::PostWorldOriginOffset);
  UWorld* World = GetWorld();
  if (InWorld != World)
  {
    return;
  }

  CurrentOriginInt = InDstOrigin;
  CurrentOriginD = FDVector(InDstOrigin);

#if WITH_EDITOR
  if (GEngine)
  {
    GEngine->AddOnScreenDebugMessage(66, MsgTime, FColor::Yellow,
      FString::Printf(TEXT("Src: %s  ->  Dst: %s"), *InSrcOrigin.ToString(), *InDstOrigin.ToString()));
  }

  // This is just to update the color of the msg with the same as the closest map
  float MinDistance = MAX_FLT;
  for (const auto& TilePair : MapTiles)
  {
    const FCarlaMapTile& Tile = TilePair.Value;
    const ULevelStreaming* Level = Tile.StreamingLevel;
    if (!Level)
    {
      continue;
    }
    const float Distance = FVector::Dist(Tile.Location, FVector(InDstOrigin));
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
  if (InWorld != GetWorld() || !InLevel)
  {
    return;
  }
  if (FCarlaMapTile* Tile = FindCarlaMapTile(InLevel))
  {
    Tile->TilesSpawned = false;
  }
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

  if (InAssetsPath.EndsWith(TEXT("/")))
  {
    InAssetsPath.LeftChopInline(1, false);
  }

  AssetsPath = InAssetsPath;

  TArray<FAssetData> AssetsData;
  UObjectLibrary* ObjectLibrary = UObjectLibrary::CreateLibrary(UWorld::StaticClass(), true, true);
  ObjectLibrary->LoadAssetDataFromPath(InAssetsPath);
  ObjectLibrary->GetAssetDataList(AssetsData);

  MapTiles.Reset();
  for (const FAssetData& AssetData : AssetsData)
  {
    const FString TileName = AssetData.AssetName.ToString();
    if (!TileName.Contains(TEXT("_Tile_")) && !TileName.StartsWith(TEXT("Tile_")))
    {
      continue;
    }

    int32 LastUnderscore = INDEX_NONE;
    if (!TileName.FindLastChar(TEXT('_'), LastUnderscore) || LastUnderscore <= 0 || LastUnderscore + 1 >= TileName.Len())
    {
      continue;
    }
    const FString TileName_Y = TileName.Mid(LastUnderscore + 1);
    const FString PrefixBeforeY = TileName.Left(LastUnderscore);
    int32 PrevUnderscore = INDEX_NONE;
    if (!PrefixBeforeY.FindLastChar(TEXT('_'), PrevUnderscore) || PrevUnderscore + 1 >= PrefixBeforeY.Len())
    {
      continue;
    }
    const FString TileName_X = PrefixBeforeY.Mid(PrevUnderscore + 1);

    int32 ParsedX = 0;
    int32 ParsedY = 0;
    if (!LexTryParseString(ParsedX, *TileName_X) || !LexTryParseString(ParsedY, *TileName_Y))
    {
      continue;
    }

    const FIntVector TileVectorID(ParsedX, ParsedY, 0);
    const TileID TileId = GetTileID(TileVectorID);
    LoadCarlaMapTile(InAssetsPath + TEXT("/") + TileName, TileId);
  }
  ObjectLibrary->ConditionalBeginDestroy();
  if (GEngine)
  {
    GEngine->ForceGarbageCollection(true);
  }

  ActorsToConsider.Reset();
  if (SpectatorAsEgo && IsValid(Spectator))
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
  CurrentTilesLoaded.Empty();
}

void ALargeMapManager::RegisterTilesInWorldComposition()
{
  UWorld* World = GetWorld();
  if (!World)
  {
    return;
  }
  UWorldComposition* WorldComposition = World->WorldComposition;
  World->ClearStreamingLevels();
  if (WorldComposition)
  {
    WorldComposition->TilesStreaming.Empty();
    WorldComposition->GetTilesList().Empty();

    for (auto& It : MapTiles)
    {
      ULevelStreamingDynamic* StreamingLevel = It.Value.StreamingLevel;
      if (StreamingLevel)
      {
        World->AddStreamingLevel(StreamingLevel);
        WorldComposition->TilesStreaming.Add(StreamingLevel);
      }
    }
  }
}


FIntVector ALargeMapManager::GetNumTilesInXY() const
{
  if (MapTiles.IsEmpty())
  {
    return FIntVector::ZeroValue;
  }
  int32 MinX = MAX_int32;
  int32 MaxX = MIN_int32;
  int32 MinY = MAX_int32;
  int32 MaxY = MIN_int32;
  for (const auto& It : MapTiles)
  {
    const FIntVector TileID = GetTileVectorID(It.Key);
    MinX = FMath::Min(MinX, TileID.X);
    MaxX = FMath::Max(MaxX, TileID.X);
    MinY = FMath::Min(MinY, TileID.Y);
    MaxY = FMath::Max(MaxY, TileID.Y);
  }
  return { MaxX - MinX + 1, MaxY - MinY + 1, 0 };
}

bool ALargeMapManager::IsLevelOfTileLoaded(FIntVector InTileID) const
{
  TileID TileID = GetTileID(InTileID);

  const FCarlaMapTile* Tile = MapTiles.Find(TileID);
  if (!Tile)
  {
    return false;
  }

  const ULevelStreamingDynamic* StreamingLevel = Tile->StreamingLevel;

  return (StreamingLevel && StreamingLevel->GetLoadedLevel());
}

FIntVector ALargeMapManager::GetTileVectorID(FVector TileLocation) const
{
  return GetTileVectorID(FDVector(TileLocation));
}

FIntVector ALargeMapManager::GetTileVectorID(FDVector TileLocation) const
{
  if (FMath::Abs(TileSide) <= KINDA_SMALL_NUMBER)
  {
    return FIntVector::ZeroValue;
  }
  const double Side = static_cast<double>(TileSide);
  const double HalfSide = 0.5 * Side;
  const double OriginX = static_cast<double>(Tile0Offset.X) + static_cast<double>(LocalTileOffset.X);
  const double OriginY = static_cast<double>(Tile0Offset.Y) + static_cast<double>(LocalTileOffset.Y);
  const int32 TileX = FMath::FloorToInt32(((TileLocation.X - OriginX) + HalfSide) / Side);
  const int32 TileY = FMath::FloorToInt32(((OriginY - TileLocation.Y) + HalfSide) / Side);
  return FIntVector(TileX, TileY, 0);
}

FIntVector ALargeMapManager::GetTileVectorID(TileID TileID) const
{
  return FIntVector{
    static_cast<int32>(TileID >> 32),
    static_cast<int32>(TileID & 0xFFFFFFFFULL),
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
  const uint64 X = static_cast<uint64>(static_cast<uint32>(TileVectorID.X)) << 32;
  const uint64 Y = static_cast<uint64>(static_cast<uint32>(TileVectorID.Y));
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

FCarlaMapTile* ALargeMapManager::FindCarlaMapTile(ULevel* InLevel)
{
  if (!InLevel)
  {
    return nullptr;
  }
  for (auto& It : MapTiles)
  {
    ULevelStreamingDynamic* StreamingLevel = It.Value.StreamingLevel;
    if (StreamingLevel && StreamingLevel->GetLoadedLevel() == InLevel)
    {
      return &(It.Value);
    }
  }
  return nullptr;
}

FCarlaMapTile& ALargeMapManager::GetCarlaMapTile(ULevel* InLevel)
{
  if (FCarlaMapTile* Tile = FindCarlaMapTile(InLevel))
  {
    return *Tile;
  }
  ensureMsgf(false, TEXT("GetCarlaMapTile: Level does not match any registered CarlaMapTile."));
  static FCarlaMapTile InvalidTile;
  InvalidTile = FCarlaMapTile();
  return InvalidTile;
}

FCarlaMapTile& ALargeMapManager::GetCarlaMapTile(FIntVector TileVectorID)
{
  const TileID TileID = GetTileID(TileVectorID);
  if (FCarlaMapTile* Tile = MapTiles.Find(TileID))
  {
    return *Tile;
  }
  ensureMsgf(false, TEXT("GetCarlaMapTile: Missing tile (%d, %d)"), TileVectorID.X, TileVectorID.Y);
  static FCarlaMapTile InvalidTile;
  InvalidTile = FCarlaMapTile();
  return InvalidTile;
}

FCarlaMapTile* ALargeMapManager::GetCarlaMapTile(TileID TileID)
{
  return MapTiles.Find(TileID);
}

FCarlaMapTile& ALargeMapManager::LoadCarlaMapTile(FString TileMapPath, TileID TileId) {
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::LoadCarlaMapTile);
  FCarlaMapTile NewTile;
  NewTile.Location = GetTileLocation(TileId);
  NewTile.Name = TileMapPath;

  const FVector TileLocation = NewTile.Location;
  const FString TileName = NewTile.Name;
  UWorld* World = GetWorld();
  check(World);

  const FString FullName = TileMapPath;
  FString PackageFileName = FullName;
  const FString LongLevelPackageName = FPackageName::FilenameToLongPackageName(PackageFileName);
  const FString UniqueLevelPackageName = LongLevelPackageName;

  ULevelStreamingDynamic* StreamingLevel = NewObject<ULevelStreamingDynamic>(World, *TileName);
  check(StreamingLevel);

  StreamingLevel->SetWorldAssetByPackageName(*UniqueLevelPackageName);

#if WITH_EDITOR
  if (World->IsPlayInEditor() && GEngine)
  {
    if (const FWorldContext* WorldContext = GEngine->GetWorldContextFromWorld(World))
    {
      StreamingLevel->RenameForPIE(WorldContext->PIEInstance);
    }
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
  StreamingLevel->PackageNameToLoad = *LongLevelPackageName;

  NewTile.StreamingLevel = StreamingLevel;

  return MapTiles.Add(TileId, NewTile);
}

void ALargeMapManager::UpdateTilesState()
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::UpdateTilesState);
  TSet<TileID> TilesToConsider;

  ActorsToConsider.RemoveAll([](const AActor* Actor) { return !IsValid(Actor); });
  for (AActor* Actor : ActorsToConsider)
  {
    GetTilesToConsider(Actor, TilesToConsider);
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
  UWorld* World = GetWorld();
  if (World && ActorsToConsider.Num() > 0)
  {
    AActor* ActorToConsider = ActorsToConsider[0];
    if (IsValid(ActorToConsider))
    {
      const FVector ActorLocation = ActorToConsider->GetActorLocation();
      if (ActorLocation.SizeSquared() > FMath::Square(RebaseOriginDistance))
      {
        const TileID TileId = GetTileID(CurrentOriginD + ActorLocation);
        const FVector NewOrigin = GetTileLocation(TileId);
        World->SetNewWorldOrigin(FIntVector(NewOrigin));
      }
    }
  }
}

void ALargeMapManager::GetTilesToConsider(const AActor* ActorToConsider,
                                          TSet<TileID>& OutTilesToConsider)
{
  TRACE_CPUPROFILER_EVENT_SCOPE(ALargeMapManager::GetTilesToConsider);
  if (!IsValid(ActorToConsider))
  {
    return;
  }
  const FDVector ActorLocation = CurrentOriginD + ActorToConsider->GetActorLocation();

  const FDVector UpperPos = ActorLocation + FDVector(LayerStreamingDistance, LayerStreamingDistance, 0);
  const FDVector LowerPos = ActorLocation + FDVector(-LayerStreamingDistance, -LayerStreamingDistance, 0);
  const FIntVector UpperTileId = GetTileVectorID(UpperPos);
  const FIntVector LowerTileId = GetTileVectorID(LowerPos);
  const int32 MinTileX = FMath::Min(LowerTileId.X, UpperTileId.X);
  const int32 MaxTileX = FMath::Max(LowerTileId.X, UpperTileId.X);
  const int32 MinTileY = FMath::Min(LowerTileId.Y, UpperTileId.Y);
  const int32 MaxTileY = FMath::Max(LowerTileId.Y, UpperTileId.Y);

  for (int32 Y = MinTileY; Y <= MaxTileY; ++Y)
  {
    for (int32 X = MinTileX; X <= MaxTileX; ++X)
    {
      const TileID TileID = GetTileID(FIntVector(X, Y, 0));
      if (MapTiles.Contains(TileID))
      {
        OutTilesToConsider.Add(TileID);
      }
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

  for (const TileID TileID : InTilesToUpdate)
  {
    FCarlaMapTile* CarlaTile = MapTiles.Find(TileID);
    if (!ensure(CarlaTile != nullptr))
    {
      continue;
    }
    ULevelStreamingDynamic* StreamingLevel = CarlaTile->StreamingLevel;
    if (!StreamingLevel)
    {
      continue;
    }
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
  FString FileContent;
  FileContent += TEXT("LargeMapManager state\n");
  FileContent += TEXT("Tile:\n");
  FileContent += TEXT("ID\tName\tLocation\n");
  for (const auto& It : MapTiles)
  {
    const FCarlaMapTile& Tile = It.Value;
    FileContent += FString::Printf(TEXT("  %llu\t%s\t%s\n"),
      static_cast<unsigned long long>(It.Key), *Tile.Name, *Tile.Location.ToString());
  }
  FileContent += FString::Printf(TEXT("\nNum generated tiles: %d\n"), MapTiles.Num());

  TArray<FString> StringArray;
  AssetsPath.ParseIntoArray(StringArray, TEXT("/"), true);
  const FString TableName = StringArray.Num() > 0
      ? StringArray.Last()
      : (LargeMapName.IsEmpty() ? TEXT("LargeMap") : LargeMapName);

  const FString FilePath = FPaths::Combine(FPaths::ProjectSavedDir(), TableName + TEXT(".txt"));
  FFileHelper::SaveStringToFile(
    FileContent,
    *FilePath,
    FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM,
    &IFileManager::Get(),
    EFileWrite::FILEWRITE_Silent);
}

void ALargeMapManager::PrintMapInfo()
{
  UWorld* World = GetWorld();
  if (!World || !GEngine)
  {
    return;
  }

  FDVector CurrentActorPosition;
  if (ActorsToConsider.Num() > 0 && IsValid(ActorsToConsider[0]))
  {
    CurrentActorPosition = CurrentOriginD + ActorsToConsider[0]->GetActorLocation();
  }

  const TArray<ULevel*>& Levels = World->GetLevels();

  FString Output;
  Output += FString::Printf(TEXT("Num levels in world composition: %d\n"), World->WorldComposition != nullptr ? World->WorldComposition->TilesStreaming.Num() : -1);
  Output += FString::Printf(TEXT("Num levels loaded: %d\n"), Levels.Num());
  Output += FString::Printf(TEXT("Num tiles loaded: %d\n"), CurrentTilesLoaded.Num());
  Output += TEXT("Tiles loaded: [ ");
  for (const TileID TileId : CurrentTilesLoaded)
  {
    Output += FString::Printf(TEXT("%s, "), *TileIDToString(TileId));
  }
  Output += TEXT("]\n");
  GEngine->AddOnScreenDebugMessage(0, MsgTime, FColor::Cyan, Output);

  int LastMsgIndex = TilesDistMsgIndex;
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nActor Global Position: %s km"), *(FDVector(CurrentActorPosition) / (1000.0 * 100.0)).ToString()));

  const FIntVector CurrentTile = GetTileVectorID(CurrentActorPosition);
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nActor Current Tile: %d_%d"), CurrentTile.X, CurrentTile.Y));

  LastMsgIndex = ClientLocMsgIndex;
  GEngine->AddOnScreenDebugMessage(LastMsgIndex++, MsgTime, FColor::White,
    FString::Printf(TEXT("\nOrigin: %s km"), *(FDVector(CurrentOriginInt) / (1000.0 * 100.0)).ToString()));

  for (const AActor* Actor : ActorsToConsider)
  {
    if (IsValid(Actor))
    {
      Output.Reset();
      const float ToKm = 1000.0f * 100.0f;
      const FVector TileActorLocation = Actor->GetActorLocation();
      const FDVector ClientActorLocation = CurrentOriginD + FDVector(TileActorLocation);

      Output += FString::Printf(TEXT("Local Loc: %s km\n"), *(TileActorLocation / ToKm).ToString());
      Output += FString::Printf(TEXT("Client Loc: %s km\n"), *(ClientActorLocation / ToKm).ToString());
      Output += TEXT("---------------");
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
