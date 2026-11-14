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
    "${CMAKE_BINARY_DIR}/LinkLibraries.txt.in"
    "${PATH}\n"
  )
endmacro ()

macro (DumpIncludePath PATH)
  file (
    APPEND
    "${CMAKE_BINARY_DIR}/IncludeDirectories.txt.in"
    "${PATH}\n"
  )
endmacro ()

macro (DumpPublicDefinition DEFINITION)
  file (
    APPEND
    "${CMAKE_BINARY_DIR}/PublicDefinitions.txt.in"
    "${DEFINITION}\n"
  )
endmacro ()

function ( DumpDependencyTargetInternal DEPENDENCY_NAME VISITED_LIST )
  list (FIND VISITED_LIST "${DEPENDENCY_NAME}" ALREADY_VISITED)
  if (NOT ALREADY_VISITED EQUAL -1)
    return ()
  endif ()
  list (APPEND VISITED_LIST "${DEPENDENCY_NAME}")

  if (NOT TARGET "${DEPENDENCY_NAME}")
    if (EXISTS "${DEPENDENCY_NAME}")
      DumpLibraryPath("${DEPENDENCY_NAME}")
    endif ()
    return ()
  endif ()

  get_target_property (DEPENDENCY_TYPE "${DEPENDENCY_NAME}" TYPE)
  if ("${DEPENDENCY_TYPE}" STREQUAL "SHARED_LIBRARY")
    DumpLibraryPath("$<TARGET_LINKER_FILE:${DEPENDENCY_NAME}>")
    if (WIN32)
      DumpLibraryPath("$<TARGET_FILE:${DEPENDENCY_NAME}>")
    endif ()
  elseif (NOT ("${DEPENDENCY_TYPE}" STREQUAL "INTERFACE_LIBRARY"))
    DumpLibraryPath("$<TARGET_FILE:${DEPENDENCY_NAME}>")
  endif ()

  get_target_property (DEPENDENCY_INCLUDE_DIRS "${DEPENDENCY_NAME}" INTERFACE_INCLUDE_DIRECTORIES)
  if (DEPENDENCY_INCLUDE_DIRS)
    DumpIncludePath("$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_INCLUDE_DIRECTORIES>,\n>")
  endif ()

  get_target_property (DEPENDENCY_COMPILE_DEFS "${DEPENDENCY_NAME}" INTERFACE_COMPILE_DEFINITIONS)
  if (DEPENDENCY_COMPILE_DEFS)
    DumpPublicDefinition("$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_COMPILE_DEFINITIONS>,\n>")
  endif ()

  get_target_property (DEPENDENCY_INTERFACE_LINK_LIBRARIES "${DEPENDENCY_NAME}" INTERFACE_LINK_LIBRARIES)
  if (DEPENDENCY_INTERFACE_LINK_LIBRARIES)
    foreach (DEPENDENCY_INTERFACE_LINK_LIBRARY ${DEPENDENCY_INTERFACE_LINK_LIBRARIES})
      string (REGEX REPLACE "^\\$<LINK_ONLY:(.*)>$" "\\1" CLEAN_LINK_ITEM "${DEPENDENCY_INTERFACE_LINK_LIBRARY}")
      if (TARGET "${CLEAN_LINK_ITEM}" OR EXISTS "${CLEAN_LINK_ITEM}")
        DumpDependencyTargetInternal("${CLEAN_LINK_ITEM}" "${VISITED_LIST}")
      endif ()
    endforeach ()
  endif ()
endfunction ()

macro (DumpDependencyInfo DEPENDENCY_NAME)
  message (STATUS "Retrieving dependency info for ${DEPENDENCY_NAME}.")
  DumpDependencyTargetInternal("${DEPENDENCY_NAME}" "")
endmacro ()

macro (GenerateFinalDumpFiles)
  foreach (DUMP_STEM LinkLibraries IncludeDirectories PublicDefinitions)
    if (NOT EXISTS "${CMAKE_BINARY_DIR}/${DUMP_STEM}.txt.in")
      file (WRITE "${CMAKE_BINARY_DIR}/${DUMP_STEM}.txt.in" "")
    endif ()
    file (
      GENERATE
      OUTPUT
        "${CMAKE_BINARY_DIR}/${DUMP_STEM}.txt"
      INPUT
        "${CMAKE_BINARY_DIR}/${DUMP_STEM}.txt.in"
    )
  endforeach ()
endmacro ()

find_package (
  Python3
  REQUIRED
)

# -- Temporary check for numpy 2 presence, which breaks our current version of Boost.Python.
execute_process (
  COMMAND
    "${Python3_EXECUTABLE}"
      -c
      "import numpy; print(numpy.__version__)"
  OUTPUT_STRIP_TRAILING_WHITESPACE
  OUTPUT_VARIABLE
    NUMPY_VERSION_STRING
  RESULT_VARIABLE
    NUMPY_VERSION_QUERY_RESULT
)
if (NUMPY_VERSION_QUERY_RESULT)
  message (WARNING "Failed to query numpy version.")
else ()
  message (STATUS "Found Numpy ${NUMPY_VERSION_STRING} for Python ${Python3_VERSION} (${Python3_EXECUTABLE}).")
  if ("${NUMPY_VERSION_STRING}" VERSION_GREATER_EQUAL "2.0.0")
    message (FATAL_ERROR "Unsupported Numpy version (${NUMPY_VERSION_STRING}), please downgrade to a version prior to numpy 2.")
  endif ()
endif ()
# --

set (
  BOOST_COMPONENTS
  asio
  iterator
  date_time
  geometry
  container
  variant2
  gil
  filesystem
)

message (STATUS "Requesting boost components:")
foreach (COMPONENT ${BOOST_COMPONENTS})
  message (STATUS " - ${COMPONENT}")
endforeach ()

list (APPEND CMAKE_PREFIX_PATH "${CMAKE_CURRENT_LIST_DIR}/CMake")
list (APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_LIST_DIR}/CMake")

include (CMake/UEPlugins/CarlaMeshGeneration.cmake)
include (CMake/UEPlugins/StreetMap.cmake)
include (CMake/UEContent.cmake)

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

set (XERCESC_VERSION 3.3.0)
message (STATUS "Looking for XercesC")
find_package (
  XercesC ${XERCESC_VERSION}
  EXACT
  REQUIRED
)

set (PROJ_VERSION 9.6.0)
message (STATUS "Looking for PROJ")
find_package (
  PROJ ${PROJ_VERSION}
  EXACT
  REQUIRED
)

set (SUMO_BRANCH carla_digitaltwins)
message (STATUS "Looking for SUMO")
find_package (
  SUMO
  REQUIRED
)

# Dump dependency information for consumption by the UE Plugin:

file (REMOVE "${CMAKE_BINARY_DIR}/LinkLibraries.txt.in")
file (REMOVE "${CMAKE_BINARY_DIR}/LinkLibraries.txt")
file (REMOVE "${CMAKE_BINARY_DIR}/IncludeDirectories.txt.in")
file (REMOVE "${CMAKE_BINARY_DIR}/IncludeDirectories.txt")
file (REMOVE "${CMAKE_BINARY_DIR}/PublicDefinitions.txt.in")
file (REMOVE "${CMAKE_BINARY_DIR}/PublicDefinitions.txt")

foreach (BOOST_COMPONENT ${BOOST_COMPONENTS})
  DumpDependencyInfo(Boost::${BOOST_COMPONENT})
endforeach ()

if (TARGET SQLite::SQLite3)
  DumpDependencyInfo(SQLite::SQLite3)
else ()
  foreach (SQLITE_INC ${SQLite3_INCLUDE_DIRS})
    DumpIncludePath("${SQLITE_INC}")
  endforeach ()
  foreach (SQLITE_LIB ${SQLite3_LIBRARIES})
    DumpDependencyInfo("${SQLITE_LIB}")
  endforeach ()
endif ()

DumpDependencyInfo(XercesC::XercesC)

if (TARGET PROJ::proj)
  DumpDependencyInfo(PROJ::proj)
else ()
  if (PROJ_INCLUDE_DIR)
    DumpIncludePath("${PROJ_INCLUDE_DIR}")
  endif ()
  foreach (PROJ_LIB ${PROJ_LIBRARIES})
    DumpDependencyInfo("${PROJ_LIB}")
  endforeach ()
endif ()
DumpPublicDefinition("PROJ_DLL=")

if (SUMO_INSTALL_DIR)
  # Find OSM2ODR static/import library first, falling back to any shared library match:
  file (
    GLOB_RECURSE
    OSM2ODR_CANDIDATES
    FOLLOW_SYMLINKS
    LIST_DIRECTORIES FALSE
    "${SUMO_INSTALL_DIR}/lib/*osm2odr*${LIB_EXT}"
  )
  if (NOT OSM2ODR_CANDIDATES)
    file (
      GLOB_RECURSE
      OSM2ODR_CANDIDATES
      FOLLOW_SYMLINKS
      LIST_DIRECTORIES FALSE
      "${SUMO_INSTALL_DIR}/lib/*osm2odr.*"
    )
  endif ()
  if (NOT OSM2ODR_CANDIDATES)
    message (FATAL_ERROR "Could not find OSM2ODR.")
  endif ()
  list (GET OSM2ODR_CANDIDATES 0 OSM2ODR_PATH)
  message (STATUS "Found OSM2ODR at \"${OSM2ODR_PATH}\".")
  DumpIncludePath("${SUMO_INSTALL_DIR}/include")
  DumpLibraryPath("${OSM2ODR_PATH}")
else ()
  message (FATAL_ERROR "Could not find SUMO install path.")
endif ()
GenerateFinalDumpFiles()
