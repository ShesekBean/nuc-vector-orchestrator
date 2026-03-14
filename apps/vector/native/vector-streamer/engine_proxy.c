/*
 * engine_proxy.c -- Engine-to-anim DGRAM socket proxy.
 *
 * Proxies the _engine_anim_server_0 Unix DGRAM socket to intercept
 * the vic-engine ↔ vic-anim IPC channel. This allows injection of
 * CLAD messages (SetTriggerWordResponse + StartWakeWordlessStreaming)
 * to trigger continuous mic audio streaming during LiveKit calls.
 *
 * Socket architecture (after proxy setup):
 *
 *   vic-engine  ──DGRAM──►  proxy_server_fd   (at _engine_anim_server_0)
 *                               │
 *                               │ forward via sendto(proxy_client_fd)
 *                               ▼
 *   vic-anim    ◄──DGRAM──  proxy_client_fd   (at _engine_anim_client_vs)
 *                               │
 *                               │ inject SetTriggerWordResponse + StartWakeWordlessStreaming
 *                               ▼
 *                         [when mic streaming requested by NUC]
 *
 * Handshake sequence (deferred ANKICONN):
 *
 *   During init:
 *     1. Rename vic-anim's server socket (_engine_anim_server_0 → _orig)
 *     2. Create proxy server at _engine_anim_server_0
 *     3. Create proxy client at _engine_anim_client_vs
 *     4. Do NOT send ANKICONN yet — wait for vic-engine
 *
 *   At runtime (after vic-engine starts via systemd Before= ordering):
 *     5. vic-engine sends ANKICONN to our proxy server
 *     6. We forward ANKICONN from proxy client to vic-anim's original server
 *     7. vic-anim receives ANKICONN, connect()s to proxy client
 *     8. vic-anim starts sending RobotToEngine messages via send()
 *     9. We receive on proxy client, forward to vic-engine via proxy server
 *    10. vic-engine receives initial messages, considers channel "up"
 *    11. vic-engine starts sending EngineToRobot messages
 *    12. We forward them to vic-anim — full bidirectional proxy active
 *
 *   This preserves the exact same handshake sequence as normal boot.
 *   vic-anim only starts sending after receiving ANKICONN from vic-engine
 *   (forwarded through us), so no messages are dropped.
 *
 * Key constraints:
 *   - vic-anim's LocalUdpServer (_bindClients=true) calls connect() on
 *     the first client that sends ANKICONN, then ONLY accepts datagrams
 *     from that peer (kernel DGRAM peer filtering).
 *   - Our proxy client MUST be the address that sends ANKICONN to vic-anim.
 *     We use _engine_anim_client_vs (not _client_0) to avoid conflicting
 *     with vic-engine's own client socket at _engine_anim_client_0.
 *
 * CLAD EngineToRobot wire format:
 *   [tag:1 byte (uint8)][payload bytes]
 */

#include "engine_proxy.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/select.h>

#define LOG_TAG "engine_proxy"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

/* Max DGRAM message size (EngineToRobot max is ~1219 bytes) */
#define MAX_DGRAM_SIZE   2048

/* Max time to wait for server socket to appear (seconds) */
#define SOCKET_WAIT_TIMEOUT  30

/* Socket paths */
static char g_server_path[256];       /* our proxy server (replaces _engine_anim_server_0) */
static char g_orig_server_path[256];  /* renamed original (_engine_anim_server_0_orig) */
static char g_client_path[256];       /* our proxy client (_engine_anim_client_vs) */
static int  g_paths_set = 0;
static int  g_socket_renamed = 0;

/* Socket file descriptors */
static int g_server_fd = -1;   /* proxy server: receives from vic-engine */
static int g_client_fd = -1;   /* proxy client: sends to vic-anim's original server */

/* Destination address for sendto() to vic-anim */
static struct sockaddr_un g_anim_addr;
static socklen_t          g_anim_addr_len = 0;
static int                g_anim_connected = 0;

/* State */
static volatile int g_running = 0;
static volatile int g_mic_streaming = 0;

/* Peer tracking: remember vic-engine's address for sending replies */
static struct sockaddr_un g_engine_addr;
static socklen_t          g_engine_addr_len = 0;
static int                g_engine_connected = 0;

/* Track whether we've forwarded ANKICONN to vic-anim */
static int g_ankiconn_forwarded = 0;

/* Stats */
static uint64_t g_msgs_engine_to_anim = 0;
static uint64_t g_msgs_anim_to_engine = 0;
static uint64_t g_msgs_injected = 0;


static void restore_original_socket(void)
{
    if (!g_socket_renamed)
        return;

    LOG("Restoring original socket: %s -> %s", g_orig_server_path, g_server_path);

    /* Remove our proxy socket */
    unlink(g_server_path);

    /* Rename the original back */
    if (rename(g_orig_server_path, g_server_path) < 0) {
        LOG("WARNING: Failed to restore original socket: %s", strerror(errno));
    } else {
        LOG("Original socket restored successfully");
    }

    g_socket_renamed = 0;
}


static uint64_t time_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}


/* Send a message to vic-anim via sendto() */
static ssize_t send_to_anim(const uint8_t *data, size_t len)
{
    if (!g_anim_connected)
        return -1;
    return sendto(g_client_fd, data, len, 0,
                  (struct sockaddr *)&g_anim_addr, g_anim_addr_len);
}


/*
 * Wait for a socket file to appear (polling).
 * Returns 0 when found, -1 on timeout or shutdown.
 */
static int wait_for_socket(const char *path, int timeout_sec)
{
    struct stat st;
    int elapsed = 0;

    while (g_running && elapsed < timeout_sec) {
        if (stat(path, &st) == 0 && S_ISSOCK(st.st_mode)) {
            return 0;
        }
        usleep(200000); /* 200ms */
        elapsed++;
        if (elapsed % 5 == 0) {
            LOG("Waiting for %s... (%ds)", path, elapsed);
        }
    }

    LOG("Timeout waiting for %s after %ds", path, timeout_sec);
    return -1;
}


/*
 * Inject SetTriggerWordResponse (tag 0x9A) so that
 * HasValidTriggerResponse() returns true.
 *
 * Wire format (25 bytes = 1 tag + 24 payload):
 *   [0x9A]
 *   [gameObject:uint64_t LE]       = 1 (Default)
 *   [audioEvent:uint32_t LE]       = Play__Dev_Robot__External_Source
 *   [callbackId:uint16_t LE]       = 0
 *   [padding:uint16_t LE]          = 0
 *   [minStreamingDuration_ms:int32_t LE] = -1
 *   [shouldTriggerWordStartStream:uint8] = 1
 *   [shouldTriggerWordSimulateStream:uint8] = 0
 *   [getInAnimationTag:uint8]      = 0
 *   [getInAnimationName_length:uint8] = 0
 */
static int inject_set_trigger_word_response(void)
{
    uint8_t msg[25];
    int pos = 0;

    /* Tag */
    msg[pos++] = CLAD_TAG_SET_TRIGGER_WORD_RESP;

    /* PostAudioEvent.gameObject = Default (1), uint64_t LE */
    uint64_t gameObject = 1;
    memcpy(&msg[pos], &gameObject, 8); pos += 8;

    /* PostAudioEvent.audioEvent = Play__Dev_Robot__External_Source (2539447680), uint32_t LE */
    uint32_t audioEvent = 2539447680U;
    memcpy(&msg[pos], &audioEvent, 4); pos += 4;

    /* PostAudioEvent.callbackId = 0, uint16_t LE */
    uint16_t callbackId = 0;
    memcpy(&msg[pos], &callbackId, 2); pos += 2;

    /* PostAudioEvent.padding = 0, uint16_t LE */
    uint16_t padding = 0;
    memcpy(&msg[pos], &padding, 2); pos += 2;

    /* minStreamingDuration_ms = -1, int32_t LE */
    int32_t minDuration = -1;
    memcpy(&msg[pos], &minDuration, 4); pos += 4;

    /* shouldTriggerWordStartStream = 1 */
    msg[pos++] = 1;

    /* shouldTriggerWordSimulateStream = 0 */
    msg[pos++] = 0;

    /* getInAnimationTag = 0 */
    msg[pos++] = 0;

    /* getInAnimationName_length = 0 (empty string) */
    msg[pos++] = 0;

    ssize_t sent = send_to_anim(msg, pos);
    if (sent < 0) {
        LOG("Failed to inject SetTriggerWordResponse: %s", strerror(errno));
        return -1;
    }

    LOG("Injected SetTriggerWordResponse (%d bytes) — audioEvent=0x%08x",
        pos, audioEvent);
    return 0;
}


static int inject_start_wakewordless(void)
{
    /*
     * CLAD EngineToRobot::StartWakeWordlessStreaming
     * Wire format: [tag:0x99][streamType:0x00][playGetIn:0x00]
     */
    uint8_t msg[3] = {
        CLAD_TAG_START_WAKEWORDLESS,  /* tag */
        STREAM_TYPE_NORMAL,           /* streamType = Normal */
        0x00                          /* playGetInFromAnimProcess = false */
    };

    ssize_t sent = send_to_anim(msg, sizeof(msg));
    if (sent < 0) {
        LOG("Failed to inject StartWakeWordlessStreaming: %s", strerror(errno));
        return -1;
    }

    g_msgs_injected++;
    if (g_msgs_injected <= 3 || g_msgs_injected % 100 == 0) {
        LOG("Injected StartWakeWordlessStreaming #%llu",
            (unsigned long long)g_msgs_injected);
    }
    return 0;
}


int engine_proxy_init(const char *server_path)
{
    /* Build socket paths */
    snprintf(g_server_path, sizeof(g_server_path), "%s", server_path);
    snprintf(g_orig_server_path, sizeof(g_orig_server_path), "%s_orig", server_path);
    snprintf(g_client_path, sizeof(g_client_path),
             "/dev/socket/_engine_anim_client_vs");
    g_paths_set = 1;
    g_running = 1;

    /*
     * Step 1: Wait for the server socket to appear.
     * vic-anim must create it first. If it already exists, proceed.
     */
    LOG("Checking for engine-anim server socket...");
    struct stat st;

    if (stat(g_orig_server_path, &st) == 0) {
        /* Previous run's _orig exists — reuse it */
        LOG("Previous _orig socket found at %s", g_orig_server_path);
        LOG("Removing stale proxy socket at %s", g_server_path);
        unlink(g_server_path);
        g_socket_renamed = 1;
    } else if (stat(g_server_path, &st) == 0) {
        /* Fresh server socket from vic-anim */
        LOG("Renaming %s -> %s", g_server_path, g_orig_server_path);
        if (rename(g_server_path, g_orig_server_path) < 0) {
            LOG("rename() failed: %s", strerror(errno));
            unlink(g_orig_server_path);
            if (rename(g_server_path, g_orig_server_path) < 0) {
                LOG("FATAL: Cannot rename engine-anim socket: %s", strerror(errno));
                return -1;
            }
        }
        g_socket_renamed = 1;
        LOG("Original socket renamed successfully");
    } else {
        /* Socket doesn't exist yet — wait for vic-anim */
        LOG("Waiting for vic-anim to create %s...", g_server_path);
        if (wait_for_socket(g_server_path, SOCKET_WAIT_TIMEOUT) < 0) {
            LOG("FATAL: vic-anim server socket never appeared");
            return -1;
        }
        /* Now rename it */
        LOG("Renaming %s -> %s", g_server_path, g_orig_server_path);
        if (rename(g_server_path, g_orig_server_path) < 0) {
            LOG("FATAL: Cannot rename engine-anim socket: %s", strerror(errno));
            return -1;
        }
        g_socket_renamed = 1;
        LOG("Original socket renamed successfully");
    }

    /* Build the destination address for vic-anim's original server */
    memset(&g_anim_addr, 0, sizeof(g_anim_addr));
    g_anim_addr.sun_family = AF_UNIX;
    strncpy(g_anim_addr.sun_path, g_orig_server_path, sizeof(g_anim_addr.sun_path) - 1);
    g_anim_addr_len = sizeof(g_anim_addr);
    g_anim_connected = 1;

    /*
     * Step 2: Create proxy server socket at the original path.
     * vic-engine will send to this address when it starts.
     */
    g_server_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (g_server_fd < 0) {
        LOG("socket(server) failed: %s", strerror(errno));
        restore_original_socket();
        return -1;
    }

    struct sockaddr_un server_addr;
    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sun_family = AF_UNIX;
    strncpy(server_addr.sun_path, g_server_path, sizeof(server_addr.sun_path) - 1);

    unlink(g_server_path);
    if (bind(g_server_fd, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        LOG("bind(%s) failed: %s", g_server_path, strerror(errno));
        close(g_server_fd);
        g_server_fd = -1;
        restore_original_socket();
        return -1;
    }
    chmod(g_server_path, 0777);
    LOG("Proxy server socket created at %s", g_server_path);

    /*
     * Step 3: Create proxy client socket.
     *
     * We use _engine_anim_client_vs (NOT _client_0) to avoid conflicting
     * with vic-engine's own client socket at _engine_anim_client_0.
     *
     * We do NOT send ANKICONN yet — we wait for vic-engine to send its
     * ANKICONN first, then forward it. This preserves the natural handshake
     * timing so vic-anim's initial response messages arrive AFTER
     * vic-engine is connected and ready to receive them through our proxy.
     */
    g_client_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (g_client_fd < 0) {
        LOG("socket(client) failed: %s", strerror(errno));
        close(g_server_fd);
        g_server_fd = -1;
        restore_original_socket();
        return -1;
    }

    struct sockaddr_un client_addr;
    memset(&client_addr, 0, sizeof(client_addr));
    client_addr.sun_family = AF_UNIX;
    strncpy(client_addr.sun_path, g_client_path, sizeof(client_addr.sun_path) - 1);

    unlink(g_client_path);
    if (bind(g_client_fd, (struct sockaddr *)&client_addr, sizeof(client_addr)) < 0) {
        LOG("bind(%s) failed: %s", g_client_path, strerror(errno));
        close(g_server_fd);
        close(g_client_fd);
        g_server_fd = -1;
        g_client_fd = -1;
        restore_original_socket();
        return -1;
    }
    chmod(g_client_path, 0777);
    LOG("Proxy client socket bound to %s", g_client_path);

    /* NOTE: No ANKICONN sent here — deferred until vic-engine connects */

    LOG("Engine-anim proxy initialized (ANKICONN deferred)");
    LOG("  Proxy server: %s (for vic-engine)", g_server_path);
    LOG("  Proxy client: %s (to vic-anim)", g_client_path);
    LOG("  Original:     %s (vic-anim's server)", g_orig_server_path);
    return 0;
}


int engine_proxy_run(void)
{
    uint8_t buf[MAX_DGRAM_SIZE];
    uint64_t last_inject_ms = 0;

    LOG("Engine-anim proxy loop started");
    LOG("Waiting for vic-engine to connect and send ANKICONN...");

    while (g_running) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(g_server_fd, &rfds);

        /* Only listen on client_fd after ANKICONN has been forwarded to vic-anim.
         * Before that, vic-anim hasn't connected to us, so nothing to receive. */
        if (g_ankiconn_forwarded) {
            FD_SET(g_client_fd, &rfds);
        }

        int maxfd = (g_server_fd > g_client_fd) ? g_server_fd : g_client_fd;

        /* Timeout for injection timer — wake up every 500ms to check */
        struct timeval tv = { .tv_sec = 0, .tv_usec = 500000 };

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

        /* Handle messages from vic-engine → forward to vic-anim */
        if (nready > 0 && FD_ISSET(g_server_fd, &rfds)) {
            struct sockaddr_un src_addr;
            socklen_t src_len = sizeof(src_addr);
            memset(&src_addr, 0, sizeof(src_addr));

            ssize_t n = recvfrom(g_server_fd, buf, sizeof(buf), 0,
                                  (struct sockaddr *)&src_addr, &src_len);
            if (n > 0) {
                /* Remember vic-engine's address for sending replies */
                if (!g_engine_connected) {
                    memcpy(&g_engine_addr, &src_addr, src_len);
                    g_engine_addr_len = src_len;
                    g_engine_connected = 1;
                    LOG("vic-engine connected from %s", src_addr.sun_path);
                }

                /* Check for ANKICONN handshake */
                if (n == ANKICONN_PACKET_LEN &&
                    memcmp(buf, ANKICONN_PACKET, ANKICONN_PACKET_LEN) == 0) {
                    LOG("Received ANKICONN from vic-engine — forwarding to vic-anim");

                    /* Forward ANKICONN to vic-anim. This is the FIRST message
                     * vic-anim receives on this channel, triggering:
                     *   1. AddClient() → connect() to our proxy client
                     *   2. vic-anim starts sending RobotToEngine messages
                     * Since g_engine_connected is now true, those responses
                     * will be immediately forwarded to vic-engine. */
                    ssize_t sent = send_to_anim(buf, n);
                    if (sent < 0) {
                        LOG("FATAL: Failed to forward ANKICONN to vic-anim: %s",
                            strerror(errno));
                        LOG("vic-anim's server may have already connected to another client");
                    } else {
                        g_ankiconn_forwarded = 1;
                        LOG("ANKICONN forwarded — vic-anim should now connect to proxy client");
                    }
                    g_msgs_engine_to_anim++;
                    continue;
                }

                /* Forward regular messages to vic-anim via sendto() */
                ssize_t sent = send_to_anim(buf, n);
                if (sent < 0) {
                    if (errno != EINTR) {
                        LOG("Forward to vic-anim failed: %s (tag=0x%02x, %zd bytes)",
                            strerror(errno), buf[0], n);
                    }
                } else {
                    g_msgs_engine_to_anim++;
                    if (g_msgs_engine_to_anim <= 10 || g_msgs_engine_to_anim % 10000 == 0) {
                        LOG("engine→anim msg #%llu (%zd bytes, tag=0x%02x)",
                            (unsigned long long)g_msgs_engine_to_anim, n,
                            (n > 0) ? buf[0] : 0);
                    }
                }
            }
        }

        /* Handle replies from vic-anim → forward to vic-engine */
        if (g_ankiconn_forwarded && nready > 0 && FD_ISSET(g_client_fd, &rfds)) {
            ssize_t n = recv(g_client_fd, buf, sizeof(buf), 0);
            if (n > 0 && g_engine_connected) {
                ssize_t sent = sendto(g_server_fd, buf, n, 0,
                                       (struct sockaddr *)&g_engine_addr,
                                       g_engine_addr_len);
                if (sent < 0) {
                    if (errno != EINTR) {
                        LOG("Forward to vic-engine failed: %s (errno=%d, %zd bytes)",
                            strerror(errno), errno, n);
                    }
                } else {
                    g_msgs_anim_to_engine++;
                    if (g_msgs_anim_to_engine <= 10 || g_msgs_anim_to_engine % 10000 == 0) {
                        LOG("anim→engine msg #%llu (%zd bytes)",
                            (unsigned long long)g_msgs_anim_to_engine, n);
                    }
                }
            }
        }

        /* Mic streaming injection timer */
        if (g_mic_streaming) {
            uint64_t now = time_ms();
            if (now - last_inject_ms >= MIC_STREAM_RETRIGGER_MS) {
                inject_start_wakewordless();
                last_inject_ms = now;
            }
        }
    }

    LOG("Engine-anim proxy loop ended");
    LOG("  engine→anim: %llu msgs", (unsigned long long)g_msgs_engine_to_anim);
    LOG("  anim→engine: %llu msgs", (unsigned long long)g_msgs_anim_to_engine);
    LOG("  injected:    %llu msgs", (unsigned long long)g_msgs_injected);
    return 0;
}


void engine_proxy_start_mic_stream(void)
{
    if (g_mic_streaming) {
        LOG("Mic streaming already active");
        return;
    }

    if (!g_ankiconn_forwarded) {
        LOG("Cannot start mic streaming — proxy not connected to vic-anim yet");
        return;
    }

    LOG("Starting continuous mic streaming via StartWakeWordlessStreaming");

    /* First, inject SetTriggerWordResponse so HasValidTriggerResponse() is true.
     * Without this, StartWakeWordlessStreaming fails with "CantStreamToCloud"
     * because the behavior tree doesn't run under SDK OVERRIDE_BEHAVIORS priority. */
    inject_set_trigger_word_response();

    /* Small delay to let vic-anim process the trigger response */
    usleep(50000);  /* 50ms */

    g_mic_streaming = 1;

    /* Inject immediately — don't wait for timer */
    inject_start_wakewordless();
}


void engine_proxy_stop_mic_stream(void)
{
    if (!g_mic_streaming) {
        LOG("Mic streaming already stopped");
        return;
    }

    LOG("Stopping continuous mic streaming (will timeout in ~6s)");
    g_mic_streaming = 0;
}


int engine_proxy_mic_streaming(void)
{
    return g_mic_streaming;
}


void engine_proxy_stop(void)
{
    g_running = 0;

    /* Unblock select() by shutting down sockets */
    if (g_server_fd >= 0)
        shutdown(g_server_fd, SHUT_RDWR);
    if (g_client_fd >= 0)
        shutdown(g_client_fd, SHUT_RDWR);
}


void engine_proxy_cleanup(void)
{
    engine_proxy_stop();

    if (g_server_fd >= 0) {
        close(g_server_fd);
        g_server_fd = -1;
    }
    if (g_client_fd >= 0) {
        close(g_client_fd);
        g_client_fd = -1;
    }

    /* Remove our proxy sockets */
    if (g_paths_set) {
        unlink(g_server_path);
        unlink(g_client_path);
    }

    /* Restore original socket path */
    restore_original_socket();
}
