"""Camera client for consuming frames from Vector over WiFi.

Connects to Vector via the wirepod-vector-sdk, subscribes to camera
image events, and maintains a thread-safe ring buffer of decoded
numpy frames for downstream consumers (detection, scene description,
photo capture).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import anki_vector
    import numpy as np

logger = logging.getLogger(__name__)

# Number of recent timestamps used for rolling FPS calculation
_FPS_WINDOW = 30


class CameraClient:
    """Consumes camera frames from Vector and buffers them for consumers.

    Args:
        robot: Connected ``anki_vector.Robot`` instance.
        buffer_size: Maximum number of frames to keep in the ring buffer.
    """

    def __init__(self, robot: anki_vector.Robot, buffer_size: int = 10) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size must be >= 1")

        self._robot = robot
        self._buffer_size = buffer_size

        # Ring buffer of BGR numpy arrays (thread-safe for single-producer)
        self._frames: deque = deque(maxlen=buffer_size)
        # Parallel buffer of JPEG bytes
        self._jpegs: deque[bytes] = deque(maxlen=buffer_size)
        # Timestamps for FPS calculation
        self._timestamps: deque[float] = deque(maxlen=_FPS_WINDOW)

        self._frame_count = 0
        self._streaming = False
        self._lock = threading.Lock()

        # Polling fallback (when SDK events don't fire)
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()

        # Reconnection state
        self._reconnect_thread: threading.Thread | None = None
        self._should_reconnect = False
        self._max_reconnect_delay = 30.0
        self._connection_lost_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the camera feed and subscribe to frame events."""
        if self._streaming:
            logger.warning("Camera feed already streaming")
            return

        self._should_reconnect = True
        self._start_feed()

    def stop(self) -> None:
        """Stop the camera feed and unsubscribe from events."""
        self._should_reconnect = False
        self._stop_feed()

        # Wait for any in-progress reconnect thread
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=5.0)
            self._reconnect_thread = None

    def get_latest_frame(self) -> np.ndarray | None:
        """Return the most recent frame as a BGR numpy array, or None."""
        with self._lock:
            if not self._frames:
                return None
            import numpy as np
            return np.copy(self._frames[-1])

    def get_latest_jpeg(self) -> bytes | None:
        """Return the most recent frame as raw JPEG bytes, or None."""
        with self._lock:
            return self._jpegs[-1] if self._jpegs else None

    def get_frame_buffer(self) -> list[np.ndarray]:
        """Return a copy of all buffered frames (oldest first)."""
        import numpy as np
        with self._lock:
            return [np.copy(f) for f in self._frames]

    def set_connection_lost_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the robot connection drops."""
        self._connection_lost_callback = callback

    @property
    def fps(self) -> float:
        """Rolling frames-per-second over the last *_FPS_WINDOW* frames."""
        with self._lock:
            if len(self._timestamps) < 2:
                return 0.0
            elapsed = self._timestamps[-1] - self._timestamps[0]
            if elapsed <= 0:
                return 0.0
            return (len(self._timestamps) - 1) / elapsed

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_feed(self) -> None:
        """Initialize camera feed with event subscription + polling fallback."""
        from anki_vector.events import Events

        try:
            # Ensure image streaming is enabled on the robot before opening
            # the feed.  After repeated connect/disconnect cycles the robot
            # can be left with streaming disabled, causing the gRPC
            # CameraFeed to deliver zero frames.
            if not self._robot.camera.image_streaming_enabled():
                logger.info("Image streaming was disabled on robot — enabling")
                from anki_vector.messaging import protocol as _proto

                async def _enable():
                    req = _proto.EnableImageStreamingRequest(enable=True)
                    return await self._robot.conn.grpc_interface.EnableImageStreaming(req)

                future = self._robot.conn.run_coroutine(_enable())
                future.result(timeout=5.0)
            self._robot.camera.init_camera_feed()
            self._robot.events.subscribe(
                self._on_new_image, Events.new_camera_image
            )
            self._streaming = True
            logger.info("Camera feed started (buffer_size=%d)", self._buffer_size)

            # Start polling fallback — picks up frames even when events don't fire
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="camera-poll"
            )
            self._poll_thread.start()
        except Exception:
            logger.exception("Failed to start camera feed")
            self._streaming = False
            raise

    def _stop_feed(self) -> None:
        """Close camera feed and unsubscribe from events."""
        from anki_vector.events import Events

        if not self._streaming:
            return

        # Stop polling thread first
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None

        try:
            self._robot.events.unsubscribe(
                self._on_new_image, Events.new_camera_image
            )
        except Exception:
            logger.debug("Unsubscribe failed (may already be unsubscribed)")
        try:
            self._robot.camera.close_camera_feed()
        except Exception:
            logger.debug("close_camera_feed failed (may already be closed)")
        self._streaming = False
        logger.info("Camera feed stopped (total frames: %d)", self._frame_count)

    def _on_new_image(self, _robot: object, _event_type: object, msg: object) -> None:
        """Callback fired by SDK on each new camera frame."""
        import cv2
        import numpy as np

        now = time.monotonic()
        try:
            image = self._robot.camera.latest_image
            if image is None:
                return

            pil_img = image.raw_image

            # Convert PIL RGB → numpy BGR (OpenCV convention)
            rgb_array = np.asarray(pil_img)
            bgr_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)

            # Encode to JPEG for consumers that want raw bytes
            ok, jpeg_buf = cv2.imencode(".jpg", bgr_array)
            jpeg_bytes = jpeg_buf.tobytes() if ok else b""

            with self._lock:
                self._frames.append(bgr_array)
                self._jpegs.append(jpeg_bytes)
                self._timestamps.append(now)
                self._frame_count += 1

        except Exception:
            logger.exception("Error processing camera frame")

    def _handle_connection_lost(self) -> None:
        """Handle SDK connection_lost event — attempt reconnection."""
        logger.warning("Vector connection lost — will attempt reconnect")
        self._streaming = False

        if self._connection_lost_callback:
            try:
                self._connection_lost_callback()
            except Exception:
                logger.exception("connection_lost callback raised")

        if self._should_reconnect:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Spawn a background thread to reconnect with exponential backoff."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # reconnect already in progress

        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True
        )
        self._reconnect_thread.start()

    def _poll_loop(self) -> None:
        """Poll robot.camera.latest_image at ~15fps as fallback for SDK events.

        The SDK's ``new_camera_image`` event doesn't always fire (e.g. when
        behavior control is held by the bridge).  This thread polls the camera
        directly and buffers any new frames it finds.
        """
        logger.info("Poll thread starting")
        import cv2
        import numpy as np
        from anki_vector.exceptions import (
            VectorCameraFeedException,
            VectorPropertyValueNotReadyException,
        )

        interval = 1.0 / 15  # ~15 fps target
        last_id = None  # track image identity to avoid duplicates

        while not self._poll_stop.is_set():
            try:
                try:
                    image = self._robot.camera.latest_image
                except (VectorPropertyValueNotReadyException, VectorCameraFeedException):
                    self._poll_stop.wait(0.5)
                    continue

                if image is None:
                    self._poll_stop.wait(interval)
                    continue

                # Deduplicate: skip if same image object as last time
                img_id = id(image)
                if img_id == last_id:
                    self._poll_stop.wait(interval)
                    continue
                last_id = img_id

                now = time.monotonic()
                pil_img = image.raw_image
                rgb_array = np.asarray(pil_img)
                bgr_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)

                ok, jpeg_buf = cv2.imencode(".jpg", bgr_array)
                jpeg_bytes = jpeg_buf.tobytes() if ok else b""

                with self._lock:
                    self._frames.append(bgr_array)
                    self._jpegs.append(jpeg_bytes)
                    self._timestamps.append(now)
                    self._frame_count += 1

            except Exception:
                logger.warning("Poll frame error", exc_info=True)

            self._poll_stop.wait(interval)

    def _reconnect_loop(self) -> None:
        """Try to restart the camera feed with exponential backoff."""
        delay = 1.0
        while self._should_reconnect and not self._streaming:
            logger.info("Reconnect attempt in %.1fs...", delay)
            time.sleep(delay)
            if not self._should_reconnect:
                break
            try:
                self._start_feed()
                logger.info("Reconnected to camera feed")
                return
            except Exception:
                logger.warning("Reconnect failed, retrying...")
                delay = min(delay * 2, self._max_reconnect_delay)
