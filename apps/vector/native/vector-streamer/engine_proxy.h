/*
 * engine_proxy.h -- Engine-to-anim DGRAM socket proxy.
 *
 * Intercepts the _engine_anim_server_0 Unix DGRAM socket to enable
 * injection of CLAD messages into the vic-engine ↔ vic-anim IPC channel.
 *
 * Architecture (after proxy setup):
 *   vic-engine (_engine_anim_client_0)
 *       → proxy server (_engine_anim_server_0)
 *       → proxy client (_engine_anim_client_vs)
 *       → vic-anim (_engine_anim_server_0_orig)
 *                   ↑
 *         inject SetMicBroadcastMode + StartWakeWordlessStreaming here
 *
 * Broadcast mode: Instead of re-injecting StartWakeWordlessStreaming
 * every few seconds (keepalive hack), we inject SetMicBroadcastMode(1)
 * which tells vic-anim to disable the kStreamingTimeout_ms check.
 * Audio streams indefinitely until SetMicBroadcastMode(0) is sent.
 *
 * Uses deferred ANKICONN: proxy waits for vic-engine's handshake and
 * forwards it to vic-anim, preserving the natural boot timing.
 * systemd Before= ordering ensures vic-engine starts after the proxy.
 */

#ifndef ENGINE_PROXY_H
#define ENGINE_PROXY_H

/* CLAD EngineToRobot tags (from messageEngineToRobot.clad) */
#define CLAD_TAG_SET_MIC_BROADCAST_MODE 0x93
#define CLAD_TAG_START_WAKEWORDLESS     0x99
#define CLAD_TAG_SET_TRIGGER_WORD_RESP  0x9A
#define CLAD_TAG_FAKE_WAKEWORD          0x80

/* SetMicBroadcastMode mode values */
#define MIC_BROADCAST_MODE_NORMAL    0x00
#define MIC_BROADCAST_MODE_BROADCAST 0x01

/* CloudMic::StreamType values */
#define STREAM_TYPE_NORMAL           0
#define STREAM_TYPE_BLACKJACK        1
#define STREAM_TYPE_KNOWLEDGE_GRAPH  2

/* Connection handshake packet */
#define ANKICONN_PACKET     "ANKICONN"
#define ANKICONN_PACKET_LEN 8

/* Initialize the engine-anim proxy.
 * server_path: e.g., "/dev/socket/_engine_anim_server_0"
 * Returns 0 on success, -1 on error.
 */
int engine_proxy_init(const char *server_path);

/* Run the bidirectional proxy loop (blocks).
 * Forwards all DGRAM messages between vic-engine and vic-anim.
 */
int engine_proxy_run(void);

/* Start continuous mic streaming.
 * Injects SetMicBroadcastMode(broadcast) + single StartWakeWordlessStreaming.
 * No keepalive timer needed — broadcast mode disables vic-anim's timeout.
 * Thread-safe.
 */
void engine_proxy_start_mic_stream(void);

/* Stop continuous mic streaming.
 * Injects SetMicBroadcastMode(normal) to restore timeout behavior.
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
