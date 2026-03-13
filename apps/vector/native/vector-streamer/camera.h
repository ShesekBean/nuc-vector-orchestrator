/*
 * camera.h — Camera frame acquisition from vic-engine.
 *
 * Phase 1: Gets JPEG frames from vic-engine's gRPC CameraFeed on localhost.
 * Future: Direct camera_client shared memory + Venus H264 encoding.
 */

#ifndef CAMERA_H
#define CAMERA_H

#include <stdint.h>
#include <stddef.h>

/* Callback invoked with each camera frame.
 * data: JPEG or H264 frame data
 * length: data length in bytes
 * is_h264: 1 if H264 encoded, 0 if JPEG
 */
typedef void (*camera_frame_cb)(const uint8_t *data, size_t length,
                                int is_h264, void *user_data);

/* Initialize camera capture.
 * Returns 0 on success, -1 on error.
 */
int camera_init(camera_frame_cb callback, void *user_data);

/* Run the camera capture loop (blocks). */
int camera_run(void);

/* Signal camera loop to stop. Thread-safe. */
void camera_stop(void);

/* Clean up camera resources. */
void camera_cleanup(void);

#endif /* CAMERA_H */
