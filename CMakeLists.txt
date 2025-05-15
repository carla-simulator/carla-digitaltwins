cmake_minimum_required (VERSION 3.25)
project (CarlaDigitalTwinsTool)



macro (DumpDependencyInfo DEPENDENCY_NAME)

  set (FILE_BODY)
  get_target_property (DEPENDENCY_TYPE ${DEPENDENCY_NAME} TYPE)
  if (NOT ("${DEPENDENCY_TYPE}" STREQUAL "INTERFACE_LIBRARY"))
    get_target_property (COMPONENT_LOCATION ${DEPENDENCY_NAME} LOCATION)
    set (FILE_BODY "${FILE_BODY}${COMPONENT_LOCATION}\n")
  endif ()
  file (
    APPEND
    ${CMAKE_BINARY_DIR}/LinkLibraries.txt
    ${FILE_BODY}
  )

  file (
    GENERATE
    OUTPUT
      ${CMAKE_BINARY_DIR}/IncludeDirectories.txt
    CONTENT
      "$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_INCLUDE_DIRECTORIES>,\n>\n"
  )

  file (
    GENERATE
    OUTPUT
      ${CMAKE_BINARY_DIR}/PublicDefinitions.txt
    CONTENT
      "$<JOIN:$<TARGET_PROPERTY:${DEPENDENCY_NAME},INTERFACE_COMPILE_DEFINITIONS>,\n>\n"
  )
endmacro ()



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

file (REMOVE ${CMAKE_BINARY_DIR}/LinkLibraries.txt)
file (REMOVE ${CMAKE_BINARY_DIR}/IncludeDirectories.txt)
file (REMOVE ${CMAKE_BINARY_DIR}/PublicDefinitions.txt)

foreach (BOOST_COMPONENT ${BOOST_COMPONENTS})
  DumpDependencyInfo(Boost::${BOOST_COMPONENT})
endforeach ()

DumpDependencyInfo(XercesC::XercesC)
DumpDependencyInfo(SUMO::)