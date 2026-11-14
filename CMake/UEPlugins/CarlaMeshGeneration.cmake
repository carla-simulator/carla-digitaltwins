if (DEFINED UE_PLUGINS_DIR AND NOT "${UE_PLUGINS_DIR}" STREQUAL "")
    set (PLUGINS_DIR "${UE_PLUGINS_DIR}")
    set (CMG_ALLOW_AUTO_CLONE TRUE)
else ()
    cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
    cmake_path (GET PLUGINS_DIR FILENAME PLUGINS_DIR_NAME)
    string (TOLOWER "${PLUGINS_DIR_NAME}" PLUGINS_DIR_NAME_LOWER)
    if ("${PLUGINS_DIR_NAME_LOWER}" STREQUAL "plugins")
        set (CMG_ALLOW_AUTO_CLONE TRUE)
    else ()
        set (CMG_ALLOW_AUTO_CLONE FALSE)
    endif ()
endif ()

set (CMG_DIR "${PLUGINS_DIR}/carla-mesh-generation")
set (CMG_BRANCH master)
set (CMG_URL https://github.com/carla-simulator/carla-mesh-generation.git)

if (EXISTS "${CMG_DIR}/.git")
    execute_process (
        COMMAND
            git -C "${CMG_DIR}" pull --ff-only
        RESULT_VARIABLE
            CMG_RESULT
    )
    if (CMG_RESULT)
        message (WARNING "Could not pull carla-mesh-generation (exit code ${CMG_RESULT}).")
    endif ()
elseif (NOT IS_DIRECTORY "${CMG_DIR}" AND CMG_ALLOW_AUTO_CLONE)
    execute_process (
        COMMAND
            git -C "${PLUGINS_DIR}" clone -b "${CMG_BRANCH}" "${CMG_URL}" carla-mesh-generation
        RESULT_VARIABLE
            CMG_RESULT
    )
    if (CMG_RESULT)
        message (WARNING "Could not clone carla-mesh-generation (exit code ${CMG_RESULT}).")
    endif ()
elseif (NOT IS_DIRECTORY "${CMG_DIR}")
    message (STATUS "Parent directory (${PLUGINS_DIR}) is not a UE Plugins folder; skipping automatic carla-mesh-generation clone.")
endif ()
