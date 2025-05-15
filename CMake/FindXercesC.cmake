
set (XERCESC_VERSION 3.3.0)
find_package (
  XercesC ${XERCESC_VERSION}
  EXACT
  QUIET
  NO_MODULE
)

cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR ${WORKSPACE_PATH}/ThirdParty)
set (THIRD_PARTY_BUILD_DIR ${THIRD_PARTY_ROOT_DIR}/Build)
set (THIRD_PARTY_INSTALL_DIR ${THIRD_PARTY_ROOT_DIR}/Install)
file (MAKE_DIRECTORY ${THIRD_PARTY_ROOT_DIR})

set (DEPENDENCY_NAME XercescC)
set (XERCESC_SOURCE_DIR ${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME})
set (XERCESC_BUILD_DIR ${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME})
set (XERCESC_INSTALL_DIR ${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME})

if (NOT ${XercesC_FOUND})

  message (STATUS "Could not find xerces-c, bootstrapping...")

  set (XERCESC_TAG xerces-c-${XERCESC_VERSION})

  if (NOT EXISTS ${THIRD_PARTY_ROOT_DIR}/xerces-c.zip)
    set (
      XERCESC_DOWNLOAD_URL
      https://archive.apache.org/dist/xerces/c/3/sources/xerces-c-${XERCESC_VERSION}.zip
    )
    message (STATUS "Downloading xerces-c from ${XERCESC_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${XERCESC_DOWNLOAD_URL}
      ${THIRD_PARTY_ROOT_DIR}/xerces-c.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${XERCESC_SOURCE_DIR})
    message (STATUS "Extracting downloaded xerces-c archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${THIRD_PARTY_ROOT_DIR}/xerces-c.zip
      DESTINATION ${THIRD_PARTY_ROOT_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${XERCESC_SOURCE_DIR})
    message (STATUS "Renaming extracted xerces-c directories.")
    file (
      RENAME
        ${THIRD_PARTY_ROOT_DIR}/${XERCESC_TAG}
        ${XERCESC_SOURCE_DIR}
    )
  endif ()

  message (STATUS "Configuring xerces-c.")

  set (
    XERCESC_CONFIGURE_COMMAND
    ${CMAKE_COMMAND}
      -S ${XERCESC_SOURCE_DIR}
      -B ${XERCESC_BUILD_DIR}
      -G ${CMAKE_GENERATOR}
      -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
      -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON
      -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
      -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
      -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
  )

  if (NOT WIN32)
    set (
      XERCESC_CONFIGURE_COMMAND
      ${XERCESC_CONFIGURE_COMMAND} 
      -Dtranscoder=gnuiconv
      -Dnetwork=OFF
    )
  endif ()

  execute_process (
    COMMAND
      ${XERCESC_CONFIGURE_COMMAND}
    RESULTS_VARIABLE
      XERCESC_CONFIGURE_RESULT
  )
  
  if (XERCESC_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap XercesC, configure step failed.")
  endif ()

  message (STATUS "Building xerces-c.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${XERCESC_BUILD_DIR}
    RESULTS_VARIABLE
      XERCESC_BUILD_RESULT
  )
  
  if (XERCESC_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap XercesC, build step failed.")
  endif ()

  message (STATUS "Installing xerces-c.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${XERCESC_BUILD_DIR}
        --prefix ${XERCESC_INSTALL_DIR}
    RESULTS_VARIABLE
      XERCESC_INSTALL_RESULT
  )

  if (XERCESC_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap XercesC, install step failed.")
  endif ()

  file (
    GLOB_RECURSE
    XERCESC_CONFIG_PATH_CANDIDATES
    FOLLOW_SYMLINKS
    LIST_DIRECTORIES FALSE
    ${XERCESC_INSTALL_DIR}/**/XercesCConfig.cmake
  )

  if (NOT XERCESC_CONFIG_PATH_CANDIDATES)
    message (FATAL_ERROR "Could not find XercesCConfig.cmake.")
  endif ()

  list (GET XERCESC_CONFIG_PATH_CANDIDATES 0 XERCESC_CONFIG_PATH)

  #[[
  # @TODO HACK:
  # --
  if ("${BASH_EXECUTABLE}" STREQUAL "")
    set (BASH_EXECUTABLE bash)
    if (WIN32)
      set (BASH_EXECUTABLE $ENV{PROGRAMFILES}/Git/bin/bash.exe)
    endif ()
  endif ()

  execute_process (
    COMMAND
      ${BASH_EXECUTABLE}
        ${CMAKE_CURRENT_LIST_DIR}/XercesCConfig_HACK.sh
        ${XERCESC_CONFIG_PATH}
  )
  # --
  ]]
  
  cmake_path (
    GET
    XERCESC_CONFIG_PATH
    PARENT_PATH
    XERCESC_CONFIG_DIR
  )

  list (APPEND CMAKE_PREFIX_PATH ${XERCESC_CONFIG_DIR})
  list (APPEND CMAKE_MODULE_PATH ${XERCESC_CONFIG_DIR})

  find_package (
    XercesC ${XERCESC_VERSION}
    EXACT
    REQUIRED
    CONFIG
  )

endif ()
