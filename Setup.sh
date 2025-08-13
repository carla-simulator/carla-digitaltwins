#! /bin/bash

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "${SCRIPT_PATH}")"

cmake \
    -S "${SOURCE_DIR}" \
    -B "${SOURCE_DIR}/Build" \
    -G Ninja \
    --toolchain "${SOURCE_DIR}/CMake/ToolchainUE5.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_MESSAGE=NEVER \
    -DBUILD_SHARED_LIBS=OFF

python3 -m pip install -r Content/Python/requirements.txt
