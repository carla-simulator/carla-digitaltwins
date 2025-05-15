

set (BOOST_VERSION 1.84.0)
find_package (
  Boost ${BOOST_VERSION}
  EXACT
  QUIET
  NO_MODULE
  COMPONENTS ${BOOST_COMPONENTS}
)

cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR ${WORKSPACE_PATH}/ThirdParty)
set (THIRD_PARTY_BUILD_DIR ${THIRD_PARTY_ROOT_DIR}/Build)
set (THIRD_PARTY_INSTALL_DIR ${THIRD_PARTY_ROOT_DIR}/Install)
file (MAKE_DIRECTORY ${THIRD_PARTY_ROOT_DIR})

set (DEPENDENCY_NAME Boost)
set (BOOST_SOURCE_DIR ${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME})
set (BOOST_BUILD_DIR ${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME})
set (BOOST_INSTALL_DIR ${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME})

if (NOT ${Boost_FOUND})

  message (STATUS "Could not find boost, bootstrapping...")

  set (BOOST_TAG boost-${BOOST_VERSION})

  if (NOT EXISTS ${THIRD_PARTY_ROOT_DIR}/boost.zip)
    set (
      BOOST_DOWNLOAD_URL
      https://github.com/boostorg/boost/releases/download/${BOOST_TAG}/${BOOST_TAG}.zip
    )
    message (STATUS "Downloading boost from ${BOOST_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${BOOST_DOWNLOAD_URL}
      ${THIRD_PARTY_ROOT_DIR}/boost.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_SOURCE_DIR})
    message (STATUS "Extracting downloaded boost archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${THIRD_PARTY_ROOT_DIR}/boost.zip
      DESTINATION ${THIRD_PARTY_ROOT_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_SOURCE_DIR})
    message (STATUS "Renaming extracted boost directories.")
    file (
      RENAME
        ${THIRD_PARTY_ROOT_DIR}/${BOOST_TAG}
        ${BOOST_SOURCE_DIR}
    )
  endif ()

  set (BUILD_BOOST_PYTHON OFF)
  list (FIND "${BOOST_COMPONENTS}" python PYTHON_COMPONENT_INDEX)
  if (${PYTHON_COMPONENT_INDEX} STREQUAL -1)
    set (BUILD_BOOST_PYTHON ON)
  endif ()

  message (STATUS "Configuring boost.")

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${BOOST_SOURCE_DIR}
        -B ${BOOST_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
        -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DBOOST_INSTALL_LAYOUT=system
        -DBOOST_ENABLE_PYTHON=${BUILD_BOOST_PYTHON}
        -DBOOST_FILESYSTEM_DISABLE_STATX=ON
        -DBOOST_FILESYSTEM_DISABLE_GETRANDOM=ON
        -DBOOST_GIL_BUILD_EXAMPLES=OFF
        -DBOOST_GIL_BUILD_HEADER_TESTS=OFF
    RESULTS_VARIABLE
      BOOST_CONFIGURE_RESULT
  )
  
  if (BOOST_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, configure step failed.")
  endif ()

  message (STATUS "Building boost.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${BOOST_BUILD_DIR}
    RESULTS_VARIABLE
      BOOST_BUILD_RESULT
  )
  
  if (BOOST_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, build step failed.")
  endif ()

  message (STATUS "Installing boost.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${BOOST_BUILD_DIR}
        --prefix ${BOOST_INSTALL_DIR}
    RESULTS_VARIABLE
      BOOST_INSTALL_RESULT
  )
  
  if (BOOST_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap Boost, install step failed.")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH ${BOOST_INSTALL_DIR})
  list (APPEND CMAKE_MODULE_PATH ${BOOST_INSTALL_DIR})

  find_package (
    Boost ${BOOST_VERSION}
    EXACT
    REQUIRED
    COMPONENTS ${BOOST_COMPONENTS}
    CONFIG
  )

endif ()
