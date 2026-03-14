cmake_minimum_required(VERSION 3.10)
project(vector-streamer C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Warn flags
add_compile_options(-Wall -Wextra -Wno-unused-parameter)

# Optimize for size on embedded target
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_C_FLAGS_RELEASE "-Os -DNDEBUG")

# Source files
set(SOURCES
    main.c
    mic.c
    opus_encoder.c
    tcp_server.c
    camera.c
    h264_encoder.c
    engine_proxy.c
)

add_executable(vector-streamer ${SOURCES})

# Link libraries
# On Vector, these are dynamically linked:
#   libopus.so.0     -> /anki/lib/
#   libtinyalsa.so.1 -> /usr/lib/
#   libpthread       -> system
target_link_libraries(vector-streamer
    opus
    pthread
)

# Include paths for opus headers
# Cross-compile: opus headers from vicos-sdk sysroot
# Native build (for testing): system opus headers
find_path(OPUS_INCLUDE_DIR opus/opus.h
    PATHS
        ${CMAKE_SYSROOT}/usr/include
        /usr/include
        /usr/local/include
)
if(OPUS_INCLUDE_DIR)
    target_include_directories(vector-streamer PRIVATE ${OPUS_INCLUDE_DIR})
endif()

# Install target
install(TARGETS vector-streamer RUNTIME DESTINATION bin)
