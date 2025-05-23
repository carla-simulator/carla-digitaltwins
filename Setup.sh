#! /bin/bash

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "${SCRIPT_PATH}")"

# Configuration
BUILD_STREETMAP=false
GIT_PULL=true
PLUGINS_DIR="$(cd "$SOURCE_DIR/.." && pwd)"

STREETMAP_DIR=$PLUGINS_DIR/StreetMap

# Git config
CURRENT_STREETMAP_COMMIT=afc7217a372e6f50dfa7eb85f819c5faf0818138
STREETMAP_BRANCH=aaron/digitalTwinsstandalone
STREETMAP_REPO=https://github.com/carla-simulator/StreetMap.git

# Clone repo if not present
if [ ! -d "$STREETMAP_DIR/.git" ]; then
    git clone -b "$STREETMAP_BRANCH" "$STREETMAP_REPO" "$STREETMAP_DIR"
fi

# Move into repo and checkout the commit
git -C "$STREETMAP_DIR" fetch
git -C "$STREETMAP_DIR" checkout $CURRENT_STREETMAP_COMMIT

# CONTENT_ prefixed variables
CONTENT_FOLDER=$SOURCE_DIR/Content
CONTENT_TARGET_DIR="$CONTENT_FOLDER/digitalTwins"
CONTENT_REPO_URL="https://bitbucket.org/carla-simulator/digitaltwins.git"

# Check if the digitalTwins folder already exists
if [ -d "$CONTENT_TARGET_DIR" ]; then

    echo "The folder \"$CONTENT_TARGET_DIR\" already exists. Pulling..."

    git -C "$CONTENT_TARGET_DIR" pull

else

    echo "The folder \"$CONTENT_TARGET_DIR\" does not exist. Cloning..."

    # Check if Git is installed
    if ! command -v git &> /dev/null; then
        echo "ERROR: Git is not installed or not found in PATH."
        exit 1
    fi

    # Create the Content folder if it doesn't exist
    mkdir -p "$CONTENT_FOLDER"

    # Clone the repository into the target directory
    git -C "$CONTENT_FOLDER" clone "$CONTENT_REPO_URL" digitalTwins
    
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to clone the repository."
        exit 1
    else
        echo "Repository successfully cloned into \"$CONTENT_TARGET_DIR\"."
    fi
fi

BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

cmake \
    -S "${SOURCE_DIR}" \
    -B "${SOURCE_DIR}/Build" \
    -G Ninja \
    --toolchain "${SOURCE_DIR}/CMake/ToolchainUE4.cmake" \
    -DCMAKE_IGNORE_PATH="${HOME}/anaconda3;${HOME}/anaconda3/bin;${HOME}/anaconda3/lib;${HOME}/anaconda3/include" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DBOOST_COMPONENTS=${BOOST_COMPONENTS}
