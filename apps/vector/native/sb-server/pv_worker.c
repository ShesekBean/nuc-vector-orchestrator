/*
 * pv_worker — Hard-float Porcupine v4 worker daemon for Vector
 *
 * Runs as a standalone service, listening on a Unix domain socket.
 * The soft-float pv_shim (libpv_porcupine_softfp.so inside vic-anim)
 * connects as a client and sends wake word detection requests.
 *
 * Usage:
 *   pv_worker              (listens on /dev/_pv_worker_)
 *
 * Protocol (Unix domain socket, stream):
 *   Client sends command, worker sends response:
 *
 *   'I' + [model_path_len:4][model_path][kw_path_len:4][kw_path][sensitivity:4]
 *       → Response: [status:4][frame_length:4]  (frame_length only if status==0)
 *
 *   'P' + [pcm_data: frame_length*2 bytes]
 *       → Response: [keyword_index:4]  (-1 = none, 0+ = detected)
 *
 *   'D' → Delete/shutdown porcupine instance. Response: [0:4]
 *
 *   'F' → Query frame length. Response: [frame_length:4]
 *
 *   'Q' → Quit worker. Response: [0:4], then worker exits.
 *
 * Access key read from /anki/data/porcupine_key.txt.
 *
 * Build (armhf cross-compiler, hard-float):
 *   arm-linux-gnueabihf-gcc -march=armv7-a -mfloat-abi=hard -mfpu=vfpv3 \
 *     -O2 -Wl,--dynamic-linker=/anki/lib/hf/ld-linux-armhf.so.3 \
 *     -Wl,-rpath,/anki/lib/hf \
 *     -o pv_worker pv_worker.c -ldl
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <syslog.h>

/* ---- Porcupine v4 types (loaded via dlopen) ---- */

typedef int32_t pv_status_t;
#define PV_STATUS_SUCCESS 0

typedef struct pv_porcupine pv_porcupine_t;

typedef pv_status_t (*pv_init_fn)(
    const char *access_key, const char *model_path, const char *device,
    int32_t num_keywords, const char *const *keyword_paths,
    const float *sensitivities, pv_porcupine_t **object);
typedef void (*pv_delete_fn)(pv_porcupine_t *object);
typedef pv_status_t (*pv_process_fn)(pv_porcupine_t *object, const int16_t *pcm, int32_t *keyword_index);
typedef int32_t (*pv_frame_length_fn)(void);
typedef int32_t (*pv_sample_rate_fn)(void);
typedef const char *(*pv_version_fn)(void);
typedef const char *(*pv_strerr_fn)(pv_status_t status);

/* ---- Config ---- */

#define SOCKET_PATH "/dev/_pv_worker_"
#define ACCESS_KEY_PATH "/anki/data/porcupine_key.txt"
#define LIB_PATH "/anki/lib/hf/libpv_porcupine.so"
#define ACCESS_KEY_MAX 256
#define MAX_FRAME 2048

static volatile int g_running = 1;
static int g_server_fd = -1;
static volatile int g_client_fd = -1;

static void signal_handler(int sig) {
    (void)sig;
    g_running = 0;
    /* Shutdown sockets to unblock accept() and read() */
    if (g_client_fd >= 0)
        shutdown(g_client_fd, SHUT_RDWR);
    if (g_server_fd >= 0)
        shutdown(g_server_fd, SHUT_RDWR);
}

/* ---- I/O helpers ---- */

static int read_exact(int fd, void *buf, size_t n) {
    size_t done = 0;
    while (done < n) {
        ssize_t r = read(fd, (char *)buf + done, n - done);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) return -1;
        done += (size_t)r;
    }
    return 0;
}

static int write_exact(int fd, const void *buf, size_t n) {
    size_t done = 0;
    while (done < n) {
        ssize_t w = write(fd, (const char *)buf + done, n - done);
        if (w < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (w == 0) return -1;
        done += (size_t)w;
    }
    return 0;
}

static int read_access_key(char *buf, size_t bufsz) {
    FILE *f = fopen(ACCESS_KEY_PATH, "r");
    if (!f) return -1;
    if (!fgets(buf, (int)bufsz, f)) { fclose(f); return -1; }
    fclose(f);
    size_t len = strlen(buf);
    while (len > 0 && (buf[len-1] == '\n' || buf[len-1] == '\r'))
        buf[--len] = '\0';
    return len > 0 ? 0 : -1;
}

int main(void) {
    openlog("pv_worker", LOG_PID | LOG_CONS, LOG_USER);
    syslog(LOG_INFO, "pv_worker daemon starting (Porcupine v4, hard-float)");

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGPIPE, SIG_IGN);

    /* Load access key */
    char access_key[ACCESS_KEY_MAX];
    if (read_access_key(access_key, sizeof(access_key)) != 0) {
        syslog(LOG_ERR, "pv_worker: cannot read access key from %s", ACCESS_KEY_PATH);
        return 1;
    }
    syslog(LOG_INFO, "pv_worker: access key loaded (%zu chars)", strlen(access_key));

    /* dlopen Porcupine v4 */
    void *lib = dlopen(LIB_PATH, RTLD_NOW);
    if (!lib) {
        syslog(LOG_ERR, "pv_worker: dlopen failed: %s", dlerror());
        return 1;
    }

    pv_init_fn pv_init = (pv_init_fn)dlsym(lib, "pv_porcupine_init");
    pv_delete_fn pv_del = (pv_delete_fn)dlsym(lib, "pv_porcupine_delete");
    pv_process_fn pv_proc = (pv_process_fn)dlsym(lib, "pv_porcupine_process");
    pv_frame_length_fn pv_fl = (pv_frame_length_fn)dlsym(lib, "pv_porcupine_frame_length");
    pv_sample_rate_fn pv_sr = (pv_sample_rate_fn)dlsym(lib, "pv_sample_rate");
    pv_version_fn pv_ver = (pv_version_fn)dlsym(lib, "pv_porcupine_version");
    pv_strerr_fn pv_err = (pv_strerr_fn)dlsym(lib, "pv_status_to_string");

    if (!pv_init || !pv_del || !pv_proc || !pv_fl || !pv_sr) {
        syslog(LOG_ERR, "pv_worker: failed to resolve Porcupine symbols");
        dlclose(lib);
        return 1;
    }

    syslog(LOG_INFO, "pv_worker: porcupine %s loaded, sr=%d, fl=%d",
           pv_ver ? pv_ver() : "?", pv_sr(), pv_fl());

    /* Create listening socket */
    unlink(SOCKET_PATH);
    g_server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    int server_fd = g_server_fd;
    if (server_fd < 0) {
        syslog(LOG_ERR, "pv_worker: socket() failed: %s", strerror(errno));
        dlclose(lib);
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        syslog(LOG_ERR, "pv_worker: bind(%s) failed: %s", SOCKET_PATH, strerror(errno));
        close(server_fd);
        dlclose(lib);
        return 1;
    }
    chmod(SOCKET_PATH, 0666);

    if (listen(server_fd, 2) < 0) {
        syslog(LOG_ERR, "pv_worker: listen() failed: %s", strerror(errno));
        close(server_fd);
        dlclose(lib);
        return 1;
    }

    syslog(LOG_INFO, "pv_worker: listening on %s", SOCKET_PATH);

    pv_porcupine_t *porcupine = NULL;
    int32_t frame_length = pv_fl();
    int16_t pcm_buf[MAX_FRAME];

    /* Main loop: accept client connections */
    while (g_running) {
        syslog(LOG_INFO, "pv_worker: waiting for client...");
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            syslog(LOG_ERR, "pv_worker: accept() failed: %s", strerror(errno));
            break;
        }
        syslog(LOG_INFO, "pv_worker: client connected");
        g_client_fd = client_fd;

        /* Command loop for this client */
        while (g_running) {
            uint8_t cmd;
            if (read_exact(client_fd, &cmd, 1) != 0) {
                syslog(LOG_INFO, "pv_worker: client disconnected");
                break;
            }

            switch (cmd) {
            case 'I': {
                int32_t path_len;
                char model_path[512], keyword_path[512];

                read_exact(client_fd, &path_len, 4);
                if (path_len <= 0 || path_len >= (int32_t)sizeof(model_path)) {
                    int32_t err = 3; /* INVALID_ARGUMENT */
                    write_exact(client_fd, &err, 4);
                    break;
                }
                read_exact(client_fd, model_path, (size_t)path_len);
                model_path[path_len] = '\0';

                read_exact(client_fd, &path_len, 4);
                if (path_len <= 0 || path_len >= (int32_t)sizeof(keyword_path)) {
                    int32_t err = 3;
                    write_exact(client_fd, &err, 4);
                    break;
                }
                read_exact(client_fd, keyword_path, (size_t)path_len);
                keyword_path[path_len] = '\0';

                float sensitivity;
                read_exact(client_fd, &sensitivity, 4);

                syslog(LOG_INFO, "pv_worker: init model=%s keyword=%s sens=%.2f",
                       model_path, keyword_path, (double)sensitivity);

                /* Delete existing instance if any */
                if (porcupine) {
                    pv_del(porcupine);
                    porcupine = NULL;
                }

                const char *kw_path = keyword_path;
                pv_status_t st = pv_init(
                    access_key, model_path, "cpu",
                    1, &kw_path, &sensitivity, &porcupine);

                write_exact(client_fd, &st, 4);
                if (st == PV_STATUS_SUCCESS) {
                    frame_length = pv_fl();
                    write_exact(client_fd, &frame_length, 4);
                    syslog(LOG_INFO, "pv_worker: initialized, frame_length=%d", frame_length);
                } else {
                    syslog(LOG_ERR, "pv_worker: init failed: %s (%d)",
                           pv_err ? pv_err(st) : "?", st);
                }
                break;
            }

            case 'P': {
                if (!porcupine || frame_length <= 0 || frame_length > MAX_FRAME) {
                    int32_t err = -1;
                    write_exact(client_fd, &err, 4);
                    break;
                }

                if (read_exact(client_fd, pcm_buf, (size_t)frame_length * sizeof(int16_t)) != 0) {
                    syslog(LOG_ERR, "pv_worker: failed to read PCM data");
                    goto client_done;
                }

                int32_t keyword_index = -1;
                pv_status_t st = pv_proc(porcupine, pcm_buf, &keyword_index);
                if (st != PV_STATUS_SUCCESS) {
                    keyword_index = -1;
                }

                if (keyword_index >= 0) {
                    syslog(LOG_INFO, "pv_worker: *** WAKE WORD DETECTED ***");
                }

                write_exact(client_fd, &keyword_index, 4);
                break;
            }

            case 'D':
                syslog(LOG_INFO, "pv_worker: delete requested");
                if (porcupine) {
                    pv_del(porcupine);
                    porcupine = NULL;
                }
                {
                    int32_t ok = 0;
                    write_exact(client_fd, &ok, 4);
                }
                break;

            case 'F': {
                write_exact(client_fd, &frame_length, 4);
                break;
            }

            case 'Q':
                syslog(LOG_INFO, "pv_worker: quit requested");
                {
                    int32_t ok = 0;
                    write_exact(client_fd, &ok, 4);
                }
                g_running = 0;
                goto client_done;

            default:
                syslog(LOG_WARNING, "pv_worker: unknown command: 0x%02x", cmd);
                break;
            }
        }

client_done:
        g_client_fd = -1;
        close(client_fd);
    }

    /* Cleanup */
    syslog(LOG_INFO, "pv_worker: shutting down");
    if (porcupine) pv_del(porcupine);
    close(server_fd);
    unlink(SOCKET_PATH);
    dlclose(lib);
    closelog();
    return 0;
}
