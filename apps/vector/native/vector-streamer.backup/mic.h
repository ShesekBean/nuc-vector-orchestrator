/*
 * mic.h -- DGRAM proxy for Vector mic audio.
 *
 * Intercepts mic_sock_cp_mic by acting as a proxy between vic-anim
 * and vic-cloud. Extracts AudioData PCM and passes to caller via
 * callback while forwarding all packets transparently.
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

/* Initialize the DGRAM proxy.
 * socket_path: path to the mic socket (e.g., /dev/socket/mic_sock_cp_mic)
 *   - Renames existing socket to socket_path + "_orig"
 *   - Creates new DGRAM socket at socket_path
 * Returns 0 on success, -1 on error.
 */
int mic_init(const char *socket_path, mic_audio_cb callback, void *user_data);

/* Run the DGRAM proxy loop (blocks).
 * Receives packets from vic-anim, forwards to vic-cloud, extracts audio.
 * Returns on error or when mic_stop() is called.
 */
int mic_run(void);

/* Signal the proxy loop to stop. Thread-safe. */
void mic_stop(void);

/* Clean up resources and restore original socket path. */
void mic_cleanup(void);

#endif /* MIC_H */
