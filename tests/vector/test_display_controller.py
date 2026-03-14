"""Tests for apps.vector.src.display_controller — face OLED rendering + animation."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.display_controller import (
    EXPRESSIONS,
    SCREEN_H,
    SCREEN_W,
    DisplayController,
    render_face,
    render_status,
)


# ---------------------------------------------------------------------------
# render_face / render_status (pure functions, no robot needed)
# ---------------------------------------------------------------------------

class TestRenderFace:
    """Tests for the render_face() helper."""

    def test_returns_correct_size(self):
        img = render_face("idle")
        assert img.size == (SCREEN_W, SCREEN_H)

    def test_returns_rgb_mode(self):
        img = render_face("idle")
        assert img.mode == "RGB"

    def test_all_expressions_render(self):
        for name in EXPRESSIONS:
            img = render_face(name)
            assert img.size == (SCREEN_W, SCREEN_H), f"Failed for {name}"

    def test_unknown_expression_uses_idle_geometry(self):
        """Unknown expression falls back to idle face geometry."""
        img = render_face("nonexistent", frame_num=5)  # non-blink frame
        # Should produce a valid image (not crash)
        assert img.size == (SCREEN_W, SCREEN_H)
        # Should have drawn something (not all black)
        assert img.tobytes() != b"\x00" * (SCREEN_W * SCREEN_H * 3)

    def test_blink_changes_frame(self):
        """Frame 0 has blink squish; frame 5 does not — pixels differ."""
        img_blink = render_face("idle", frame_num=0)
        img_open = render_face("idle", frame_num=5)
        assert img_blink.tobytes() != img_open.tobytes()

    def test_speaking_animation_varies(self):
        frames = [render_face("speaking", frame_num=i) for i in range(8)]
        pixel_data = [f.tobytes() for f in frames]
        unique_frames = len(set(pixel_data))
        assert unique_frames > 1

    def test_thinking_dots_animate(self):
        frames = [render_face("thinking", frame_num=i) for i in range(9)]
        pixel_data = [f.tobytes() for f in frames]
        unique_frames = len(set(pixel_data))
        assert unique_frames > 1

    def test_has_non_black_pixels(self):
        """Every expression should draw something (not all-black)."""
        black = b"\x00" * (SCREEN_W * SCREEN_H * 3)
        for name in EXPRESSIONS:
            img = render_face(name, frame_num=5)  # avoid blink frame
            assert img.tobytes() != black, f"Expression {name} is all black"


class TestRenderStatus:
    """Tests for the render_status() helper."""

    def test_returns_correct_size(self):
        img = render_status(battery_pct=50)
        assert img.size == (SCREEN_W, SCREEN_H)

    def test_battery_indicator_draws_pixels(self):
        img = render_status(battery_pct=80)
        black = b"\x00" * (SCREEN_W * SCREEN_H * 3)
        assert img.tobytes() != black

    def test_wifi_indicator_draws_pixels(self):
        img = render_status(wifi_strength=3)
        black = b"\x00" * (SCREEN_W * SCREEN_H * 3)
        assert img.tobytes() != black

    def test_detection_text_draws_pixels(self):
        img = render_status(detection_text="Ophir")
        black = b"\x00" * (SCREEN_W * SCREEN_H * 3)
        assert img.tobytes() != black

    def test_all_status_fields(self):
        img = render_status(battery_pct=50, wifi_strength=2, detection_text="test")
        raw = img.tobytes()
        non_zero = sum(1 for b in raw if b != 0)
        assert non_zero > 30  # Multiple elements drawn

    def test_empty_status_is_black(self):
        img = render_status()
        black = b"\x00" * (SCREEN_W * SCREEN_H * 3)
        assert img.tobytes() == black

    def test_battery_low_color(self):
        """Battery <= 10 should use red pixels."""
        img = render_status(battery_pct=5)
        raw = img.tobytes()
        # Check for red pixels: R > 200, G < 50, B < 50 in RGB triplets
        has_red = False
        for i in range(0, len(raw) - 2, 3):
            if raw[i] > 200 and raw[i + 1] < 50 and raw[i + 2] < 50:
                has_red = True
                break
        assert has_red, "Low battery should have red pixels"

    def test_battery_clamped(self):
        """battery_pct > 100 should be clamped, not crash."""
        img = render_status(battery_pct=150)
        assert img.size == (SCREEN_W, SCREEN_H)

        img2 = render_status(battery_pct=-5)
        assert img2.size == (SCREEN_W, SCREEN_H)


# ---------------------------------------------------------------------------
# DisplayController (mocked robot)
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.screen.set_screen_with_image_data = MagicMock()
    return robot


@pytest.fixture()
def mock_bus():
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    return NucEventBus()


class TestDisplayController:
    """Tests for DisplayController lifecycle and API."""

    def test_set_expression(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        ctrl.set_expression("happy")
        assert ctrl.expression == "happy"

    def test_set_expression_unknown_falls_back(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        ctrl.set_expression("confused")
        assert ctrl.expression == "idle"

    def test_initial_state(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        assert ctrl.expression == "idle"
        assert ctrl.mode == "face"

    def test_show_status_switches_mode(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        ctrl.show_status(battery_pct=50)
        assert ctrl.mode == "status"

    def test_set_expression_returns_to_face_mode(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        ctrl.show_status(battery_pct=50)
        ctrl.set_expression("happy")
        assert ctrl.mode == "face"

    @patch("apps.vector.src.display_controller.convert_image_to_screen_data",
           create=True)
    def test_show_image(self, mock_convert, mock_robot):
        """show_image sends an arbitrary PIL image to the screen."""
        from PIL import Image
        mock_convert.return_value = b"\x00" * 100

        ctrl = DisplayController(mock_robot)
        img = Image.new("RGB", (SCREEN_W, SCREEN_H))
        ctrl.show_image(img)
        mock_robot.screen.set_screen_with_image_data.assert_called_once()

    def test_start_stop(self, mock_robot):
        """Animation thread starts and stops cleanly."""
        ctrl = DisplayController(mock_robot, fps=10)
        ctrl.start()
        assert ctrl._thread is not None
        assert ctrl._thread.is_alive()
        ctrl.stop()
        assert ctrl._thread is None

    def test_double_start_is_safe(self, mock_robot):
        ctrl = DisplayController(mock_robot, fps=10)
        ctrl.start()
        ctrl.start()  # Should not crash or spawn a second thread
        ctrl.stop()

    def test_fps_clamping(self, mock_robot):
        ctrl_low = DisplayController(mock_robot, fps=0)
        assert ctrl_low._fps == 1

        ctrl_high = DisplayController(mock_robot, fps=100)
        assert ctrl_high._fps == 30


class TestDisplayControllerEvents:
    """Tests for event bus integration."""

    def test_subscribes_to_events(self, mock_robot, mock_bus):
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        from apps.vector.src.events.event_types import (
            FACE_RECOGNIZED,
            FOLLOW_STATE_CHANGED,
            TOUCH_DETECTED,
            TTS_PLAYING,
            COMMAND_RECEIVED,
            STT_RESULT,
        )
        for event_name in [FACE_RECOGNIZED, FOLLOW_STATE_CHANGED,
                           TOUCH_DETECTED, TTS_PLAYING,
                           COMMAND_RECEIVED, STT_RESULT]:
            assert mock_bus.listener_count(event_name) == 1
        ctrl.stop()

    def test_stop_unsubscribes(self, mock_robot, mock_bus):
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        ctrl.stop()
        from apps.vector.src.events.event_types import FACE_RECOGNIZED
        assert mock_bus.listener_count(FACE_RECOGNIZED) == 0

    def test_face_recognized_sets_happy(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import FACE_RECOGNIZED, FaceRecognizedEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(FACE_RECOGNIZED, FaceRecognizedEvent(
            name="Ophir", confidence=0.9, x=100, y=50, width=40, height=40
        ))
        assert ctrl.expression == "happy"
        ctrl.stop()

    def test_follow_state_idle(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import FOLLOW_STATE_CHANGED, FollowStateChangedEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(FOLLOW_STATE_CHANGED, FollowStateChangedEvent(state="searching"))
        assert ctrl.expression == "thinking"
        ctrl.stop()

    def test_touch_sets_happy(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import TOUCH_DETECTED, TouchDetectedEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(TOUCH_DETECTED, TouchDetectedEvent(is_pressed=True))
        assert ctrl.expression == "happy"
        ctrl.stop()

    def test_tts_playing_sets_speaking(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import TTS_PLAYING, TtsPlayingEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hello"))
        assert ctrl.expression == "speaking"
        ctrl.stop()

    def test_tts_stopped_sets_idle(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import TTS_PLAYING, TtsPlayingEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False))
        assert ctrl.expression == "idle"
        ctrl.stop()

    def test_command_sets_thinking(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import COMMAND_RECEIVED, CommandReceivedEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(COMMAND_RECEIVED, CommandReceivedEvent(command="dance", source="voice"))
        assert ctrl.expression == "thinking"
        ctrl.stop()

    def test_stt_sets_listening(self, mock_robot, mock_bus):
        from apps.vector.src.events.event_types import STT_RESULT, SttResultEvent
        ctrl = DisplayController(mock_robot, event_bus=mock_bus)
        mock_bus.emit(STT_RESULT, SttResultEvent(text="hello"))
        assert ctrl.expression == "listening"
        ctrl.stop()


class TestDisplayControllerThreadSafety:
    """Concurrent access tests."""

    def test_concurrent_expression_changes(self, mock_robot):
        ctrl = DisplayController(mock_robot)
        errors = []

        def set_many(name, count):
            try:
                for _ in range(count):
                    ctrl.set_expression(name)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=set_many, args=("happy", 100)),
            threading.Thread(target=set_many, args=("sad", 100)),
            threading.Thread(target=set_many, args=("thinking", 100)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert ctrl.expression in EXPRESSIONS
