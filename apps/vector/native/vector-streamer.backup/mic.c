/*
 * mic.c -- DGRAM proxy for Vector mic audio.
 *
 * Intercepts audio flowing from vic-anim to vic-cloud by proxying
 * the /dev/socket/mic_sock_cp_mic Unix datagram socket:
 *
 *   1. Rename original socket -> mic_sock_cp_mic_orig
 *   2. Create new DGRAM socket at mic_sock_cp_mic
 *   3. Receive packets from vic-anim (recvfrom)
 *   4. Forward each packet to vic-cloud at _orig path (sendto)
 *   5. Parse CLAD header, extract PCM, feed to Opus encoder
 *
 * CLAD CloudMic::Message format:
 *   [2 bytes: payload size (LE uint16)]
 *   [2 bytes: message tag  (LE uint16)]
 *   [N bytes: payload]
 *
 * AudioData messages contain raw int16 PCM at 16kHz mono from
 * the beamformed mic array.
 */

#include "mic.h"
#include "protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>

#define LOG_TAG "mic"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

/* Expected PCM chunk sizes (samples) for 5-60ms at 16kHz */
#define MIN_PCM_SAMPLES  80    /* 5ms */
#define MAX_PCM_SAMPLES  960   /* 60ms */

/* Max CLAD datagram size */
#define MAX_DGRAM_SIZE   8192

/* Socket paths */
static char g_sock_path[256];       /* our proxy socket (replaces original) */
static char g_orig_sock_path[256];  /* renamed original vic-cloud socket */
static int  g_paths_set = 0;
static int  g_socket_renamed = 0;

static int            g_proxy_fd = -1;   /* our DGRAM socket (receives from vic-anim) */
static volatile int   g_running = 0;
static mic_audio_cb   g_callback = NULL;
static void          *g_user_data = NULL;

/* Auto-detected AudioData message tag */
static uint16_t g_audio_tag = 0;
static int      g_tag_detected = 0;

/* Stats */
static uint64_t g_chunks_received = 0;
static uint64_t g_bytes_received = 0;
static uint64_t g_packets_proxied = 0;


static int is_likely_audio(uint16_t tag, const uint8_t *payload, uint16_t payload_size)
{
    /*
     * AudioData payload is raw PCM int16 samples.
     * Expected size: N * 2 bytes where N is 80-960 samples (5-60ms at 16kHz).
     * The payload starts directly with PCM data (no sub-header for AudioData).
     */
    if (payload_size < MIN_PCM_SAMPLES * 2 || payload_size > MAX_PCM_SAMPLES * 2)
        return 0;
    if (payload_size % 2 != 0)
        return 0;

    /* Check for reasonable PCM values -- most samples should be within
     * a reasonable range (not all zeros, not all max) */
    const int16_t *samples = (const int16_t *)payload;
    int num_samples = payload_size / 2;
    int nonzero = 0;
    int reasonable = 0;

    int check_count = (num_samples < 50) ? num_samples : 50;
    for (int i = 0; i < check_count; i++) {
        if (samples[i] != 0) nonzero++;
        if (samples[i] > -20000 && samples[i] < 20000) reasonable++;
    }

    /* At least some non-zero samples and most are reasonable amplitude */
    return (nonzero > 0 && reasonable > check_count / 2);
}


static void restore_original_socket(void)
{
    if (!g_socket_renamed)
        return;

    LOG("Restoring original socket: %s -> %s", g_orig_sock_path, g_sock_path);

    /* Remove our proxy socket */
    unlink(g_sock_path);

    /* Rename the original back */
    if (rename(g_orig_sock_path, g_sock_path) < 0) {
        LOG("WARNING: Failed to restore original socket: %s", strerror(errno));
    } else {
        LOG("Original socket restored successfully");
    }

    g_socket_renamed = 0;
}


int mic_init(const char *socket_path, mic_audio_cb callback, void *user_data)
{
    g_callback = callback;
    g_user_data = user_data;

    /* Build socket paths */
    snprintf(g_sock_path, sizeof(g_sock_path), "%s", socket_path);
    snprintf(g_orig_sock_path, sizeof(g_orig_sock_path), "%s_orig", socket_path);
    g_paths_set = 1;

    /*
     * Step 1: Check if the original socket exists.
     * If _orig already exists (from a previous crash), skip the rename.
     */
    struct stat st;
    if (stat(g_orig_sock_path, &st) == 0) {
        LOG("Original socket backup already exists at %s (previous crash?)", g_orig_sock_path);
        LOG("Removing stale proxy socket at %s", g_sock_path);
        unlink(g_sock_path);
        g_socket_renamed = 1;
    } else if (stat(g_sock_path, &st) == 0) {
        /* Rename original socket to _orig */
        LOG("Renaming %s -> %s", g_sock_path, g_orig_sock_path);
        if (rename(g_sock_path, g_orig_sock_path) < 0) {
            LOG("rename() failed: %s", strerror(errno));
            LOG("vic-cloud may have the socket locked. Retrying after unlink...");
            /* Try removing our target and renaming again */
            unlink(g_orig_sock_path);
            if (rename(g_sock_path, g_orig_sock_path) < 0) {
                LOG("FATAL: Cannot rename mic socket: %s", strerror(errno));
                return -1;
            }
        }
        g_socket_renamed = 1;
        LOG("Original socket renamed successfully");
    } else {
        LOG("WARNING: Original socket %s does not exist yet", g_sock_path);
        LOG("Will create proxy socket and wait for vic-anim to connect");
    }

    /*
     * Step 2: Create DGRAM socket at the original path.
     * vic-anim will send packets here (it doesn't know we replaced the socket).
     */
    g_proxy_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (g_proxy_fd < 0) {
        LOG("socket(SOCK_DGRAM) failed: %s", strerror(errno));
        restore_original_socket();
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, g_sock_path, sizeof(addr.sun_path) - 1);

    /* Remove any existing socket file at this path */
    unlink(g_sock_path);

    if (bind(g_proxy_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOG("bind(%s) failed: %s", g_sock_path, strerror(errno));
        close(g_proxy_fd);
        g_proxy_fd = -1;
        restore_original_socket();
        return -1;
    }

    /* Make socket world-writable so vic-anim (which runs as different user) can write */
    chmod(g_sock_path, 0777);

    LOG("DGRAM proxy socket created at %s", g_sock_path);
    LOG("Forwarding to vic-cloud at %s", g_orig_sock_path);

    g_running = 1;
    return 0;
}


int mic_run(void)
{
    uint8_t dgram_buf[MAX_DGRAM_SIZE];

    /* vic-cloud destination address */
    struct sockaddr_un fwd_addr;
    memset(&fwd_addr, 0, sizeof(fwd_addr));
    fwd_addr.sun_family = AF_UNIX;
    strncpy(fwd_addr.sun_path, g_orig_sock_path, sizeof(fwd_addr.sun_path) - 1);

    LOG("Mic DGRAM proxy loop started");
    LOG("Waiting for packets from vic-anim...");

    while (g_running) {
        /* Receive packet from vic-anim */
        struct sockaddr_un src_addr;
        socklen_t src_len = sizeof(src_addr);

        ssize_t n = recvfrom(g_proxy_fd, dgram_buf, sizeof(dgram_buf), 0,
                             (struct sockaddr *)&src_addr, &src_len);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            if (!g_running)
                break;
            LOG("recvfrom() error: %s", strerror(errno));
            /* Brief sleep to avoid tight error loop */
            usleep(10000);  /* 10ms */
            continue;
        }

        if (n == 0)
            continue;

        g_packets_proxied++;

        /*
         * Forward the raw packet to vic-cloud at the _orig socket.
         * Use sendto since DGRAM sockets are connectionless.
         */
        ssize_t sent = sendto(g_proxy_fd, dgram_buf, n, 0,
                              (struct sockaddr *)&fwd_addr, sizeof(fwd_addr));
        if (sent < 0) {
            if (errno == ENOENT || errno == ECONNREFUSED) {
                /* vic-cloud hasn't created its socket yet, or restarted.
                 * This is normal during startup -- just drop the packet. */
                if (g_packets_proxied <= 5 || g_packets_proxied % 1000 == 0) {
                    LOG("Forward to vic-cloud failed (not ready): %s", strerror(errno));
                }
            } else if (errno != EINTR) {
                LOG("sendto(_orig) error: %s", strerror(errno));
            }
            /* Continue processing -- we still want the audio data even if
             * we can't forward to vic-cloud temporarily. */
        }

        if (g_packets_proxied <= 3 || g_packets_proxied % 5000 == 0) {
            LOG("Proxied packet #%llu (%zd bytes)",
                (unsigned long long)g_packets_proxied, n);
        }

        /*
         * Parse CLAD header: [size:2 LE][tag:2 LE][payload:N]
         * The datagram contains the full CLAD message.
         */
        if (n < 4)
            continue;  /* Too small for CLAD header */

        uint16_t msg_size = dgram_buf[0] | (dgram_buf[1] << 8);
        uint16_t msg_tag  = dgram_buf[2] | (dgram_buf[3] << 8);

        /* msg_size includes the tag (2 bytes), so payload starts at offset 4 */
        uint16_t payload_size = (msg_size >= 2) ? (msg_size - 2) : 0;
        const uint8_t *payload = dgram_buf + 4;

        /* Sanity check: payload_size should match datagram size - 4 */
        if (payload_size > (uint16_t)(n - 4))
            payload_size = (uint16_t)(n - 4);

        if (payload_size == 0)
            continue;

        /* Auto-detect or match AudioData tag */
        if (!g_tag_detected) {
            if (is_likely_audio(msg_tag, payload, payload_size)) {
                g_audio_tag = msg_tag;
                g_tag_detected = 1;
                LOG("Auto-detected AudioData tag: 0x%04x (payload=%u bytes, %u samples)",
                    msg_tag, payload_size, payload_size / 2);
            }
        }

        if (g_tag_detected && msg_tag == g_audio_tag) {
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

    LOG("Mic DGRAM proxy loop ended (proxied %llu packets, received %llu audio chunks, %llu bytes)",
        (unsigned long long)g_packets_proxied,
        (unsigned long long)g_chunks_received,
        (unsigned long long)g_bytes_received);
    return 0;
}


void mic_stop(void)
{
    g_running = 0;
    if (g_proxy_fd >= 0) {
        /* Unblock recvfrom() by shutting down the socket */
        shutdown(g_proxy_fd, SHUT_RDWR);
    }
}


void mic_cleanup(void)
{
    mic_stop();

    if (g_proxy_fd >= 0) {
        close(g_proxy_fd);
        g_proxy_fd = -1;
    }

    /* Remove our proxy socket */
    if (g_paths_set) {
        unlink(g_sock_path);
    }

    /* Restore original socket path */
    restore_original_socket();
}
