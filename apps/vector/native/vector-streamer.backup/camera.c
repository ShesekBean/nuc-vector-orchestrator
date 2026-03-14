/*
 * camera.c — Camera frame acquisition from vic-engine.
 *
 * Phase 1 implementation: Connects to vic-engine's gRPC CameraFeed
 * on localhost:8888 to receive JPEG frames.
 *
 * Since implementing full gRPC/protobuf in C is complex, this initial
 * version uses a simpler approach: it reads from the camera_client
 * shared memory interface (via /dev/socket/vic-engine-cam_client0).
 *
 * If that fails (single-client restriction), falls back to a stub
 * that the NUC can fill via TCP (camera frames flow NUC → Vector
 * via the existing SDK gRPC path, so camera.c is optional).
 *
 * NOTE: Camera out already works via the existing NUC-side pipeline:
 *   Vector gRPC CameraFeed → NUC CameraClient → LiveKit
 * This module is for future H264 hardware encoding optimization.
 */

#include "camera.h"
#include "protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>

#define LOG_TAG "camera"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

static volatile int    g_running = 0;
static camera_frame_cb g_callback = NULL;
static void           *g_user_data = NULL;


int camera_init(camera_frame_cb callback, void *user_data)
{
    g_callback = callback;
    g_user_data = user_data;

    /* Camera frames already flow through the NUC-side pipeline.
     * This native camera capture is a future optimization for
     * Venus H264 hardware encoding.
     *
     * For now, the camera module is a no-op stub.
     * The NUC CameraClient → LiveKit path handles video out.
     */
    LOG("Camera module initialized (stub — video uses NUC-side pipeline)");
    LOG("Future: direct camera_client + Venus H264 encoding");

    g_running = 1;
    return 0;
}


int camera_run(void)
{
    LOG("Camera capture loop started (stub mode — sleeping)");

    /* In stub mode, just sleep. The NUC handles camera → LiveKit.
     * When Venus H264 encoding is implemented, this loop will:
     * 1. Connect to vic-engine camera_client socket
     * 2. Receive NV12 frames via shared memory
     * 3. Feed frames to h264_encoder
     * 4. Send encoded NALUs via tcp_server_send()
     */
    while (g_running) {
        usleep(1000000);  /* 1 second */
    }

    LOG("Camera capture loop ended");
    return 0;
}


void camera_stop(void)
{
    g_running = 0;
}


void camera_cleanup(void)
{
    camera_stop();
}
