// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Carla/Road/element/RoadWaypoint.h"

#include <Carla/disable-ue4-macros.h>
#include <boost/container_hash/hash.hpp>
#include <Carla/enable-ue4-macros.h>

namespace std {

  using WaypointHash = hash<carla::road::element::Waypoint>;

  WaypointHash::result_type WaypointHash::operator()(const argument_type &waypoint) const {
    WaypointHash::result_type seed = 0u;
    boost::hash_combine(seed, waypoint.road_id);
    boost::hash_combine(seed, waypoint.section_id);
    boost::hash_combine(seed, waypoint.lane_id);
    boost::hash_combine(seed, static_cast<float>(std::floor(waypoint.s * 200.0)));
    return seed;
  }

} // namespace std
