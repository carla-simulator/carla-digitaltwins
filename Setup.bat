@echo off

set BOOST_COMPONENTS="asio;iterator;date_time;geometry;container;variant2;gil;filesystem"
set SCRIPT_PATH=%~f0
set SOURCE_PATH=%SCRIPT_PATH:Setup.bat=%

call "%PROGRAMFILES%/Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build/vcvars64.bat"
cmake -S %SOURCE_PATH% -B %SOURCE_PATH%/Build -G Ninja --toolchain %SOURCE_PATH%/CMake/Toolchain.cmake -DCMAKE_BUILD_TYPE=Release -DBOOST_COMPONENTS=%BOOST_COMPONENTS%
