/*
 * sb_server — Porcupine v4 wake word detector for Vector (OSKR/WireOS)
 *
 * Drop-in replacement for the original Snowboy-based sb_server.
 * vic-anim launches this binary and communicates via Unix socket.
 *
 * Usage (called by vic-anim):
 *   sb_server <model_path> <keyword_path>
 *
 *   model_path  = path to porcupine_params.pv
 *   keyword_path = path to .ppn keyword file
 *
 * Protocol (Unix socket at /dev/_anim_sb_wakeword_):
 *   Client sends: [4-byte length (int32 LE)][PCM int16 samples]
 *   Server sends: [4-byte result (int32 LE)]
 *     1  = wake word detected
 *     0  = no detection
 *    -1  = error
 *
 * Access key is read from /anki/data/porcupine_key.txt (first line, trimmed).
 *
 * Build (on Jetson with vicos toolchain):
 *   clang --target=armv7a-linux-gnueabi --sysroot=<sysroot> \
 *     -march=armv7-a -mfloat-abi=softfp -mfpu=neon \
 *     -o sb_server sb_server.c -ldl -lpthread
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

/* ---- Porcupine types (loaded via dlopen) ---- */

typedef int32_t pv_status_t;
#define PV_STATUS_SUCCESS 0

typedef struct pv_porcupine pv_porcupine_t;

typedef pv_status_t (*pv_porcupine_init_fn)(
    const char *access_key,
    const char *model_path,
    const char *device,
    int32_t num_keywords,
    const char *const *keyword_paths,
    const float *sensitivities,
    pv_porcupine_t **object);

typedef void (*pv_porcupine_delete_fn)(pv_porcupine_t *object);

typedef pv_status_t (*pv_porcupine_process_fn)(
    pv_porcupine_t *object,
    const int16_t *pcm,
    int32_t *keyword_index);

typedef int32_t (*pv_porcupine_frame_length_fn)(void);
typedef int32_t (*pv_sample_rate_fn)(void);
typedef const char *(*pv_porcupine_version_fn)(void);
typedef const char *(*pv_status_to_string_fn)(pv_status_t status);

/* ---- Globals ---- */

#define SOCKET_PATH "/dev/_anim_sb_wakeword_"
#define ACCESS_KEY_PATH "/anki/data/porcupine_key.txt"
#define LIB_PATH "/anki/lib/hf/libpv_porcupine.so"

/* Max PCM buffer: vic-anim sends 1024 samples (2048 bytes) at a time */
#define MAX_PCM_SAMPLES 4096
#define ACCESS_KEY_MAX 256

static volatile int g_running = 1;

static void signal_handler(int sig) {
    (void)sig;
    g_running = 0;
}

/* ---- Helpers ---- */

static int read_access_key(char *buf, size_t bufsz) {
    FILE *f = fopen(ACCESS_KEY_PATH, "r");
    if (!f) {
        syslog(LOG_ERR, "sb_server: cannot open %s: %s", ACCESS_KEY_PATH, strerror(errno));
        return -1;
    }
    if (!fgets(buf, (int)bufsz, f)) {
        fclose(f);
        syslog(LOG_ERR, "sb_server: empty access key file");
        return -1;
    }
    fclose(f);
    /* trim newline */
    size_t len = strlen(buf);
    while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
        buf[--len] = '\0';
    if (len == 0) {
        syslog(LOG_ERR, "sb_server: access key is empty");
        return -1;
    }
    return 0;
}

/* Read exactly n bytes from fd, returns 0 on success, -1 on error/EOF */
static int read_exact(int fd, void *buf, size_t n) {
    size_t done = 0;
    while (done < n) {
        ssize_t r = read(fd, (char *)buf + done, n - done);
        if (r <= 0) return -1;
        done += (size_t)r;
    }
    return 0;
}

/* Write exactly n bytes to fd */
static int write_exact(int fd, const void *buf, size_t n) {
    size_t done = 0;
    while (done < n) {
        ssize_t w = write(fd, (const char *)buf + done, n - done);
        if (w <= 0) return -1;
        done += (size_t)w;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "Usage: sb_server <model_path> <keyword_path>\n");
        return 1;
    }

    const char *model_path = argv[1];
    const char *keyword_path = argv[2];

    openlog("sb_server", LOG_PID | LOG_CONS, LOG_USER);
    syslog(LOG_INFO, "sb_server starting (Porcupine v4)");
    syslog(LOG_INFO, "  model:   %s", model_path);
    syslog(LOG_INFO, "  keyword: %s", keyword_path);

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGPIPE, SIG_IGN);

    /* ---- Load access key ---- */
    char access_key[ACCESS_KEY_MAX];
    if (read_access_key(access_key, sizeof(access_key)) != 0) {
        syslog(LOG_ERR, "sb_server: failed to read access key, exiting");
        return 1;
    }
    syslog(LOG_INFO, "  access key loaded (%zu chars)", strlen(access_key));

    /* ---- Ensure hard-float libs are findable ---- */
    setenv("LD_LIBRARY_PATH", "/anki/lib/hf", 1);

    /* ---- dlopen Porcupine ---- */
    void *lib = dlopen(LIB_PATH, RTLD_NOW);
    if (!lib) {
        syslog(LOG_ERR, "sb_server: dlopen(%s) failed: %s", LIB_PATH, dlerror());
        return 1;
    }

    pv_porcupine_init_fn pv_init = (pv_porcupine_init_fn)dlsym(lib, "pv_porcupine_init");
    pv_porcupine_delete_fn pv_delete = (pv_porcupine_delete_fn)dlsym(lib, "pv_porcupine_delete");
    pv_porcupine_process_fn pv_process = (pv_porcupine_process_fn)dlsym(lib, "pv_porcupine_process");
    pv_porcupine_frame_length_fn pv_frame_len = (pv_porcupine_frame_length_fn)dlsym(lib, "pv_porcupine_frame_length");
    pv_sample_rate_fn pv_rate = (pv_sample_rate_fn)dlsym(lib, "pv_sample_rate");
    pv_porcupine_version_fn pv_version = (pv_porcupine_version_fn)dlsym(lib, "pv_porcupine_version");
    pv_status_to_string_fn pv_strerr = (pv_status_to_string_fn)dlsym(lib, "pv_status_to_string");

    if (!pv_init || !pv_delete || !pv_process || !pv_frame_len || !pv_rate) {
        syslog(LOG_ERR, "sb_server: failed to resolve Porcupine symbols");
        dlclose(lib);
        return 1;
    }

    syslog(LOG_INFO, "  porcupine version: %s", pv_version ? pv_version() : "unknown");
    syslog(LOG_INFO, "  sample rate: %d, frame length: %d",
           pv_rate(), pv_frame_len());

    /* ---- Init Porcupine ---- */
    const float sensitivity = 0.5f;
    pv_porcupine_t *porcupine = NULL;

    pv_status_t status = pv_init(
        access_key,
        model_path,
        "cpu",          /* device — CPU only on Vector */
        1,              /* num_keywords */
        &keyword_path,  /* keyword_paths */
        &sensitivity,
        &porcupine);

    if (status != PV_STATUS_SUCCESS) {
        syslog(LOG_ERR, "sb_server: pv_porcupine_init failed: %s (%d)",
               pv_strerr ? pv_strerr(status) : "?", status);
        dlclose(lib);
        return 1;
    }

    int32_t frame_length = pv_frame_len();
    syslog(LOG_INFO, "  porcupine initialized, frame_length=%d", frame_length);

    /* ---- Create Unix socket ---- */
    unlink(SOCKET_PATH);

    int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server_fd < 0) {
        syslog(LOG_ERR, "sb_server: socket() failed: %s", strerror(errno));
        pv_delete(porcupine);
        dlclose(lib);
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        syslog(LOG_ERR, "sb_server: bind(%s) failed: %s", SOCKET_PATH, strerror(errno));
        close(server_fd);
        pv_delete(porcupine);
        dlclose(lib);
        return 1;
    }

    chmod(SOCKET_PATH, 0666);

    if (listen(server_fd, 1) < 0) {
        syslog(LOG_ERR, "sb_server: listen() failed: %s", strerror(errno));
        close(server_fd);
        pv_delete(porcupine);
        dlclose(lib);
        return 1;
    }

    syslog(LOG_INFO, "sb_server: listening on %s", SOCKET_PATH);

    /* ---- PCM ring buffer for Porcupine frame alignment ---- */
    int16_t pcm_buf[MAX_PCM_SAMPLES];
    int pcm_buf_count = 0;

    /* ---- Main loop: accept connections, process audio ---- */
    while (g_running) {
        syslog(LOG_INFO, "sb_server: waiting for connection...");
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            syslog(LOG_ERR, "sb_server: accept() failed: %s", strerror(errno));
            break;
        }
        syslog(LOG_INFO, "sb_server: client connected");
        pcm_buf_count = 0;

        while (g_running) {
            /* Read message: [4-byte length][PCM data] */
            int32_t msg_len = 0;
            if (read_exact(client_fd, &msg_len, 4) != 0) {
                syslog(LOG_INFO, "sb_server: client disconnected (read header)");
                break;
            }

            if (msg_len <= 0 || msg_len > (int32_t)(MAX_PCM_SAMPLES * sizeof(int16_t))) {
                syslog(LOG_WARNING, "sb_server: invalid message length: %d", msg_len);
                int32_t result = -1;
                write_exact(client_fd, &result, 4);
                break;
            }

            int32_t num_samples = msg_len / (int32_t)sizeof(int16_t);

            /* Read PCM data into temp buffer */
            int16_t temp_pcm[MAX_PCM_SAMPLES];
            if (read_exact(client_fd, temp_pcm, (size_t)msg_len) != 0) {
                syslog(LOG_INFO, "sb_server: client disconnected (read data)");
                break;
            }

            /* Append to ring buffer */
            if (pcm_buf_count + num_samples > MAX_PCM_SAMPLES) {
                /* Overflow — discard oldest, keep tail */
                int keep = MAX_PCM_SAMPLES - num_samples;
                if (keep > 0 && keep < pcm_buf_count) {
                    memmove(pcm_buf, pcm_buf + (pcm_buf_count - keep),
                            (size_t)keep * sizeof(int16_t));
                    pcm_buf_count = keep;
                } else {
                    pcm_buf_count = 0;
                }
            }
            memcpy(pcm_buf + pcm_buf_count, temp_pcm,
                   (size_t)num_samples * sizeof(int16_t));
            pcm_buf_count += num_samples;

            /* Process as many complete frames as possible */
            int32_t detected = 0;
            while (pcm_buf_count >= frame_length) {
                int32_t keyword_index = -1;
                pv_status_t pst = pv_process(porcupine, pcm_buf, &keyword_index);
                if (pst != PV_STATUS_SUCCESS) {
                    syslog(LOG_ERR, "sb_server: pv_porcupine_process failed: %s",
                           pv_strerr ? pv_strerr(pst) : "?");
                }
                if (keyword_index >= 0) {
                    detected = 1;
                    syslog(LOG_INFO, "sb_server: *** WAKE WORD DETECTED ***");
                }
                /* Shift buffer: remove consumed frame */
                pcm_buf_count -= frame_length;
                if (pcm_buf_count > 0) {
                    memmove(pcm_buf, pcm_buf + frame_length,
                            (size_t)pcm_buf_count * sizeof(int16_t));
                }
            }

            /* Send result: 1=detected, 0=nothing */
            int32_t result = detected ? 1 : 0;
            if (write_exact(client_fd, &result, 4) != 0) {
                syslog(LOG_INFO, "sb_server: client disconnected (write)");
                break;
            }
        }

        close(client_fd);
    }

    /* ---- Cleanup ---- */
    syslog(LOG_INFO, "sb_server: shutting down");
    close(server_fd);
    unlink(SOCKET_PATH);
    pv_delete(porcupine);
    dlclose(lib);
    closelog();
    return 0;
}
