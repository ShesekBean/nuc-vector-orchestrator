/*
 * h264_encoder.c — Venus V4L2 M2M H264 hardware encoder.
 *
 * Uses /dev/video32 (qcom,vidc) for hardware H264 encoding.
 * This is a stub implementation — full V4L2 M2M integration
 * requires testing the Venus driver's capabilities on msm8909.
 *
 * The Venus encoder on Snapdragon 212 (msm8909) supports:
 *   - H264 Baseline/Main/High profile encoding
 *   - Input: NV12/NV21
 *   - Output: H264 elementary stream (NAL units)
 *   - Hardware encoding = zero CPU cost
 */

#include "h264_encoder.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>

#define LOG_TAG "h264"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

#define VENUS_DEVICE "/dev/video32"

static int g_venus_fd = -1;


int h264_enc_available(void)
{
    int fd = open(VENUS_DEVICE, O_RDWR);
    if (fd < 0) {
        LOG("Venus device not available: %s", strerror(errno));
        return 0;
    }

    struct v4l2_capability cap;
    memset(&cap, 0, sizeof(cap));
    if (ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {
        LOG("VIDIOC_QUERYCAP failed: %s", strerror(errno));
        close(fd);
        return 0;
    }

    LOG("Venus device: driver=%s card=%s bus=%s caps=0x%08x",
        cap.driver, cap.card, cap.bus_info, cap.capabilities);

    int has_m2m = (cap.capabilities & V4L2_CAP_VIDEO_M2M) ||
                  (cap.capabilities & V4L2_CAP_VIDEO_M2M_MPLANE);

    close(fd);

    if (has_m2m) {
        LOG("Venus M2M encoder available");
    } else {
        LOG("Venus device found but no M2M capability");
    }

    return has_m2m;
}


int h264_enc_init(int width, int height, int fps, int bitrate)
{
    /* Full V4L2 M2M initialization is complex and requires:
     * 1. Open /dev/video32
     * 2. Set output format (NV12, width x height)
     * 3. Set capture format (H264, bitrate)
     * 4. Request buffers on both planes
     * 5. Memory-map buffers
     * 6. Start streaming on both planes
     *
     * This will be implemented after verifying Venus driver
     * capabilities via Phase 0 testing.
     */
    LOG("H264 encoder init (stub): %dx%d @ %dfps, %d bps", width, height, fps, bitrate);
    LOG("Full Venus V4L2 M2M implementation pending driver testing");

    /* Check if Venus is available */
    if (!h264_enc_available()) {
        LOG("Venus encoder not available — will use JPEG passthrough");
        return -1;
    }

    return -1;  /* Stub: not yet implemented */
}


int h264_enc_encode(const uint8_t *in_data, size_t in_size,
                    uint8_t *out_data, size_t out_max)
{
    /* Stub — not yet implemented */
    (void)in_data;
    (void)in_size;
    (void)out_data;
    (void)out_max;
    return -1;
}


void h264_enc_cleanup(void)
{
    if (g_venus_fd >= 0) {
        close(g_venus_fd);
        g_venus_fd = -1;
    }
}
