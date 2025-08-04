// Copyright (c) 2023 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

// Engine headers
#include "Generation/PoissonDiscSampling.h"
#include "PCGContext.h"
#include "PCGComponent.h"
#include "PCGCustomVersion.h"
#include "PCGPin.h"

#include <unordered_map>
#include <optional>
#include <algorithm>
#include <vector>
#include <random>

bool FPCGPoissonDiscSampling::ExecuteInternal(FPCGContext* Context) const
{
  using RealT = double;
  using IntT = int32;
  using V2 = UE::Math::TVector2<RealT>;
  using I2 = FIntPoint;

  auto Sqrt2 = FMath::Sqrt((RealT)2);
  auto Pi = (RealT)PI;
  auto Tau = Pi * 2;

  TArray<V2> Results;

  auto SettingsPtr = Context->GetInputSettings<UPCGPoissonDiscSamplingSettings>();
  check(SettingsPtr);
  auto& Settings = *SettingsPtr;
  auto Extent = (V2)Settings.Extent;
  auto R = (RealT)Settings.MinDistance;
  auto R2 = R * R;
  auto MaxRetries = Settings.MaxRetries;
  auto ZeroVec = V2(0, 0);

  auto CellSize = R / Sqrt2; // Do not use InvSqrt, we want precision.
  auto GridFloat = Extent / CellSize;
  FIntPoint GridSize(FMath::CeilToInt(GridFloat.X), FMath::CeilToInt(GridFloat.Y));
  auto CellCount = GridSize.X * GridSize.Y;
  bool UseBitMask = 4096 >= (CellCount / 8);

  std::unordered_map<IntT, V2> Grid;
  std::vector<bool> Occupancy;
  if (UseBitMask)
    Occupancy = std::vector<bool>(CellCount);

  std::random_device RD;
  std::ranlux48 PRNG(RD());
  std::uniform_real_distribution<RealT> URD((RealT)0, (RealT)1);

  auto GetRandomScalar01 = [&]
  {
    return URD(PRNG);
  };

  auto GetRandomScalar = [&](RealT Min, RealT Max)
  {
      return std::fma(GetRandomScalar01(), Max - Min, Min);
  };

  auto GetRandomPoint = [&](V2 Min, V2 Max)
  {
      return V2(
        GetRandomScalar(Min.X, Max.X),
        GetRandomScalar(Min.Y, Max.Y));
  };

  auto GridCoordToFlatIndex = [&](I2 Key)
  {
    return Key.X + Key.Y * GridSize.X;
  };

  auto GridQuery = [&](I2 Key) -> std::optional<V2>
  {
      auto Flat = GridCoordToFlatIndex(Key);
      if (UseBitMask)
        if (!Occupancy[Flat])
          return std::nullopt;
      auto it = Grid.find(Flat);
      if (it == Grid.cend())
        return std::nullopt;
      return it->second;
  };

  auto GridAdd = [&](I2 Key, V2 Value)
  {
      auto Flat = GridCoordToFlatIndex(Key);
      if (UseBitMask)
        Occupancy[Flat] = true;
      Grid.insert({ Flat, Value });
  };

  std::vector<V2> Pending;
  Results.Reserve(CellCount);
  Pending.reserve(CellCount);
  Grid.reserve(CellCount);

  auto First = GetRandomPoint(ZeroVec, Extent);
  Results.Add(First);
  Pending.push_back(First);

  while (!Pending.empty())
  {
    std::uniform_int_distribution<IntT> UID(0, (IntT)Pending.size() - 1);
    auto Index = UID(PRNG);
    check(Index < (IntT)Pending.size());
    auto Point = Pending[Index];
    bool Found = false;
    for (IntT i = 0; i != MaxRetries; ++i)
    {
      auto Theta = GetRandomScalar(0, Tau);
      auto Rho = GetRandomScalar(R, R * 2);
      V2 SinCos;
      UE::Math::GetSinCos(SinCos.Y, SinCos.X, Theta);
      auto NewPoint = Point + Rho * SinCos;
      if (
        NewPoint.X < 0 || NewPoint.X >= Extent.X ||
        NewPoint.Y < 0 || NewPoint.Y >= Extent.Y)
        continue;
      auto Tmp = NewPoint / CellSize;
      auto GridCoord = I2((IntT)Tmp.X, (IntT)Tmp.Y);
      I2 Begin(
        std::max(GridCoord.X - 2, 0),
        std::max(GridCoord.Y - 2, 0));
      I2 End(
        std::min(GridCoord.X + 2, GridSize.X),
        std::min(GridCoord.Y + 2, GridSize.Y));
      bool OK = true;
      for (IntT Y = Begin.Y; Y <= End.Y && OK; ++Y)
      {
        for (IntT X = Begin.X; X <= End.X && OK; ++X)
        {
          auto NeighborOpt = GridQuery(I2(X, Y));
          if (!NeighborOpt.has_value())
            continue;
          auto Neighbor = NeighborOpt.value();
          OK = V2::DistSquared(NewPoint, Neighbor) >= R2;
        }
      }
      if (OK)
      {
        Results.Add(NewPoint);
        Pending.push_back(NewPoint);
        GridAdd(GridCoord, NewPoint);
        Found = true;
        break;
      }
    }
    if (!Found)
      Pending.erase(Pending.begin() + Index);
  }
  return true;
}
