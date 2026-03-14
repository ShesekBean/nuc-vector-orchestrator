/*
 * main.c -- vector-streamer entry point.
 *
 * Native binary that runs on Vector to stream mic audio (and eventually
 * H264-encoded camera video) to the NUC over TCP. Also receives PCM
 * audio from NUC for speaker playback via tinyalsa.
 *
 * Architecture:
 *   Main thread:  TCP server accept loop
 *   Mic thread:   DGRAM proxy (vic-anim -> vic-cloud) + Opus encode -> TCP send
 *   Camera thread: (future) camera_client -> H264 encode -> TCP send
 *   Speaker:      TCP recv callback -> tinyalsa playback
 *
 * Usage:
 *   vector-streamer [-p PORT] [-m MIC_SOCK_PATH] [-v]
 *
 * Defaults:
 *   PORT = 5555
 *   MIC_SOCK_PATH = /dev/socket/mic_sock
 */

#include "protocol.h"
#include "mic.h"
#include "opus_encoder.h"
#include "tcp_server.h"
#include "camera.h"
#include "h264_encoder.h"
#include "engine_proxy.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <getopt.h>
#include <errno.h>

#define LOG_TAG "main"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

/* Configuration */
static int         g_port = DEFAULT_TCP_PORT;
static const char *g_mic_path = "/dev/socket/mic_sock";
static const char *g_engine_anim_path = "/dev/socket/_engine_anim_server_0";
static int         g_verbose = 0;
static int         g_enable_camera = 0;  /* Camera disabled by default (NUC handles it) */
static int         g_enable_engine_proxy = 0;  /* Engine proxy disabled by default */

/* Thread handles */
static pthread_t g_mic_thread;
static pthread_t g_camera_thread;
static pthread_t g_engine_proxy_thread;
static int       g_mic_thread_started = 0;
static int       g_camera_thread_started = 0;
static int       g_engine_proxy_thread_started = 0;

/* Opus encoding buffer — accumulates PCM until we have a full frame */
static int16_t g_opus_pcm_buf[OPUS_FRAME_SAMPLES];
static int     g_opus_pcm_pos = 0;
static pthread_mutex_t g_opus_mutex = PTHREAD_MUTEX_INITIALIZER;

/* Speaker playback state */
static volatile int g_speaker_active = 0;


/* ── Signal handling ─────────────────────────────────────────── */

static volatile int g_shutdown = 0;

static void signal_handler(int sig)
{
    (void)sig;
    LOG("Signal %d received, shutting down...", sig);
    g_shutdown = 1;
    tcp_server_stop();
    mic_stop();
    camera_stop();
    engine_proxy_stop();
}


/* ── Mic audio callback ─────────────────────────────────────── */

static void on_mic_audio(const int16_t *pcm_data, size_t num_samples, void *user_data)
{
    (void)user_data;

    if (!tcp_server_has_client())
        return;

    pthread_mutex_lock(&g_opus_mutex);

    const int16_t *src = pcm_data;
    size_t remaining = num_samples;

    while (remaining > 0) {
        /* Fill the Opus frame buffer */
        size_t space = OPUS_FRAME_SAMPLES - g_opus_pcm_pos;
        size_t copy = (remaining < space) ? remaining : space;

        memcpy(&g_opus_pcm_buf[g_opus_pcm_pos], src, copy * sizeof(int16_t));
        g_opus_pcm_pos += copy;
        src += copy;
        remaining -= copy;

        /* Encode and send when we have a full frame */
        if (g_opus_pcm_pos >= OPUS_FRAME_SAMPLES) {
            uint8_t opus_out[OPUS_MAX_PACKET];
            int nbytes = opus_enc_encode(g_opus_pcm_buf, OPUS_FRAME_SAMPLES,
                                         opus_out, sizeof(opus_out));
            if (nbytes > 0) {
                tcp_server_send(FRAME_TYPE_OPUS, opus_out, nbytes);
            }
            g_opus_pcm_pos = 0;
        }
    }

    pthread_mutex_unlock(&g_opus_mutex);
}


/* ── Camera frame callback ───────────────────────────────────── */

static void on_camera_frame(const uint8_t *data, size_t length,
                            int is_h264, void *user_data)
{
    (void)user_data;

    if (!tcp_server_has_client())
        return;

    uint8_t type = is_h264 ? FRAME_TYPE_H264 : FRAME_TYPE_JPEG;
    tcp_server_send(type, data, length);
}


/* ── TCP receive callback (NUC → Vector) ─────────────────────── */

static void on_tcp_recv(uint8_t type, const uint8_t *data, uint32_t length,
                        void *user_data)
{
    (void)user_data;

    switch (type) {
    case FRAME_TYPE_PCM:
        /* TODO: Play PCM via tinyalsa on /dev/snd/pcmC0D0p
         * For now, log receipt. Speaker playback will be added
         * when tinyalsa integration is tested.
         */
        if (g_verbose) {
            LOG("Received PCM from NUC: %u bytes", length);
        }
        break;

    case FRAME_TYPE_CMD:
        if (length >= 1) {
            uint8_t cmd = data[0];
            switch (cmd) {
            case CMD_MIC_STREAM_START:
                LOG("NUC command: start mic streaming");
                if (g_enable_engine_proxy) {
                    engine_proxy_start_mic_stream();
                } else {
                    LOG("WARNING: engine proxy not enabled (-e flag)");
                }
                break;
            case CMD_MIC_STREAM_STOP:
                LOG("NUC command: stop mic streaming");
                if (g_enable_engine_proxy) {
                    engine_proxy_stop_mic_stream();
                } else {
                    LOG("WARNING: engine proxy not enabled (-e flag)");
                }
                break;
            default:
                LOG("Unknown command from NUC: 0x%02x", cmd);
                break;
            }
        }
        break;

    default:
        if (g_verbose) {
            LOG("Unknown frame type from NUC: 0x%02x (%u bytes)", type, length);
        }
        break;
    }
}


/* ── Thread entry points ─────────────────────────────────────── */

static void *mic_thread_func(void *arg)
{
    (void)arg;
    LOG("Mic thread started (DGRAM proxy mode)");

    if (mic_init(g_mic_path, on_mic_audio, NULL) < 0) {
        LOG("Mic init failed — mic audio will not be available");
        LOG("Hint: ensure vic-anim is running and %s exists", g_mic_path);
        return NULL;
    }

    mic_run();
    mic_cleanup();
    LOG("Mic thread exited");
    return NULL;
}


static void *camera_thread_func(void *arg)
{
    (void)arg;
    LOG("Camera thread started");

    if (camera_init(on_camera_frame, NULL) < 0) {
        LOG("Camera init failed");
        return NULL;
    }

    camera_run();
    camera_cleanup();
    LOG("Camera thread exited");
    return NULL;
}


static void *engine_proxy_thread_func(void *arg)
{
    (void)arg;
    LOG("Engine proxy thread started");

    if (engine_proxy_init(g_engine_anim_path) < 0) {
        LOG("Engine proxy init failed — mic streaming control unavailable");
        LOG("Hint: ensure vic-anim is running and %s exists", g_engine_anim_path);
        return NULL;
    }

    engine_proxy_run();
    engine_proxy_cleanup();
    LOG("Engine proxy thread exited");
    return NULL;
}


/* ── Usage ───────────────────────────────────────────────────── */

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [OPTIONS]\n"
        "\n"
        "DGRAM proxy for mic audio + TCP streamer to NUC.\n"
        "\n"
        "Options:\n"
        "  -p PORT    TCP port (default: %d)\n"
        "  -m PATH    mic_sock path (default: %s)\n"
        "  -e         Enable engine-anim proxy (for mic streaming control)\n"
        "  -E PATH    engine_anim_server path (default: %s)\n"
        "  -c         Enable camera capture (disabled by default)\n"
        "  -v         Verbose logging\n"
        "  -h         Show this help\n"
        "\n"
        "The NUC connects to this TCP server to receive audio/video\n"
        "and send speaker PCM for playback.\n"
        "\n"
        "The -e flag enables proxying the engine-anim socket so the NUC\n"
        "can trigger continuous mic streaming (for LiveKit calls).\n"
        "After first enabling, restart vic-engine once:\n"
        "  systemctl restart vic-engine\n",
        prog, DEFAULT_TCP_PORT, g_mic_path, g_engine_anim_path);
}


/* ── Main ────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    int opt;
    while ((opt = getopt(argc, argv, "p:m:eE:cvh")) != -1) {
        switch (opt) {
        case 'p':
            g_port = atoi(optarg);
            break;
        case 'm':
            g_mic_path = optarg;
            break;
        case 'e':
            g_enable_engine_proxy = 1;
            break;
        case 'E':
            g_engine_anim_path = optarg;
            g_enable_engine_proxy = 1;
            break;
        case 'c':
            g_enable_camera = 1;
            break;
        case 'v':
            g_verbose = 1;
            break;
        case 'h':
            usage(argv[0]);
            return 0;
        default:
            usage(argv[0]);
            return 1;
        }
    }

    LOG("vector-streamer starting");
    LOG("  TCP port:       %d", g_port);
    LOG("  Mic socket:     %s", g_mic_path);
    LOG("  Engine proxy:   %s", g_enable_engine_proxy ? "enabled" : "disabled");
    if (g_enable_engine_proxy)
        LOG("  Engine socket:  %s", g_engine_anim_path);
    LOG("  Camera:         %s", g_enable_camera ? "enabled" : "disabled (NUC handles it)");
    LOG("  Verbose:        %s", g_verbose ? "yes" : "no");

    /* Install signal handlers */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGPIPE, SIG_IGN);

    /* Initialize Opus encoder */
    if (opus_enc_init(MIC_SAMPLE_RATE, MIC_CHANNELS, 24000) < 0) {
        LOG("FATAL: Opus encoder init failed");
        return 1;
    }

    /* Initialize TCP server */
    if (tcp_server_init(g_port, on_tcp_recv, NULL) < 0) {
        LOG("FATAL: TCP server init failed");
        opus_enc_cleanup();
        return 1;
    }

    /* Start mic thread */
    if (pthread_create(&g_mic_thread, NULL, mic_thread_func, NULL) == 0) {
        g_mic_thread_started = 1;
    } else {
        LOG("Failed to create mic thread: %s", strerror(errno));
    }

    /* Start engine proxy thread (optional — for mic streaming control) */
    if (g_enable_engine_proxy) {
        if (pthread_create(&g_engine_proxy_thread, NULL, engine_proxy_thread_func, NULL) == 0) {
            g_engine_proxy_thread_started = 1;
        } else {
            LOG("Failed to create engine proxy thread: %s", strerror(errno));
        }
    }

    /* Start camera thread (optional) */
    if (g_enable_camera) {
        if (pthread_create(&g_camera_thread, NULL, camera_thread_func, NULL) == 0) {
            g_camera_thread_started = 1;
        } else {
            LOG("Failed to create camera thread: %s", strerror(errno));
        }
    }

    /* Run TCP server in main thread (blocks until shutdown) */
    LOG("vector-streamer ready — waiting for NUC connection on port %d", g_port);
    tcp_server_run();

    /* Cleanup */
    LOG("Shutting down...");

    mic_stop();
    camera_stop();
    engine_proxy_stop();

    if (g_mic_thread_started) {
        pthread_join(g_mic_thread, NULL);
    }
    if (g_engine_proxy_thread_started) {
        pthread_join(g_engine_proxy_thread, NULL);
    }
    if (g_camera_thread_started) {
        pthread_join(g_camera_thread, NULL);
    }

    tcp_server_cleanup();
    opus_enc_cleanup();

    LOG("vector-streamer stopped");
    return 0;
}
