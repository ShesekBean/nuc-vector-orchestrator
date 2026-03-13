"""Phase 9 — Event Bus Integration.

Tests the hybrid event system (SDK events + NUC bus).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.phase9


class TestEventBusPubSub:
    def test_init_publish_subscribe(self):
        """9.1 — Bus creates, pub/sub works, events delivered in order."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        assert bus.listener_count("test_event") == 0

        received = []
        bus.on("order_test", lambda data: received.append(data))

        for i in range(10):
            bus.emit("order_test", i)

        assert received == list(range(10)), f"Out of order: {received}"


class TestSDKEventForwarding:
    def test_cliff_event_to_emergency_stop(self):
        """9.2 — SDK cliff event → NUC bus emergency_stop event."""
        from unittest.mock import MagicMock

        from apps.vector.src.events.event_types import EMERGENCY_STOP
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.events.sdk_events import SdkEventBridge

        bus = NucEventBus()
        received = []
        bus.on(EMERGENCY_STOP, lambda data: received.append(data))

        mock_robot = MagicMock()
        bridge = SdkEventBridge(mock_robot, bus)

        mock_msg = MagicMock()
        mock_msg.cliff_detected_flags = 0b0001
        mock_msg.touch_detected = False
        bridge._on_robot_state(mock_robot, "robot_state", mock_msg)

        assert len(received) == 1
        assert received[0].source == "cliff"
