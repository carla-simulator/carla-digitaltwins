cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH PLUGINS_DIR)
cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)
cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)

set (STREETMAP_DIR ${PLUGINS_DIR}/StreetMap)
set (STREETMAP_BRANCH ue5-digitaltwins)
set (STREETMAP_URL https://github.com/carla-simulator/StreetMap.git)

if (NOT IS_DIRECTORY "${STREETMAP_DIR}")
    execute_process (
        COMMAND
            git -C ${PLUGINS_DIR} clone -b ${STREETMAP_BRANCH} ${STREETMAP_URL} StreetMap
        RESULT_VARIABLE
            STREETMAP_RESULT
    )
    if (STREETMAP_RESULT)
        message (WARNING "Could not clone StreetMap (exit code ${STREETMAP_RESULT}).")
    endif ()
else ()
    execute_process (
        COMMAND
            git -C ${STREETMAP_DIR} pull
        RESULT_VARIABLE
            STREETMAP_RESULT
    )
    if (STREETMAP_RESULT)
        message (WARNING "Could not pull StreetMap (exit code ${STREETMAP_RESULT}).")
    endif ()
endif ()
