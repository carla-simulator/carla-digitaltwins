if (DEFINED UE_PLUGINS_DIR AND NOT "${UE_PLUGINS_DIR}" STREQUAL "")
    set (PLUGINS_DIR "${UE_PLUGINS_DIR}")
    set (STREETMAP_ALLOW_AUTO_CLONE TRUE)
else ()
    cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR FILENAME PLUGINS_DIR_NAME)
    string (TOLOWER "${PLUGINS_DIR_NAME}" PLUGINS_DIR_NAME_LOWER)
    if ("${PLUGINS_DIR_NAME_LOWER}" STREQUAL "plugins")
        set (STREETMAP_ALLOW_AUTO_CLONE TRUE)
    else ()
        set (STREETMAP_ALLOW_AUTO_CLONE FALSE)
    endif ()
endif ()

set (STREETMAP_DIR "${PLUGINS_DIR}/StreetMap")
set (STREETMAP_BRANCH ue5-digitaltwins)
set (STREETMAP_URL https://github.com/carla-simulator/StreetMap.git)

if (EXISTS "${STREETMAP_DIR}/.git")
    execute_process (
        COMMAND
            git -C "${STREETMAP_DIR}" pull --ff-only
        RESULT_VARIABLE
            STREETMAP_RESULT
    )
    if (STREETMAP_RESULT)
        message (WARNING "Could not pull StreetMap (exit code ${STREETMAP_RESULT}).")
    endif ()
elseif (NOT IS_DIRECTORY "${STREETMAP_DIR}" AND STREETMAP_ALLOW_AUTO_CLONE)
    execute_process (
        COMMAND
            git -C "${PLUGINS_DIR}" clone -b "${STREETMAP_BRANCH}" "${STREETMAP_URL}" StreetMap
        RESULT_VARIABLE
            STREETMAP_RESULT
    )
    if (STREETMAP_RESULT)
        message (WARNING "Could not clone StreetMap (exit code ${STREETMAP_RESULT}).")
    endif ()
elseif (NOT IS_DIRECTORY "${STREETMAP_DIR}")
    message (STATUS "Parent directory (${PLUGINS_DIR}) is not a UE Plugins folder; skipping automatic StreetMap clone.")
endif ()
