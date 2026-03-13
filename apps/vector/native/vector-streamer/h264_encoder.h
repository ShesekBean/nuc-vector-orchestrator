/*
 * h264_encoder.h — Venus V4L2 M2M H264 hardware encoder.
 *
 * Uses /dev/video32 (qcom,vidc) for zero-CPU H264 encoding.
 * Input: NV12/NV21 raw frames from camera
 * Output: H264 NAL units
 */

#ifndef H264_ENCODER_H
#define H264_ENCODER_H

#include <stdint.h>
#include <stddef.h>

/* Initialize the Venus H264 encoder.
 * width, height: frame dimensions
 * fps: target framerate
 * bitrate: target bitrate in bps
 * Returns 0 on success, -1 on error.
 */
int h264_enc_init(int width, int height, int fps, int bitrate);

/* Encode a raw frame (NV12 format) to H264.
 * in_data: raw NV12 frame
 * in_size: input data size
 * out_data: output buffer for H264 NALU
 * out_max: max output buffer size
 * Returns number of bytes written, or -1 on error.
 */
int h264_enc_encode(const uint8_t *in_data, size_t in_size,
                    uint8_t *out_data, size_t out_max);

/* Check if the Venus encoder is available on this device.
 * Returns 1 if available, 0 if not.
 */
int h264_enc_available(void);

/* Clean up encoder resources. */
void h264_enc_cleanup(void);

#endif /* H264_ENCODER_H */
