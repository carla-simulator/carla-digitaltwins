#! /bin/bash

# Configuration
BUILD_STREETMAP=false
GIT_PULL=true

# Resolve script directory and one level up
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DIGITAL_TWINS_TOOL_PLUGINS="$PLUGIN_BASE_DIR"
DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP="$DIGITAL_TWINS_TOOL_PLUGINS/StreetMap"

# Git config
CURRENT_STREETMAP_COMMIT=afc7217a372e6f50dfa7eb85f819c5faf0818138
STREETMAP_BRANCH=aaron/digitalTwinsstandalone
STREETMAP_REPO=https://github.com/carla-simulator/StreetMap.git

# Clone repo if not present
if [ ! -d "$DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP/.git" ]; then
    git clone -b "$STREETMAP_BRANCH" "$STREETMAP_REPO" "$DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP"
fi

# Move into repo and checkout the commit
cd "$DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP" || exit 1
git fetch
git checkout "$CURRENT_STREETMAP_COMMIT"

BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

SCRIPT_PATH=$(readlink -f -- "${BASH_SOURCE[0]}")
SOURCE_PATH=$(dirname "${SCRIPT_PATH}")

cmake -S ${SOURCE_PATH} -B ${SOURCE_PATH}/Build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBOOST_COMPONENTS=${BOOST_COMPONENTS}
