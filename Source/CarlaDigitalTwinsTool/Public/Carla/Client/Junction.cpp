// Copyright (c) 2020 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Carla/Client/Junction.h"
#include "Carla/Client/Map.h"
#include "Carla/Road/element/RoadWaypoint.h"

namespace carla {
namespace client {

  Junction::Junction(SharedPtr<const Map> parent, const road::Junction *junction) : _parent(parent) {
    _bounding_box = junction->GetBoundingBox();
    _id = junction->GetId();
  }

  std::vector<std::pair<SharedPtr<Waypoint>, SharedPtr<Waypoint>>> Junction::GetWaypoints(
      road::Lane::LaneType type) const {
    return _parent->GetJunctionWaypoints(GetId(), type);
  }

  geom::BoundingBox Junction::GetBoundingBox() const {
    return _bounding_box;
  }

} // namespace client
} // namespace carla
