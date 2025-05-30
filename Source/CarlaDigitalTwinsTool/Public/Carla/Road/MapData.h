// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Carla/Geom/GeoLocation.h"
#include "Carla/Iterator.h"
#include "Carla/ListView.h"
#include "Carla/NonCopyable.h"
#include "Carla/Road/Controller.h"
#include "Carla/Road/element/RoadInfo.h"
#include "Carla/Road/Junction.h"
#include "Carla/Road/Road.h"
#include "Carla/Road/RoadTypes.h"
#include "Carla/Road/Signal.h"

#include "Carla/disable-ue4-macros.h"
#include <boost/iterator/transform_iterator.hpp>
#include "Carla/enable-ue4-macros.h"

#include <unordered_map>

namespace carla {
namespace road {

  class Lane;

  class MapData : private MovableNonCopyable {
  public:

    const geom::GeoLocation &GetGeoReference() const {
      return _geo_reference;
    }

    std::unordered_map<RoadId, Road> &GetRoads();

    const std::unordered_map<RoadId, Road> &GetRoads() const {
      return _roads;
    }

    std::unordered_map<JuncId, Junction> &GetJunctions();

    const std::unordered_map<JuncId, Junction> &GetJunctions() const {
      return _junctions;
    }

    bool ContainsRoad(RoadId id) const {
      return (_roads.find(id) != _roads.end());
    }

    Road &GetRoad(const RoadId id);

    const Road &GetRoad(const RoadId id) const;

    Junction *GetJunction(JuncId id);

    const Junction *GetJunction(JuncId id) const;

    template <typename T>
    auto GetRoadInfo(const RoadId id, const double s) {
      return GetRoad(id).template GetInfo<T>(s);
    }

    template <typename T>
    auto GetLaneInfo(
        const RoadId road_id,
        const SectionId section_id,
        const LaneId lane_id,
        const double s) {
      return GetRoad(road_id).GetLaneById(section_id, lane_id).template GetInfo<T>(s);
    }

    size_t GetRoadCount() const {
      return _roads.size();
    }

    const std::unordered_map<SignId, std::unique_ptr<Signal>> &GetSignals() const {
      return _signals;
    }

    const std::unordered_map<ContId, std::unique_ptr<Controller>>& GetControllers() const {
      return _controllers;
    }

  private:

    friend class MapBuilder;

    MapData() = default;

    geom::GeoLocation _geo_reference;

    std::unordered_map<RoadId, Road> _roads;

    std::unordered_map<JuncId, Junction> _junctions;

    std::unordered_map<SignId, std::unique_ptr<Signal>> _signals;

    std::unordered_map<ContId, std::unique_ptr<Controller>> _controllers;
  };

} // namespace road
} // namespace carla
