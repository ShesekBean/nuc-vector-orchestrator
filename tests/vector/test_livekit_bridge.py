"""Tests for the LiveKit WebRTC bridge module.

All tests use mocked LiveKit SDK and Vector clients — no real
LiveKit Cloud connection or Vector hardware needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.vector.src.events.event_types import LIVEKIT_SESSION
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.livekit_bridge import (
    DEFAULT_LIVEKIT_URL,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    LiveKitBridge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_camera() -> MagicMock:
    """Create a mock CameraClient."""
    cam = MagicMock()
    # Return a minimal valid JPEG (SOI marker + padding)
    cam.get_latest_jpeg.return_value = None
    return cam


def _make_mock_audio() -> MagicMock:
    """Create a mock AudioClient."""
    audio = MagicMock()
    audio.get_latest_chunk.return_value = None
    return audio


def _make_mock_robot() -> MagicMock:
    """Create a mock robot."""
    robot = MagicMock()
    robot.behavior.say_text = MagicMock()
    robot.audio.stream_wav_file = MagicMock()
    return robot


def _make_bridge(**kwargs) -> LiveKitBridge:
    """Create a LiveKitBridge with mocked dependencies."""
    defaults = {
        "camera_client": _make_mock_camera(),
        "audio_client": _make_mock_audio(),
        "robot": _make_mock_robot(),
        "event_bus": NucEventBus(),
        "api_key": "test-key",
        "api_secret": "test-secret",
    }
    defaults.update(kwargs)
    return LiveKitBridge(**defaults)


# ---------------------------------------------------------------------------
# Construction and properties
# ---------------------------------------------------------------------------


class TestLiveKitBridgeInit:
    """Construction and default property tests."""

    def test_defaults(self):
        bridge = _make_bridge()
        assert not bridge.is_active
        assert bridge.room_name == ""
        assert bridge._livekit_url == DEFAULT_LIVEKIT_URL

    def test_custom_url(self):
        bridge = _make_bridge(livekit_url="wss://custom.example.com")
        assert bridge._livekit_url == "wss://custom.example.com"


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


class TestTokenGeneration:
    """Test that token generation produces a valid JWT."""

    def test_generate_token_returns_string(self):
        bridge = _make_bridge()
        token = bridge._generate_token("test-room")
        assert isinstance(token, str)
        assert len(token) > 0
        # JWT has 3 dot-separated parts
        parts = token.split(".")
        assert len(parts) == 3

    def test_generate_token_different_rooms(self):
        bridge = _make_bridge()
        t1 = bridge._generate_token("room-a")
        t2 = bridge._generate_token("room-b")
        # Different rooms should produce different tokens
        assert t1 != t2


# ---------------------------------------------------------------------------
# Frame conversion
# ---------------------------------------------------------------------------


class TestFrameConversion:
    """Test JPEG→VideoFrame and PCM→AudioFrame conversions."""

    def test_jpeg_to_video_frame_valid(self):
        """Valid JPEG should produce a VideoFrame with correct dimensions."""
        # Create a minimal valid image using PIL
        from PIL import Image
        import io
        img = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        jpeg_bytes = buf.getvalue()

        frame = LiveKitBridge._jpeg_to_video_frame(jpeg_bytes)
        assert frame is not None
        assert frame.width == FRAME_WIDTH
        assert frame.height == FRAME_HEIGHT

    def test_jpeg_to_video_frame_invalid(self):
        """Invalid JPEG bytes should return None."""
        frame = LiveKitBridge._jpeg_to_video_frame(b"not-a-jpeg")
        assert frame is None

    def test_jpeg_to_video_frame_empty(self):
        """Empty bytes should return None."""
        frame = LiveKitBridge._jpeg_to_video_frame(b"")
        assert frame is None

    # NOTE: _pcm_to_audio_frame tests removed — method was deleted when
    # mic audio publishing was removed (SDK AudioFeed provides signal_power
    # calibration tone, not raw PCM).  Audio is now receive-only via
    # _remote_audio_loop → _play_pcm_on_vector.


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------


class TestEventBus:
    """Test that LiveKitSessionEvent is emitted correctly."""

    def test_emit_session_start(self):
        bus = NucEventBus()
        bridge = _make_bridge(event_bus=bus)

        received = []
        bus.on(LIVEKIT_SESSION, lambda evt: received.append(evt))

        bridge._emit_session_event(active=True, room="test-room")
        assert len(received) == 1
        assert received[0].active is True
        assert received[0].room == "test-room"

    def test_emit_session_stop(self):
        bus = NucEventBus()
        bridge = _make_bridge(event_bus=bus)

        received = []
        bus.on(LIVEKIT_SESSION, lambda evt: received.append(evt))

        bridge._emit_session_event(active=False, room="test-room")
        assert len(received) == 1
        assert received[0].active is False

    def test_no_event_bus(self):
        """No crash when event_bus is None."""
        bridge = _make_bridge(event_bus=None)
        bridge._emit_session_event(active=True, room="test")  # should not raise


# ---------------------------------------------------------------------------
# Start/stop lifecycle (mocked LiveKit)
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Test start/stop with mocked LiveKit room."""

    @pytest.mark.asyncio
    async def test_start_connects_to_room(self):
        bridge = _make_bridge()

        mock_room = MagicMock()
        mock_room.connect = AsyncMock()
        mock_room.disconnect = AsyncMock()
        mock_room.on = MagicMock()
        mock_room.local_participant = MagicMock()
        mock_room.local_participant.publish_track = AsyncMock()

        with (
            patch("apps.vector.src.livekit_bridge.rtc.Room", return_value=mock_room),
            patch("apps.vector.src.livekit_bridge.rtc.VideoSource"),
            patch("apps.vector.src.livekit_bridge.rtc.LocalVideoTrack") as mock_vt,
            patch("apps.vector.src.livekit_bridge.rtc.AudioSource"),
            patch("apps.vector.src.livekit_bridge.rtc.LocalAudioTrack") as mock_at,
        ):
            mock_vt.create_video_track.return_value = MagicMock()
            mock_at.create_audio_track.return_value = MagicMock()

            await bridge.start(room="test-room")

            assert bridge.is_active
            assert bridge.room_name == "test-room"
            mock_room.connect.assert_called_once()
            # Should publish both video and audio tracks
            assert mock_room.local_participant.publish_track.call_count == 2

            # Cleanup
            await bridge.stop()
            assert not bridge.is_active

    @pytest.mark.asyncio
    async def test_start_when_already_active(self):
        bridge = _make_bridge()
        bridge._active = True
        bridge._room_name = "existing"

        # Should return early without error
        await bridge.start(room="new-room")
        assert bridge.room_name == "existing"

    @pytest.mark.asyncio
    async def test_stop_when_not_active(self):
        bridge = _make_bridge()
        # Should not raise
        await bridge.stop()
        assert not bridge.is_active

    @pytest.mark.asyncio
    async def test_get_status(self):
        bridge = _make_bridge()
        status = await bridge.get_status()
        assert status["active"] is False
        assert status["room"] == ""
        assert "livekit_url" in status

    @pytest.mark.asyncio
    async def test_cleanup_cancels_tasks(self):
        bridge = _make_bridge()

        # Simulate active tasks
        async def _forever():
            await asyncio.sleep(3600)

        bridge._video_task = asyncio.create_task(_forever())
        bridge._audio_sub_task = asyncio.create_task(_forever())
        bridge._active = True

        await bridge._cleanup()

        assert bridge._video_task is None
        assert bridge._audio_sub_task is None
        assert not bridge.is_active
