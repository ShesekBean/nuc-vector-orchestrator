"""Phase 9 — Event Bus Integration (~8s).

Tests the hybrid event system (SDK events + NUC bus).

Tests 9.1–9.5 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations


import pytest


pytestmark = pytest.mark.phase9


# 9.1 NUC event bus init
class TestNucEventBusInit:
    def test_bus_creates(self):
        """9.1 — Event bus creates without error."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        assert bus is not None
        assert bus.listener_count("test_event") == 0


# 9.2 Event publish/subscribe
class TestEventPubSub:
    def test_publish_subscribe(self):
        """9.2 — Publish test event → subscriber receives it."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        received = []
        bus.on("test_event", lambda data: received.append(data))
        bus.emit("test_event", {"value": 42})
        assert len(received) == 1
        assert received[0]["value"] == 42


# 9.3 SDK event forwarding
class TestSDKEventForwarding:
    def test_cliff_event_forwarding(self):
        """9.3 — SDK cliff event → NUC bus emergency_stop event."""
        from unittest.mock import MagicMock

        from apps.vector.src.events.event_types import EMERGENCY_STOP
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.events.sdk_events import SdkEventBridge

        bus = NucEventBus()
        received = []
        bus.on(EMERGENCY_STOP, lambda data: received.append(data))

        mock_robot = MagicMock()
        bridge = SdkEventBridge(mock_robot, bus)

        # Simulate cliff detection via robot_state handler
        mock_msg = MagicMock()
        mock_msg.cliff_detected_flags = 0b0001  # front-left cliff
        mock_msg.touch_detected = False
        bridge._on_robot_state(mock_robot, "robot_state", mock_msg)

        assert len(received) == 1
        assert received[0].source == "cliff"


# 9.4 Event type registry
class TestEventTypeRegistry:
    def test_all_event_types_defined(self):
        """9.4 — All expected event types registered as constants."""
        from apps.vector.src.events import event_types

        expected = [
            "YOLO_PERSON_DETECTED",
            "FACE_RECOGNIZED",
            "FOLLOW_STATE_CHANGED",
            "MOTOR_COMMAND",
            "STT_RESULT",
            "COMMAND_RECEIVED",
            "TTS_PLAYING",
            "EMERGENCY_STOP",
            "CLIFF_TRIGGERED",
            "TOUCH_DETECTED",
            "TRACKED_PERSON",
            "SCENE_DESCRIPTION",
            "WAKE_WORD_DETECTED",
            "LIFT_HEIGHT_CHANGED",
            "LED_STATE_CHANGED",
            "SLAM_POSE_UPDATED",
        ]
        for name in expected:
            assert hasattr(event_types, name), f"Missing event type: {name}"
            val = getattr(event_types, name)
            assert isinstance(val, str), f"{name} should be a string constant"


# 9.5 Event ordering
class TestEventOrdering:
    def test_events_in_publish_order(self):
        """9.5 — Events delivered in publish order."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        received = []
        bus.on("order_test", lambda data: received.append(data))

        for i in range(10):
            bus.emit("order_test", i)

        assert received == list(range(10)), f"Out of order: {received}"
