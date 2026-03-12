"""Tests for visual SLAM module — monocular visual odometry + occupancy grid."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import numpy as np
import pytest

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    MOTOR_COMMAND,
    SLAM_POSE_UPDATED,
    CliffTriggeredEvent,
    MotorCommandEvent,
    SlamPoseUpdatedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.visual_slam import (
    MIN_FEATURE_MATCHES,
    CellState,
    OccupancyGrid,
    Pose2D,
    VisualOdometry,
    VisualSLAM,
    WaypointNavigator,
    _bresenham,
    _normalise_angle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def slam(bus):
    s = VisualSLAM(bus, grid_size_mm=2000, cell_size_mm=50, n_features=100)
    s.start()
    yield s
    s.stop()


@pytest.fixture()
def grid():
    return OccupancyGrid(size_mm=2000, cell_size_mm=50)


@pytest.fixture()
def mock_motor():
    motor = MagicMock()
    motor.turn_then_drive = MagicMock()
    return motor


# ---------------------------------------------------------------------------
# Pose2D tests
# ---------------------------------------------------------------------------


class TestPose2D:
    def test_default_origin(self):
        p = Pose2D()
        assert p.x == 0.0
        assert p.y == 0.0
        assert p.theta == 0.0

    def test_copy_independence(self):
        p = Pose2D(100.0, 200.0, 1.5)
        c = p.copy()
        c.x = 999.0
        assert p.x == 100.0

    def test_custom_values(self):
        p = Pose2D(x=42.0, y=-10.0, theta=math.pi / 4)
        assert p.x == 42.0
        assert p.y == -10.0
        assert abs(p.theta - math.pi / 4) < 1e-10


# ---------------------------------------------------------------------------
# OccupancyGrid tests
# ---------------------------------------------------------------------------


class TestOccupancyGrid:
    def test_initial_state_all_unknown(self, grid):
        assert grid.free_cell_count == 0
        assert grid.occupied_cell_count == 0

    def test_set_and_get_cell(self, grid):
        grid.set_cell(0.0, 0.0, CellState.FREE)
        assert grid.get_cell(0.0, 0.0) == CellState.FREE

        grid.set_cell(100.0, 100.0, CellState.OCCUPIED)
        assert grid.get_cell(100.0, 100.0) == CellState.OCCUPIED

    def test_out_of_bounds_returns_unknown(self, grid):
        assert grid.get_cell(99999.0, 99999.0) == CellState.UNKNOWN

    def test_out_of_bounds_set_is_safe(self, grid):
        # Should not raise
        grid.set_cell(99999.0, 99999.0, CellState.FREE)
        assert grid.free_cell_count == 0

    def test_world_to_cell_centre(self, grid):
        row, col = grid.world_to_cell(0.0, 0.0)
        assert row == grid.grid_dim // 2
        assert col == grid.grid_dim // 2

    def test_mark_line_free(self, grid):
        grid.mark_line_free(0.0, 0.0, 200.0, 0.0)
        assert grid.free_cell_count > 0
        # Check that start and end cells are free
        assert grid.get_cell(0.0, 0.0) == CellState.FREE
        assert grid.get_cell(200.0, 0.0) == CellState.FREE

    def test_grid_dimension(self, grid):
        assert grid.grid_dim == 40  # 2000 / 50

    def test_cell_counts(self, grid):
        grid.set_cell(0.0, 0.0, CellState.FREE)
        grid.set_cell(100.0, 0.0, CellState.FREE)
        grid.set_cell(0.0, 100.0, CellState.OCCUPIED)
        assert grid.free_cell_count == 2
        assert grid.occupied_cell_count == 1


# ---------------------------------------------------------------------------
# Bresenham tests
# ---------------------------------------------------------------------------


class TestBresenham:
    def test_horizontal_line(self):
        cells = _bresenham(0, 0, 0, 3)
        assert (0, 0) in cells
        assert (0, 3) in cells
        assert len(cells) == 4

    def test_vertical_line(self):
        cells = _bresenham(0, 0, 3, 0)
        assert (0, 0) in cells
        assert (3, 0) in cells
        assert len(cells) == 4

    def test_diagonal_line(self):
        cells = _bresenham(0, 0, 3, 3)
        assert (0, 0) in cells
        assert (3, 3) in cells

    def test_single_point(self):
        cells = _bresenham(5, 5, 5, 5)
        assert cells == [(5, 5)]

    def test_reverse_direction(self):
        cells = _bresenham(3, 3, 0, 0)
        assert (0, 0) in cells
        assert (3, 3) in cells


# ---------------------------------------------------------------------------
# Normalise angle tests
# ---------------------------------------------------------------------------


class TestNormaliseAngle:
    def test_already_normalised(self):
        assert abs(_normalise_angle(0.5) - 0.5) < 1e-10

    def test_positive_overflow(self):
        result = _normalise_angle(3 * math.pi)
        assert -math.pi <= result <= math.pi
        assert abs(result - math.pi) < 1e-10

    def test_negative_overflow(self):
        result = _normalise_angle(-3 * math.pi)
        assert -math.pi <= result <= math.pi

    def test_two_pi_wraps_to_zero(self):
        result = _normalise_angle(2 * math.pi)
        assert abs(result) < 1e-10


# ---------------------------------------------------------------------------
# VisualOdometry tests
# ---------------------------------------------------------------------------


class TestVisualOdometry:
    def test_first_frame_returns_none(self):
        """First frame has no previous — delta_theta should be None."""
        vo = VisualOdometry(n_features=100)
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        delta, matches, ms = vo.process_frame(frame)
        assert delta is None
        assert matches == 0
        assert vo.frame_count == 1

    def test_identical_frames_small_rotation(self):
        """Two identical frames should match well; rotation ≈ 0."""
        vo = VisualOdometry(n_features=100)
        # Create a frame with some texture (random noise gives ORB features)
        rng = np.random.RandomState(42)
        frame = rng.randint(0, 255, (360, 640, 3), dtype=np.uint8)

        vo.process_frame(frame)
        delta, matches, ms = vo.process_frame(frame)

        # With identical frames, should get many matches
        if matches >= MIN_FEATURE_MATCHES:
            # Rotation between identical frames should be ~0
            assert delta is not None
            assert abs(delta) < 0.5  # less than ~30 degrees

    def test_blank_frame_no_features(self):
        """Blank frame produces no ORB features — should return None."""
        vo = VisualOdometry(n_features=100)
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        vo.process_frame(blank)
        delta, matches, ms = vo.process_frame(blank)
        assert delta is None
        assert matches == 0

    def test_get_descriptors_none_initially(self):
        vo = VisualOdometry(n_features=100)
        assert vo.get_descriptors() is None

    def test_get_descriptors_after_frame(self):
        vo = VisualOdometry(n_features=100)
        rng = np.random.RandomState(42)
        frame = rng.randint(0, 255, (360, 640, 3), dtype=np.uint8)
        vo.process_frame(frame)
        des = vo.get_descriptors()
        # Textured frame should produce descriptors
        assert des is not None

    def test_process_time_tracked(self):
        vo = VisualOdometry(n_features=100)
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        _, _, ms = vo.process_frame(frame)
        assert ms >= 0
        assert vo.last_process_time_ms >= 0


# ---------------------------------------------------------------------------
# VisualSLAM tests
# ---------------------------------------------------------------------------


class TestVisualSLAM:
    def test_initial_pose_at_origin(self, slam):
        pose = slam.get_pose()
        assert pose.x == 0.0
        assert pose.y == 0.0
        assert pose.theta == 0.0

    def test_motor_command_updates_pose(self, bus, slam):
        """Motor command event should update pose via dead reckoning."""
        event = MotorCommandEvent(
            left_speed_mmps=100.0, right_speed_mmps=100.0, duration_ms=1000
        )
        bus.emit(MOTOR_COMMAND, event)

        pose = slam.get_pose()
        # Straight forward at 100mm/s for 1s = 100mm
        assert abs(pose.x - 100.0) < 1.0
        assert abs(pose.y) < 1.0

    def test_motor_command_rotation(self, bus, slam):
        """Differential speeds should cause rotation."""
        # Right faster than left → turns left (CCW)
        event = MotorCommandEvent(
            left_speed_mmps=50.0, right_speed_mmps=100.0, duration_ms=500
        )
        bus.emit(MOTOR_COMMAND, event)

        pose = slam.get_pose()
        # Should have turned and moved forward somewhat
        assert pose.theta != 0.0

    def test_motor_command_zero_duration_ignored(self, bus, slam):
        """Zero-duration motor command should not change pose."""
        event = MotorCommandEvent(
            left_speed_mmps=100.0, right_speed_mmps=100.0, duration_ms=0
        )
        bus.emit(MOTOR_COMMAND, event)

        pose = slam.get_pose()
        assert pose.x == 0.0

    def test_cliff_marks_occupied(self, bus, slam):
        """Cliff event should mark cell ahead as occupied."""
        event = CliffTriggeredEvent(cliff_flags=0x01, timestamp_ms=0.0)
        bus.emit(CLIFF_TRIGGERED, event)

        grid = slam.get_grid()
        # Cell ~50mm ahead of origin (facing +x) should be occupied
        assert grid.get_cell(50.0, 0.0) == CellState.OCCUPIED

    def test_process_frame_increments_count(self, slam):
        """Processing a frame should increment frame counter."""
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        slam.process_frame(frame)
        assert slam.frames_processed == 1

    def test_process_frame_emits_event(self, bus, slam):
        """Processing a frame should emit SLAM_POSE_UPDATED event."""
        received = []
        bus.on(SLAM_POSE_UPDATED, lambda e: received.append(e))

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        slam.process_frame(frame)

        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, SlamPoseUpdatedEvent)
        assert evt.x == 0.0
        assert evt.y == 0.0

    def test_motor_then_frame_marks_free(self, bus, slam):
        """Moving then processing frame should mark traversed cells free."""
        # Move forward 200mm
        event = MotorCommandEvent(
            left_speed_mmps=100.0, right_speed_mmps=100.0, duration_ms=2000
        )
        bus.emit(MOTOR_COMMAND, event)

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        slam.process_frame(frame)

        grid = slam.get_grid()
        # Origin should be free (traversed)
        assert grid.get_cell(0.0, 0.0) == CellState.FREE

    def test_start_stop_idempotent(self, bus):
        """Starting/stopping multiple times should not error."""
        s = VisualSLAM(bus, grid_size_mm=1000, cell_size_mm=50)
        s.start()
        s.start()  # double start
        s.stop()
        s.stop()  # double stop

    def test_landmark_stored_periodically(self, slam):
        """Landmarks should be stored at LANDMARK_SAMPLE_INTERVAL."""
        rng = np.random.RandomState(42)
        frame = rng.randint(0, 255, (360, 640, 3), dtype=np.uint8)

        # Process enough frames to trigger landmark storage
        for _ in range(6):
            slam.process_frame(frame)

        # At least 1 landmark should be stored (frame 0 and frame 5)
        assert slam.landmark_count >= 1


# ---------------------------------------------------------------------------
# WaypointNavigator tests
# ---------------------------------------------------------------------------


class TestWaypointNavigator:
    def test_already_at_target(self, slam, mock_motor):
        """Should return True without moving if already within tolerance."""
        nav = WaypointNavigator(slam, mock_motor, arrival_tolerance_mm=100.0)
        result = nav.navigate_to(0.0, 0.0)
        assert result is True
        mock_motor.turn_then_drive.assert_not_called()

    def test_navigate_calls_motor(self, slam, mock_motor):
        """Should call turn_then_drive for distant targets."""
        nav = WaypointNavigator(slam, mock_motor, arrival_tolerance_mm=50.0)
        nav.navigate_to(500.0, 0.0)
        mock_motor.turn_then_drive.assert_called_once()
        args = mock_motor.turn_then_drive.call_args
        # Should turn ~0° (target is straight ahead) and drive ~500mm
        assert abs(args.kwargs["angle_deg"]) < 5.0
        assert abs(args.kwargs["distance_mm"] - 500.0) < 1.0

    def test_navigate_to_angled_target(self, slam, mock_motor):
        """Should compute correct turn angle for angled target."""
        nav = WaypointNavigator(slam, mock_motor, arrival_tolerance_mm=50.0)
        # Target at 90° (straight left)
        nav.navigate_to(0.0, 500.0)
        args = mock_motor.turn_then_drive.call_args
        # Should turn ~90° CCW
        assert abs(args.kwargs["angle_deg"] - 90.0) < 5.0

    def test_navigate_blocked_by_occupied_cell(self, bus, slam, mock_motor):
        """Should return False if target cell is occupied."""
        grid = slam.get_grid()
        grid.set_cell(500.0, 0.0, CellState.OCCUPIED)

        nav = WaypointNavigator(slam, mock_motor, arrival_tolerance_mm=50.0)
        result = nav.navigate_to(500.0, 0.0)
        assert result is False
        mock_motor.turn_then_drive.assert_not_called()

    def test_navigate_cliff_safety_error(self, slam, mock_motor):
        """Should return False on CliffSafetyError."""
        from apps.vector.src.motor_controller import CliffSafetyError

        mock_motor.turn_then_drive.side_effect = CliffSafetyError("cliff")
        nav = WaypointNavigator(slam, mock_motor, arrival_tolerance_mm=50.0)
        result = nav.navigate_to(500.0, 0.0)
        assert result is False


# ---------------------------------------------------------------------------
# Event type tests
# ---------------------------------------------------------------------------


class TestSlamEventTypes:
    def test_slam_pose_updated_event_fields(self):
        evt = SlamPoseUpdatedEvent(
            x=100.0,
            y=200.0,
            theta=1.5,
            feature_matches=42,
            process_time_ms=15.3,
            landmark_count=5,
            loop_closures=1,
            free_cells=100,
        )
        assert evt.x == 100.0
        assert evt.y == 200.0
        assert evt.theta == 1.5
        assert evt.feature_matches == 42
        assert evt.process_time_ms == 15.3
        assert evt.landmark_count == 5
        assert evt.loop_closures == 1
        assert evt.free_cells == 100

    def test_slam_pose_event_is_frozen(self):
        evt = SlamPoseUpdatedEvent(
            x=0, y=0, theta=0, feature_matches=0,
            process_time_ms=0, landmark_count=0,
            loop_closures=0, free_cells=0,
        )
        with pytest.raises(AttributeError):
            evt.x = 999  # type: ignore[misc]
