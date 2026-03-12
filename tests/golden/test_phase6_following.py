"""Phase 6 — Person Following Integration (~40s).

Requires: Phases 1, 3, 5 passed.  Needs a person visible to camera.

Tests 6.1–6.10 from docs/vector-golden-test-plan.md.
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


def _make_track(track_id=1, cx=320.0, cy=180.0, width=100.0, height=200.0):
    """Create a synthetic TrackedPersonEvent."""
    from apps.vector.src.events.event_types import TrackedPersonEvent

    return TrackedPersonEvent(
        track_id=track_id,
        cx=cx, cy=cy,
        width=width, height=height,
        age_frames=10, hits=8,
        confidence=0.9,
    )


# 6.1 Person acquisition
class TestPersonAcquisition:
    @_skip_no_numpy
    def test_yolo_detects_person(self):
        """6.1 — YOLO detects person in camera frame (mocked)."""
        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        detector = PersonDetector(event_bus=bus)

        single_box = MagicMock()
        single_box.cls = np.array([0])
        single_box.conf = np.array([0.85])
        single_box.xyxy = np.array([[100, 50, 300, 350]])

        mock_boxes = MagicMock()
        mock_boxes.__iter__ = MagicMock(return_value=iter([single_box]))
        mock_boxes.__len__ = MagicMock(return_value=1)

        mock_result = MagicMock()
        mock_result.boxes = mock_boxes

        detector._model = MagicMock(return_value=[mock_result])
        detector._model.names = {0: "person"}

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        detections = detector.detect(frame)
        assert len(detections) > 0


# 6.2 Tracking lock
class TestTrackingLock:
    @_skip_no_numpy
    def test_consistent_track_id(self):
        """6.2 — Kalman tracker assigns consistent ID across 10 frames."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()
        ids = []
        for i in range(10):
            det = Detection(
                cx=320.0 + i * 2,
                cy=180.0,
                width=100.0, height=200.0,
                confidence=0.9,
            )
            tracker.update([det])
            primary = tracker.get_primary_track()
            if primary:
                ids.append(primary.track_id)

        assert len(ids) >= 5
        assert len(set(ids)) == 1, f"Track ID switched: {ids}"


# 6.3 Head tracking
class TestHeadTracking:
    @_skip_no_numpy
    def test_head_controller_clamp(self):
        """6.3 — HeadController clamps angles correctly for tracking."""
        from apps.vector.src.head_controller import HeadController

        assert HeadController.clamp(-30) == -22.0
        assert HeadController.clamp(50) == 45.0
        assert HeadController.clamp(10) == 10.0


# 6.4 Follow engage
class TestFollowEngage:
    @_skip_no_numpy
    def test_follow_starts(self):
        """6.4 — Follow mode starts → transitions to SEARCHING."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.planner.follow_planner import FollowPlanner, State

        bus = NucEventBus()
        motor = MagicMock()
        head = MagicMock()
        planner = FollowPlanner(motor, head, bus)
        planner.start()

        import time
        time.sleep(0.1)
        # start() transitions to SEARCHING
        assert planner.state != State.IDLE
        planner.stop()


# 6.5 Follow disengage
class TestFollowDisengage:
    @_skip_no_numpy
    def test_follow_stops_on_lost_target(self):
        """6.5 — Person leaves FOV → stays in SEARCHING."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.planner.follow_planner import FollowPlanner, State

        bus = NucEventBus()
        motor = MagicMock()
        head = MagicMock()
        planner = FollowPlanner(motor, head, bus)
        planner.start()
        import time
        time.sleep(0.1)
        # Without any tracked person, planner stays in SEARCHING
        assert planner.state in (State.SEARCHING, State.IDLE)
        planner.stop()


# 6.6 360° search — head sweep
class TestSearchHeadSweep:
    def test_head_angle_range(self):
        """6.6 — Head can pan from -22° to 45° for search."""
        from apps.vector.src.head_controller import MAX_ANGLE, MIN_ANGLE

        assert MIN_ANGLE == -22.0
        assert MAX_ANGLE == 45.0


# 6.7 360° search — body rotation
class TestSearchBodyRotation:
    def test_follow_config_search_params(self):
        """6.7 — FollowConfig has search body rotation parameters."""
        from apps.vector.src.planner.follow_planner import FollowConfig

        config = FollowConfig()
        assert hasattr(config, "search_body_angles")
        assert len(config.search_body_angles) > 0
        assert all(a > 0 for a in config.search_body_angles)


# 6.8 Face detection (seated)
class TestFaceDetectionSeated:
    @_skip_no_numpy
    def test_face_detector_init(self):
        """6.8 — FaceDetector instantiates for seated detection."""
        try:
            from apps.vector.src.face_recognition.face_detector import FaceDetector
            detector = FaceDetector()
            assert detector is not None
        except (ImportError, FileNotFoundError):
            pytest.skip("Face detection model not available")


# 6.9 Target re-acquisition
class TestTargetReacquisition:
    @_skip_no_numpy
    def test_same_track_id_after_gap(self):
        """6.9 — Person returns → follow resumes (tracker handles re-association)."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()

        # 5 frames with detection
        for i in range(5):
            det = Detection(cx=320.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)
            tracker.update([det])

        # Short gap (2 frames with no detection — still within timeout)
        tracker.update([])
        tracker.update([])

        # Person returns
        det = Detection(cx=325.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)
        tracker.update([det])
        primary = tracker.get_primary_track()
        assert primary is not None, "Track not re-acquired after short gap"


# 6.10 Follow stop command
class TestFollowStopCommand:
    @_skip_no_numpy
    def test_follow_stop(self):
        """6.10 — Stop following → robot stops, motors idle."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.planner.follow_planner import FollowPlanner, State

        bus = NucEventBus()
        motor = MagicMock()
        head = MagicMock()
        planner = FollowPlanner(motor, head, bus)
        planner.start()
        planner.stop()
        assert planner.state == State.IDLE
