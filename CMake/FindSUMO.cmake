
cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR "${WORKSPACE_PATH}/ThirdParty")
set (THIRD_PARTY_BUILD_DIR "${THIRD_PARTY_ROOT_DIR}/Build")
set (THIRD_PARTY_INSTALL_DIR "${THIRD_PARTY_ROOT_DIR}/Install")
file (MAKE_DIRECTORY "${THIRD_PARTY_ROOT_DIR}")

set (DEPENDENCY_NAME SUMO)
if (NOT DEFINED SUMO_BRANCH OR "${SUMO_BRANCH}" STREQUAL "")
  set (SUMO_BRANCH carla_digitaltwins)
endif ()
string (REPLACE "/" "-" SUMO_BRANCH_CLEAN "${SUMO_BRANCH}")
set (SUMO_SOURCE_DIR "${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME}")
set (SUMO_BUILD_DIR "${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME}")
set (SUMO_INSTALL_DIR "${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME}")

find_package (
  SUMO
  QUIET
  NO_MODULE
)

if (NOT SUMO_FOUND AND IS_DIRECTORY "${SUMO_INSTALL_DIR}/include" AND IS_DIRECTORY "${SUMO_INSTALL_DIR}/lib")
  file (
    GLOB_RECURSE
    SUMO_EXISTING_OSM2ODR
    FOLLOW_SYMLINKS
    LIST_DIRECTORIES FALSE
    "${SUMO_INSTALL_DIR}/lib/*osm2odr*"
  )
  if (SUMO_EXISTING_OSM2ODR)
    set (SUMO_FOUND TRUE)
  endif ()
endif ()

if (NOT SUMO_FOUND)

  message (STATUS "Could not find SUMO, bootstrapping...")

  set (SUMO_ARCHIVE "${THIRD_PARTY_ROOT_DIR}/sumo-${SUMO_BRANCH_CLEAN}.zip")
  if (NOT EXISTS "${SUMO_ARCHIVE}")
    set (
      SUMO_DOWNLOAD_URL
      https://github.com/carla-simulator/sumo/archive/refs/heads/${SUMO_BRANCH}.zip
    )
    message (STATUS "Downloading SUMO from ${SUMO_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      "${SUMO_DOWNLOAD_URL}"
      "${SUMO_ARCHIVE}"
      STATUS SUMO_DL_STATUS
      TLS_VERIFY ON
    )
    list (GET SUMO_DL_STATUS 0 SUMO_DL_CODE)
    if (NOT SUMO_DL_CODE EQUAL 0)
      file (REMOVE "${SUMO_ARCHIVE}")
      message (FATAL_ERROR "Failed to download SUMO from ${SUMO_DOWNLOAD_URL}: ${SUMO_DL_STATUS}")
    endif ()
  endif ()

  if (NOT IS_DIRECTORY "${SUMO_SOURCE_DIR}")
    message (STATUS "Extracting downloaded SUMO archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT "${SUMO_ARCHIVE}"
      DESTINATION "${THIRD_PARTY_ROOT_DIR}"
    )
  endif ()

  if (NOT IS_DIRECTORY "${SUMO_SOURCE_DIR}")
    message (STATUS "Renaming extracted SUMO directories.")
    file (
      RENAME
        "${THIRD_PARTY_ROOT_DIR}/sumo-${SUMO_BRANCH_CLEAN}"
        "${SUMO_SOURCE_DIR}"
    )
  endif ()

  message (STATUS "Configuring SUMO.")

  find_package (XercesC REQUIRED)
  find_package (PROJ REQUIRED)

  execute_process (
    COMMAND
      "${CMAKE_COMMAND}"
        -S "${SUMO_SOURCE_DIR}"
        -B "${SUMO_BUILD_DIR}"
        -G "${CMAKE_GENERATOR}"
        "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}"
        "-DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}"
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DBUILD_SHARED_LIBS=OFF
        "-DCMAKE_IGNORE_PATH=${CMAKE_IGNORE_PATH}"
        "-DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}"
        "-DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}"
        "-DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}"
        "-DCMAKE_INSTALL_PREFIX=${SUMO_INSTALL_DIR}"
        "-DCMAKE_POLICY_VERSION_MINIMUM=${CMAKE_POLICY_VERSION_MINIMUM}"
        -DCHECK_OPTIONAL_LIBS=OFF
        -DPROFILING=OFF
        -DPPROF=OFF
        -DCOVERAGE=OFF
        -DSUMO_UTILS=OFF
        -DFMI=OFF
        -DNETEDIT=OFF
        -DENABLE_PYTHON_BINDINGS=OFF
        -DENABLE_JAVA_BINDINGS=OFF
        -DENABLE_CS_BINDINGS=OFF
        -DCCACHE_SUPPORT=OFF
        -DTCMALLOC=OFF
        -DVERBOSE_SUB=OFF
        -DENABLE_GTEST=OFF
    RESULT_VARIABLE
      SUMO_CONFIGURE_RESULT
  )

  if (SUMO_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, configure step failed (exit code ${SUMO_CONFIGURE_RESULT}).")
  endif ()

  message (STATUS "Building SUMO.")
  execute_process (
    COMMAND
      "${CMAKE_COMMAND}"
        --build "${SUMO_BUILD_DIR}"
    RESULT_VARIABLE
      SUMO_BUILD_RESULT
  )

  if (SUMO_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, build step failed (exit code ${SUMO_BUILD_RESULT}).")
  endif ()

  message (STATUS "Installing SUMO.")
  execute_process (
    COMMAND
      "${CMAKE_COMMAND}"
        --install "${SUMO_BUILD_DIR}"
        --prefix "${SUMO_INSTALL_DIR}"
    RESULT_VARIABLE
      SUMO_INSTALL_RESULT
  )

  if (SUMO_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap SUMO, install step failed (exit code ${SUMO_INSTALL_RESULT}).")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH "${SUMO_INSTALL_DIR}")
  list (APPEND CMAKE_MODULE_PATH "${SUMO_INSTALL_DIR}")
  set (SUMO_FOUND TRUE)

else ()

  message (STATUS "Found ${DEPENDENCY_NAME}. Skipping build...")

endif ()

add_compile_definitions (PROJ_DLL=)
