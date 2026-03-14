/*
 * engine_proxy.h -- Engine-to-anim DGRAM socket proxy.
 *
 * Intercepts the _engine_anim_server_0 Unix DGRAM socket to enable
 * injection of CLAD messages (e.g., StartWakeWordlessStreaming) into
 * the vic-engine → vic-anim IPC channel.
 *
 * Architecture:
 *   vic-engine → [proxy server socket] → [proxy client socket] → vic-anim
 *   vic-anim   → [proxy client socket] → [proxy server socket] → vic-engine
 *                                    ↑
 *                          inject StartWakeWordlessStreaming here
 *
 * After deployment, vic-engine must be restarted once so it connects
 * to the proxy instead of directly to vic-anim.
 */

#ifndef ENGINE_PROXY_H
#define ENGINE_PROXY_H

/* CLAD EngineToRobot tags (from messageEngineToRobot.clad) */
#define CLAD_TAG_START_WAKEWORDLESS     0x99
#define CLAD_TAG_SET_TRIGGER_WORD_RESP  0x9A
#define CLAD_TAG_FAKE_WAKEWORD          0x80

/* CloudMic::StreamType values */
#define STREAM_TYPE_NORMAL           0
#define STREAM_TYPE_BLACKJACK        1
#define STREAM_TYPE_KNOWLEDGE_GRAPH  2

/* Connection handshake packet */
#define ANKICONN_PACKET     "ANKICONN"
#define ANKICONN_PACKET_LEN 8

/* Streaming re-trigger interval (ms).
 * vic-anim's kStreamingTimeout_ms is ~6050ms, so we re-trigger before that. */
#define MIC_STREAM_RETRIGGER_MS  4500

/* Initialize the engine-anim proxy.
 * server_path: e.g., "/dev/socket/_engine_anim_server_0"
 * Returns 0 on success, -1 on error.
 */
int engine_proxy_init(const char *server_path);

/* Run the bidirectional proxy loop (blocks).
 * Forwards all DGRAM messages between vic-engine and vic-anim,
 * and injects StartWakeWordlessStreaming when mic streaming is active.
 */
int engine_proxy_run(void);

/* Start continuous mic streaming.
 * Injects StartWakeWordlessStreaming and keeps re-triggering
 * every MIC_STREAM_RETRIGGER_MS until engine_proxy_stop_mic_stream().
 * Thread-safe.
 */
void engine_proxy_start_mic_stream(void);

/* Stop continuous mic streaming.
 * The current stream will naturally time out after ~6s.
 * Thread-safe.
 */
void engine_proxy_stop_mic_stream(void);

/* Returns 1 if mic streaming is active, 0 otherwise. Thread-safe. */
int engine_proxy_mic_streaming(void);

/* Signal the proxy loop to stop. Thread-safe. */
void engine_proxy_stop(void);

/* Clean up resources and restore original socket path. */
void engine_proxy_cleanup(void);

#endif /* ENGINE_PROXY_H */
