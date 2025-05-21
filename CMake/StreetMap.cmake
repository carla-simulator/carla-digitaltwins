cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH PLUGINS_DIR)
cmake_path (GET PLUGINS_DIR PARENT_PATH PLUGINS_DIR)

set (STREETMAP_DIR ${PLUGINS_DIR}/StreetMap)
set (STREETMAP_COMMIT afc7217a372e6f50dfa7eb85f819c5faf0818138)
set (STREETMAP_BRANCH aaron/digitalTwinsstandalone)
set (STREETMAP_URL https://github.com/carla-simulator/StreetMap.git)

if (NOT IS_DIRECTORY "${STREETMAP_DIR}")
    execute_process (
        COMMAND
            git -C ${STREETMAP_DIR} clone -b ${STREETMAP_BRANCH} ${STREETMAP_URL} .
        RESULT_VARIABLE
            STREETMAP_RESULT
    )
    if (STREETMAP_RESULT)
        message (FATAL_ERROR "Could not clone StreetMap (exit code ${STREETMAP_RESULT}).")
    endif ()
endif ()

execute_process (
    COMMAND
        git -C ${STREETMAP_DIR} fetch
    RESULT_VARIABLE
        STREETMAP_RESULT
)
if (STREETMAP_RESULT)
    message (FATAL_ERROR "Could not fetch from StreetMap repo (exit code ${STREETMAP_RESULT}).")
endif ()

execute_process (
    COMMAND
        git -C ${STREETMAP_DIR} checkout ${STREETMAP_COMMIT}
    RESULT_VARIABLE
        STREETMAP_RESULT
)

if (STREETMAP_RESULT)
    message (FATAL_ERROR "Could not checkout StreetMap commit (exit code ${STREETMAP_RESULT}).")
endif ()