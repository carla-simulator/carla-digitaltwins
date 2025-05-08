cmake_minimum_required(VERSION 3.25)
cmake_policy (SET CMP0167 NEW)

project (CarlaDigitalTwinsTool)

set(
  BOOST_COMPONENTS
  asio
  iterator
  date_time
  geometry
  container
  variant2
  gil
)

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

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${BOOST_SOURCE_DIR}
        -B ${BOOST_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DBOOST_INSTALL_LAYOUT=system
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        # -DBOOST_ENABLE_PYTHON=ON
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

  list (
    APPEND
    CMAKE_PREFIX_PATH
    ${BOOST_INSTALL_DIR}
  )
  
  list (
    APPEND
    CMAKE_MODULE_PATH
    ${BOOST_INSTALL_DIR}
  )
endif ()

find_package (
  Boost ${BOOST_VERSION}
  EXACT
  REQUIRED
  COMPONENTS ${BOOST_COMPONENTS}
)

# file (
#   GENERATE
#   OUTPUT
#     Dependencies.txt
#   CONTENT
#     $<JOIN:$<TARGET_FILE:$<TARGET_PROPERTY:test_lib,LINK_LIBRARIES>>,\n>
# )
