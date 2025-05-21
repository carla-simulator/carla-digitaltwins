#! /bin/bash

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "${SCRIPT_PATH}")"

BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

cmake \
    -S "${SOURCE_DIR}" \
    -B "${SOURCE_DIR}/Build" \
    -G Ninja \
    --toolchain "${SOURCE_DIR}/CMake/ToolchainUE4.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DBOOST_COMPONENTS=${BOOST_COMPONENTS}
