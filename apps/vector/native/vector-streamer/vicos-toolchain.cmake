# vicos-toolchain.cmake — Cross-compilation toolchain for Vector (Vicos/msm8909)
#
# Target: ARMv7-A (Snapdragon 212 / msm8909)
# Toolchain: vicos-sdk 5.3.0-r07 Clang
#
# Usage:
#   cmake .. -DCMAKE_TOOLCHAIN_FILE=../vicos-toolchain.cmake
#
# Build machine: Jetson (192.168.1.70) or NUC with downloaded vicos-sdk
#
# Required environment:
#   VICOS_SDK — path to vicos-sdk root (contains sysroot/)
#   OR set it here if building on a known machine.

# Auto-detect vicos-sdk location
if(NOT DEFINED VICOS_SDK)
    # Check common locations
    if(EXISTS "/tmp/victor-build/victor/vicos-sdk")
        set(VICOS_SDK "/tmp/victor-build/victor/vicos-sdk")
    elseif(EXISTS "$ENV{HOME}/vicos-sdk")
        set(VICOS_SDK "$ENV{HOME}/vicos-sdk")
    elseif(EXISTS "$ENV{VICOS_SDK}")
        set(VICOS_SDK "$ENV{VICOS_SDK}")
    else()
        message(FATAL_ERROR
            "Cannot find vicos-sdk. Set VICOS_SDK environment variable or "
            "cmake -DVICOS_SDK=/path/to/vicos-sdk")
    endif()
endif()

message(STATUS "Using vicos-sdk: ${VICOS_SDK}")

# System
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)

# Sysroot
set(CMAKE_SYSROOT "${VICOS_SDK}/sysroot")

# Compiler — try Clang from vicos-sdk first, fall back to system cross-compiler
set(VICOS_TOOLCHAIN "${VICOS_SDK}/prebuilt")

if(EXISTS "${VICOS_TOOLCHAIN}/bin/clang")
    set(CMAKE_C_COMPILER "${VICOS_TOOLCHAIN}/bin/clang")
    set(CMAKE_CXX_COMPILER "${VICOS_TOOLCHAIN}/bin/clang++")
    set(CMAKE_C_COMPILER_TARGET "armv7-linux-gnueabihf")
    set(CMAKE_CXX_COMPILER_TARGET "armv7-linux-gnueabihf")
elseif(EXISTS "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang")
    set(CMAKE_C_COMPILER "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang")
    set(CMAKE_CXX_COMPILER "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang++")
else()
    # Fall back to system arm-linux-gnueabihf-gcc
    find_program(ARM_GCC arm-linux-gnueabihf-gcc)
    if(ARM_GCC)
        set(CMAKE_C_COMPILER "arm-linux-gnueabihf-gcc")
        set(CMAKE_CXX_COMPILER "arm-linux-gnueabihf-g++")
    else()
        message(FATAL_ERROR
            "No ARM cross-compiler found. Install arm-linux-gnueabihf-gcc or "
            "ensure vicos-sdk toolchain is in ${VICOS_TOOLCHAIN}/bin/")
    endif()
endif()

# Compiler flags for Snapdragon 212 (ARMv7-A, Cortex-A7, NEON)
set(CMAKE_C_FLAGS_INIT "-march=armv7-a -mfpu=neon -mfloat-abi=hard -mthumb")
set(CMAKE_CXX_FLAGS_INIT "${CMAKE_C_FLAGS_INIT}")

# Linker
set(CMAKE_EXE_LINKER_FLAGS_INIT "-L${CMAKE_SYSROOT}/usr/lib -L${CMAKE_SYSROOT}/anki/lib")

# Search paths
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

# pkg-config
set(ENV{PKG_CONFIG_DIR} "")
set(ENV{PKG_CONFIG_LIBDIR} "${CMAKE_SYSROOT}/usr/lib/pkgconfig")
set(ENV{PKG_CONFIG_SYSROOT_DIR} "${CMAKE_SYSROOT}")
