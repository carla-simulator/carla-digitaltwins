
set (PROJ_VERSION 9.6.0)

cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR ${WORKSPACE_PATH}/ThirdParty)
set (THIRD_PARTY_BUILD_DIR ${THIRD_PARTY_ROOT_DIR}/Build)
set (THIRD_PARTY_INSTALL_DIR ${THIRD_PARTY_ROOT_DIR}/Install)
file (MAKE_DIRECTORY ${THIRD_PARTY_ROOT_DIR})

set (DEPENDENCY_NAME PROJ)
set (PROJ_SOURCE_DIR ${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME})
set (PROJ_BUILD_DIR ${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME})
set (PROJ_INSTALL_DIR ${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME})

find_package (
  ${DEPENDENCY_NAME} ${PROJ_VERSION}
  EXACT
  QUIET
  NO_MODULE
)

if (NOT ${PROJ_FOUND})
    
  message (STATUS "Could not find ${DEPENDENCY_NAME}, bootstrapping...")

  if (NOT EXISTS ${THIRD_PARTY_ROOT_DIR}/proj.zip)
    set (
      PROJ_DOWNLOAD_URL
      https://download.osgeo.org/proj/proj-${PROJ_VERSION}.tar.gz
    )
    message (STATUS "Downloading ${DEPENDENCY_NAME} from ${PROJ_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${PROJ_DOWNLOAD_URL}
      ${THIRD_PARTY_ROOT_DIR}/proj.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${PROJ_SOURCE_DIR})
    message (STATUS "Extracting downloaded ${DEPENDENCY_NAME} archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${THIRD_PARTY_ROOT_DIR}/proj.zip
      DESTINATION ${THIRD_PARTY_ROOT_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${PROJ_SOURCE_DIR})
    message (STATUS "Renaming extracted ${DEPENDENCY_NAME} directories.")
    file (
      RENAME
        ${THIRD_PARTY_ROOT_DIR}/proj-${PROJ_VERSION}
        ${PROJ_SOURCE_DIR}
    )
  endif ()

  message (STATUS "Configuring ${DEPENDENCY_NAME}.")

  set (LIB_EXT .a)
  set (EXE_EXT)
  if (WIN32)
    set (LIB_EXT .lib)
    set (EXE_EXT .exe)
  endif ()

  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${PROJ_SOURCE_DIR}
        -B ${PROJ_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DBUILD_SHARED_LIBS=OFF
        -DCMAKE_IGNORE_PATH=${CMAKE_IGNORE_PATH}
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
        -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
        -DCMAKE_INSTALL_PREFIX=${PROJ_INSTALL_DIR}
        -DEXE_SQLITE3=${THIRD_PARTY_INSTALL_DIR}/SQLite3/bin/sqlite3${EXE_EXT}
        -DPROJ_OBJECT_LIBRARIES_POSITION_INDEPENDENT_CODE=ON
        -DINSTALL_LEGACY_CMAKE_FILES=OFF
        -DBUILD_FRAMEWORKS_AND_BUNDLE=OFF
        -DENABLE_TIFF=OFF
        -DENABLE_CURL=OFF
        -DBUILD_APPS=OFF
    RESULTS_VARIABLE
      PROJ_CONFIGURE_RESULT
  )
  
  if (PROJ_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, configure step failed.")
  endif ()

  message (STATUS "Building ${DEPENDENCY_NAME}.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${PROJ_BUILD_DIR}
    RESULTS_VARIABLE
      PROJ_BUILD_RESULT
  )
  
  if (PROJ_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, build step failed.")
  endif ()

  message (STATUS "Installing ${DEPENDENCY_NAME}.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${PROJ_BUILD_DIR}
        --prefix ${PROJ_INSTALL_DIR}
    RESULTS_VARIABLE
      PROJ_INSTALL_RESULT
  )
  
  if (PROJ_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, install step failed.")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH ${PROJ_INSTALL_DIR}/lib/cmake/proj)
  list (APPEND CMAKE_MODULE_PATH ${PROJ_INSTALL_DIR}/lib/cmake/proj)

  find_package (
    ${DEPENDENCY_NAME} ${PROJ_VERSION}
    EXACT
    CONFIG
    REQUIRED
  )

  set (PROJ_INCLUDE_DIR ${PROJ_INSTALL_DIR}/include CACHE FILEPATH "")
  set (PROJ_API_FILE ${PROJ_INSTALL_DIR}/include/proj.h CACHE FILEPATH "")
  set (PROJ_LIBRARY ${PROJ_LIBRARIES})

else ()

  message (STATUS "Found ${DEPENDENCY_NAME}. Skipping build...")

  if ("${PROJ_INCLUDE_DIR}" STREQUAL "")
    message (FATAL_ERROR "PROJ_INCLUDE_DIR is not set!")
  endif ()

  if ("${PROJ_API_FILE}" STREQUAL "")
    message (FATAL_ERROR "PROJ_API_FILE is not set!")
  endif ()

  if ("${PROJ_LIBRARY}" STREQUAL "")
    set (PROJ_LIBRARY ${PROJ_LIBRARIES})
  endif ()

  message (STATUS "PROJ_INCLUDE_DIR=${PROJ_INCLUDE_DIR}")
  message (STATUS "PROJ_API_FILE=${PROJ_API_FILE}")
  message (STATUS "PROJ_LIBRARY=${PROJ_LIBRARY}")

endif ()

add_compile_definitions (PROJ_DLL=)
