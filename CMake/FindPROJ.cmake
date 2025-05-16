
set (PROJ_VERSION 7.2.1)

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
  ${DEPENDENCY_NAME}
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
        -DBUILD_SHARED_LIBS=${BUILD_SHARED_LIBS}
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
        -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
        -DSQLITE3_INCLUDE_DIR=${THIRD_PARTY_INSTALL_DIR}/SQLite3/include
        -DSQLITE3_LIBRARY=${THIRD_PARTY_INSTALL_DIR}/SQLite3/lib/libsqlite3${LIB_EXT}
        -DEXE_SQLITE3=${THIRD_PARTY_INSTALL_DIR}/SQLite3/bin/sqlite3${EXE_EXT}
        -DENABLE_TIFF=OFF
        -DENABLE_CURL=OFF
        -DBUILD_SHARED_LIBS=OFF
        -DBUILD_PROJSYNC=OFF
        -DBUILD_PROJINFO=OFF
        -DBUILD_CCT=OFF
        -DBUILD_CS2CS=OFF
        -DBUILD_GEOD=OFF
        -DBUILD_GIE=OFF
        -DBUILD_PROJ=OFF
        -DBUILD_TESTING=OFF
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

  list (APPEND CMAKE_PREFIX_PATH ${PROJ_INSTALL_DIR})
  list (APPEND CMAKE_MODULE_PATH ${PROJ_INSTALL_DIR})

endif ()
