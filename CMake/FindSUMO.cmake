
find_package (
  SUMO
  QUIET
  NO_MODULE
)

if (NOT ${Boost_FOUND})

  set (
    DEPENDENCY_NAME
    Boost
  )

  set (
    DEPENDENCY_URL
    https://github.com/boostorg/boost/releases/download/${BOOST_TAG}/${BOOST_TAG}.zip
  )

  set (
    DEPENDENCY_TAG
    boost-${BOOST_VERSION}
  )

  include (${CMAKE_CURRENT_LIST_DIR}/Util.cmake)

  find_package (
    Boost ${BOOST_VERSION}
    EXACT
    REQUIRED
    COMPONENTS ${BOOST_COMPONENTS}
    CONFIG
  )

endif ()

cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR ${WORKSPACE_PATH}/ThirdParty)
set (THIRD_PARTY_BUILD_DIR ${THIRD_PARTY_ROOT_DIR}/Build)
set (THIRD_PARTY_INSTALL_DIR ${THIRD_PARTY_ROOT_DIR}/Install)
file (MAKE_DIRECTORY ${THIRD_PARTY_ROOT_DIR})

set (DEPENDENCY_NAME SUMO)
set (SUMO_SOURCE_DIR ${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME})
set (SUMO_BUILD_DIR ${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME})
set (SUMO_INSTALL_DIR ${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME})

if (NOT ${SUMO_FOUND})
    
  message (STATUS "Could not find SUMO, bootstrapping...")

  if (NOT EXISTS ${THIRD_PARTY_ROOT_DIR}/sumo.zip)
    set (
      SUMO_DOWNLOAD_URL
      https://github.com/carla-simulator/sumo/archive/${SUMO_COMMIT}.zip
    )
    message (STATUS "Downloading SUMO from ${SUMO_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${SUMO_DOWNLOAD_URL}
      ${THIRD_PARTY_ROOT_DIR}/sumo.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${SUMO_SOURCE_DIR})
    message (STATUS "Extracting downloaded SUMO archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${THIRD_PARTY_ROOT_DIR}/sumo.zip
      DESTINATION ${THIRD_PARTY_ROOT_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${SUMO_SOURCE_DIR})
    message (STATUS "Renaming extracted SUMO directories.")
    file (
      RENAME
        ${THIRD_PARTY_ROOT_DIR}/sumo-${SUMO_COMMIT}
        ${SUMO_SOURCE_DIR}
    )
  endif ()

  message (STATUS "Configuring SUMO.")

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${SUMO_SOURCE_DIR}
        -B ${SUMO_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DBUILD_SHARED_LIBS=${BUILD_SHARED_LIBS}
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
        -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
    RESULTS_VARIABLE
      SUMO_CONFIGURE_RESULT
  )
  
  if (SUMO_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, configure step failed.")
  endif ()

  message (STATUS "Building SUMO.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${SUMO_BUILD_DIR}
    RESULTS_VARIABLE
      SUMO_BUILD_RESULT
  )
  
  if (SUMO_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, build step failed.")
  endif ()

  message (STATUS "Installing SUMO.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${SUMO_BUILD_DIR}
        --prefix ${SUMO_INSTALL_DIR}
    RESULTS_VARIABLE
      SUMO_INSTALL_RESULT
  )
  
  if (SUMO_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, install step failed.")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH ${SUMO_INSTALL_DIR})
  list (APPEND CMAKE_MODULE_PATH ${SUMO_INSTALL_DIR})

endif ()
