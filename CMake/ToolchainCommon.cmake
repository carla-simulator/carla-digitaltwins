#[[

  Copyright (c) 2024 Computer Vision Center (CVC) at the Universitat Autonoma
  de Barcelona (UAB).
  
  This work is licensed under the terms of the MIT license.
  For a copy, see <https://opensource.org/licenses/MIT>.

]]

if (LINUX)

cmake_policy (SET CMP0012 NEW)

macro (create_filepath_variable_checked NAME VALUE)
	set (${NAME}_FOUND FALSE)
	if (IS_DIRECTORY "${VALUE}")
		set (${NAME}_FOUND TRUE)
	endif ()
	if (EXISTS "${VALUE}")
		set (${NAME}_FOUND TRUE)
	endif ()
	if (NOT ${${NAME}_FOUND})
		message (FATAL_ERROR "${NAME}: Assigned value \"${VALUE}\" does not exist.")
	endif ()
	set (${NAME} ${VALUE} CACHE FILEPATH "")
endmacro ()

set (UE_ROOT $ENV{${CARLA_UE_ENV_VAR_NAME}})

if (NOT EXISTS ${UE_ROOT})
	message (FATAL_ERROR "The specified Carla Unreal Engine path does not exist (\"${UE_ROOT}\").")
endif ()

set (ARCH ${CMAKE_HOST_SYSTEM_PROCESSOR})

if	(${ARCH} STREQUAL "x86_64")
	set	(CMAKE_SYSTEM_PROCESSOR x86_64 CACHE STRING "")
	set	(TARGET_TRIPLE "x86_64-unknown-linux-gnu" CACHE STRING "")
elseif (${ARCH} STREQUAL "aarch64")
	set (CMAKE_SYSTEM_PROCESSOR aarch64 CACHE STRING "")
	set (TARGET_TRIPLE "aarch64-unknown-linux-gnueabi" CACHE STRING "")
endif()

file (
	GLOB
	UE_SYSROOT_CANDIDATES
	${UE_ROOT}/Engine/Extras/ThirdPartyNotUE/SDKs/HostLinux/Linux_x64/v*_clang-*.*.*-*/${TARGET_TRIPLE}
	LIST_DIRECTORIES TRUE
	FOLLOW_SYMLINKS
)

set (UE_SYSROOT_CANDIDATE)
foreach (CANDIDATE ${UE_SYSROOT_CANDIDATES})
	if (IS_DIRECTORY ${CANDIDATE})
		set (UE_SYSROOT_CANDIDATE ${CANDIDATE})
		break ()
	endif ()
endforeach ()

if (NOT UE_SYSROOT_CANDIDATE)
	message (FATAL_ERROR "Could not find Unreal Engine clang sysroot.")
endif ()

set (
	UE_SYSROOT
	${UE_SYSROOT_CANDIDATE}
	CACHE PATH ""
)

set (UE_THIRD_PARTY ${UE_ROOT}/Engine/Source/ThirdParty CACHE PATH "")

if (IS_DIRECTORY ${UE_THIRD_PARTY}/Unix/LibCxx/include)
	set (UE_INCLUDE ${UE_THIRD_PARTY}/Unix/LibCxx/include CACHE PATH "")
else ()
	set (UE_INCLUDE ${UE_THIRD_PARTY}/Linux/LibCxx/include CACHE PATH "")
endif ()

if (IS_DIRECTORY ${UE_THIRD_PARTY}/Unix/LibCxx/lib/Unix/${TARGET_TRIPLE})
	set (UE_LIBS ${UE_THIRD_PARTY}/Unix/LibCxx/lib/Unix/${TARGET_TRIPLE} CACHE PATH "")
else ()
	set (UE_LIBS ${UE_THIRD_PARTY}/Linux/LibCxx/lib/Linux/${TARGET_TRIPLE} CACHE PATH "")
endif ()

if (IS_DIRECTORY ${UE_THIRD_PARTY}/OpenSSL/1.1.1t/include/Unix)
	set (UE_OPENSSL_INCLUDE ${UE_THIRD_PARTY}/OpenSSL/1.1.1t/include/Unix CACHE PATH "")
else ()
	set (UE_OPENSSL_INCLUDE ${UE_THIRD_PARTY}/OpenSSL/1.1.1c/include/Linux CACHE PATH "")
endif ()

if (IS_DIRECTORY ${UE_THIRD_PARTY}/OpenSSL/1.1.1t/lib/Unix/x86_64-unknown-linux-gnu)
	set (UE_OPENSSL_LIBS ${UE_THIRD_PARTY}/OpenSSL/1.1.1t/lib/Unix/x86_64-unknown-linux-gnu CACHE PATH "")
else ()
	set (UE_OPENSSL_LIBS ${UE_THIRD_PARTY}/OpenSSL/1.1.1c/lib/Linux/x86_64-unknown-linux-gnu CACHE PATH "")
endif ()

set (
	CHECK_PATHS
	${UE_THIRD_PARTY}
	${UE_INCLUDE}
	${UE_LIBS}
	${UE_OPENSSL_INCLUDE}
	${UE_OPENSSL_LIBS}
)

foreach (CHECK_PATH ${CHECK_PATHS})
	if (NOT IS_DIRECTORY "${CHECK_PATH}")
		message (FATAL_ERROR "${CHECK_PATH} is not a valid directory.")
	endif ()
endforeach ()

add_compile_options (
	-fms-extensions
	-fno-math-errno
	-fdiagnostics-absolute-paths
	$<$<COMPILE_LANGUAGE:CXX>:-stdlib=libc++>
)

add_link_options (-stdlib=libc++ -L${UE_LIBS} )

create_filepath_variable_checked (
	CMAKE_AR
	${UE_SYSROOT}/bin/llvm-ar
)

create_filepath_variable_checked (
	CMAKE_ASM_COMPILER
	${UE_SYSROOT}/bin/clang
)

create_filepath_variable_checked (
	CMAKE_C_COMPILER
	${UE_SYSROOT}/bin/clang
)

create_filepath_variable_checked (
	CMAKE_C_COMPILER_AR
	${UE_SYSROOT}/bin/llvm-ar
)

create_filepath_variable_checked (
	CMAKE_CXX_COMPILER
	${UE_SYSROOT}/bin/clang++
)

create_filepath_variable_checked (
	CMAKE_CXX_COMPILER_AR
	${UE_SYSROOT}/bin/llvm-ar
)

create_filepath_variable_checked (
	CMAKE_OBJCOPY
	${UE_SYSROOT}/bin/llvm-objcopy
)

create_filepath_variable_checked (
	CMAKE_ADDR2LINE
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-addr2line
)

create_filepath_variable_checked (
	CMAKE_C_COMPILER_RANLIB
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-ranlib
)

create_filepath_variable_checked (
	CMAKE_CXX_COMPILER_RANLIB
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-ranlib
)

create_filepath_variable_checked (
	CMAKE_LINKER
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-ld
)

create_filepath_variable_checked (
	CMAKE_NM
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-nm
)

create_filepath_variable_checked (
	CMAKE_OBJDUMP
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-objdump
)

create_filepath_variable_checked (
	CMAKE_RANLIB
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-ranlib
)

create_filepath_variable_checked (
	CMAKE_READELF
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-readelf
)

create_filepath_variable_checked (
	CMAKE_STRIP
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-strip
)

set (
	COVERAGE_COMMAND
	${UE_SYSROOT}/bin/${TARGET_TRIPLE}-gcov
	CACHE FILEPATH ""
)

set (
	CMAKE_CXX_STANDARD_LIBRARIES
	${UE_LIBS}/libc++.a
	${UE_LIBS}/libc++abi.a
)

foreach (CXX_STDLIB ${CMAKE_CXX_STANDARD_LIBRARIES})
	if (NOT EXISTS ${CXX_STDLIB})
		message (FATAL_ERROR "Could not find ${CXX_STDLIB}")
	endif ()
endforeach ()

string (REPLACE ";" " " CMAKE_CXX_STANDARD_LIBRARIES "${CMAKE_CXX_STANDARD_LIBRARIES}")

set (
	CMAKE_CXX_STANDARD_INCLUDE_DIRECTORIES
	${UE_INCLUDE}
	${UE_INCLUDE}/c++/v1
)

foreach (CXX_STDINC ${CMAKE_CXX_STANDARD_INCLUDE_DIRECTORIES})
	if (NOT EXISTS ${CXX_STDINC})
		message (FATAL_ERROR "Could not find ${CXX_STDINC}")
	endif ()
endforeach ()


endif ()
