"""MicChannel -- dual-source mic audio channel.

Supports two audio sources:

1. **Wire-pod audio tap** (localhost:5556) -- raw 16kHz S16LE mono PCM
   streamed during voice sessions (after "Hey Vector"). This is the
   working path. The wire-pod chipper decodes Opus from vic-cloud and
   taps decoded PCM to connected TCP clients.

2. **vector-streamer** (Vector:5555) -- Opus-encoded mic audio from
   the DGRAM proxy on Vector. This requires vector-streamer to be
   running on the robot. Decodes Opus to PCM using opuslib.
   (Currently limited: mic_sock_cp_mic proxy doesn't work because
   vic-cloud recreates the socket.)

The channel tries both sources and uses whichever connects.

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

# Frame types from protocol.h (for vector-streamer source)
FRAME_TYPE_OPUS = 0x02
FRAME_TYPE_PING = 0xF0
FRAME_TYPE_PONG = 0xF1
FRAME_HEADER_SIZE = 5
MAX_FRAME_SIZE = 512 * 1024

# Audio settings
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
OPUS_FRAME_MS = 20
OPUS_FRAME_SAMPLES = AUDIO_SAMPLE_RATE * OPUS_FRAME_MS // 1000  # 320

# Wire-pod audio tap (raw PCM over TCP, no framing)
WIREPOD_TAP_HOST = "127.0.0.1"
WIREPOD_TAP_PORT = 5556
PCM_CHUNK_SIZE = 640  # 20ms of 16kHz mono int16 = 320 samples = 640 bytes

# Vector-streamer (Opus over TCP with framing)
DEFAULT_STREAMER_HOST = "192.168.1.73"
DEFAULT_STREAMER_PORT = 5555

# Reconnect backoff
INITIAL_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0


class MicChannel(MediaChannel):
    """Mic audio channel with dual-source support.

    Connects to wire-pod audio tap (primary) or vector-streamer (secondary)
    and publishes decoded 16kHz S16LE mono PCM to subscribers.

    Args:
        host: Vector IP address (for vector-streamer source).
        port: TCP port for vector-streamer (default 5555).
        use_wirepod_tap: If True, connect to wire-pod audio tap first.
    """

    def __init__(
        self,
        host: str = DEFAULT_STREAMER_HOST,
        port: int = DEFAULT_STREAMER_PORT,
        use_wirepod_tap: bool = True,
    ) -> None:
        super().__init__("mic")
        self._streamer_host = host
        self._streamer_port = port
        self._use_wirepod_tap = use_wirepod_tap
        self._thread: threading.Thread | None = None
        self._opus_decoder = None
        self._sock: socket.socket | None = None
        self._source: str = "none"
        self._frames_decoded = 0

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
        logger.info("MicChannel started")

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

        logger.info("MicChannel stopped (decoded %d frames, source=%s)",
                    self._frames_decoded, self._source)

    def get_status(self) -> dict:
        status = super().get_status()
        status.update({
            "source": self._source,
            "streamer_host": self._streamer_host,
            "streamer_port": self._streamer_port,
            "wirepod_tap": f"{WIREPOD_TAP_HOST}:{WIREPOD_TAP_PORT}",
            "connected": self._sock is not None,
            "frames_decoded": self._frames_decoded,
        })
        return status

    # -- Main loop ----------------------------------------------------------

    def _run_loop(self) -> None:
        """Try connecting to audio sources with reconnect logic."""
        delay = INITIAL_RECONNECT_DELAY

        while self._running:
            try:
                connected = False

                # Try wire-pod audio tap first (primary source)
                if self._use_wirepod_tap:
                    try:
                        self._connect_wirepod_tap()
                        connected = True
                        delay = INITIAL_RECONNECT_DELAY
                        self._read_wirepod_pcm()
                    except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
                        logger.debug("Wire-pod tap not available: %s", e)

                # Try vector-streamer (secondary source)
                if not connected:
                    try:
                        self._connect_streamer()
                        connected = True
                        delay = INITIAL_RECONNECT_DELAY
                        self._read_streamer_frames()
                    except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
                        logger.debug("vector-streamer not available: %s", e)

            except Exception as exc:
                if not self._running:
                    break
                logger.warning("MicChannel error: %s", exc)
            finally:
                self._disconnect()

            if not self._running:
                break

            logger.debug("MicChannel reconnecting in %.1fs...", delay)
            end = time.monotonic() + delay
            while self._running and time.monotonic() < end:
                time.sleep(0.25)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    # -- Wire-pod audio tap -------------------------------------------------

    def _connect_wirepod_tap(self) -> None:
        """Connect to wire-pod chipper audio tap on localhost:5556."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((WIREPOD_TAP_HOST, WIREPOD_TAP_PORT))
        self._source = "wirepod-tap"
        logger.info("Connected to wire-pod audio tap at %s:%d",
                    WIREPOD_TAP_HOST, WIREPOD_TAP_PORT)

    def _read_wirepod_pcm(self) -> None:
        """Read raw PCM from wire-pod audio tap (no framing)."""
        logger.info("Reading PCM from wire-pod audio tap...")

        while self._running:
            try:
                pcm_data = self._sock.recv(PCM_CHUNK_SIZE)
                if not pcm_data:
                    logger.debug("Wire-pod tap: EOF")
                    break

                if len(pcm_data) < 2:
                    continue

                self._frames_decoded += 1
                if self._frames_decoded <= 3 or self._frames_decoded % 1000 == 0:
                    logger.info(
                        "Wire-pod tap PCM #%d: %d bytes (%d samples)",
                        self._frames_decoded, len(pcm_data), len(pcm_data) // 2,
                    )

                self._publish(pcm_data)

            except socket.timeout:
                # No data -- wire-pod only sends during voice sessions
                continue
            except ConnectionError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("Wire-pod tap read error: %s", exc)
                break

    # -- Vector-streamer (Opus) ---------------------------------------------

    def _connect_streamer(self) -> None:
        """Connect to vector-streamer TCP on Vector."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10.0)
        self._sock.connect((self._streamer_host, self._streamer_port))
        self._sock.settimeout(5.0)
        self._source = "vector-streamer"
        logger.info("Connected to vector-streamer at %s:%d",
                    self._streamer_host, self._streamer_port)

    def _init_opus_decoder(self) -> None:
        """Initialize the Opus decoder (lazy)."""
        if self._opus_decoder is not None:
            return
        from opuslib import Decoder
        self._opus_decoder = Decoder(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        logger.info("Opus decoder initialized")

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes from the socket."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("vector-streamer disconnected")
            buf.extend(chunk)
        return bytes(buf)

    def _read_streamer_frames(self) -> None:
        """Read framed Opus packets from vector-streamer."""
        self._init_opus_decoder()
        logger.info("Reading Opus frames from vector-streamer...")

        while self._running:
            try:
                hdr_data = self._recv_exact(FRAME_HEADER_SIZE)
                frame_type = hdr_data[0]
                frame_length = struct.unpack_from("<I", hdr_data, 1)[0]

                if frame_length > MAX_FRAME_SIZE:
                    logger.warning("Frame too large: %d", frame_length)
                    break

                if frame_type == FRAME_TYPE_PING:
                    pong = struct.pack("<BI", FRAME_TYPE_PONG, 0)
                    self._sock.sendall(pong)
                    continue

                if frame_type == FRAME_TYPE_PONG:
                    continue

                if frame_length == 0:
                    continue

                payload = self._recv_exact(frame_length)

                if frame_type == FRAME_TYPE_OPUS:
                    try:
                        pcm = self._opus_decoder.decode(payload, OPUS_FRAME_SAMPLES)
                        self._frames_decoded += 1
                        if self._frames_decoded <= 3 or self._frames_decoded % 1000 == 0:
                            logger.info(
                                "Opus frame #%d: %d->%d bytes",
                                self._frames_decoded, len(payload), len(pcm),
                            )
                        self._publish(pcm)
                    except Exception:
                        logger.debug("Opus decode error", exc_info=True)

            except socket.timeout:
                try:
                    ping = struct.pack("<BI", FRAME_TYPE_PING, 0)
                    self._sock.sendall(ping)
                except Exception:
                    break
            except ConnectionError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("Streamer read error: %s", exc)
                break

    # -- Common -------------------------------------------------------------

    def _disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._source = "none"
