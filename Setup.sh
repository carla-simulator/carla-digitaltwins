#! /bin/bash

BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

SCRIPT_PATH=$(readlink -f -- "${BASH_SOURCE[0]}")
SOURCE_PATH=$(dirname "${SCRIPT_PATH}")

cmake -S ${SOURCE_PATH} -B ${SOURCE_PATH}/Build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBOOST_COMPONENTS=${BOOST_COMPONENTS}
