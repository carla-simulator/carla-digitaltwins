
cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH WORKSPACE_PATH)
set (THIRD_PARTY_ROOT_DIR ${WORKSPACE_PATH}/ThirdParty)
set (THIRD_PARTY_BUILD_DIR ${THIRD_PARTY_ROOT_DIR}/Build)
set (THIRD_PARTY_INSTALL_DIR ${THIRD_PARTY_ROOT_DIR}/Install)
file (MAKE_DIRECTORY ${THIRD_PARTY_ROOT_DIR})

set (DEPENDENCY_NAME SQLite3)
set (SQLITE3_SOURCE_DIR ${THIRD_PARTY_ROOT_DIR}/${DEPENDENCY_NAME})
set (SQLITE3_BUILD_DIR ${THIRD_PARTY_BUILD_DIR}/${DEPENDENCY_NAME})
set (SQLITE3_INSTALL_DIR ${THIRD_PARTY_INSTALL_DIR}/${DEPENDENCY_NAME})

set (SQLITE3_AMALG_VERSION 3490200)

find_package (
  ${DEPENDENCY_NAME}
  QUIET
  NO_MODULE
)

if (NOT ${SQLite3_FOUND})
    
  message (STATUS "Could not find ${DEPENDENCY_NAME}, bootstrapping...")

  if (NOT EXISTS ${THIRD_PARTY_ROOT_DIR}/sqlite3.zip)
    set (
      SQLITE3_DOWNLOAD_URL
      https://www.sqlite.org/2025/sqlite-amalgamation-${SQLITE3_AMALG_VERSION}.zip
    )
    message (STATUS "Downloading ${DEPENDENCY_NAME} from ${SQLITE3_DOWNLOAD_URL}")
    file (
      DOWNLOAD
      ${SQLITE3_DOWNLOAD_URL}
      ${THIRD_PARTY_ROOT_DIR}/sqlite3.zip
    )
  endif ()

  if (NOT IS_DIRECTORY ${SQLITE3_SOURCE_DIR})
    message (STATUS "Extracting downloaded ${DEPENDENCY_NAME} archive.")
    file (
      ARCHIVE_EXTRACT
      INPUT ${THIRD_PARTY_ROOT_DIR}/sqlite3.zip
      DESTINATION ${THIRD_PARTY_ROOT_DIR}
    )
  endif ()

  if (NOT IS_DIRECTORY ${SQLITE3_SOURCE_DIR})
    message (STATUS "Renaming extracted ${DEPENDENCY_NAME} directories.")
    file (
      RENAME
        ${THIRD_PARTY_ROOT_DIR}/sqlite-amalgamation-${SQLITE3_AMALG_VERSION}
        ${SQLITE3_SOURCE_DIR}
    )
  endif ()

  message (STATUS "Configuring ${DEPENDENCY_NAME}.")

  file (
    WRITE ${SQLITE3_SOURCE_DIR}/CMakeLists.txt
    "cmake_minimum_required (VERSION ${CMAKE_VERSION})\n"
    "project (SQLite3)\n"
    "add_compile_definitions (SQLITE_DISABLE_LFS=1)\n"
    "add_library (libsqlite3 sqlite3.h sqlite3ext.h sqlite3.c)\n"
    "if (LINUX)\n"
    " find_package (Threads REQUIRED)\n"
    " target_link_libraries (libsqlite3 PRIVATE \${CMAKE_DL_LIBS})\n"
    " target_link_libraries (libsqlite3 PRIVATE Threads::Threads)\n"
    " target_link_libraries (libsqlite3 PRIVATE m)\n"
    "endif ()\n"
    "set_target_properties (libsqlite3 PROPERTIES PREFIX \"\")\n"
    "add_executable (sqlite3 shell.c)\n"
    "target_link_libraries (sqlite3 PRIVATE libsqlite3)\n"
    "install (FILES sqlite3.h sqlite3ext.h DESTINATION include)\n"
    "install (TARGETS libsqlite3 sqlite3)\n"
  )
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        -S ${SQLITE3_SOURCE_DIR}
        -B ${SQLITE3_BUILD_DIR}
        -G ${CMAKE_GENERATOR}
        -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
        -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DBUILD_SHARED_LIBS=OFF
        -DCMAKE_IGNORE_PATH=${CMAKE_IGNORE_PATH}
        -DCMAKE_INSTALL_PREFIX=${SQLITE3_INSTALL_DIR}
        -DCMAKE_INSTALL_MESSAGE=${CMAKE_INSTALL_MESSAGE}
        -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}
        -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
    RESULTS_VARIABLE
      SQLITE3_CONFIGURE_RESULT
  )
  
  if (SQLITE3_CONFIGURE_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, configure step failed.")
  endif ()

  message (STATUS "Building ${DEPENDENCY_NAME}.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --build ${SQLITE3_BUILD_DIR}
    RESULTS_VARIABLE
      SQLITE3_BUILD_RESULT
  )
  
  if (SQLITE3_BUILD_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, build step failed.")
  endif ()

  message (STATUS "Installing ${DEPENDENCY_NAME}.")
  execute_process (
    COMMAND
      ${CMAKE_COMMAND}
        --install ${SQLITE3_BUILD_DIR}
        --prefix ${SQLITE3_INSTALL_DIR}
    RESULTS_VARIABLE
      SQLITE3_INSTALL_RESULT
  )
  
  if (SQLITE3_INSTALL_RESULT)
    message (FATAL_ERROR "Could not bootstrap ${DEPENDENCY_NAME}, install step failed.")
  endif ()

  list (APPEND CMAKE_PREFIX_PATH ${SQLITE3_INSTALL_DIR})
  list (APPEND CMAKE_MODULE_PATH ${SQLITE3_INSTALL_DIR})

  set (LIB_EXT .a)
  set (EXE_EXT)
  if (WIN32)
    set (LIB_EXT .lib)
    set (EXE_EXT .exe)
  endif ()

  if (NOT TARGET SQLite::SQLite3)
    add_library (SQLite::SQLite3 STATIC IMPORTED)
    set_target_properties (
      SQLite::SQLite3
      PROPERTIES
      IMPORTED_LOCATION ${SQLITE3_INSTALL_DIR}/lib/libsqlite3${LIB_EXT}
      INTERFACE_INCLUDE_DIRECTORIES ${SQLITE3_INSTALL_DIR}/include
    )
  endif ()

  set (SQLite3_INCLUDE_DIRS ${SQLITE3_INSTALL_DIR}/include)
  set (SQLite3_LIBRARIES SQLite::SQLite3)
  set (SQLite3_VERSION 3.11)
  set (SQLite3_FOUND TRUE)
  message (STATUC "SQLite3_INCLUDE_DIRS=${SQLite3_INCLUDE_DIRS}")
  message (STATUC "SQLite3_LIBRARIES=${SQLite3_LIBRARIES}")
  message (STATUC "SQLite3_VERSION=${SQLite3_VERSION}")
  message (STATUC "SQLite3_FOUND=${SQLite3_FOUND}")

else ()

  message (STATUS "Found ${DEPENDENCY_NAME}. Skipping build...")

endif ()
