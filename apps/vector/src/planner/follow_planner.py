"""Person-following planner for Vector's differential drive.

State machine:  IDLE → SEARCHING → TRACKING → FOLLOWING → SEARCHING
PD controller:  camera detection → Kalman smoothing → motor commands
Head tracking:  vertical P-controller on person center-y
Body rotation:  horizontal PD on person center-x (differential drive)
Search:         head sweep → body rotation scan (360° with YOLO + face)

Usage::

    from apps.vector.src.planner.follow_planner import FollowPlanner

    planner = FollowPlanner(motor_controller, head_controller, nuc_bus)
    planner.start()   # begins SEARCHING state
    # ... planner subscribes to TRACKED_PERSON events from Kalman tracker
    planner.stop()    # returns to IDLE, stops motors
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    FOLLOW_STATE_CHANGED,
    MOTOR_COMMAND,
    TRACKED_PERSON,
    FollowStateChangedEvent,
    MotorCommandEvent,
    TrackedPersonEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.motor_controller import MotorController

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_W = 640
FRAME_H = 360


class State(enum.Enum):
    """Follow planner states."""

    IDLE = "idle"
    SEARCHING = "searching"
    TRACKING = "tracking"
    FOLLOWING = "following"


@dataclass
class FollowConfig:
    """Tunable follow parameters.

    All PD gains, dead zones, speed limits, and search parameters live here
    so they can be adjusted without touching planner logic.
    """

    # --- PD gains for turning (horizontal centering) ---
    kp_turn: float = 0.35
    kd_turn: float = 0.06
    dead_zone_x: float = 30.0  # pixels — no turn if error < this

    # --- P gain for driving (distance control via bbox height) ---
    kp_drive: float = 0.8
    kd_drive: float = 0.1
    target_height: float = 150.0  # target bbox height in pixels (~1m)
    dead_zone_h: float = 15.0  # pixels — no drive if height error < this

    # --- P gain for head pitch (vertical tracking) ---
    kp_head: float = 0.15  # degrees per pixel of vertical offset

    # --- Speed limits ---
    max_wheel_speed: float = 120.0  # mm/s safety cap
    min_tracking_confidence: float = 0.3

    # --- Tracking thresholds ---
    min_hits_for_following: int = 5  # Kalman hits before TRACKING → FOLLOWING
    target_lost_frames: int = 15  # frames without detection → SEARCHING

    # --- Search behaviour ---
    search_head_angles: tuple[float, ...] = (10.0, 30.0, -10.0, 0.0)
    search_head_dwell_s: float = 0.8  # seconds to wait at each head angle
    search_body_angles: tuple[float, ...] = (90.0, 90.0, 90.0, 90.0)
    search_body_dwell_s: float = 1.0
    search_turn_speed_dps: float = 80.0

    # --- Control loop ---
    loop_hz: float = 10.0  # target control loop rate


# ---------------------------------------------------------------------------
# PD Controller
# ---------------------------------------------------------------------------


class PDController:
    """PD controller for differential-drive person following.

    Computes (left_speed, right_speed) from tracked person position.
    Turn: PD on horizontal offset from image center.
    Drive: PD on bbox height error (target height = follow distance proxy).
    """

    def __init__(self, config: FollowConfig) -> None:
        self._cfg = config
        self._prev_error_x: float = 0.0
        self._prev_error_h: float = 0.0
        self._prev_time: float = 0.0

    def reset(self) -> None:
        """Reset derivative state (call on state transitions)."""
        self._prev_error_x = 0.0
        self._prev_error_h = 0.0
        self._prev_time = 0.0

    def compute(
        self, center_x: float, bbox_height: float
    ) -> tuple[float, float]:
        """Compute wheel speeds from detection position.

        Args:
            center_x: X-center of detected person (0=left, FRAME_W=right).
            bbox_height: Height of bounding box in pixels.

        Returns:
            (left_speed_mmps, right_speed_mmps) clamped to max_wheel_speed.
        """
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time > 0 else 0.1
        if dt <= 0:
            dt = 0.1
        self._prev_time = now

        cfg = self._cfg

        # --- Turn error: offset from image center (positive = person right) ---
        error_x = center_x - (FRAME_W / 2)
        d_error_x = (error_x - self._prev_error_x) / dt

        # --- Drive error: height difference (positive = too far away) ---
        error_h = cfg.target_height - bbox_height
        d_error_h = (error_h - self._prev_error_h) / dt

        # PD outputs
        turn_speed = cfg.kp_turn * error_x + cfg.kd_turn * d_error_x
        drive_speed = cfg.kp_drive * error_h + cfg.kd_drive * d_error_h

        # Dead zones
        if abs(error_x) < cfg.dead_zone_x:
            turn_speed = 0.0
        if abs(error_h) < cfg.dead_zone_h:
            drive_speed = 0.0

        # Differential drive: turn adds to one side, subtracts from the other
        left = drive_speed + turn_speed
        right = drive_speed - turn_speed

        # Clamp
        max_spd = cfg.max_wheel_speed
        left = max(-max_spd, min(max_spd, left))
        right = max(-max_spd, min(max_spd, right))

        # Save state for derivative
        self._prev_error_x = error_x
        self._prev_error_h = error_h

        return left, right


# ---------------------------------------------------------------------------
# Follow Planner
# ---------------------------------------------------------------------------


class FollowPlanner:
    """Person-following planner with state machine and event-bus integration.

    Subscribes to ``TRACKED_PERSON`` events (from Kalman tracker) and emits
    ``FOLLOW_STATE_CHANGED`` + ``MOTOR_COMMAND`` events.

    Thread-safe: the control loop runs in a daemon thread; start/stop can
    be called from any thread.

    Args:
        motor_controller: Cliff-safe motor controller.
        head_controller: Head servo controller.
        nuc_bus: NUC event bus for pub/sub.
        config: Tunable follow parameters (optional).
        search_detector: Optional callable(frame) returning detections for
            search mode.  Not used in normal follow (Kalman handles that).
    """

    def __init__(
        self,
        motor_controller: MotorController,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
        config: FollowConfig | None = None,
    ) -> None:
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus
        self._cfg = config or FollowConfig()
        self._pd = PDController(self._cfg)

        # State
        self._state = State.IDLE
        self._state_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        # Latest tracked person from event bus
        self._latest_track: TrackedPersonEvent | None = None
        self._track_lock = threading.Lock()
        self._frames_without_track: int = 0

        # Search state
        self._search_found = False

    # -- Properties ----------------------------------------------------------

    @property
    def state(self) -> State:
        with self._state_lock:
            return self._state

    @property
    def config(self) -> FollowConfig:
        return self._cfg

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start following — transitions to SEARCHING."""
        with self._state_lock:
            if self._running:
                logger.warning("FollowPlanner already running")
                return
            self._running = True

        self._bus.on(TRACKED_PERSON, self._on_tracked_person)
        self._bus.on(EMERGENCY_STOP, self._on_emergency_stop)

        self._transition(State.SEARCHING)

        self._thread = threading.Thread(
            target=self._control_loop, name="follow-planner", daemon=True
        )
        self._thread.start()
        logger.info("FollowPlanner started")

    def stop(self) -> None:
        """Stop following — returns to IDLE, stops motors."""
        with self._state_lock:
            if not self._running:
                return
            self._running = False

        self._bus.off(TRACKED_PERSON, self._on_tracked_person)
        self._bus.off(EMERGENCY_STOP, self._on_emergency_stop)

        # Stop motors
        try:
            self._motor.drive_wheels(0, 0)
        except Exception:
            logger.exception("Failed to stop motors on planner stop")

        self._transition(State.IDLE)

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        logger.info("FollowPlanner stopped")

    # -- Event callbacks -----------------------------------------------------

    def _on_tracked_person(self, event: TrackedPersonEvent) -> None:
        """Receive smoothed track from Kalman tracker."""
        with self._track_lock:
            self._latest_track = event
            self._frames_without_track = 0

    def _on_emergency_stop(self, _event: Any) -> None:
        """Emergency stop — return to IDLE."""
        logger.warning("Emergency stop received — halting follow planner")
        with self._state_lock:
            self._running = False
        self._transition(State.IDLE)

    # -- State transitions ---------------------------------------------------

    def _transition(self, new_state: State) -> None:
        """Transition to a new state with logging and event emission."""
        with self._state_lock:
            old = self._state
            if old == new_state:
                return
            self._state = new_state

        logger.info("Follow state: %s → %s", old.value, new_state.value)

        self._bus.emit(
            FOLLOW_STATE_CHANGED,
            FollowStateChangedEvent(state=new_state.value),
        )

        # Reset PD on state change to avoid derivative kick
        self._pd.reset()
        self._frames_without_track = 0

    # -- Control loop --------------------------------------------------------

    def _control_loop(self) -> None:
        """Main control loop — runs at config.loop_hz in daemon thread."""
        period = 1.0 / self._cfg.loop_hz

        while self._running:
            loop_start = time.monotonic()

            try:
                state = self.state
                if state == State.SEARCHING:
                    self._tick_searching()
                elif state == State.TRACKING:
                    self._tick_tracking()
                elif state == State.FOLLOWING:
                    self._tick_following()
                # IDLE: do nothing
            except Exception:
                logger.exception("Error in follow planner control loop")

            # Rate limit
            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # -- State tick handlers -------------------------------------------------

    def _tick_searching(self) -> None:
        """SEARCHING: head sweep then body rotation to find target."""
        # Check if Kalman tracker already has a target
        track = self._consume_track()
        if track is not None:
            self._transition(State.TRACKING)
            return

        # Head sweep
        self._search_found = False
        for angle in self._cfg.search_head_angles:
            if not self._running:
                return
            try:
                self._head.set_angle(angle)
            except Exception:
                logger.exception("Head set_angle failed during search")
            time.sleep(self._cfg.search_head_dwell_s)

            track = self._consume_track()
            if track is not None:
                self._transition(State.TRACKING)
                return

        # Body rotation scan
        for turn_angle in self._cfg.search_body_angles:
            if not self._running:
                return
            try:
                self._motor.turn_in_place(
                    turn_angle, speed_dps=self._cfg.search_turn_speed_dps
                )
            except Exception:
                logger.exception("Turn failed during search scan")
                # Cliff or e-stop — abort search
                return

            # Reset head to neutral for detection
            try:
                self._head.set_angle(10.0)
            except Exception:
                pass
            time.sleep(self._cfg.search_body_dwell_s)

            track = self._consume_track()
            if track is not None:
                self._transition(State.TRACKING)
                return

        # Full 360° with no detection → IDLE
        logger.info("Search complete — no target found, returning to IDLE")
        self._transition(State.IDLE)

    def _tick_tracking(self) -> None:
        """TRACKING: target acquired but not yet confirmed for following."""
        track = self._consume_track()
        if track is None:
            self._frames_without_track += 1
            if self._frames_without_track >= self._cfg.target_lost_frames:
                self._transition(State.SEARCHING)
            return

        # Check if track is confirmed enough for following
        if track.hits >= self._cfg.min_hits_for_following:
            self._transition(State.FOLLOWING)
            # Process this track in FOLLOWING mode immediately
            self._apply_tracking(track)
            return

        # Not enough hits yet — just track with head
        self._apply_head_tracking(track)

    def _tick_following(self) -> None:
        """FOLLOWING: active person following with motor commands."""
        track = self._consume_track()
        if track is None:
            self._frames_without_track += 1
            if self._frames_without_track >= self._cfg.target_lost_frames:
                # Lost target — stop motors, search
                try:
                    self._motor.drive_wheels(0, 0)
                except Exception:
                    pass
                self._transition(State.SEARCHING)
            return

        self._apply_tracking(track)

    # -- Tracking helpers ----------------------------------------------------

    def _apply_tracking(self, track: TrackedPersonEvent) -> None:
        """Apply full tracking: head pitch + motor commands."""
        self._apply_head_tracking(track)
        self._apply_motor_commands(track)

    def _apply_head_tracking(self, track: TrackedPersonEvent) -> None:
        """Adjust head pitch to keep person vertically centered."""
        # Error: positive = person is below center → look down
        error_y = track.cy - (FRAME_H / 2)
        # Negative kp because: person below center → need to decrease angle
        angle_adjust = -self._cfg.kp_head * error_y

        current_angle = self._head.last_angle
        if current_angle is None:
            current_angle = 10.0  # neutral

        new_angle = current_angle + angle_adjust
        try:
            self._head.set_angle(new_angle)
        except Exception:
            logger.exception("Head tracking failed")

    def _apply_motor_commands(self, track: TrackedPersonEvent) -> None:
        """Compute and send motor commands for following."""
        if track.confidence < self._cfg.min_tracking_confidence:
            return

        left, right = self._pd.compute(track.cx, track.height)

        # Emit event
        self._bus.emit(
            MOTOR_COMMAND,
            MotorCommandEvent(left_speed_mmps=left, right_speed_mmps=right),
        )

        # Send to motors
        try:
            self._motor.drive_wheels(left, right)
        except Exception:
            logger.exception("Motor command failed during following")

    # -- Helpers -------------------------------------------------------------

    def _consume_track(self) -> TrackedPersonEvent | None:
        """Get and clear the latest tracked person event."""
        with self._track_lock:
            track = self._latest_track
            self._latest_track = None
            return track
