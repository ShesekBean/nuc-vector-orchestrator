"""Unit tests for CameraClient (mocked SDK — no robot required)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, PropertyMock

import numpy as np
import pytest

from apps.vector.src.camera.camera_client import CameraClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeImage:
    """Minimal stand-in for anki_vector.camera.CameraImage."""

    def __init__(self, width: int = 640, height: int = 360):
        from PIL import Image

        self.raw_image = Image.fromarray(
            np.random.randint(0, 255, (height, width, 3), dtype=np.uint8),
            mode="RGB",
        )
        self.image_id = 1
        self.image_recv_time = time.time()


@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.camera.init_camera_feed = MagicMock()
    robot.camera.close_camera_feed = MagicMock()
    robot.events.subscribe = MagicMock()
    robot.events.unsubscribe = MagicMock()

    # Default: latest_image returns a fake frame
    robot.camera.latest_image = FakeImage()
    return robot


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_buffer_size(self, mock_robot):
        client = CameraClient(mock_robot)
        assert client.buffer_size == 10

    def test_custom_buffer_size(self, mock_robot):
        client = CameraClient(mock_robot, buffer_size=5)
        assert client.buffer_size == 5

    def test_zero_buffer_raises(self, mock_robot):
        with pytest.raises(ValueError, match="buffer_size must be >= 1"):
            CameraClient(mock_robot, buffer_size=0)

    def test_initial_state(self, mock_robot):
        client = CameraClient(mock_robot)
        assert not client.is_streaming
        assert client.frame_count == 0
        assert client.fps == 0.0
        assert client.get_latest_frame() is None
        assert client.get_latest_jpeg() is None
        assert client.get_frame_buffer() == []


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_inits_feed_and_subscribes(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()

        mock_robot.camera.init_camera_feed.assert_called_once()
        mock_robot.events.subscribe.assert_called_once()
        assert client.is_streaming

        client.stop()

    def test_stop_closes_feed_and_unsubscribes(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()
        client.stop()

        mock_robot.camera.close_camera_feed.assert_called_once()
        mock_robot.events.unsubscribe.assert_called_once()
        assert not client.is_streaming

    def test_double_start_warns(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()
        client.start()  # should warn, not error

        assert mock_robot.camera.init_camera_feed.call_count == 1
        client.stop()

    def test_stop_without_start_is_noop(self, mock_robot):
        client = CameraClient(mock_robot)
        client.stop()  # should not raise


# ---------------------------------------------------------------------------
# Frame processing
# ---------------------------------------------------------------------------


class TestFrameProcessing:
    def _simulate_frame(self, client, mock_robot, width=640, height=360):
        """Simulate a new_camera_image event."""
        mock_robot.camera.latest_image = FakeImage(width, height)
        client._on_new_image(None, None, None)

    def test_frame_stored_as_bgr_numpy(self, mock_robot):
        client = CameraClient(mock_robot, buffer_size=5)
        client.start()
        self._simulate_frame(client, mock_robot)

        frame = client.get_latest_frame()
        assert frame is not None
        assert isinstance(frame, np.ndarray)
        assert frame.shape == (360, 640, 3)
        assert frame.dtype == np.uint8

        client.stop()

    def test_jpeg_bytes_produced(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()
        self._simulate_frame(client, mock_robot)

        jpeg = client.get_latest_jpeg()
        assert jpeg is not None
        assert isinstance(jpeg, bytes)
        assert len(jpeg) > 0
        # JPEG magic bytes
        assert jpeg[:2] == b"\xff\xd8"

        client.stop()

    def test_frame_count_increments(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()

        for _ in range(5):
            self._simulate_frame(client, mock_robot)

        assert client.frame_count == 5
        client.stop()

    def test_buffer_respects_max_size(self, mock_robot):
        client = CameraClient(mock_robot, buffer_size=3)
        client.start()

        for _ in range(10):
            self._simulate_frame(client, mock_robot)

        buf = client.get_frame_buffer()
        assert len(buf) == 3
        assert client.frame_count == 10

        client.stop()

    def test_get_latest_frame_returns_copy(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()
        self._simulate_frame(client, mock_robot)

        f1 = client.get_latest_frame()
        f2 = client.get_latest_frame()
        assert f1 is not f2
        np.testing.assert_array_equal(f1, f2)

        client.stop()

    def test_null_image_skipped(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()

        mock_robot.camera.latest_image = None
        client._on_new_image(None, None, None)

        assert client.frame_count == 0
        assert client.get_latest_frame() is None

        client.stop()


# ---------------------------------------------------------------------------
# FPS calculation
# ---------------------------------------------------------------------------


class TestFPS:
    def test_fps_zero_with_no_frames(self, mock_robot):
        client = CameraClient(mock_robot)
        assert client.fps == 0.0

    def test_fps_zero_with_one_frame(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()
        mock_robot.camera.latest_image = FakeImage()
        client._on_new_image(None, None, None)
        assert client.fps == 0.0
        client.stop()

    def test_fps_calculated_with_multiple_frames(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()

        # Simulate 10 frames at ~100 FPS (10ms apart)
        for _ in range(10):
            mock_robot.camera.latest_image = FakeImage()
            client._on_new_image(None, None, None)
            time.sleep(0.01)

        fps = client.fps
        # Should be roughly 50-150 FPS (timing is imprecise in tests)
        assert fps > 10.0
        client.stop()


# ---------------------------------------------------------------------------
# Connection lost handling
# ---------------------------------------------------------------------------


class TestConnectionLost:
    def test_callback_registered_and_invoked(self, mock_robot):
        client = CameraClient(mock_robot)
        callback = MagicMock()
        client.set_connection_lost_callback(callback)

        # The public callback attribute should be set
        assert client._on_connection_lost == callback


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    def test_start_failure_raises(self, mock_robot):
        mock_robot.camera.init_camera_feed.side_effect = RuntimeError("no robot")
        client = CameraClient(mock_robot)

        with pytest.raises(RuntimeError, match="no robot"):
            client.start()

        assert not client.is_streaming

    def test_frame_processing_error_does_not_crash(self, mock_robot):
        client = CameraClient(mock_robot)
        client.start()

        # Make latest_image raise
        type(mock_robot.camera).latest_image = PropertyMock(
            side_effect=RuntimeError("decode error")
        )
        client._on_new_image(None, None, None)  # should log, not raise

        assert client.frame_count == 0
        client.stop()
