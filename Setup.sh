#! /bin/bash

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "${SCRIPT_PATH}")"

BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

cmake \
    -S "${SOURCE_DIR}" \
    -B "${SOURCE_DIR}/Build" \
    -G Ninja \
    --toolchain "${SOURCE_DIR}/CMake/ToolchainUE4.cmake" \
    -DCMAKE_IGNORE_PATH="${HOME}/anaconda3;${HOME}/anaconda3/bin;${HOME}/anaconda3/lib;${HOME}/anaconda3/include" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DBUILD_SHARED_LIBS=OFF \
    -DBOOST_COMPONENTS=${BOOST_COMPONENTS}
