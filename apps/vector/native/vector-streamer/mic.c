/*
 * mic.c -- Server-side DGRAM proxy for Vector mic audio.
 *
 * Intercepts audio flowing from vic-anim's MicDataSystem to vic-cloud
 * by proxying the /dev/socket/mic_sock Unix datagram SERVER socket.
 *
 * Architecture (after proxy setup):
 *
 *   vic-cloud ──DGRAM──► proxy_fd (at mic_sock)
 *                              │
 *                              │ forward via forwarder_fd
 *                              ▼
 *   vic-anim  ◄──DGRAM── forwarder_fd (at mic_sock_vs)
 *                              │
 *                              │ vic-anim stores _client = mic_sock_vs
 *                              ▼
 *   vic-anim ──sendto(mic_sock_vs)──► forwarder_fd
 *                              │
 *                              │ extract PCM → Opus → NUC
 *                              │ forward to vic-cloud via proxy_fd
 *                              ▼
 *   vic-cloud ◄──sendto──── proxy_fd (at mic_sock)
 *
 * Key insights:
 *   - mic_sock is the MicDataSystem server (created by vic-anim)
 *   - mic_sock has _bindClients=false, so it uses sendto() with
 *     the stored _client address (the source of the first packet)
 *   - vic-cloud creates mic_sock_cp_mic and connects to mic_sock
 *   - By proxying the server, we control what address vic-anim
 *     stores as _client (our forwarder), ensuring audio comes to us
 *   - vic-cloud's connected DGRAM socket accepts from mic_sock only,
 *     and our proxy IS mic_sock, so forwarding to vic-cloud works
 *
 * CLAD CloudMic::Message format (NO size prefix — DGRAM framing):
 *   [1 byte:  tag  (uint8)]
 *   [N bytes: payload]
 *
 * AudioData (tag=1) payload: [count:2 LE uint16][samples:count*2 bytes]
 * int16 PCM at 16kHz mono from the beamformed mic array.
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
#include <sys/select.h>

#define LOG_TAG "mic"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

/* Expected PCM chunk sizes (samples) for 5-60ms at 16kHz */
#define MIN_PCM_SAMPLES  80    /* 5ms */
#define MAX_PCM_SAMPLES  960   /* 60ms */

/* Max CLAD datagram size */
#define MAX_DGRAM_SIZE   8192

/* CloudMic::MessageTag values (from generated micTag.h — uint8_t enum) */
#define CLOUDMIC_TAG_HOTWORD       0  /* Hotword — streaming start announcement */
#define CLOUDMIC_TAG_AUDIO         1  /* AudioData — raw PCM int16 samples */
#define CLOUDMIC_TAG_AUDIO_DONE    2  /* Void — streaming end */

/* ANKICONN handshake packet (same as LocalUdpServer::kConnectionPacket) */
#define ANKICONN_PACKET     "ANKICONN"
#define ANKICONN_LEN        8

/* Socket paths */
static char g_server_path[256];      /* our proxy server (replaces mic_sock) */
static char g_orig_path[256];        /* renamed original (mic_sock_orig) */
static char g_forwarder_path[256];   /* our forwarder (mic_sock_vs) */
static int  g_paths_set = 0;
static int  g_socket_renamed = 0;

/* Socket file descriptors */
static int g_proxy_fd = -1;       /* proxy server at mic_sock (receives from vic-cloud) */
static int g_forwarder_fd = -1;   /* forwarder at mic_sock_vs (relays to/from vic-anim) */

static volatile int   g_running = 0;
static volatile int   g_mute_to_cloud = 0;  /* When set, audio is NOT forwarded to vic-cloud */
static mic_audio_cb   g_callback = NULL;
static void          *g_user_data = NULL;

/* vic-cloud address (learned from first packet) */
static struct sockaddr_un g_cloud_addr;
static socklen_t          g_cloud_addr_len = 0;
static int                g_cloud_connected = 0;

/* Stats */
static uint64_t g_chunks_received = 0;
static uint64_t g_bytes_received = 0;
static uint64_t g_cloud_to_anim = 0;
static uint64_t g_anim_to_cloud = 0;


static size_t parse_audio_data(const uint8_t *payload, uint16_t payload_size,
                               const int16_t **pcm_out)
{
    if (payload_size < 4)
        return 0;

    uint16_t count = payload[0] | (payload[1] << 8);
    uint16_t expected_bytes = count * 2;

    if (expected_bytes != payload_size - 2)
        return 0;

    if (count < MIN_PCM_SAMPLES || count > MAX_PCM_SAMPLES)
        return 0;

    *pcm_out = (const int16_t *)(payload + 2);
    return count;
}


static void restore_original_socket(void)
{
    if (!g_socket_renamed)
        return;

    LOG("Restoring original socket: %s -> %s", g_orig_path, g_server_path);
    unlink(g_server_path);

    if (rename(g_orig_path, g_server_path) < 0) {
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
    snprintf(g_server_path, sizeof(g_server_path), "%s", socket_path);
    snprintf(g_orig_path, sizeof(g_orig_path), "%s_orig", socket_path);
    snprintf(g_forwarder_path, sizeof(g_forwarder_path), "%s_vs", socket_path);
    g_paths_set = 1;

    /*
     * Step 1: Rename the original mic_sock server socket.
     * vic-anim must have created it already (startup script ensures this).
     */
    struct stat st;
    if (stat(g_orig_path, &st) == 0) {
        LOG("Original backup exists at %s (previous crash?)", g_orig_path);
        unlink(g_server_path);
        g_socket_renamed = 1;
    } else if (stat(g_server_path, &st) == 0) {
        LOG("Renaming %s -> %s", g_server_path, g_orig_path);
        if (rename(g_server_path, g_orig_path) < 0) {
            unlink(g_orig_path);
            if (rename(g_server_path, g_orig_path) < 0) {
                LOG("FATAL: Cannot rename mic socket: %s", strerror(errno));
                return -1;
            }
        }
        g_socket_renamed = 1;
        LOG("Original socket renamed successfully");
    } else {
        LOG("WARNING: %s does not exist yet — creating proxy anyway", g_server_path);
    }

    /*
     * Step 2: Create proxy server socket at mic_sock.
     * vic-cloud will connect here when it starts.
     */
    g_proxy_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (g_proxy_fd < 0) {
        LOG("socket(proxy) failed: %s", strerror(errno));
        restore_original_socket();
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, g_server_path, sizeof(addr.sun_path) - 1);

    unlink(g_server_path);
    if (bind(g_proxy_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOG("bind(%s) failed: %s", g_server_path, strerror(errno));
        close(g_proxy_fd);
        g_proxy_fd = -1;
        restore_original_socket();
        return -1;
    }
    chmod(g_server_path, 0777);
    LOG("Proxy server socket created at %s", g_server_path);

    /*
     * Step 3: Create forwarder socket.
     * We bind to mic_sock_vs so that when we forward vic-cloud's
     * packets to vic-anim, vic-anim stores _client = mic_sock_vs.
     * Audio sent by vic-anim goes to mic_sock_vs (our forwarder).
     */
    g_forwarder_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (g_forwarder_fd < 0) {
        LOG("socket(forwarder) failed: %s", strerror(errno));
        close(g_proxy_fd);
        g_proxy_fd = -1;
        restore_original_socket();
        return -1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, g_forwarder_path, sizeof(addr.sun_path) - 1);

    unlink(g_forwarder_path);
    if (bind(g_forwarder_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOG("bind(%s) failed: %s", g_forwarder_path, strerror(errno));
        close(g_proxy_fd);
        close(g_forwarder_fd);
        g_proxy_fd = -1;
        g_forwarder_fd = -1;
        restore_original_socket();
        return -1;
    }
    chmod(g_forwarder_path, 0777);
    LOG("Forwarder socket created at %s", g_forwarder_path);

    LOG("Mic proxy initialized");
    LOG("  Proxy server: %s (for vic-cloud)", g_server_path);
    LOG("  Forwarder:    %s (to/from vic-anim)", g_forwarder_path);
    LOG("  Original:     %s (vic-anim's server)", g_orig_path);

    g_running = 1;
    return 0;
}


int mic_run(void)
{
    uint8_t dgram_buf[MAX_DGRAM_SIZE];

    /* vic-anim's original server address (for forwarding) */
    struct sockaddr_un anim_addr;
    memset(&anim_addr, 0, sizeof(anim_addr));
    anim_addr.sun_family = AF_UNIX;
    strncpy(anim_addr.sun_path, g_orig_path, sizeof(anim_addr.sun_path) - 1);

    LOG("Mic server-side proxy loop started");
    LOG("Waiting for vic-cloud to connect...");

    while (g_running) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(g_proxy_fd, &rfds);
        FD_SET(g_forwarder_fd, &rfds);

        int maxfd = (g_proxy_fd > g_forwarder_fd) ? g_proxy_fd : g_forwarder_fd;

        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        int nready = select(maxfd + 1, &rfds, NULL, NULL, &tv);

        if (nready < 0) {
            if (errno == EINTR)
                continue;
            if (!g_running)
                break;
            LOG("select() error: %s", strerror(errno));
            usleep(10000);
            continue;
        }

        /*
         * Handle packets from vic-cloud → forward to vic-anim.
         * Received on proxy_fd (mic_sock), forward via forwarder_fd (mic_sock_vs).
         * vic-anim will store _client = mic_sock_vs (our forwarder's address).
         */
        if (nready > 0 && FD_ISSET(g_proxy_fd, &rfds)) {
            struct sockaddr_un src_addr;
            socklen_t src_len = sizeof(src_addr);

            ssize_t n = recvfrom(g_proxy_fd, dgram_buf, sizeof(dgram_buf), 0,
                                 (struct sockaddr *)&src_addr, &src_len);
            if (n > 0) {
                /* Remember vic-cloud's address for forwarding audio back */
                if (!g_cloud_connected) {
                    memcpy(&g_cloud_addr, &src_addr, src_len);
                    g_cloud_addr_len = src_len;
                    g_cloud_connected = 1;
                    LOG("vic-cloud connected from %s", src_addr.sun_path);
                }

                /* Forward to vic-anim via the forwarder socket.
                 * Source address will be mic_sock_vs (our forwarder),
                 * so vic-anim stores _client = mic_sock_vs. */
                ssize_t sent = sendto(g_forwarder_fd, dgram_buf, n, 0,
                                      (struct sockaddr *)&anim_addr, sizeof(anim_addr));
                if (sent < 0) {
                    if (errno == ENOENT || errno == ECONNREFUSED) {
                        if (g_cloud_to_anim <= 3) {
                            LOG("Forward to vic-anim failed (not ready): %s", strerror(errno));
                        }
                    } else if (errno != EINTR) {
                        LOG("Forward cloud→anim failed: %s", strerror(errno));
                    }
                } else {
                    g_cloud_to_anim++;
                    if (g_cloud_to_anim <= 3 || g_cloud_to_anim % 5000 == 0) {
                        LOG("cloud→anim packet #%llu (%zd bytes)",
                            (unsigned long long)g_cloud_to_anim, n);
                    }

                    /* Check for ANKICONN */
                    if (n == ANKICONN_LEN &&
                        memcmp(dgram_buf, ANKICONN_PACKET, ANKICONN_LEN) == 0) {
                        LOG("Forwarded ANKICONN from vic-cloud to vic-anim");
                    }
                }
            }
        }

        /*
         * Handle packets from vic-anim → extract audio + forward to vic-cloud.
         * Received on forwarder_fd (mic_sock_vs) because vic-anim stored
         * _client = mic_sock_vs.
         */
        if (nready > 0 && FD_ISSET(g_forwarder_fd, &rfds)) {
            ssize_t n = recv(g_forwarder_fd, dgram_buf, sizeof(dgram_buf), 0);
            if (n > 0) {
                g_anim_to_cloud++;

                /* Forward to vic-cloud via proxy socket (source = mic_sock).
                 * vic-cloud's connected DGRAM accepts from mic_sock only,
                 * and our proxy IS mic_sock, so this works.
                 * When muted, we still read (to drain the buffer) but don't forward. */
                if (g_cloud_connected && !g_mute_to_cloud) {
                    ssize_t sent = sendto(g_proxy_fd, dgram_buf, n, 0,
                                          (struct sockaddr *)&g_cloud_addr,
                                          g_cloud_addr_len);
                    if (sent < 0 && errno != EINTR) {
                        if (g_anim_to_cloud <= 3) {
                            LOG("Forward anim→cloud failed: %s", strerror(errno));
                        }
                    }
                }

                if (g_anim_to_cloud <= 3 || g_anim_to_cloud % 5000 == 0) {
                    LOG("anim→cloud packet #%llu (%zd bytes)",
                        (unsigned long long)g_anim_to_cloud, n);
                }

                /*
                 * Parse CLAD CloudMic::Message.
                 * Wire format: [tag:1 uint8][payload:N]
                 * NO size prefix — DGRAM boundaries provide framing.
                 * (Message::Pack writes tag + union data directly)
                 */
                if (n < 1)
                    continue;

                uint8_t  msg_tag  = dgram_buf[0];
                uint16_t payload_size = (uint16_t)(n - 1);
                const uint8_t *payload = dgram_buf + 1;

                if (msg_tag == CLOUDMIC_TAG_AUDIO && payload_size > 0) {
                    const int16_t *pcm;
                    size_t num_samples = parse_audio_data(payload, payload_size, &pcm);

                    if (num_samples > 0) {
                        g_chunks_received++;
                        g_bytes_received += num_samples * 2;

                        if (g_chunks_received <= 5 || g_chunks_received % 1000 == 0) {
                            LOG("Audio chunk #%llu: %zu samples (%u bytes)",
                                (unsigned long long)g_chunks_received, num_samples,
                                payload_size);
                        }

                        if (g_callback) {
                            g_callback(pcm, num_samples, g_user_data);
                        }
                    }
                } else if (msg_tag == CLOUDMIC_TAG_HOTWORD) {
                    LOG("CloudMic: Hotword message (streaming started)");
                } else if (msg_tag == CLOUDMIC_TAG_AUDIO_DONE) {
                    LOG("CloudMic: AudioDone (streaming ended)");
                }
            }
        }
    }

    LOG("Mic proxy loop ended");
    LOG("  cloud→anim: %llu packets", (unsigned long long)g_cloud_to_anim);
    LOG("  anim→cloud: %llu packets", (unsigned long long)g_anim_to_cloud);
    LOG("  audio chunks: %llu (%llu bytes)",
        (unsigned long long)g_chunks_received,
        (unsigned long long)g_bytes_received);
    return 0;
}


void mic_mute_cloud(void)
{
    if (!g_mute_to_cloud) {
        g_mute_to_cloud = 1;
        LOG("Mic→vic-cloud MUTED (wire-pod will not receive audio)");
    }
}

void mic_unmute_cloud(void)
{
    if (g_mute_to_cloud) {
        g_mute_to_cloud = 0;
        LOG("Mic→vic-cloud UNMUTED");
    }
}

int mic_is_cloud_muted(void)
{
    return g_mute_to_cloud;
}

void mic_stop(void)
{
    g_running = 0;
    if (g_proxy_fd >= 0)
        shutdown(g_proxy_fd, SHUT_RDWR);
    if (g_forwarder_fd >= 0)
        shutdown(g_forwarder_fd, SHUT_RDWR);
}


void mic_cleanup(void)
{
    mic_stop();

    if (g_proxy_fd >= 0) {
        close(g_proxy_fd);
        g_proxy_fd = -1;
    }
    if (g_forwarder_fd >= 0) {
        close(g_forwarder_fd);
        g_forwarder_fd = -1;
    }

    /* Remove our sockets */
    if (g_paths_set) {
        unlink(g_server_path);
        unlink(g_forwarder_path);
    }

    /* Restore original socket path */
    restore_original_socket();
}
