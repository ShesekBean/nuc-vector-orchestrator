/*
 * tcp_server.h — TCP streaming server for vector-streamer.
 *
 * Accepts one client at a time. Sends framed H264/Opus/JPEG to client.
 * Receives PCM audio from client for speaker playback.
 */

#ifndef TCP_SERVER_H
#define TCP_SERVER_H

#include <stdint.h>
#include <stddef.h>

/* Callback for received frames from the NUC client. */
typedef void (*tcp_recv_cb)(uint8_t type, const uint8_t *data, uint32_t length,
                            void *user_data);

/* Initialize the TCP server on the given port.
 * Returns 0 on success, -1 on error.
 */
int tcp_server_init(int port, tcp_recv_cb recv_callback, void *user_data);

/* Run the TCP server accept loop (blocks).
 * Handles one client at a time; reconnects on disconnect.
 */
int tcp_server_run(void);

/* Send a framed message to the connected client.
 * Thread-safe (uses internal mutex).
 * Returns 0 on success, -1 if no client connected or send error.
 */
int tcp_server_send(uint8_t type, const uint8_t *data, uint32_t length);

/* Returns 1 if a client is currently connected, 0 otherwise. */
int tcp_server_has_client(void);

/* Signal the server to stop. Thread-safe. */
void tcp_server_stop(void);

/* Clean up server resources. */
void tcp_server_cleanup(void);

#endif /* TCP_SERVER_H */
