// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Carla/Geom/Mesh.h"
#include <memory>

namespace carla {
namespace geom {

  class Simplification {
  public:

    Simplification() = default;

    Simplification(float simplificationrate)
      : simplification_percentage(simplificationrate)
      {}

    float simplification_percentage;

    void Simplificate(const std::unique_ptr<geom::Mesh>& pmesh);
  };

} // namespace geom
} // namespace carla
