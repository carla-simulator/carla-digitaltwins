@echo off

set SCRIPT_PATH=%~f0
set SOURCE_PATH=%SCRIPT_PATH:Setup.bat=%

python -m pip install -r requirements.txt

call "%PROGRAMFILES%/Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build/vcvars64.bat"

set BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"

cmake ^
    -S %SOURCE_PATH% ^
    -B %SOURCE_PATH%/Build ^
    -G Ninja ^
    --toolchain %SOURCE_PATH%/CMake/ToolchainUE5.cmake ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DBUILD_SHARED_LIBS=OFF ^
    -DBOOST_COMPONENTS=%BOOST_COMPONENTS%
