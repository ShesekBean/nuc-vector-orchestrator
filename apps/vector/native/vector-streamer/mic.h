/*
 * mic.h — Mic audio tap via mic_sock Unix socket.
 *
 * Reads CLAD CloudMic::Message frames from vic-anim's mic_sock,
 * extracts AudioData PCM, and passes it to the caller.
 */

#ifndef MIC_H
#define MIC_H

#include <stdint.h>
#include <stddef.h>

/* Callback invoked with each PCM audio chunk.
 * pcm_data: int16_t samples, mono, 16kHz
 * num_samples: number of samples in pcm_data
 */
typedef void (*mic_audio_cb)(const int16_t *pcm_data, size_t num_samples, void *user_data);

/* Initialize mic tap. Returns 0 on success, -1 on error. */
int mic_init(const char *socket_path, mic_audio_cb callback, void *user_data);

/* Run the mic read loop (blocks). Returns on error or when mic_stop() is called. */
int mic_run(void);

/* Signal the mic loop to stop. Thread-safe. */
void mic_stop(void);

/* Clean up mic resources. */
void mic_cleanup(void);

#endif /* MIC_H */
