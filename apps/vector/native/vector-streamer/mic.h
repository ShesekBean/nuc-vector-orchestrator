/*
 * mic.h -- Server-side DGRAM proxy for Vector mic audio.
 *
 * Intercepts mic_sock (vic-anim's MicDataSystem server socket) to
 * transparently proxy audio between vic-anim and vic-cloud while
 * extracting PCM samples for Opus encoding and TCP streaming.
 *
 * Architecture:
 *   vic-cloud → proxy(mic_sock) → forwarder(mic_sock_vs) → vic-anim(mic_sock_orig)
 *   vic-anim  → forwarder → extract PCM + forward to vic-cloud via proxy
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

/* Initialize the server-side DGRAM proxy.
 * socket_path: path to the mic socket (e.g., /dev/socket/mic_sock)
 *   - Renames existing socket to socket_path + "_orig"
 *   - Creates proxy server at socket_path (receives from vic-cloud)
 *   - Creates forwarder at socket_path + "_vs" (relays to/from vic-anim)
 * Returns 0 on success, -1 on error.
 */
int mic_init(const char *socket_path, mic_audio_cb callback, void *user_data);

/* Run the DGRAM proxy loop (blocks).
 * Multiplexes proxy_fd and forwarder_fd with select():
 *   - Forwards vic-cloud packets to vic-anim via forwarder
 *   - Receives audio from vic-anim on forwarder, extracts PCM, forwards to vic-cloud
 * Returns on error or when mic_stop() is called.
 */
int mic_run(void);

/* Signal the proxy loop to stop. Thread-safe. */
void mic_stop(void);

/* Clean up resources and restore original socket path. */
void mic_cleanup(void);

#endif /* MIC_H */
