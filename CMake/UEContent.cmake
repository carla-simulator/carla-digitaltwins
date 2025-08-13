cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH DIGITALTWINS_DIR)

set (CONTENT_DIR ${DIGITALTWINS_DIR}/Content)
set (CONTENT_URL https://bitbucket.org/carla-simulator/digitaltwins.git)
set (CONTENT_BRANCH ue5-digitaltwins)
message (STATUS "CONTENT_DIR=${CONTENT_DIR}")

if (NOT IS_DIRECTORY "${CONTENT_DIR}")
    file (MAKE_DIRECTORY "${CONTENT_DIR}")
    execute_process (
        COMMAND
            git -C ${CONTENT_DIR} clone -b ${CONTENT_BRANCH} ${CONTENT_URL} .
        RESULT_VARIABLE
            CONTENT_RESULT
    )
    if (CONTENT_RESULT)
        message (WARNING "Could not clone UE Content (exit code ${STREETMAP_RESULT}).")
    endif ()
else ()
    execute_process (
        COMMAND
            git -C ${CONTENT_DIR} pull
        RESULT_VARIABLE
            CONTENT_RESULT
    )
    if (CONTENT_RESULT)
        message (WARNING "Could not pull UE Content (exit code ${STREETMAP_RESULT}).")
    endif ()
endif ()
