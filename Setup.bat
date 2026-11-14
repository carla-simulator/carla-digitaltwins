@echo off
setlocal

set "SOURCE_PATH=%~dp0"
if "%SOURCE_PATH:~-1%"=="\" set "SOURCE_PATH=%SOURCE_PATH:~0,-1%"

cmake ^
    -S "%SOURCE_PATH%" ^
    -B "%SOURCE_PATH%\Build" ^
    -G Ninja ^
    --toolchain "%SOURCE_PATH%\CMake\ToolchainUE5.cmake" ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_INSTALL_MESSAGE=NEVER ^
    -DBUILD_SHARED_LIBS=OFF ^
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5
if errorlevel 1 exit /b %errorlevel%

if exist "%SOURCE_PATH%\Content\Python\requirements.txt" (
    python -m pip install -r "%SOURCE_PATH%\Content\Python\requirements.txt"
    if errorlevel 1 exit /b %errorlevel%
)

