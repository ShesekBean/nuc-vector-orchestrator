"""Phase 6 — Person Following Integration.

Tests follow planner state machine and tracker re-acquisition.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


pytestmark = [pytest.mark.phase6, pytest.mark.robot]

_skip_no_numpy = pytest.mark.skipif(np is None, reason="numpy not installed")


class TestFollowLifecycle:
    @_skip_no_numpy
    def test_start_search_stop(self):
        """6.1 — Follow: start → SEARCHING → stop → IDLE."""
        import time
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.planner.follow_planner import FollowPlanner, State

        bus = NucEventBus()
        planner = FollowPlanner(MagicMock(), MagicMock(), bus)

        planner.start()
        time.sleep(0.1)
        assert planner.state != State.IDLE

        planner.stop()
        assert planner.state == State.IDLE


class TestTargetReacquisition:
    @_skip_no_numpy
    def test_track_survives_short_gap(self):
        """6.2 — Person disappears for 2 frames then returns → track re-acquired."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()

        # 5 frames with detection
        for i in range(5):
            tracker.update([Detection(cx=320.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)])

        # Short gap
        tracker.update([])
        tracker.update([])

        # Person returns
        tracker.update([Detection(cx=325.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)])
        assert tracker.get_primary_track() is not None, "Track not re-acquired after short gap"


class TestHeadControllerClamp:
    @_skip_no_numpy
    def test_head_angle_clamping(self):
        """6.3 — HeadController clamps angles to [-22°, 45°] for tracking."""
        from apps.vector.src.head_controller import HeadController

        assert HeadController.clamp(-30) == -22.0
        assert HeadController.clamp(50) == 45.0
        assert HeadController.clamp(10) == 10.0
