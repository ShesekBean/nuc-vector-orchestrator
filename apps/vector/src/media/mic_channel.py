"""MicChannel -- connects to vector-streamer TCP on Vector:5555.

Reads framed Opus packets (protocol.h format: [type:1][length:4 LE][data:N]),
decodes Opus to 16kHz S16LE mono PCM using opuslib, and publishes decoded
PCM chunks to subscribers via _publish().

vector-streamer runs on Vector as a DGRAM proxy on mic_sock_cp_mic,
intercepting audio from vic-anim, Opus-encoding it, and streaming via TCP.
Mic audio flows during voice sessions (after "Hey Vector" wake word).

Auto-reconnects with exponential backoff if the connection drops.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from apps.vector.src.media.channel import MediaChannel

logger = logging.getLogger(__name__)

# Frame types from protocol.h
FRAME_TYPE_OPUS = 0x02
FRAME_TYPE_PING = 0xF0
FRAME_TYPE_PONG = 0xF1
FRAME_HEADER_SIZE = 5  # type:1 + length:4 LE
MAX_FRAME_SIZE = 512 * 1024

# Opus decode settings (must match vector-streamer encoder)
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_MS = 20
OPUS_FRAME_SAMPLES = OPUS_SAMPLE_RATE * OPUS_FRAME_MS // 1000  # 320

# Default vector-streamer address
DEFAULT_HOST = "192.168.1.73"
DEFAULT_PORT = 5555

# Reconnect backoff
INITIAL_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0


class MicChannel(MediaChannel):
    """Mic audio channel that connects to vector-streamer on Vector.

    Receives Opus-encoded mic audio over TCP, decodes to PCM, and
    publishes to subscribers.

    Args:
        host: Vector IP address.
        port: TCP port (default 5555).
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        super().__init__("mic")
        self._host = host
        self._port = port
        self._thread: threading.Thread | None = None
        self._decoder = None
        self._sock: socket.socket | None = None
        self._opus_frames_decoded = 0

    def start(self) -> None:
        """Start the mic channel background thread."""
        if self._running:
            logger.warning("MicChannel already running")
            return

        super().start()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="mic-channel",
            daemon=True,
        )
        self._thread.start()
        logger.info("MicChannel started (connecting to %s:%d)", self._host, self._port)

    def stop(self) -> None:
        """Stop the mic channel."""
        super().stop()

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

        logger.info("MicChannel stopped (decoded %d Opus frames)",
                    self._opus_frames_decoded)

    def get_status(self) -> dict:
        status = super().get_status()
        status.update({
            "host": self._host,
            "port": self._port,
            "connected": self._sock is not None,
            "opus_frames_decoded": self._opus_frames_decoded,
        })
        return status

    # -- Internal -----------------------------------------------------------

    def _init_decoder(self) -> None:
        """Initialize the Opus decoder (lazy import)."""
        if self._decoder is not None:
            return

        try:
            from opuslib import Decoder
            self._decoder = Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)
            logger.info("Opus decoder initialized (%d Hz, %d ch)",
                        OPUS_SAMPLE_RATE, OPUS_CHANNELS)
        except ImportError:
            logger.error(
                "opuslib not installed. Install with: pip install opuslib"
            )
            raise

    def _run_loop(self) -> None:
        """Main loop: connect, read frames, decode, publish. Reconnect on error."""
        self._init_decoder()
        delay = INITIAL_RECONNECT_DELAY

        while self._running:
            try:
                self._connect()
                delay = INITIAL_RECONNECT_DELAY  # Reset on successful connect
                self._read_frames()
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("MicChannel connection error: %s", exc)
            finally:
                self._disconnect()

            if not self._running:
                break

            logger.info("MicChannel reconnecting in %.1fs...", delay)
            # Sleep with early exit check
            end = time.monotonic() + delay
            while self._running and time.monotonic() < end:
                time.sleep(0.25)

            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _connect(self) -> None:
        """Connect to vector-streamer TCP server."""
        logger.info("Connecting to vector-streamer at %s:%d", self._host, self._port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10.0)
        self._sock.connect((self._host, self._port))
        self._sock.settimeout(5.0)  # Read timeout for keepalive detection
        logger.info("Connected to vector-streamer")

    def _disconnect(self) -> None:
        """Close the TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes from the socket."""
        buf = bytearray()
        while len(buf) < n:
            remaining = n - len(buf)
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError("vector-streamer disconnected (EOF)")
            buf.extend(chunk)
        return bytes(buf)

    def _read_frames(self) -> None:
        """Read framed packets from vector-streamer and decode Opus audio."""
        logger.info("Reading frames from vector-streamer...")

        while self._running:
            try:
                # Read frame header: [type:1][length:4 LE]
                hdr_data = self._recv_exact(FRAME_HEADER_SIZE)
                frame_type = hdr_data[0]
                frame_length = struct.unpack_from("<I", hdr_data, 1)[0]

                if frame_length > MAX_FRAME_SIZE:
                    logger.warning("Frame too large: %d bytes, disconnecting",
                                   frame_length)
                    break

                # Handle ping/pong
                if frame_type == FRAME_TYPE_PING:
                    pong = struct.pack("<BI", FRAME_TYPE_PONG, 0)
                    self._sock.sendall(pong)
                    continue

                if frame_type == FRAME_TYPE_PONG:
                    continue

                # Read frame payload
                if frame_length > 0:
                    payload = self._recv_exact(frame_length)
                else:
                    continue

                # Handle Opus audio
                if frame_type == FRAME_TYPE_OPUS:
                    self._decode_and_publish(payload)

            except socket.timeout:
                # No data for a while -- send a ping to check connection
                try:
                    ping = struct.pack("<BI", FRAME_TYPE_PING, 0)
                    self._sock.sendall(ping)
                except Exception:
                    logger.debug("Ping send failed, disconnecting")
                    break
            except ConnectionError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("Frame read error: %s", exc)
                break

    def _decode_and_publish(self, opus_data: bytes) -> None:
        """Decode an Opus frame to PCM and publish to subscribers."""
        try:
            # opuslib.Decoder.decode() returns bytes (PCM S16LE)
            pcm = self._decoder.decode(opus_data, OPUS_FRAME_SAMPLES)
            self._opus_frames_decoded += 1

            if self._opus_frames_decoded <= 3 or self._opus_frames_decoded % 1000 == 0:
                logger.info(
                    "Decoded Opus frame #%d: %d bytes opus -> %d bytes PCM (%d samples)",
                    self._opus_frames_decoded, len(opus_data), len(pcm),
                    len(pcm) // 2,
                )

            self._publish(pcm)

        except Exception:
            logger.debug("Opus decode error", exc_info=True)
