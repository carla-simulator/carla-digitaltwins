@echo off

set SCRIPT_PATH=%~f0
set SOURCE_PATH=%SCRIPT_PATH:Setup.bat=%

cmake ^
    -S %SOURCE_PATH% ^
    -B %SOURCE_PATH%/Build ^
    -G Ninja ^
    --toolchain %SOURCE_PATH%/CMake/ToolchainUE5.cmake ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_INSTALL_MESSAGE=NEVER ^
    -DBUILD_SHARED_LIBS=OFF ^
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5

python -m pip install -r Content\Python\requirements.txt
