// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Carla/Memory.h"
#include "Carla/NonCopyable.h"
#include "Carla/Road/RoadTypes.h"

#include <cstdint>

namespace carla {
namespace road {

  class TrafficGroup : private MovableNonCopyable {
  public:

    TrafficGroup(
        const TrafficGroupId& id,
        uint16_t red_time,
        uint16_t yellow_time,
        uint16_t green_time)
      : _id(id),
        _red_time(red_time),
        _yellow_time(yellow_time),
        _green_time(green_time) {}

    TrafficGroupId GetId() const {
      return _id;
    }

    uint16_t GetRedTime() const {
      return _red_time;
    }

    uint16_t GetYellowTime() const {
      return _yellow_time;
    }

    uint16_t GetGreenTime() const {
      return _green_time;
    }

  private:

    TrafficGroupId _id;

    uint16_t _red_time;

    uint16_t _yellow_time;

    uint16_t _green_time;

  };

} // namespace road
} // namespace carla