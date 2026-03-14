/*
 * pv_shim.c — Porcupine v1.5→v4 shim library for Vector (WireOS)
 *
 * Replaces libpv_porcupine_softfp.so. Loaded by vic-anim via dlopen.
 * Exports the v1.5 API that vic-anim expects, but connects to the
 * pv_worker daemon (running as a separate hard-float process) via
 * Unix domain socket at /dev/_pv_worker_.
 *
 * v1.5 API (single keyword):
 *   pv_porcupine_init_softfp(model_path, keyword_path, &sensitivity, &obj)
 *   pv_porcupine_process(obj, pcm, &detected)  // detected is bool
 *   pv_porcupine_delete(obj)
 *
 * Build (vicos softfp toolchain):
 *   clang --sysroot=<sysroot> -march=armv7-a -mfloat-abi=softfp -mfpu=neon \
 *     -shared -fPIC -Wl,--version-script=pv_shim.version \
 *     -o libpv_porcupine_softfp.so pv_shim.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <syslog.h>

/* ---- Types matching Porcupine v1.5 API ---- */

typedef int32_t pv_status_t;
#define PV_STATUS_SUCCESS 0
#define PV_STATUS_OUT_OF_MEMORY 1
#define PV_STATUS_IO_ERROR 2
#define PV_STATUS_INVALID_ARGUMENT 3
#define PV_STATUS_RUNTIME_ERROR 7

#define SOCKET_PATH "/dev/_pv_worker_"
#define CONNECT_RETRIES 20
#define CONNECT_RETRY_MS 500

typedef struct {
    int sock_fd;
    int32_t frame_length;
} pv_porcupine_object_t;

static pv_porcupine_object_t *g_instance = NULL;

/* ---- Socket I/O helpers (EINTR-safe) ---- */

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

/* ---- Connect to pv_worker daemon ---- */

static int connect_to_worker(void) {
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    for (int attempt = 0; attempt < CONNECT_RETRIES; attempt++) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) continue;

        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            syslog(LOG_INFO, "pv_shim: connected to pv_worker (attempt %d)", attempt + 1);
            return fd;
        }
        close(fd);

        if (attempt < CONNECT_RETRIES - 1) {
            syslog(LOG_INFO, "pv_shim: worker not ready, retrying in %dms... (attempt %d/%d)",
                   CONNECT_RETRY_MS, attempt + 1, CONNECT_RETRIES);
            usleep(CONNECT_RETRY_MS * 1000);
        }
    }

    syslog(LOG_ERR, "pv_shim: failed to connect to pv_worker after %d attempts", CONNECT_RETRIES);
    return -1;
}

/* ---- Internal init ---- */

static pv_status_t do_init(
        const char *model_path,
        const char *keyword_path,
        float sensitivity,
        pv_porcupine_object_t **object) {

    openlog("pv_shim", LOG_PID, LOG_USER);
    syslog(LOG_INFO, "pv_shim: init model=%s keyword=%s sens=%.2f",
           model_path, keyword_path, (double)sensitivity);

    if (!model_path || !keyword_path || !object) {
        syslog(LOG_ERR, "pv_shim: invalid arguments");
        return PV_STATUS_INVALID_ARGUMENT;
    }

    int sock_fd = connect_to_worker();
    if (sock_fd < 0) {
        return PV_STATUS_IO_ERROR;
    }

    /* Send init command */
    uint8_t cmd = 'I';
    write_exact(sock_fd, &cmd, 1);

    int32_t len = (int32_t)strlen(model_path);
    write_exact(sock_fd, &len, 4);
    write_exact(sock_fd, model_path, (size_t)len);

    len = (int32_t)strlen(keyword_path);
    write_exact(sock_fd, &len, 4);
    write_exact(sock_fd, keyword_path, (size_t)len);

    write_exact(sock_fd, &sensitivity, 4);

    /* Read response */
    int32_t status = -1;
    if (read_exact(sock_fd, &status, 4) != 0) {
        syslog(LOG_ERR, "pv_shim: failed to read init response from worker");
        close(sock_fd);
        return PV_STATUS_RUNTIME_ERROR;
    }

    if (status != PV_STATUS_SUCCESS) {
        syslog(LOG_ERR, "pv_shim: worker init failed with status %d", status);
        close(sock_fd);
        return (pv_status_t)status;
    }

    int32_t fl = 512;
    if (read_exact(sock_fd, &fl, 4) != 0) {
        syslog(LOG_ERR, "pv_shim: failed to read frame_length");
        close(sock_fd);
        return PV_STATUS_RUNTIME_ERROR;
    }

    pv_porcupine_object_t *inst = (pv_porcupine_object_t *)calloc(1, sizeof(*inst));
    if (!inst) {
        close(sock_fd);
        return PV_STATUS_OUT_OF_MEMORY;
    }

    inst->sock_fd = sock_fd;
    inst->frame_length = fl;

    g_instance = inst;
    *object = inst;

    syslog(LOG_INFO, "pv_shim: initialized OK, frame_length=%d", fl);
    return PV_STATUS_SUCCESS;
}

/* ---- API: pv_porcupine_init_softfp (v1.5 softfp wrapper) ---- */

__attribute__((visibility("default")))
pv_status_t pv_porcupine_init_softfp(
        const char *model_path,
        const char *keyword_path,
        float *sensitivity,
        pv_porcupine_object_t **object) {

    float sens = sensitivity ? *sensitivity : 0.5f;
    return do_init(model_path, keyword_path, sens, object);
}

/* ---- API: pv_porcupine_init (v1.5 hard-float variant) ---- */

__attribute__((visibility("default")))
pv_status_t pv_porcupine_init(
        const char *model_path,
        const char *keyword_path,
        float sensitivity,
        pv_porcupine_object_t **object) {

    return do_init(model_path, keyword_path, sensitivity, object);
}

/* ---- API: pv_porcupine_process ---- */

__attribute__((visibility("default")))
pv_status_t pv_porcupine_process(
        pv_porcupine_object_t *object,
        const int16_t *pcm,
        bool *result) {

    if (!object || !pcm || !result) return PV_STATUS_INVALID_ARGUMENT;
    *result = false;

    uint8_t cmd = 'P';
    if (write_exact(object->sock_fd, &cmd, 1) != 0 ||
        write_exact(object->sock_fd, pcm, (size_t)object->frame_length * sizeof(int16_t)) != 0) {
        return PV_STATUS_RUNTIME_ERROR;
    }

    int32_t keyword_index = -1;
    if (read_exact(object->sock_fd, &keyword_index, 4) != 0) {
        return PV_STATUS_RUNTIME_ERROR;
    }

    *result = (keyword_index >= 0);
    return PV_STATUS_SUCCESS;
}

/* ---- API: pv_porcupine_delete ---- */

__attribute__((visibility("default")))
void pv_porcupine_delete(pv_porcupine_object_t *object) {
    if (!object) return;

    syslog(LOG_INFO, "pv_shim: deleting instance");
    uint8_t cmd = 'D';
    write_exact(object->sock_fd, &cmd, 1);
    int32_t ok;
    read_exact(object->sock_fd, &ok, 4);

    close(object->sock_fd);
    if (object == g_instance) g_instance = NULL;
    free(object);
}

/* ---- API: pv_porcupine_frame_length ---- */

__attribute__((visibility("default")))
int pv_porcupine_frame_length(void) {
    if (g_instance) return g_instance->frame_length;
    return 512;
}

/* ---- API: pv_sample_rate ---- */

__attribute__((visibility("default")))
int32_t pv_sample_rate(void) {
    return 16000;
}

/* ---- API: pv_porcupine_version ---- */

__attribute__((visibility("default")))
const char *pv_porcupine_version(void) {
    return "4.0.0-shim";
}

/* ---- API: pv_status_to_string ---- */

__attribute__((visibility("default")))
const char *pv_status_to_string(pv_status_t status) {
    switch (status) {
        case 0: return "SUCCESS";
        case 1: return "OUT_OF_MEMORY";
        case 2: return "IO_ERROR";
        case 3: return "INVALID_ARGUMENT";
        case 7: return "RUNTIME_ERROR";
        default: return "UNKNOWN";
    }
}

/* ---- API: multiple keywords (v1.5 compat) ---- */

__attribute__((visibility("default")))
pv_status_t pv_porcupine_multiple_keywords_init(
        const char *model_path,
        int num_keywords,
        const char *const *keyword_paths,
        const float *sensitivities,
        pv_porcupine_object_t **object) {

    if (num_keywords < 1 || !keyword_paths || !sensitivities)
        return PV_STATUS_INVALID_ARGUMENT;
    return do_init(model_path, keyword_paths[0], sensitivities[0], object);
}

__attribute__((visibility("default")))
pv_status_t pv_porcupine_multiple_keywords_process(
        pv_porcupine_object_t *object,
        const int16_t *pcm,
        int *keyword_index) {

    if (!object || !pcm || !keyword_index) return PV_STATUS_INVALID_ARGUMENT;
    *keyword_index = -1;

    bool detected = false;
    pv_status_t st = pv_porcupine_process(object, pcm, &detected);
    if (detected) *keyword_index = 0;
    return st;
}
