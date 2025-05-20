cmake_minimum_required (VERSION 3.25)
project (CarlaDigitalTwinsTool)

set (LIB_EXT .a)
if (WIN32)
  set (LIB_EXT .lib)
endif ()

set (CMAKE_POSITION_INDEPENDENT_CODE ON)
set (THREADS_PREFER_PTHREAD_FLAG ON)

macro (DumpLibraryPath PATH)
  file (
    APPEND
    ${CMAKE_BINARY_DIR}/LinkLibraries.txt.in
    "${PATH}\n"
  )
endmacro ()

macro (DumpIncludePath PATH)
  file (
    APPEND
    ${CMAKE_BINARY_DIR}/IncludeDirectories.txt.in
    "${PATH}\n"
  )
endmacro ()

macro (DumpPublicDefinition DEFINITION)
  file (
    APPEND
    ${CMAKE_BINARY_DIR}/PublicDefinitions.txt.in
    "${DEFINITION}\n"
  )
endmacro ()

macro (DumpDependencyInfo DEPENDENCY_NAME)
  message (STATUS "Retrieving dependency info for ${DEPENDENCY_NAME}.")

  get_target_property (DEPENDENCY_TYPE ${DEPENDENCY_NAME} TYPE)
  if (NOT ("${DEPENDENCY_TYPE}" STREQUAL "INTERFACE_LIBRARY"))
    get_target_property (COMPONENT_LOCATION ${DEPENDENCY_NAME} LOCATION)
    DumpLibraryPath("${COMPONENT_LOCATION}\n")
  else ()
    get_target_property (DEPENDENCY_INTERFACE_LINK_LIBRARIES ${DEPENDENCY_NAME} INTERFACE_LINK_LIBRARIES)
    foreach (DEPENDENCY_INTERFACE_LINK_LIBRARY ${DEPENDENCY_INTERFACE_LINK_LIBRARIES})
      DumpLibraryPath("$<TARGET_PROPERTY:${DEPENDENCY_INTERFACE_LINK_LIBRARY},COMPONENT_LOCATION>\n")
    endforeach ()
  endif ()

  DumpIncludePath("$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_INCLUDE_DIRECTORIES>,\n>\n")
  DumpPublicDefinition("$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_COMPILE_DEFINITIONS>,\n>\n")
endmacro ()

macro (GenerateFinalDumpFiles)
  file (
    GENERATE
    OUTPUT
      ${CMAKE_BINARY_DIR}/LinkLibraries.txt
    INPUT
      ${CMAKE_BINARY_DIR}/LinkLibraries.txt.in
  )

  file (
    GENERATE
    OUTPUT
      ${CMAKE_BINARY_DIR}/IncludeDirectories.txt
    INPUT
      ${CMAKE_BINARY_DIR}/IncludeDirectories.txt.in
  )

  file (
    GENERATE
    OUTPUT
      ${CMAKE_BINARY_DIR}/PublicDefinitions.txt
    INPUT
      ${CMAKE_BINARY_DIR}/PublicDefinitions.txt.in
  )
endmacro ()



message (STATUS "Requesting boost components:")
foreach (COMPONENT ${BOOST_COMPONENTS})
  message (STATUS " - ${COMPONENT}")
endforeach ()

list (APPEND CMAKE_PREFIX_PATH ${CMAKE_CURRENT_LIST_DIR}/CMake)
list (APPEND CMAKE_MODULE_PATH ${CMAKE_CURRENT_LIST_DIR}/CMake)

set (BOOST_VERSION 1.84.0)
message (STATUS "Looking for Boost")
find_package (
  Boost ${BOOST_VERSION}
  EXACT
  REQUIRED
  COMPONENTS ${BOOST_COMPONENTS}
)

message (STATUS "Looking for SQLite3")
find_package (
  SQLite3
  REQUIRED
)

set (XERCESC_VERSION 3.2.3)
message (STATUS "Looking for XercesC")
find_package (
  XercesC ${XERCESC_VERSION}
  EXACT
  REQUIRED
)

set (PROJ_VERSION 7.2.0)
message (STATUS "Looking for PROJ")
find_package (
  PROJ ${PROJ_VERSION}
  EXACT
  REQUIRED
)

# This is incorrect:
set (SUMO_COMMIT 1da3c07e39f3e2e0d97f8f709a7255a0df8c6200)
message (STATUS "Looking for SUMO")
find_package (
  SUMO
  REQUIRED
)

# Dump dependency information for consumption by the UE Plugin:

file (REMOVE ${CMAKE_BINARY_DIR}/LinkLibraries.txt.in)
file (REMOVE ${CMAKE_BINARY_DIR}/LinkLibraries.txt)
file (REMOVE ${CMAKE_BINARY_DIR}/IncludeDirectories.txt.in)
file (REMOVE ${CMAKE_BINARY_DIR}/IncludeDirectories.txt)
file (REMOVE ${CMAKE_BINARY_DIR}/PublicDefinitions.txt.in)
file (REMOVE ${CMAKE_BINARY_DIR}/PublicDefinitions.txt)

foreach (BOOST_COMPONENT ${BOOST_COMPONENTS})
  DumpDependencyInfo(Boost::${BOOST_COMPONENT})
endforeach ()

DumpIncludePath(${THIRD_PARTY_INSTALL_DIR}/SQLite3/include)
DumpLibraryPath(${THIRD_PARTY_INSTALL_DIR}/SQLite3/lib/libsqlite3${LIB_EXT})

DumpDependencyInfo(XercesC::XercesC)

foreach (PROJ_LIB ${PROJ_LIBRARIES})
  DumpDependencyInfo(${PROJ_LIB})
endforeach ()

if (SUMO_INSTALL_DIR)
  # Find OSM2ODR:
  file (
    GLOB_RECURSE
    OSM2ODR_CANDIDATES
    FOLLOW_SYMLINKS
    LIST_DIRECTORIES FALSE
    ${SUMO_INSTALL_DIR}/lib/*osm2odr.*
  )
  if (NOT OSM2ODR_CANDIDATES)
    message (FATAL_ERROR "Could not find OSM2ODR.")
  endif ()
  list (GET OSM2ODR_CANDIDATES 0 OSM2ODR_PATH)
  message (STATUS "Found OSM2ODR at \"${OSM2ODR_PATH}\".")
  DumpIncludePath(${SUMO_INSTALL_DIR}/include)
  DumpLibraryPath(${OSM2ODR_PATH})
else ()
  message (FATAL_ERROR "Could not find SUMO install path.")
endif ()
GenerateFinalDumpFiles()
