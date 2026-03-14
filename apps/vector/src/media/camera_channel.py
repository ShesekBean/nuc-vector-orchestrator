"""CameraChannel — wraps CameraClient in MediaChannel fan-out pattern.

Taps into the existing CameraClient's frame buffer and publishes JPEG
frames to all subscribers.  Runs a background thread that polls the
CameraClient at ~15 fps and calls ``_publish()`` for each new frame.

Usage::

    camera_channel = CameraChannel(camera_client)
    camera_channel.start()
    with camera_channel.subscribe() as sub:
        jpeg = sub.queue.get(timeout=1)
        ...
    camera_channel.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from apps.vector.src.media.channel import MediaChannel

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient

logger = logging.getLogger(__name__)

# Publish rate — matches CameraClient poll rate
PUBLISH_FPS = 15


class CameraChannel(MediaChannel):
    """Camera video channel wrapping CameraClient.

    Publishes JPEG-encoded frames to subscribers.  The CameraClient
    must be started separately — this channel only taps into its buffer.

    Args:
        camera_client: Running CameraClient instance.
    """

    def __init__(self, camera_client: CameraClient) -> None:
        super().__init__("camera", ring_size=30)
        self._camera = camera_client
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start publishing camera frames to subscribers."""
        if self._running:
            logger.warning("CameraChannel already running")
            return

        if not self._camera.is_streaming:
            logger.warning("CameraClient not streaming — starting it")
            self._camera.start()

        super().start()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._publish_loop,
            name="camera-channel",
            daemon=True,
        )
        self._thread.start()
        logger.info("CameraChannel started")

    def stop(self) -> None:
        """Stop publishing (does NOT stop CameraClient)."""
        super().stop()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        logger.info("CameraChannel stopped")

    def get_latest_frame(self) -> Any:
        """Return the latest BGR numpy frame (delegates to CameraClient)."""
        return self._camera.get_latest_frame()

    def get_latest_jpeg(self) -> bytes | None:
        """Return the latest JPEG bytes (delegates to CameraClient)."""
        return self._camera.get_latest_jpeg()

    def get_status(self) -> dict:
        status = super().get_status()
        status.update({
            "camera_streaming": self._camera.is_streaming,
            "camera_fps": round(self._camera.fps, 1),
            "camera_frame_count": self._camera.frame_count,
        })
        return status

    # -- Internal -----------------------------------------------------------

    def _publish_loop(self) -> None:
        """Poll CameraClient for new JPEG frames and publish."""
        interval = 1.0 / PUBLISH_FPS
        last_count = self._camera.frame_count

        while not self._stop_event.is_set():
            current_count = self._camera.frame_count
            if current_count > last_count:
                last_count = current_count
                jpeg = self._camera.get_latest_jpeg()
                if jpeg:
                    self._publish(jpeg)

            self._stop_event.wait(interval)
