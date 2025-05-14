cmake_minimum_required (VERSION 3.25)
project (CarlaDigitalTwinsTool)

message (STATUS "Requesting boost components:")
foreach (COMPONENT ${BOOST_COMPONENTS})
  message (STATUS " - ${COMPONENT}")
endforeach ()

list (APPEND CMAKE_PREFIX_PATH ${CMAKE_CURRENT_LIST_DIR}/CMake)
list (APPEND CMAKE_MODULE_PATH ${CMAKE_CURRENT_LIST_DIR}/CMake)

set (BOOST_VERSION 1.84.0)
find_package (
  Boost ${BOOST_VERSION}
  EXACT
  REQUIRED
  COMPONENTS ${BOOST_COMPONENTS}
)

set (XERCESC_VERSION 3.3.0)
find_package (
  XercesC ${XERCESC_VERSION}
  EXACT
  REQUIRED
)

# This is incorrect:
set (SUMO_COMMIT 1da3c07e39f3e2e0d97f8f709a7255a0df8c6200)
find_package (
  SUMO
  REQUIRED
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
