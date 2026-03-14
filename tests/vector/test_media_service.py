"""Tests for on-demand MediaService and media channels."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.media.channel import MediaChannel
from apps.vector.src.media.camera_channel import CameraChannel
from apps.vector.src.media.speaker_channel import SpeakerChannel
from apps.vector.src.media.display_channel import DisplayChannel
from apps.vector.src.media.service import MediaService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_camera_client():
    cam = MagicMock()
    cam.is_streaming = True
    cam.fps = 15.0
    cam.frame_count = 0
    cam.get_latest_jpeg = MagicMock(return_value=b"\xff\xd8fake-jpeg")
    cam.get_latest_frame = MagicMock(return_value=None)
    cam.start = MagicMock()
    return cam


@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.behavior = MagicMock()
    robot.behavior.say_text = MagicMock()
    robot.audio = MagicMock()
    robot.audio.stream_wav_file = MagicMock()
    robot.screen = MagicMock()
    robot.screen.set_screen_with_image_data = MagicMock()
    return robot


# ---------------------------------------------------------------------------
# MediaChannel base
# ---------------------------------------------------------------------------

class TestMediaChannel:
    def test_subscribe_unsubscribe(self):
        ch = MediaChannel("test")
        ch.start()

        sub = ch.subscribe()
        assert ch.subscriber_count == 1
        assert not sub.closed

        sub.close()
        assert sub.closed
        assert ch.subscriber_count == 0

    def test_context_manager(self):
        ch = MediaChannel("test")
        ch.start()

        with ch.subscribe() as _sub:
            assert ch.subscriber_count == 1
        assert ch.subscriber_count == 0

    def test_publish_fan_out(self):
        ch = MediaChannel("test")
        ch.start()

        sub1 = ch.subscribe()
        sub2 = ch.subscribe()

        ch._publish(b"data1")
        ch._publish(b"data2")

        assert sub1.queue.get_nowait() == b"data1"
        assert sub1.queue.get_nowait() == b"data2"
        assert sub2.queue.get_nowait() == b"data1"
        assert sub2.queue.get_nowait() == b"data2"
        assert ch.chunk_count == 2

        sub1.close()
        sub2.close()

    def test_get_latest(self):
        ch = MediaChannel("test")
        assert ch.get_latest() is None

        ch._publish(b"first")
        ch._publish(b"second")
        assert ch.get_latest() == b"second"

    def test_get_status(self):
        ch = MediaChannel("test")
        ch.start()
        status = ch.get_status()
        assert status["name"] == "test"
        assert status["running"] is True
        assert status["chunk_count"] == 0
        assert status["subscriber_count"] == 0


# ---------------------------------------------------------------------------
# CameraChannel
# ---------------------------------------------------------------------------

class TestCameraChannel:
    def test_starts_and_stops(self, mock_camera_client):
        ch = CameraChannel(mock_camera_client)
        ch.start()
        assert ch.is_running
        ch.stop()
        assert not ch.is_running

    def test_publishes_on_new_frames(self, mock_camera_client):
        ch = CameraChannel(mock_camera_client)
        sub = ch.subscribe()

        # Simulate frame count advancing
        mock_camera_client.frame_count = 0
        ch.start()

        # Advance frame count to trigger publish
        mock_camera_client.frame_count = 1
        time.sleep(0.2)  # Let publish loop run

        ch.stop()
        assert not sub.queue.empty()
        data = sub.queue.get_nowait()
        assert data == b"\xff\xd8fake-jpeg"
        sub.close()

    def test_delegates_to_camera_client(self, mock_camera_client):
        ch = CameraChannel(mock_camera_client)
        ch.get_latest_frame()
        mock_camera_client.get_latest_frame.assert_called_once()

        ch.get_latest_jpeg()
        mock_camera_client.get_latest_jpeg.assert_called_once()

    def test_status_includes_camera_info(self, mock_camera_client):
        ch = CameraChannel(mock_camera_client)
        ch.start()
        status = ch.get_status()
        assert "camera_streaming" in status
        assert "camera_fps" in status
        assert "camera_frame_count" in status
        ch.stop()

    def test_starts_camera_if_not_streaming(self, mock_camera_client):
        mock_camera_client.is_streaming = False
        ch = CameraChannel(mock_camera_client)
        ch.start()
        mock_camera_client.start.assert_called_once()
        ch.stop()


# ---------------------------------------------------------------------------
# SpeakerChannel
# ---------------------------------------------------------------------------

class TestSpeakerChannel:
    def test_starts_and_stops(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.start()
        assert ch.is_running
        ch.stop()
        assert not ch.is_running

    def test_say_text_blocking(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.start()
        ch.say_text("Hello")
        mock_robot.behavior.say_text.assert_called_once_with("Hello")
        ch.stop()

    def test_say_text_nonblocking(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.start()
        ch.say_text("Hello", blocking=False)
        time.sleep(0.2)
        mock_robot.behavior.say_text.assert_called_once_with("Hello")
        ch.stop()

    def test_say_text_when_not_started(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.say_text("Hello")
        mock_robot.behavior.say_text.assert_not_called()

    def test_play_wav(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.start()
        ch.play_wav("/tmp/test.wav")
        mock_robot.audio.stream_wav_file.assert_called_once()
        ch.stop()

    def test_status_includes_playing(self, mock_robot):
        ch = SpeakerChannel(mock_robot)
        ch.start()
        status = ch.get_status()
        assert "playing" in status
        assert status["playing"] is False
        ch.stop()


# ---------------------------------------------------------------------------
# DisplayChannel
# ---------------------------------------------------------------------------

class TestDisplayChannel:
    def test_starts_and_stops(self, mock_robot):
        ch = DisplayChannel(mock_robot)
        ch.start()
        assert ch.is_running
        ch.stop()
        assert not ch.is_running

    def test_show_text_when_not_started(self, mock_robot):
        ch = DisplayChannel(mock_robot)
        ch.show_text("Hi")
        mock_robot.screen.set_screen_with_image_data.assert_not_called()

    @patch("apps.vector.src.media.display_channel.DisplayChannel.show_image")
    def test_show_text_calls_show_image(self, mock_show, mock_robot):
        ch = DisplayChannel(mock_robot)
        ch.start()
        ch.show_text("Hello", duration=1.0)
        mock_show.assert_called_once()
        ch.stop()

    def test_status_includes_display_dims(self, mock_robot):
        ch = DisplayChannel(mock_robot)
        ch.start()
        status = ch.get_status()
        assert status["display_width"] == 160
        assert status["display_height"] == 80
        ch.stop()


# ---------------------------------------------------------------------------
# MediaService
# ---------------------------------------------------------------------------

class TestMediaService:
    def test_create_with_all_deps(self, mock_camera_client, mock_robot):
        ms = MediaService(
            camera_client=mock_camera_client,
            robot=mock_robot,
        )
        assert ms.has_camera
        assert ms.has_speaker
        assert ms.has_display

    def test_create_minimal(self):
        ms = MediaService()
        assert not ms.has_camera
        assert not ms.has_speaker
        assert not ms.has_display
        # mic is always available
        assert ms.mic is not None

    def test_camera_raises_without_client(self):
        ms = MediaService()
        with pytest.raises(RuntimeError, match="CameraChannel not available"):
            ms.camera

    def test_speaker_raises_without_robot(self):
        ms = MediaService()
        with pytest.raises(RuntimeError, match="SpeakerChannel not available"):
            ms.speaker

    def test_display_raises_without_robot(self):
        ms = MediaService()
        with pytest.raises(RuntimeError, match="DisplayChannel not available"):
            ms.display

    def test_start_channel_by_name(self, mock_camera_client, mock_robot):
        ms = MediaService(camera_client=mock_camera_client, robot=mock_robot)
        ms.start_channel("speaker")
        assert ms.speaker.is_running
        ms.stop_channel("speaker")
        assert not ms.speaker.is_running

    def test_start_channel_invalid_name(self, mock_camera_client, mock_robot):
        ms = MediaService(camera_client=mock_camera_client, robot=mock_robot)
        with pytest.raises(ValueError, match="Unknown channel"):
            ms.start_channel("nonexistent")

    def test_start_channel_unavailable(self):
        ms = MediaService()
        with pytest.raises(RuntimeError, match="not available"):
            ms.start_channel("camera")

    def test_start_stop_all(self, mock_camera_client, mock_robot):
        ms = MediaService(camera_client=mock_camera_client, robot=mock_robot)
        ms.start()
        assert ms.speaker.is_running
        assert ms.display.is_running
        assert ms.camera.is_running
        ms.stop()
        assert not ms.speaker.is_running
        assert not ms.display.is_running
        assert not ms.camera.is_running

    def test_get_status(self, mock_camera_client, mock_robot):
        ms = MediaService(camera_client=mock_camera_client, robot=mock_robot)
        status = ms.get_status()
        assert "channels" in status
        assert "camera" in status["channels"]
        assert "mic" in status["channels"]
        assert "speaker" in status["channels"]
        assert "display" in status["channels"]

    def test_get_status_minimal(self):
        ms = MediaService()
        status = ms.get_status()
        assert status["channels"]["camera"] == {"available": False}
        assert status["channels"]["speaker"] == {"available": False}
        assert status["channels"]["display"] == {"available": False}
        # mic always has status
        assert "name" in status["channels"]["mic"]

    def test_independent_channel_lifecycle(self, mock_camera_client, mock_robot):
        """Channels can be started/stopped independently."""
        ms = MediaService(camera_client=mock_camera_client, robot=mock_robot)
        ms.start_channel("speaker")
        assert ms.speaker.is_running
        assert not ms.display.is_running
        assert not ms.camera.is_running

        ms.start_channel("camera")
        assert ms.camera.is_running
        assert ms.speaker.is_running

        ms.stop_channel("speaker")
        assert not ms.speaker.is_running
        assert ms.camera.is_running  # still running

        ms.stop()  # stops remaining
        assert not ms.camera.is_running
