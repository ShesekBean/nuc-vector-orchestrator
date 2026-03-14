# vicos-toolchain.cmake -- Cross-compilation toolchain for Vector (Vicos/msm8909)
#
# Target: ARMv7-A (Snapdragon 212 / msm8909)
# Toolchain: vicos-sdk 5.3.0-r07 Clang + OE binutils
#
# Usage:
#   cmake .. -DCMAKE_TOOLCHAIN_FILE=../vicos-toolchain.cmake
#   cmake .. -DCMAKE_TOOLCHAIN_FILE=../vicos-toolchain.cmake -DVICOS_SDK=/path/to/sdk
#
# Build machine: Jetson (192.168.1.70) or NUC with downloaded vicos-sdk

# Auto-detect vicos-sdk location
if(NOT DEFINED VICOS_SDK)
    if(EXISTS "/tmp/victor-build/victor/vicos-sdk")
        set(VICOS_SDK "/tmp/victor-build/victor/vicos-sdk" CACHE PATH "vicos-sdk path")
    elseif(EXISTS "$ENV{HOME}/.anki/vicos-sdk/dist/5.3.0-r07")
        set(VICOS_SDK "$ENV{HOME}/.anki/vicos-sdk/dist/5.3.0-r07" CACHE PATH "vicos-sdk path")
    elseif(EXISTS "$ENV{HOME}/vicos-sdk")
        set(VICOS_SDK "$ENV{HOME}/vicos-sdk" CACHE PATH "vicos-sdk path")
    elseif(DEFINED ENV{VICOS_SDK})
        set(VICOS_SDK "$ENV{VICOS_SDK}" CACHE PATH "vicos-sdk path")
    else()
        message(FATAL_ERROR
            "Cannot find vicos-sdk. Set VICOS_SDK environment variable or "
            "cmake -DVICOS_SDK=/path/to/vicos-sdk")
    endif()
else()
    set(VICOS_SDK "${VICOS_SDK}" CACHE PATH "vicos-sdk path")
endif()

message(STATUS "Using vicos-sdk: ${VICOS_SDK}")

# System
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)

# Sysroot
set(CMAKE_SYSROOT "${VICOS_SDK}/sysroot")

# Compiler -- use arm-oe-linux-gnueabi-clang from vicos-sdk
set(VICOS_TOOLCHAIN "${VICOS_SDK}/prebuilt")

if(EXISTS "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang")
    set(CMAKE_C_COMPILER "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang")
    set(CMAKE_CXX_COMPILER "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-clang++")
elseif(EXISTS "${VICOS_TOOLCHAIN}/bin/clang")
    set(CMAKE_C_COMPILER "${VICOS_TOOLCHAIN}/bin/clang")
    set(CMAKE_CXX_COMPILER "${VICOS_TOOLCHAIN}/bin/clang++")
    set(CMAKE_C_COMPILER_TARGET "armv7-linux-gnueabihf")
    set(CMAKE_CXX_COMPILER_TARGET "armv7-linux-gnueabihf")
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

# Use the OE binutils linker instead of system ld
if(EXISTS "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-ld")
    set(CMAKE_LINKER "${VICOS_TOOLCHAIN}/bin/arm-oe-linux-gnueabi-ld")
endif()

# Tell clang where to find GCC runtime (crtbeginS.o, libgcc)
set(GCC_TOOLCHAIN_DIR "${CMAKE_SYSROOT}/usr/lib/arm-oe-linux-gnueabi/15.2.0")
if(NOT EXISTS "${GCC_TOOLCHAIN_DIR}")
    # Try to auto-detect
    file(GLOB _GCC_DIRS "${CMAKE_SYSROOT}/usr/lib/arm-oe-linux-gnueabi/*")
    list(LENGTH _GCC_DIRS _GCC_DIRS_LEN)
    if(_GCC_DIRS_LEN GREATER 0)
        list(GET _GCC_DIRS 0 GCC_TOOLCHAIN_DIR)
    endif()
endif()

# Compiler flags for Snapdragon 212 (ARMv7-A, Cortex-A7, NEON)
# Use -B to point clang to the GCC resource dir for crtbeginS.o etc.
set(CMAKE_C_FLAGS_INIT "-march=armv7-a -mfpu=neon -mfloat-abi=softfp -mthumb -B${GCC_TOOLCHAIN_DIR}")
set(CMAKE_CXX_FLAGS_INIT "${CMAKE_C_FLAGS_INIT}")

# Linker flags
set(CMAKE_EXE_LINKER_FLAGS_INIT "-L${CMAKE_SYSROOT}/usr/lib -L${CMAKE_SYSROOT}/anki/lib -L${GCC_TOOLCHAIN_DIR}")

# Search paths
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

# pkg-config
set(ENV{PKG_CONFIG_DIR} "")
set(ENV{PKG_CONFIG_LIBDIR} "${CMAKE_SYSROOT}/usr/lib/pkgconfig")
set(ENV{PKG_CONFIG_SYSROOT_DIR} "${CMAKE_SYSROOT}")
