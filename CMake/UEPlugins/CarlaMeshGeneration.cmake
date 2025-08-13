cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH PLUGINS_DIR)
cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)

set (CMG_DIR ${PLUGINS_DIR}/carla-mesh-generation)
set (CMG_BRANCH master)
set (CMG_URL https://github.com/carla-simulator/carla-mesh-generation.git)

if (NOT IS_DIRECTORY "${CMG_DIR}")
    execute_process (
        COMMAND
            git -C ${PLUGINS_DIR} clone -b ${CMG_BRANCH} ${CMG_URL} carla-mesh-generation
        RESULT_VARIABLE
            CMG_RESULT
    )
    if (CMG_RESULT)
        message (WARNING "Could not clone carla-mesh-generation (exit code ${CMG_RESULT}).")
    endif ()
else ()
    execute_process (
        COMMAND
            git -C ${CMG_DIR} pull
        RESULT_VARIABLE
            CMG_RESULT
    )
    if (CMG_RESULT)
        message (WARNING "Could not pull carla-mesh-generation (exit code ${CMG_RESULT}).")
    endif ()
endif ()
