// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Carla/OpenDrive/OpenDriveParser.h"

#include "Carla/Logging.h"
#include "Carla/OpenDrive/parser/ControllerParser.h"
#include "Carla/OpenDrive/parser/GeoReferenceParser.h"
#include "Carla/OpenDrive/parser/GeometryParser.h"
#include "Carla/OpenDrive/parser/JunctionParser.h"
#include "Carla/OpenDrive/parser/LaneParser.h"
#include "Carla/OpenDrive/parser/ObjectParser.h"
#include "Carla/OpenDrive/parser/ProfilesParser.h"
#include "Carla/OpenDrive/parser/RoadParser.h"
#include "Carla/OpenDrive/parser/SignalParser.h"
#include "Carla/OpenDrive/parser/TrafficGroupParser.h"
#include "Carla/Road/MapBuilder.h"

#include <Carla/pugixml/pugixml.hpp>

namespace carla {
namespace opendrive {

  boost::optional<road::Map> OpenDriveParser::Load(const std::string &opendrive) {
    pugi::xml_document xml;
    pugi::xml_parse_result parse_result = xml.load_string(opendrive.c_str());

    if (parse_result == false) {
      log_error("unable to parse the OpenDRIVE XML string \"", opendrive, "\".");
      return {};
    }

    carla::road::MapBuilder map_builder;

    parser::GeoReferenceParser::Parse(xml, map_builder);
    parser::RoadParser::Parse(xml, map_builder);
    parser::JunctionParser::Parse(xml, map_builder);
    parser::GeometryParser::Parse(xml, map_builder);
    parser::LaneParser::Parse(xml, map_builder);
    parser::ProfilesParser::Parse(xml, map_builder);
    parser::TrafficGroupParser::Parse(xml, map_builder);
    parser::SignalParser::Parse(xml, map_builder);
    parser::ObjectParser::Parse(xml, map_builder);
    parser::ControllerParser::Parse(xml, map_builder);

    return map_builder.Build();
  }

} // namespace opendrive
} // namespace carla
