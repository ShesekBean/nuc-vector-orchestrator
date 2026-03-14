/*
 * liblog stub — provides __android_log_vprint for Porcupine v4's
 * Android armeabi-v7a library when running on Vector (non-Android Linux).
 *
 * Build:
 *   clang --target=armv7a-linux-gnueabi --sysroot=<sysroot> \
 *     -march=armv7-a -mfloat-abi=softfp -mfpu=neon \
 *     -shared -o liblog.so liblog_stub.c
 */

#include <stdarg.h>
#include <stdio.h>

/* Android log priorities — we don't use them, just need the signature */
int __android_log_vprint(int prio, const char *tag, const char *fmt, va_list ap) {
    (void)prio;
    (void)tag;
    (void)fmt;
    (void)ap;
    /* Silently discard Android log messages */
    return 0;
}
