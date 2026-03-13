/*
 * tcp_server.c — TCP streaming server for vector-streamer.
 *
 * Single-client TCP server that sends framed data (H264/Opus/JPEG)
 * to the NUC and receives PCM audio for speaker playback.
 */

#include "tcp_server.h"
#include "protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <signal.h>
#include <fcntl.h>

#define LOG_TAG "tcp"
#define LOG(fmt, ...) fprintf(stderr, "[%s] " fmt "\n", LOG_TAG, ##__VA_ARGS__)

static int             g_listen_fd = -1;
static int             g_client_fd = -1;
static pthread_mutex_t g_send_mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile int    g_running = 0;
static tcp_recv_cb     g_recv_cb = NULL;
static void           *g_recv_user = NULL;

/* Stats */
static uint64_t g_bytes_sent = 0;
static uint64_t g_frames_sent = 0;


static int send_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t remaining = len;

    while (remaining > 0) {
        ssize_t n = send(fd, p, remaining, MSG_NOSIGNAL);
        if (n <= 0) {
            if (n < 0 && (errno == EINTR))
                continue;
            return -1;
        }
        p += n;
        remaining -= n;
    }
    return 0;
}


static int recv_all(int fd, void *buf, size_t len)
{
    uint8_t *p = (uint8_t *)buf;
    size_t remaining = len;

    while (remaining > 0) {
        ssize_t n = recv(fd, p, remaining, 0);
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


int tcp_server_init(int port, tcp_recv_cb recv_callback, void *user_data)
{
    g_recv_cb = recv_callback;
    g_recv_user = user_data;

    g_listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (g_listen_fd < 0) {
        LOG("socket() failed: %s", strerror(errno));
        return -1;
    }

    int opt = 1;
    setsockopt(g_listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
        .sin_addr.s_addr = INADDR_ANY,
    };

    if (bind(g_listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOG("bind(%d) failed: %s", port, strerror(errno));
        close(g_listen_fd);
        g_listen_fd = -1;
        return -1;
    }

    if (listen(g_listen_fd, 1) < 0) {
        LOG("listen() failed: %s", strerror(errno));
        close(g_listen_fd);
        g_listen_fd = -1;
        return -1;
    }

    LOG("TCP server listening on port %d", port);
    g_running = 1;
    return 0;
}


static void handle_client(int client_fd)
{
    LOG("Client connected (fd=%d)", client_fd);

    /* Set TCP_NODELAY for low-latency streaming */
    int opt = 1;
    setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    /* Set send buffer to 256KB for video frames */
    int sndbuf = 256 * 1024;
    setsockopt(client_fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    pthread_mutex_lock(&g_send_mutex);
    g_client_fd = client_fd;
    pthread_mutex_unlock(&g_send_mutex);

    /* Receive loop — read framed messages from NUC */
    while (g_running) {
        frame_header_t hdr;
        if (recv_all(client_fd, &hdr, FRAME_HEADER_SIZE) < 0) {
            LOG("Client disconnected (recv header)");
            break;
        }

        if (hdr.length > MAX_FRAME_SIZE) {
            LOG("Frame too large: %u bytes (max %d)", hdr.length, MAX_FRAME_SIZE);
            break;
        }

        if (hdr.type == FRAME_TYPE_PING) {
            /* Respond with pong */
            frame_header_t pong = { .type = FRAME_TYPE_PONG, .length = 0 };
            pthread_mutex_lock(&g_send_mutex);
            send_all(client_fd, &pong, FRAME_HEADER_SIZE);
            pthread_mutex_unlock(&g_send_mutex);
            continue;
        }

        if (hdr.length > 0) {
            uint8_t *data = (uint8_t *)malloc(hdr.length);
            if (!data) {
                LOG("malloc(%u) failed", hdr.length);
                break;
            }

            if (recv_all(client_fd, data, hdr.length) < 0) {
                LOG("Client disconnected (recv data)");
                free(data);
                break;
            }

            if (g_recv_cb) {
                g_recv_cb(hdr.type, data, hdr.length, g_recv_user);
            }

            free(data);
        }
    }

    pthread_mutex_lock(&g_send_mutex);
    g_client_fd = -1;
    pthread_mutex_unlock(&g_send_mutex);

    close(client_fd);
    LOG("Client disconnected");
}


int tcp_server_run(void)
{
    while (g_running) {
        struct sockaddr_in client_addr;
        socklen_t addrlen = sizeof(client_addr);

        int client_fd = accept(g_listen_fd, (struct sockaddr *)&client_addr, &addrlen);
        if (client_fd < 0) {
            if (errno == EINTR)
                continue;
            if (!g_running)
                break;
            LOG("accept() failed: %s", strerror(errno));
            usleep(100000);  /* 100ms backoff */
            continue;
        }

        char ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &client_addr.sin_addr, ip, sizeof(ip));
        LOG("Accepted connection from %s:%d", ip, ntohs(client_addr.sin_port));

        handle_client(client_fd);
    }

    return 0;
}


int tcp_server_send(uint8_t type, const uint8_t *data, uint32_t length)
{
    int ret = -1;

    pthread_mutex_lock(&g_send_mutex);
    if (g_client_fd >= 0) {
        frame_header_t hdr = { .type = type, .length = length };
        if (send_all(g_client_fd, &hdr, FRAME_HEADER_SIZE) == 0) {
            if (length > 0 && data) {
                ret = send_all(g_client_fd, data, length);
            } else {
                ret = 0;
            }
        }
        if (ret == 0) {
            g_bytes_sent += FRAME_HEADER_SIZE + length;
            g_frames_sent++;
        }
    }
    pthread_mutex_unlock(&g_send_mutex);

    return ret;
}


int tcp_server_has_client(void)
{
    int has;
    pthread_mutex_lock(&g_send_mutex);
    has = (g_client_fd >= 0) ? 1 : 0;
    pthread_mutex_unlock(&g_send_mutex);
    return has;
}


void tcp_server_stop(void)
{
    g_running = 0;

    /* Close listen socket to unblock accept() */
    if (g_listen_fd >= 0) {
        shutdown(g_listen_fd, SHUT_RDWR);
        close(g_listen_fd);
        g_listen_fd = -1;
    }

    /* Close client socket */
    pthread_mutex_lock(&g_send_mutex);
    if (g_client_fd >= 0) {
        shutdown(g_client_fd, SHUT_RDWR);
        close(g_client_fd);
        g_client_fd = -1;
    }
    pthread_mutex_unlock(&g_send_mutex);
}


void tcp_server_cleanup(void)
{
    tcp_server_stop();
    LOG("Stats: sent %llu bytes in %llu frames",
        (unsigned long long)g_bytes_sent,
        (unsigned long long)g_frames_sent);
}
