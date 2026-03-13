/*
 * opus_encoder.c — Opus audio encoding wrapper.
 *
 * Links against libopus.so.0 (already on Vector at /anki/lib/).
 */

#include "opus_encoder.h"

#include <stdio.h>
#include <stdlib.h>
#include <opus/opus.h>

#define LOG_TAG "opus"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

static OpusEncoder *g_encoder = NULL;


int opus_enc_init(int sample_rate, int channels, int bitrate)
{
    int error;

    g_encoder = opus_encoder_create(sample_rate, channels,
                                     OPUS_APPLICATION_VOIP, &error);
    if (error != OPUS_OK || !g_encoder) {
        LOG("opus_encoder_create failed: %s", opus_strerror(error));
        return -1;
    }

    /* Configure for low-latency voice */
    opus_encoder_ctl(g_encoder, OPUS_SET_BITRATE(bitrate));
    opus_encoder_ctl(g_encoder, OPUS_SET_COMPLEXITY(5));
    opus_encoder_ctl(g_encoder, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    opus_encoder_ctl(g_encoder, OPUS_SET_INBAND_FEC(1));
    opus_encoder_ctl(g_encoder, OPUS_SET_DTX(1));  /* Discontinuous transmission for silence */

    LOG("Opus encoder initialized: %d Hz, %d ch, %d bps",
        sample_rate, channels, bitrate);
    return 0;
}


int opus_enc_encode(const int16_t *pcm, int frame_size,
                    uint8_t *out, int out_max)
{
    if (!g_encoder) {
        return -1;
    }

    int nbytes = opus_encode(g_encoder, pcm, frame_size, out, out_max);
    if (nbytes < 0) {
        LOG("opus_encode failed: %s", opus_strerror(nbytes));
        return -1;
    }

    return nbytes;
}


void opus_enc_cleanup(void)
{
    if (g_encoder) {
        opus_encoder_destroy(g_encoder);
        g_encoder = NULL;
        LOG("Opus encoder destroyed");
    }
}
