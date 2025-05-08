cmake_minimum_required(VERSION 3.25)
cmake_policy (SET CMP0167 NEW)

project (CarlaDigitalTwinsTool)

set (BOOST_COMPONENTS date_time) # Set at CMake configure time.

# Boost config
set (BOOST_VERSION 1.84.0)
set (BOOST_TAG boost-${BOOST_VERSION})

# Carpetas locales
set (BOOST_BASE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
file (MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
set (BOOST_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/boost)
set (BOOST_BUILD_DIR ${CMAKE_CURRENT_BINARY_DIR}/third_party/boost-build)
set (BOOST_INSTALL_DIR ${CMAKE_CURRENT_BINARY_DIR}/third_party/boost-install)

set (Boost_VERBOSE ON)
set (Boost_DEBUG ON)
set (Boost_NO_BOOST_CMAKE ON)

find_package (
  Boost ${BOOST_VERSION}
  EXACT
  QUIET
  COMPONENTS ${BOOST_COMPONENTS}
)

if (NOT ${Boost_FOUND})

  message (STATUS "Could not find boost, bootstrapping...")

  file (MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/third_party)

  if (NOT EXISTS ${${BOOST_BASE_DIR}/boost.zip})
    file (
      DOWNLOAD
      https://github.com/boostorg/boost/releases/download/${BOOST_TAG}/${BOOST_TAG}.zip
      ${BOOST_BASE_DIR}/boost.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_BASE_DIR})
    file (
      ARCHIVE_EXTRACT
      INPUT ${BOOST_BASE_DIR}/boost.zip
      DESTINATION ${BOOST_BASE_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_SOURCE_DIR})
    file (
      RENAME
        ${BOOST_BASE_DIR}/${BOOST_TAG}
        ${BOOST_SOURCE_DIR}
    )
  endif ()

  set (BUILD_BOOST_PYTHON OFF)
  list (FIND ${BOOST_COMPONENTS} python PYTHON_COMPONENT_INDEX)
  if (${PYTHON_COMPONENT_INDEX} STREQUAL -1)
    set (BUILD_BOOST_PYTHON ON)
  endif ()

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${BOOST_SOURCE_DIR}
        -B ${BOOST_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DBOOST_INSTALL_LAYOUT=system
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DBOOST_ENABLE_PYTHON=${BUILD_BOOST_PYTHON}
        -DBOOST_GIL_BUILD_EXAMPLES=OFF
        -DBOOST_GIL_BUILD_HEADER_TESTS=OFF
    RESULTS_VARIABLE
      BOOST_SETUP_RESULT
  )
  
  if (BOOST_SETUP_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, configure step failed.")
  endif ()

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${BOOST_BUILD_DIR}
    RESULTS_VARIABLE
      BOOST_SETUP_RESULT
  )
  
  if (BOOST_SETUP_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, build step failed.")
  endif ()

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${BOOST_BUILD_DIR}
        --prefix ${BOOST_INSTALL_DIR}
    RESULTS_VARIABLE
      BOOST_SETUP_RESULT
  )
  
  if (BOOST_SETUP_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, install step failed.")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH ${BOOST_INSTALL_DIR})
  list (APPEND CMAKE_MODULE_PATH ${BOOST_INSTALL_DIR})
endif ()

find_package (
  Boost ${BOOST_VERSION}
  EXACT
  REQUIRED
  COMPONENTS ${BOOST_COMPONENTS}
)

macro (generate_target_properties_to_file FILENAME PROPERTY TARGETS)
  set (FILE_BODY)
  foreach (T ${TARGETS})
    set (FILE_BODY "${CONTENT}\n$<JOIN:$<TARGET_PROPERTY:Boost::${T},${PROPERTY}>,\n>")
  endforeach ()
  file (
    GENERATE
    OUTPUT
      ${FILENAME}
    CONTENT
      ${FILE_BODY}
  )
endmacro ()

generate_target_properties_to_file (
  ${CMAKE_BINARY_DIR}/LinkLibraries.txt
  LINK_LIBRARIES
  ${BOOST_COMPONENTS}
)

generate_target_properties_to_file (
  ${CMAKE_BINARY_DIR}/IncludeDirectories.txt
  INCLUDE_DIRECTORIES
  ${BOOST_COMPONENTS}
)

generate_target_properties_to_file (
  ${CMAKE_BINARY_DIR}/PublicDefinitions.txt
  INTERFACE_COMPILE_DEFINITIONS
  ${BOOST_COMPONENTS}
)
