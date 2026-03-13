/*
 * opus_encoder.h — Opus audio encoding wrapper.
 *
 * Encodes int16 PCM (mono 16kHz) into Opus frames for streaming.
 * Links against libopus.so.0 on Vector.
 */

#ifndef OPUS_ENCODER_H
#define OPUS_ENCODER_H

#include <stdint.h>
#include <stddef.h>

/* Initialize the Opus encoder.
 * sample_rate: input PCM sample rate (16000)
 * channels: number of channels (1)
 * bitrate: target bitrate in bps (24000 recommended for voice)
 * Returns 0 on success, -1 on error.
 */
int opus_enc_init(int sample_rate, int channels, int bitrate);

/* Encode PCM samples to Opus.
 * pcm: input PCM int16 samples (frame_size samples per channel)
 * frame_size: number of samples per channel (320 for 20ms at 16kHz)
 * out: output buffer for encoded Opus data
 * out_max: maximum output buffer size
 * Returns number of bytes written to out, or -1 on error.
 */
int opus_enc_encode(const int16_t *pcm, int frame_size,
                    uint8_t *out, int out_max);

/* Clean up the Opus encoder. */
void opus_enc_cleanup(void);

#endif /* OPUS_ENCODER_H */
