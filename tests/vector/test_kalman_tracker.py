"""Unit tests for Kalman tracker with synthetic detection sequences.

Tests cover:
- Single-track lifecycle (create, update, predict, expire)
- Position-only prediction (no bbox dimension oscillation)
- Multi-track IoU matching
- Track ID management and primary track selection
- Configurable prediction rate
- Thread safety
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from apps.vector.src.detector.kalman_tracker import (
    Detection,
    KalmanTrack,
    KalmanTracker,
    _iou,
)
from apps.vector.src.events.event_types import (
    TRACKED_PERSON,
    TrackedPersonEvent,
)


# ---------------------------------------------------------------------------
# KalmanTrack unit tests
# ---------------------------------------------------------------------------


class TestKalmanTrack:
    def test_init_state(self):
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)
        assert track.cx == pytest.approx(320.0)
        assert track.cy == pytest.approx(180.0)
        assert track.vx == pytest.approx(0.0)
        assert track.vy == pytest.approx(0.0)
        assert track.hits == 1
        assert track.age == 0

    def test_predict_advances_age(self):
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)
        track.predict(dt=0.1)
        assert track.age == 1
        assert track.time_since_update == 1

    def test_predict_with_velocity(self):
        """After enough measurements with motion, predict should extrapolate."""
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(100.0, 100.0)

        # Feed several measurements to establish velocity (Kalman needs time to converge)
        for i in range(10):
            track.predict(dt=0.1)
            track.update(100.0 + i * 10.0, 100.0, 100, 200, 0.9)

        last_cx = track.cx
        # Now predict — should extrapolate rightward
        cx, cy = track.predict(dt=0.1)
        assert cx > last_cx, "Should predict continued rightward motion"

    def test_update_freezes_dimensions(self):
        """Bbox dimensions should be frozen at last measured values."""
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)

        # Update with different dimensions
        track.update(320.0, 180.0, 120, 250, 0.85)
        assert track.width == 120
        assert track.height == 250

        # Predict should NOT change dimensions
        track.predict(dt=0.1)
        assert track.width == 120, "Dimensions must not change during predict"
        assert track.height == 250, "Dimensions must not change during predict"

        # Another predict
        track.predict(dt=0.1)
        assert track.width == 120
        assert track.height == 250

    def test_no_dimension_oscillation(self):
        """Verify dimensions don't oscillate — they only change on measurement."""
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)

        # Simulate oscillating YOLO bbox sizes (the R3 problem)
        sizes = [(157, 200), (373, 300), (248, 180), (388, 350), (160, 190)]

        for w, h in sizes:
            track.update(320.0, 180.0, w, h, 0.9)
            assert track.width == w, "Width should match last measurement exactly"
            assert track.height == h, "Height should match last measurement exactly"

            # Predictions between measurements should NOT change dimensions
            for _ in range(5):
                track.predict(dt=0.1)
                assert track.width == w
                assert track.height == h

    def test_update_resets_time_since_update(self):
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)

        track.predict(dt=0.1)
        track.predict(dt=0.1)
        assert track.time_since_update == 2

        track.update(320.0, 180.0, 100, 200, 0.9)
        assert track.time_since_update == 0

    def test_hit_count_increments(self):
        track = KalmanTrack(track_id=1, width=100, height=200, confidence=0.9)
        track.init_state(320.0, 180.0)
        assert track.hits == 1

        track.update(320.0, 180.0, 100, 200, 0.9)
        assert track.hits == 2

        track.update(325.0, 182.0, 100, 200, 0.85)
        assert track.hits == 3


# ---------------------------------------------------------------------------
# IoU tests
# ---------------------------------------------------------------------------


class TestIoU:
    def test_identical_boxes(self):
        assert _iou((100, 100, 50, 50), (100, 100, 50, 50)) == pytest.approx(1.0)

    def test_no_overlap(self):
        assert _iou((0, 0, 10, 10), (100, 100, 10, 10)) == pytest.approx(0.0)

    def test_partial_overlap(self):
        iou = _iou((50, 50, 40, 40), (60, 50, 40, 40))
        assert 0.0 < iou < 1.0

    def test_zero_area(self):
        assert _iou((50, 50, 0, 0), (50, 50, 10, 10)) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# KalmanTracker integration tests
# ---------------------------------------------------------------------------


class TestKalmanTracker:
    def test_single_detection_creates_track(self):
        tracker = KalmanTracker(min_hits=1)
        dets = [Detection(320, 180, 100, 200, 0.9)]
        confirmed = tracker.update(dets)
        assert len(confirmed) == 1
        assert confirmed[0].track_id == 1

    def test_min_hits_gate(self):
        """Track must receive min_hits measurements before being confirmed."""
        tracker = KalmanTracker(min_hits=3)
        det = Detection(320, 180, 100, 200, 0.9)

        # First update — only 1 hit (init_state counts as 1)
        confirmed = tracker.update([det])
        assert len(confirmed) == 0

        # Second update — 2 hits
        confirmed = tracker.update([det])
        assert len(confirmed) == 0

        # Third update — 3 hits
        confirmed = tracker.update([det])
        assert len(confirmed) == 1

    def test_track_expiry(self):
        """Track should be removed after max_age frames without measurement."""
        tracker = KalmanTracker(max_age=5, min_hits=1)
        tracker.update([Detection(320, 180, 100, 200, 0.9)])
        assert tracker.track_count == 1

        # Empty updates (no detections) — track ages
        for _ in range(4):
            tracker.update([])
        assert tracker.track_count == 1  # Still alive (time_since_update=4 < 5)

        tracker.update([])
        assert tracker.track_count == 0  # Expired (time_since_update=5 >= 5)

    def test_predict_returns_confirmed(self):
        tracker = KalmanTracker(min_hits=1)
        tracker.update([Detection(320, 180, 100, 200, 0.9)])
        confirmed = tracker.predict()
        assert len(confirmed) == 1

    def test_predict_only_no_dimension_change(self):
        """Predict-only calls must not alter frozen dimensions."""
        tracker = KalmanTracker(min_hits=1)
        tracker.update([Detection(320, 180, 100, 200, 0.9)])

        for _ in range(10):
            confirmed = tracker.predict()
            assert len(confirmed) == 1
            assert confirmed[0].width == 100
            assert confirmed[0].height == 200

    def test_multi_track_matching(self):
        """Two detections create two tracks, matched by IoU on next frame."""
        tracker = KalmanTracker(min_hits=1)

        # Frame 1: two people
        dets1 = [
            Detection(100, 180, 80, 200, 0.9),
            Detection(500, 180, 80, 200, 0.85),
        ]
        confirmed = tracker.update(dets1)
        assert len(confirmed) == 2

        # Frame 2: same two people, slightly moved
        dets2 = [
            Detection(105, 180, 80, 200, 0.9),
            Detection(495, 180, 80, 200, 0.85),
        ]
        confirmed = tracker.update(dets2)
        assert len(confirmed) == 2
        # Track IDs should be preserved (same tracks updated)
        ids = {t.track_id for t in confirmed}
        assert ids == {1, 2}

    def test_primary_track_prefers_most_hits(self):
        tracker = KalmanTracker(min_hits=1)

        # Person A: seen 5 times
        for _ in range(5):
            tracker.update([
                Detection(100, 180, 80, 200, 0.9),
                Detection(500, 180, 80, 200, 0.85),
            ])

        # Person B only has same 5 hits. Let's add 3 more for A only
        for _ in range(3):
            tracker.update([Detection(100, 180, 80, 200, 0.9)])

        primary = tracker.get_primary_track()
        assert primary is not None
        assert primary.track_id == 1  # Person A has more hits

    def test_clear(self):
        tracker = KalmanTracker(min_hits=1)
        tracker.update([Detection(320, 180, 100, 200, 0.9)])
        assert tracker.track_count == 1
        tracker.clear()
        assert tracker.track_count == 0

    def test_no_primary_track_when_empty(self):
        tracker = KalmanTracker()
        assert tracker.get_primary_track() is None

    def test_confirmed_count(self):
        tracker = KalmanTracker(min_hits=2)
        tracker.update([Detection(320, 180, 100, 200, 0.9)])
        assert tracker.confirmed_count == 0
        tracker.update([Detection(320, 180, 100, 200, 0.9)])
        assert tracker.confirmed_count == 1


# ---------------------------------------------------------------------------
# Synthetic sequence tests
# ---------------------------------------------------------------------------


class TestSyntheticSequences:
    def test_linear_motion_prediction(self):
        """Track a person moving linearly — predictions should follow."""
        tracker = KalmanTracker(min_hits=1, iou_threshold=0.1)

        # Person moves rightward at ~10px/frame (realistic for 10Hz updates)
        for i in range(10):
            cx = 100.0 + i * 10.0
            tracker.update([Detection(cx, 180, 80, 200, 0.9)])

        # Should be a single track (IoU matches across small movements)
        primary = tracker.get_primary_track()
        assert primary is not None
        last_cx = primary.cx

        confirmed = tracker.predict()
        primary_after = max(confirmed, key=lambda t: t.hits)
        assert primary_after.cx > last_cx, "Should predict continued rightward motion"

    def test_smooth_output_from_noisy_input(self):
        """Noisy YOLO detections should produce smoother Kalman output."""
        tracker = KalmanTracker(min_hits=1)
        rng = np.random.RandomState(42)

        true_positions = []
        measured_positions = []
        filtered_positions = []

        # Person at cx=320, stationary, with YOLO noise
        for i in range(30):
            true_cx = 320.0
            noise = rng.normal(0, 10)  # ~10px YOLO jitter
            measured_cx = true_cx + noise
            measured_positions.append(measured_cx)

            confirmed = tracker.update([
                Detection(measured_cx, 180, 80, 200, 0.9)
            ])
            if confirmed:
                filtered_positions.append(confirmed[0].cx)
                true_positions.append(true_cx)

        # Kalman output should have lower variance than raw measurements
        meas_var = np.var(measured_positions[-20:])
        filt_var = np.var(filtered_positions[-20:])
        assert filt_var < meas_var, (
            f"Kalman variance ({filt_var:.1f}) should be less than "
            f"measurement variance ({meas_var:.1f})"
        )

    def test_prediction_fills_gaps(self):
        """Between YOLO frames, predict should produce smooth positions."""
        tracker = KalmanTracker(min_hits=1)

        # Establish track
        for i in range(5):
            tracker.update([Detection(100.0 + i * 20, 180, 80, 200, 0.9)])

        # Now do 5 predict-only steps (simulating gap between YOLO frames)
        positions = []
        for _ in range(5):
            confirmed = tracker.predict()
            if confirmed:
                positions.append(confirmed[0].cx)

        # Positions should be monotonically increasing (continuing rightward)
        for i in range(1, len(positions)):
            assert positions[i] > positions[i - 1], (
                f"Position should increase: {positions}"
            )

    def test_detection_dropout_and_recovery(self):
        """Track survives a brief YOLO dropout and recovers on re-detection."""
        tracker = KalmanTracker(max_age=10, min_hits=1)

        # Establish track
        for i in range(5):
            tracker.update([Detection(320, 180, 80, 200, 0.9)])

        # 5 frames with no detection (dropout)
        for _ in range(5):
            tracker.predict()

        assert tracker.track_count == 1, "Track should survive brief dropout"

        # Re-detect — track should be updated (not a new track)
        confirmed = tracker.update([Detection(325, 182, 80, 200, 0.9)])
        assert len(confirmed) == 1
        assert confirmed[0].track_id == 1, "Should update existing track, not create new"


# ---------------------------------------------------------------------------
# Event type tests
# ---------------------------------------------------------------------------


class TestTrackedPersonEvent:
    def test_event_creation(self):
        event = TrackedPersonEvent(
            track_id=1, cx=320.0, cy=180.0,
            width=100, height=200,
            age_frames=10, hits=5, confidence=0.9,
        )
        assert event.track_id == 1
        assert event.cx == 320.0
        assert event.width == 100
        assert event.frame_width == 640

    def test_event_constant(self):
        assert TRACKED_PERSON == "tracked_person"


# ---------------------------------------------------------------------------
# Thread safety tests
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_update_and_predict(self):
        """Multiple threads calling update/predict should not crash."""
        tracker = KalmanTracker(min_hits=1, max_age=100)
        errors = []

        def updater():
            try:
                for i in range(50):
                    tracker.update([Detection(320 + i, 180, 80, 200, 0.9)])
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def predictor():
            try:
                for _ in range(100):
                    tracker.predict()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=updater),
            threading.Thread(target=predictor),
            threading.Thread(target=predictor),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread safety errors: {errors}"
