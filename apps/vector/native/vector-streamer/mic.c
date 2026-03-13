/*
 * mic.c — Mic audio tap via mic_sock Unix socket.
 *
 * Connects to /dev/socket/mic_sock and reads CLAD-framed audio data.
 *
 * mic_sock protocol (CLAD CloudMic::Message):
 *   [2 bytes: payload size (LE uint16)]
 *   [2 bytes: message tag  (LE uint16)]
 *   [N bytes: payload]
 *
 * AudioData messages (tag varies by firmware) contain raw int16 PCM
 * at 16kHz mono from the beamformed mic array.
 *
 * We auto-detect the AudioData tag by looking for messages with
 * payload sizes that are multiples of 2 (PCM samples) and within
 * the expected range for 10-30ms audio chunks (160-480 samples).
 */

#include "mic.h"
#include "protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>

#define LOG_TAG "mic"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

/* Expected PCM chunk sizes (samples) for 10/20/30ms at 16kHz */
#define MIN_PCM_SAMPLES  80    /* 5ms */
#define MAX_PCM_SAMPLES  960   /* 60ms */

static int            g_sock_fd = -1;
static volatile int   g_running = 0;
static mic_audio_cb   g_callback = NULL;
static void          *g_user_data = NULL;

/* Auto-detected AudioData message tag */
static uint16_t g_audio_tag = 0;
static int      g_tag_detected = 0;

/* Stats */
static uint64_t g_chunks_received = 0;
static uint64_t g_bytes_received = 0;


static int read_exact(int fd, void *buf, size_t len)
{
    uint8_t *p = (uint8_t *)buf;
    size_t remaining = len;

    while (remaining > 0) {
        ssize_t n = read(fd, p, remaining);
        if (n <= 0) {
            if (n < 0 && errno == EINTR)
                continue;
            return -1;
        }
        p += n;
        remaining -= n;
    }
    return 0;
}


int mic_init(const char *socket_path, mic_audio_cb callback, void *user_data)
{
    g_callback = callback;
    g_user_data = user_data;

    g_sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_sock_fd < 0) {
        LOG("socket() failed: %s", strerror(errno));
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    LOG("Connecting to mic_sock: %s", socket_path);
    if (connect(g_sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOG("connect() failed: %s", strerror(errno));
        LOG("mic_sock may be exclusive to vic-cloud. "
            "Try Alternative B (wire-pod tap) or C (StartWakeWordlessStreaming).");
        close(g_sock_fd);
        g_sock_fd = -1;
        return -1;
    }

    LOG("Connected to mic_sock");
    g_running = 1;
    return 0;
}


static int is_likely_audio(uint16_t tag, const uint8_t *payload, uint16_t payload_size)
{
    /* AudioData payload is raw PCM int16 samples.
     * Expected size: N * 2 bytes where N is 160-480 samples (10-30ms at 16kHz).
     * The payload starts directly with PCM data (no sub-header for AudioData).
     */
    if (payload_size < MIN_PCM_SAMPLES * 2 || payload_size > MAX_PCM_SAMPLES * 2)
        return 0;
    if (payload_size % 2 != 0)
        return 0;

    /* Check for reasonable PCM values — most samples should be within
     * a reasonable range (not all zeros, not all max) */
    const int16_t *samples = (const int16_t *)payload;
    int num_samples = payload_size / 2;
    int nonzero = 0;
    int reasonable = 0;

    for (int i = 0; i < num_samples && i < 50; i++) {
        if (samples[i] != 0) nonzero++;
        if (samples[i] > -20000 && samples[i] < 20000) reasonable++;
    }

    int checked = (num_samples < 50) ? num_samples : 50;
    /* At least some non-zero samples and most are reasonable amplitude */
    return (nonzero > 0 && reasonable > checked / 2);
}


int mic_run(void)
{
    uint8_t header_buf[4];  /* 2 bytes size + 2 bytes tag */
    uint8_t *payload = NULL;
    size_t payload_alloc = 0;

    LOG("Mic read loop started");

    while (g_running) {
        /* Read CLAD message header: [size:2 LE][tag:2 LE] */
        if (read_exact(g_sock_fd, header_buf, 4) < 0) {
            if (g_running)
                LOG("mic_sock read error: %s", strerror(errno));
            break;
        }

        uint16_t msg_size = header_buf[0] | (header_buf[1] << 8);
        uint16_t msg_tag  = header_buf[2] | (header_buf[3] << 8);

        /* msg_size includes the tag (2 bytes) */
        uint16_t payload_size = (msg_size >= 2) ? (msg_size - 2) : 0;

        if (payload_size > 0) {
            /* Ensure payload buffer is large enough */
            if (payload_size > payload_alloc) {
                free(payload);
                payload_alloc = payload_size + 256;
                payload = (uint8_t *)malloc(payload_alloc);
                if (!payload) {
                    LOG("malloc failed");
                    break;
                }
            }

            if (read_exact(g_sock_fd, payload, payload_size) < 0) {
                if (g_running)
                    LOG("mic_sock payload read error: %s", strerror(errno));
                break;
            }
        }

        /* Auto-detect or match AudioData tag */
        if (!g_tag_detected) {
            if (payload_size > 0 && is_likely_audio(msg_tag, payload, payload_size)) {
                g_audio_tag = msg_tag;
                g_tag_detected = 1;
                LOG("Auto-detected AudioData tag: 0x%04x (payload=%u bytes, %u samples)",
                    msg_tag, payload_size, payload_size / 2);
            }
        }

        if (g_tag_detected && msg_tag == g_audio_tag && payload_size > 0) {
            size_t num_samples = payload_size / 2;
            g_chunks_received++;
            g_bytes_received += payload_size;

            if (g_chunks_received <= 5 || g_chunks_received % 1000 == 0) {
                LOG("Audio chunk #%llu: %zu samples",
                    (unsigned long long)g_chunks_received, num_samples);
            }

            if (g_callback) {
                g_callback((const int16_t *)payload, num_samples, g_user_data);
            }
        }
    }

    free(payload);
    LOG("Mic read loop ended (received %llu chunks, %llu bytes)",
        (unsigned long long)g_chunks_received,
        (unsigned long long)g_bytes_received);
    return 0;
}


void mic_stop(void)
{
    g_running = 0;
    if (g_sock_fd >= 0) {
        shutdown(g_sock_fd, SHUT_RDWR);
    }
}


void mic_cleanup(void)
{
    mic_stop();
    if (g_sock_fd >= 0) {
        close(g_sock_fd);
        g_sock_fd = -1;
    }
}
