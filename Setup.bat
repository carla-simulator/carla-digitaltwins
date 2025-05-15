@echo off

setlocal

:: Config
set "BUILD_STREETMAP=false"
set "GIT_PULL=true"

:: Get script folder and go one level up
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_BASE_DIR=%SCRIPT_DIR%.."
set "DIGITAL_TWINS_TOOL_PLUGINS=%PLUGIN_BASE_DIR%"
set "DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP=%DIGITAL_TWINS_TOOL_PLUGINS%\StreetMap"

rem ============================================================================
rem -- StreetMaps      ---------------------------------------------------------
rem ============================================================================

set "CURRENT_STREETMAP_COMMIT=afc7217a372e6f50dfa7eb85f819c5faf0818138"
set "STREETMAP_BRANCH=aaron/digitalTwinsstandalone"
set "STREETMAP_REPO=https://github.com/carla-simulator/StreetMap.git"

:: Clone repo if it doesn't exist
if not exist "%DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP%\" (
    git clone -b %STREETMAP_BRANCH% %STREETMAP_REPO% "%DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP%"
)

cd /d "%DIGITAL_TWINS_TOOL_PLUGINS_STREETMAP%"
git fetch
git checkout %CURRENT_STREETMAP_COMMIT%

rem ============================================================================
rem -- Digital TwinsContent-----------------------------------------------------
rem ============================================================================

cd /d "%~dp0"

set "CONTENT_FOLDER=Content"
set "CONTENT_TARGET_DIR=%CONTENT_FOLDER%\digitalTwins"
set "CONTENT_REPO_URL=https://bitbucket.org/carla-simulator/digitaltwins.git"

if exist "%CONTENT_TARGET_DIR%\" (
    echo Folder "%CONTENT_TARGET_DIR%" already exists. Skipping clone.
) else (
    echo Folder does not exist. Cloning repository...

    if not exist "%CONTENT_FOLDER%\" (
        echo Creating content folder...
        mkdir "%CONTENT_FOLDER%"
    )

    echo Running git clone...
    git clone "%CONTENT_REPO_URL%" "%CONTENT_TARGET_DIR%"

    echo Git clone returned code: %ERRORLEVEL%
    if errorlevel 1 (
        echo ERROR: Failed to clone repository!
        pause
        exit /b 1
    ) else (
        echo SUCCESS: Repo cloned into "%CONTENT_TARGET_DIR%"
    )
)

rem ============================================================================
rem -- Boost  ------------------------------------------------------------------
rem ============================================================================

set BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"
set SCRIPT_PATH=%~f0
set SOURCE_PATH=%SCRIPT_PATH:Setup.bat=%

call "%PROGRAMFILES%/Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build/vcvars64.bat"
cmake -S %SOURCE_PATH% -B %SOURCE_PATH%/Build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBOOST_COMPONENTS=%BOOST_COMPONENTS%

endlocal
