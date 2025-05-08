cmake_minimum_required(VERSION 3.25)

project (CarlaDigitalTwinsTool)

# Boost config
set (BOOST_VERSION 1.84.0)
set (BOOST_TAG boost-${BOOST_VERSION})

set(
  BOOST_COMPONENTS
  asio
  iterator
  python
  date_time
  geometry
  container
  variant2
  gil
)

# Carpetas locales
set (BOOST_BASE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/boost)
set (BOOST_DOWNLOAD_DIR ${BOOST_BASE_DIR}/downloads)
set (BOOST_SOURCE_DIR ${BOOST_BASE_DIR}/src)
set (BOOST_INSTALL_DIR ${BOOST_BASE_DIR}/install)
set (BOOST_BUILD_DIR ${BOOST_BASE_DIR}/build)

find_package (
  Boost ${BOOST_VERSION}
  EXACT
  COMPONENTS ${BOOST_COMPONENTS}
)

if (NOT ${Boost_FOUND})

  execute_process (
    COMMAND
      git
        clone
        -C ${BOOST_BASE_DIR}
        -b ${BOOST_TAG}
        --recursive
        https://github.com/boostorg/boost.git
        boost
    COMMAND
      ${CMAKE_COMMAND}
        -S ${BOOST_SOURCE_DIR}
        -B ${BOOST_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DBOOST_INCLUDE_LIBRARIES="${BOOST_COMPONENTS}"
        -DBOOST_EXCLUDE_LIBRARIES="${BOOST_EXCLUDED_PROJECTS}"
        -DBOOST_ENABLE_PYTHON=OFF
        -DBOOST_ENABLE_MPI=OFF
        -DBOOST_LOCALE_WITH_ICU=OFF
        -DBOOST_LOCALE_WITH_ICONV=OFF
        -DBOOST_GIL_BUILD_EXAMPLES=OFF
        -DBOOST_GIL_BUILD_HEADER_TESTS=OFF
    COMMAND
      ${CMAKE_COMMAND}
        --build ${BOOST_BUILD_DIR}
    COMMAND
      ${CMAKE_COMMAND}
        --install ${BOOST_BUILD_DIR}
        --prefix ${BOOST_INSTALL_DIR}
  )

  list (
    APPEND
    CMAKE_MODULE_PATH
    ${BOOST_INSTALL_DIR}
  )

  find_package (
    Boost ${BOOST_VERSION}
    EXACT
    MODULE
    REQUIRED
    COMPONENTS ${BOOST_COMPONENTS}
  )
endif ()
