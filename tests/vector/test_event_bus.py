"""Tests for the NUC event bus — thread safety, pub/sub, typed payloads."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    YOLO_PERSON_DETECTED,
    CommandReceivedEvent,
    EmergencyStopEvent,
    FaceRecognizedEvent,
    FollowStateChangedEvent,
    LiveKitSessionEvent,
    MotorCommandEvent,
    SttResultEvent,
    TtsPlayingEvent,
    YoloPersonDetectedEvent,
)
from apps.vector.src.events.sdk_events import SdkEventBridge


# ---------------------------------------------------------------------------
# NucEventBus core tests
# ---------------------------------------------------------------------------


class TestNucEventBus:
    def test_on_and_emit(self):
        bus = NucEventBus()
        received = []
        bus.on("test", received.append)
        bus.emit("test", 42)
        assert received == [42]

    def test_off_removes_listener(self):
        bus = NucEventBus()
        received = []
        bus.on("test", received.append)
        bus.off("test", received.append)
        bus.emit("test", "should_not_appear")
        assert received == []

    def test_off_nonexistent_is_noop(self):
        bus = NucEventBus()
        bus.off("no_such_event", lambda d: None)  # should not raise

    def test_emit_no_listeners(self):
        bus = NucEventBus()
        bus.emit("nobody_listening", {"data": 1})  # should not raise

    def test_emit_none_data(self):
        bus = NucEventBus()
        received = []
        bus.on("test", received.append)
        bus.emit("test")
        assert received == [None]

    def test_multiple_listeners(self):
        bus = NucEventBus()
        a, b = [], []
        bus.on("evt", a.append)
        bus.on("evt", b.append)
        bus.emit("evt", "hello")
        assert a == ["hello"]
        assert b == ["hello"]

    def test_different_events_isolated(self):
        bus = NucEventBus()
        a, b = [], []
        bus.on("alpha", a.append)
        bus.on("beta", b.append)
        bus.emit("alpha", 1)
        bus.emit("beta", 2)
        assert a == [1]
        assert b == [2]

    def test_once_fires_once(self):
        bus = NucEventBus()
        received = []
        bus.once("test", received.append)
        bus.emit("test", "first")
        bus.emit("test", "second")
        assert received == ["first"]

    def test_clear_specific_event(self):
        bus = NucEventBus()
        received = []
        bus.on("test", received.append)
        bus.clear("test")
        bus.emit("test", "gone")
        assert received == []

    def test_clear_all(self):
        bus = NucEventBus()
        a, b = [], []
        bus.on("a", a.append)
        bus.on("b", b.append)
        bus.clear()
        bus.emit("a", 1)
        bus.emit("b", 2)
        assert a == []
        assert b == []

    def test_listener_count(self):
        bus = NucEventBus()
        assert bus.listener_count("test") == 0
        cb = lambda d: None  # noqa: E731
        bus.on("test", cb)
        assert bus.listener_count("test") == 1
        bus.off("test", cb)
        assert bus.listener_count("test") == 0

    def test_duplicate_subscribe_ignored(self):
        bus = NucEventBus()
        received = []
        bus.on("test", received.append)
        bus.on("test", received.append)  # duplicate
        bus.emit("test", "x")
        assert received == ["x"]  # called only once

    def test_callback_exception_does_not_block_others(self):
        bus = NucEventBus()
        received = []

        def bad_cb(_data):
            raise RuntimeError("boom")

        bus.on("test", bad_cb)
        bus.on("test", received.append)
        bus.emit("test", "ok")
        assert received == ["ok"]  # second callback still fires


# ---------------------------------------------------------------------------
# Concurrent publish/subscribe stress test
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_emit_and_subscribe(self):
        bus = NucEventBus()
        results = []
        lock = threading.Lock()

        def listener(data):
            with lock:
                results.append(data)

        n_publishers = 4
        n_events_each = 50
        barrier = threading.Barrier(n_publishers + 1)

        def publisher(pub_id):
            barrier.wait()
            for i in range(n_events_each):
                bus.emit("stress", f"{pub_id}-{i}")

        # Subscribe before starting
        bus.on("stress", listener)

        threads = [
            threading.Thread(target=publisher, args=(pid,))
            for pid in range(n_publishers)
        ]
        for t in threads:
            t.start()
        barrier.wait()  # release all publishers at once
        for t in threads:
            t.join()

        assert len(results) == n_publishers * n_events_each

    def test_concurrent_subscribe_unsubscribe(self):
        bus = NucEventBus()

        def worker(worker_id):
            cb = lambda d: None  # noqa: E731
            for _ in range(100):
                bus.on("churn", cb)
                bus.emit("churn", worker_id)
                bus.off("churn", cb)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No exceptions = success; bus should be empty after all unsubscribes
        assert bus.listener_count("churn") == 0


# ---------------------------------------------------------------------------
# Typed event payload tests
# ---------------------------------------------------------------------------


class TestEventPayloads:
    def test_yolo_person_detected(self):
        evt = YoloPersonDetectedEvent(
            x=100.0, y=50.0, width=200.0, height=300.0, confidence=0.95
        )
        assert evt.frame_width == 800
        assert evt.frame_height == 600
        assert evt.confidence == 0.95

    def test_face_recognized(self):
        evt = FaceRecognizedEvent(
            name="Ophir", confidence=0.92, x=10, y=20, width=50, height=60
        )
        assert evt.name == "Ophir"

    def test_follow_state_changed(self):
        evt = FollowStateChangedEvent(state="tracking", target_name="Ophir")
        assert evt.state == "tracking"

    def test_motor_command(self):
        evt = MotorCommandEvent(left_speed_mmps=100.0, right_speed_mmps=-50.0)
        assert evt.duration_ms == 0

    def test_stt_result(self):
        evt = SttResultEvent(text="hello vector")
        assert evt.language == "en"
        assert evt.confidence == 1.0

    def test_command_received(self):
        evt = CommandReceivedEvent(
            command="follow", source="voice", args={"target": "Ophir"}
        )
        assert evt.args["target"] == "Ophir"

    def test_tts_playing(self):
        evt = TtsPlayingEvent(playing=True, text="Hello")
        assert evt.playing is True

    def test_livekit_session(self):
        evt = LiveKitSessionEvent(active=True, room="robot-cam")
        assert evt.room == "robot-cam"

    def test_emergency_stop(self):
        evt = EmergencyStopEvent(source="cliff", details="cliff_flags=3")
        assert evt.source == "cliff"

    def test_frozen_payloads(self):
        evt = YoloPersonDetectedEvent(
            x=0, y=0, width=0, height=0, confidence=0
        )
        with pytest.raises(AttributeError):
            evt.x = 999  # type: ignore[misc]

    def test_typed_payload_on_bus(self):
        bus = NucEventBus()
        received = []
        bus.on(YOLO_PERSON_DETECTED, received.append)
        payload = YoloPersonDetectedEvent(
            x=100, y=50, width=200, height=300, confidence=0.9
        )
        bus.emit(YOLO_PERSON_DETECTED, payload)
        assert len(received) == 1
        assert isinstance(received[0], YoloPersonDetectedEvent)
        assert received[0].confidence == 0.9


# ---------------------------------------------------------------------------
# SDK Event Bridge tests (mocked robot)
# ---------------------------------------------------------------------------


class TestSdkEventBridge:
    def _make_mock_robot(self):
        robot = MagicMock()
        robot.events.subscribe = MagicMock()
        robot.events.unsubscribe = MagicMock()
        return robot

    @patch("apps.vector.src.events.sdk_events.SdkEventBridge.__init__", return_value=None)
    def test_bridge_setup_without_sdk(self, mock_init):
        """When anki_vector is not installed, setup logs warning and returns."""
        bridge = SdkEventBridge.__new__(SdkEventBridge)
        bridge._robot = self._make_mock_robot()
        bridge._bus = NucEventBus()
        bridge._subscribed = False

        with patch.dict("sys.modules", {"anki_vector": None, "anki_vector.events": None}):
            with patch("apps.vector.src.events.sdk_events.logger"):
                # Import will fail, so setup should handle gracefully
                bridge.setup()
                # _subscribed should remain False since import fails
                assert bridge._subscribed is False

    def test_on_robot_state_cliff_emits_emergency_stop(self):
        robot = self._make_mock_robot()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)

        received = []
        bus.on(EMERGENCY_STOP, received.append)

        # Simulate a cliff detection message
        msg = MagicMock()
        msg.cliff_detected_flags = 3
        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(received) == 1
        assert isinstance(received[0], EmergencyStopEvent)
        assert received[0].source == "cliff"

    def test_on_robot_state_no_cliff_no_emit(self):
        robot = self._make_mock_robot()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)

        received = []
        bus.on(EMERGENCY_STOP, received.append)

        msg = MagicMock()
        msg.cliff_detected_flags = 0
        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(received) == 0

    def test_on_connection_lost_emits_emergency_stop(self):
        robot = self._make_mock_robot()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)

        received = []
        bus.on(EMERGENCY_STOP, received.append)

        bridge._on_connection_lost(robot, "connection_lost", MagicMock())

        assert len(received) == 1
        assert received[0].source == "connection_lost"

    def test_teardown(self):
        robot = self._make_mock_robot()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)
        bridge._subscribed = True

        with patch("apps.vector.src.events.sdk_events.Events", create=True):
            bridge.teardown()

        assert bridge._subscribed is False
