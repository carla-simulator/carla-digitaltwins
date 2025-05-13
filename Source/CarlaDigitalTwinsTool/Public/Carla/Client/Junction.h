// Copyright (c) 2020 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Carla/Memory.h"
#include "Carla/NonCopyable.h"
#include "Carla/Road/Junction.h"
#include "Carla/Road/RoadTypes.h"
#include "Carla/Geom/BoundingBox.h"
#include "Carla/Client/Waypoint.h"

#include <vector>

namespace carla {
namespace client {

  class Map;

  class Junction
    : public EnableSharedFromThis<Junction>,
    private NonCopyable
  {
  public:

    carla::road::JuncId GetId() const {
      return _id;
    }

    std::vector<std::pair<SharedPtr<Waypoint>,SharedPtr<Waypoint>>> GetWaypoints(
        road::Lane::LaneType type = road::Lane::LaneType::Driving) const;

    geom::BoundingBox GetBoundingBox() const;

  private:

    friend class Map;

    Junction(SharedPtr<const Map> parent, const road::Junction *junction);

    SharedPtr<const Map> _parent;

    geom::BoundingBox _bounding_box;

    road::JuncId _id;
  };

} // namespace client
} // namespace carla
