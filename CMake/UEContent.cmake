cmake_path (GET CMAKE_CURRENT_LIST_DIR PARENT_PATH DIGITALTWINS_DIR)

set (CONTENT_DIR "${DIGITALTWINS_DIR}/Content")
set (CONTENT_URL https://bitbucket.org/carla-simulator/digitaltwins.git)
set (CONTENT_BRANCH ue5-digitaltwins)
message (STATUS "CONTENT_DIR=${CONTENT_DIR}")

if (EXISTS "${CONTENT_DIR}/.git")
    execute_process (
        COMMAND
            git -C "${CONTENT_DIR}" pull --ff-only
        RESULT_VARIABLE
            CONTENT_RESULT
    )
    if (CONTENT_RESULT)
        message (WARNING "Could not pull UE Content (exit code ${CONTENT_RESULT}).")
    endif ()
elseif (NOT EXISTS "${CONTENT_DIR}")
    execute_process (
        COMMAND
            git clone -b "${CONTENT_BRANCH}" "${CONTENT_URL}" "${CONTENT_DIR}"
        RESULT_VARIABLE
            CONTENT_RESULT
    )
    if (CONTENT_RESULT)
        message (WARNING "Could not clone UE Content (exit code ${CONTENT_RESULT}).")
    endif ()
else ()
    file (GLOB CONTENT_EXISTING_ENTRIES "${CONTENT_DIR}/*")
    if (NOT CONTENT_EXISTING_ENTRIES)
        execute_process (
            COMMAND
                git -C "${CONTENT_DIR}" clone -b "${CONTENT_BRANCH}" "${CONTENT_URL}" .
            RESULT_VARIABLE
                CONTENT_RESULT
        )
        if (CONTENT_RESULT)
            message (WARNING "Could not clone UE Content into empty Content directory (exit code ${CONTENT_RESULT}).")
        endif ()
    else ()
        message (STATUS "Content directory already exists without .git (${CONTENT_DIR}); skipping automatic git pull.")
    endif ()
endif ()
