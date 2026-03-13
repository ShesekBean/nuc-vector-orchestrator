/*
 * protocol.h — Wire protocol for vector-streamer TCP framing.
 *
 * Framing: [type:1][length:4 LE][data:N]
 *
 * Shared between vector-streamer (Vector) and livekit_bridge.py (NUC).
 */

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>

/* Frame types: Vector → NUC */
#define FRAME_TYPE_H264    0x01  /* H264 NALU (camera video) */
#define FRAME_TYPE_OPUS    0x02  /* Opus-encoded audio (mic) */
#define FRAME_TYPE_JPEG    0x03  /* JPEG frame (fallback camera) */

/* Frame types: NUC → Vector */
#define FRAME_TYPE_PCM     0x10  /* Raw PCM int16 mono 16kHz (speaker) */

/* Frame types: Control */
#define FRAME_TYPE_PING    0xF0  /* Keepalive ping */
#define FRAME_TYPE_PONG    0xF1  /* Keepalive pong */
#define FRAME_TYPE_STATS   0xF2  /* JSON stats blob */

/* Frame header (5 bytes, little-endian length) */
typedef struct __attribute__((packed)) {
    uint8_t  type;
    uint32_t length;
} frame_header_t;

#define FRAME_HEADER_SIZE  5
#define MAX_FRAME_SIZE     (512 * 1024)  /* 512 KB max payload */

/* TCP server defaults */
#define DEFAULT_TCP_PORT       5555
#define DEFAULT_STATS_PORT     5556

/* Audio constants */
#define MIC_SAMPLE_RATE    16000
#define MIC_CHANNELS       1
#define MIC_SAMPLE_BITS    16
#define OPUS_FRAME_MS      20       /* 20ms Opus frames */
#define OPUS_FRAME_SAMPLES (MIC_SAMPLE_RATE * OPUS_FRAME_MS / 1000)  /* 320 */
#define OPUS_MAX_PACKET    4000

/* Camera constants */
#define CAMERA_WIDTH       640
#define CAMERA_HEIGHT      360
#define CAMERA_FPS         15

#endif /* PROTOCOL_H */
