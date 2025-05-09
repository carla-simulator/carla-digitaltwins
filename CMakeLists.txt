cmake_minimum_required(VERSION 3.25)
cmake_policy (SET CMP0167 NEW)

project (CarlaDigitalTwinsTool)

message (STATUS "Requesting boost components:")
foreach (COMPONENT ${BOOST_COMPONENTS})
  message (STATUS " - ${COMPONENT}")
endforeach ()

# Boost config
set (BOOST_VERSION 1.84.0)
set (BOOST_TAG boost-${BOOST_VERSION})

# Carpetas locales
set (BOOST_BASE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
file (MAKE_DIRECTORY ${BOOST_BASE_DIR})
set (BOOST_SOURCE_DIR ${BOOST_BASE_DIR}/boost)
set (BOOST_BUILD_DIR ${CMAKE_CURRENT_BINARY_DIR}/boost-build)
set (BOOST_INSTALL_DIR ${CMAKE_CURRENT_BINARY_DIR}/boost-install)

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

  file (MAKE_DIRECTORY ${BOOST_BASE_DIR})

  if (NOT EXISTS ${BOOST_BASE_DIR}/boost.zip)
    set (
      BOOST_DOWNLOAD_URL
      https://github.com/boostorg/boost/releases/download/${BOOST_TAG}/${BOOST_TAG}.zip
    )
    message (STATUS "Downloading boost from ${BOOST_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${BOOST_DOWNLOAD_URL}
      ${BOOST_BASE_DIR}/boost.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_SOURCE_DIR})
    message (STATUS "Extracting downloaded boost archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${BOOST_BASE_DIR}/boost.zip
      DESTINATION ${BOOST_BASE_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${BOOST_SOURCE_DIR})
    message (STATUS "Renaming extracted boost directories.")
    file (
      RENAME
        ${BOOST_BASE_DIR}/${BOOST_TAG}
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

  message (STATUS "Building boost.")
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

  message (STATUS "Installing boost.")
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

set (FILE_BODY)
foreach (T ${BOOST_COMPONENTS})
  set (BOOST_COMPONENT Boost::${T})
  get_target_property (DEPENDENCY_TYPE ${BOOST_COMPONENT} TYPE)
  if (NOT ("${DEPENDENCY_TYPE}" STREQUAL "INTERFACE_LIBRARY"))
    get_target_property (COMPONENT_LOCATION ${BOOST_COMPONENT} LOCATION)
    set (FILE_BODY "${FILE_BODY}${COMPONENT_LOCATION}\n")
  endif ()
endforeach ()
file (
  WRITE
  ${CMAKE_BINARY_DIR}/LinkLibraries.txt
  ${FILE_BODY}
)

set (FILE_BODY)
foreach (T ${BOOST_COMPONENTS})
  set (BOOST_COMPONENT Boost::${T})
  set (FILE_BODY "${FILE_BODY}$<JOIN:$<TARGET_PROPERTY:${BOOST_COMPONENT},INTERFACE_INCLUDE_DIRECTORIES>,\n>\n")
endforeach ()
file (
  WRITE
  ${CMAKE_BINARY_DIR}/IncludeDirectories.sample.txt
  ${FILE_BODY}
)
file (
  GENERATE
  OUTPUT
    ${CMAKE_BINARY_DIR}/IncludeDirectories.txt
  CONTENT
    ${FILE_BODY}
)

set (FILE_BODY)
foreach (T ${BOOST_COMPONENTS})
  set (BOOST_COMPONENT Boost::${T})
  set (FILE_BODY "${FILE_BODY}$<JOIN:$<TARGET_PROPERTY:${BOOST_COMPONENT},INTERFACE_COMPILE_DEFINITIONS>,\n>\n")
endforeach ()
file (
  WRITE
  ${CMAKE_BINARY_DIR}/PublicDefinitions.sample.txt
  ${FILE_BODY}
)
file (
  GENERATE
  OUTPUT
    ${CMAKE_BINARY_DIR}/PublicDefinitions.txt
  CONTENT
    ${FILE_BODY}
)
