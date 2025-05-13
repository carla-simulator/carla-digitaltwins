// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once


#include "Carla/MsgPackAdaptors.h"
#include "Carla/Geom/BoundingBox.h"
#include "Carla/Geom/Location.h"
#include "Carla/Geom/Rotation.h"
#include "Carla/RPC/Color.h"

#include <Carla/disable-ue4-macros.h>
#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable:4583)
#pragma warning(disable:4582)
#include <boost/variant2/variant.hpp>
#pragma warning(pop)
#else
#include <boost/variant2/variant.hpp>
#endif
#include <Carla/enable-ue4-macros.h>
namespace carla {
namespace rpc {

  class DebugShape {
  public:

    struct Point {
      geom::Location location;
      float size;
      ;
    };

    struct HUDPoint {
      geom::Location location;
      float size;
      ;
    };

    struct Line {
      geom::Location begin;
      geom::Location end;
      float thickness;
      ;
    };

    struct HUDLine {
      geom::Location begin;
      geom::Location end;
      float thickness;
      ;
    };

    struct Arrow {
      Line line;
      float arrow_size;
      ;
    };

    struct HUDArrow {
      HUDLine line;
      float arrow_size;
      ;
    };

    struct Box {
      geom::BoundingBox box;
      geom::Rotation rotation;
      float thickness;
      ;
    };

    struct HUDBox {
      geom::BoundingBox box;
      geom::Rotation rotation;
      float thickness;
      ;
    };

    struct String {
      geom::Location location;
      std::string text;
      bool draw_shadow;
      ;
    };

    boost::variant2::variant<Point, Line, Arrow, Box, String, HUDPoint, HUDLine, HUDArrow, HUDBox> primitive;

    Color color = {255u, 0u, 0u};

    float life_time = -1.0f;

    bool persistent_lines = true;

    ;
  };

} // namespace rpc
} // namespace carla
