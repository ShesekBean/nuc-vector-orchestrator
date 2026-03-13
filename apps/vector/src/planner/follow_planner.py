"""Person-following planner for Vector's differential drive.

State machine:  IDLE → SEARCHING → TRACKING → FOLLOWING → SEARCHING
PD controller:  camera detection → Kalman smoothing → EMA motor commands
Head tracking:  vertical P-controller on person center-y
Body rotation:  horizontal PD on person center-x (differential drive)
Search:         velocity-biased direction → multi-cycle scan → IDLE
Track lock:     locks to a specific track_id to prevent target switching

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
from apps.vector.src.planner.head_tracker import HeadTracker, HeadTrackerConfig

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.motor_controller import MotorController
    from apps.vector.src.planner.obstacle_detector import ObstacleDetector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_W = 800
FRAME_H = 600


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
    kp_turn: float = 0.55
    kd_turn: float = 0.08
    dead_zone_x: float = 40.0  # pixels — no turn if error < this

    # --- P gain for driving (distance control via bbox height) ---
    kp_drive: float = 0.5
    kd_drive: float = 0.08
    target_height: float = 300.0  # target bbox height in pixels (~1m follow distance at 800x600)
    dead_zone_h: float = 20.0  # pixels — no drive if height error < this

    # --- P gain for head pitch (vertical tracking) ---
    kp_head: float = 0.10  # degrees per pixel of vertical offset (gentler for 600px frame)

    # --- Speed limits ---
    max_wheel_speed: float = 160.0  # mm/s (Vector max ~200, leave headroom)
    min_tracking_confidence: float = 0.3

    # --- EMA smoothing on motor output (0.0=raw, 1.0=frozen) ---
    ema_alpha: float = 0.3  # lower = smoother, higher = more responsive

    # --- Derivative low-pass filter ---
    derivative_filter_alpha: float = 0.5  # EMA on d_error to reject spikes

    # --- Tracking thresholds ---
    min_hits_for_following: int = 3  # Kalman hits before TRACKING → FOLLOWING (faster lock)
    target_lost_frames: int = 40  # control-loop ticks without detection → SEARCHING (~4s at 10Hz)

    # --- Search behaviour ---
    search_head_angles: tuple[float, ...] = (10.0, 30.0, -10.0, 0.0)
    search_head_dwell_s: float = 0.8  # seconds to wait at each head angle
    search_body_dwell_s: float = 1.0
    search_turn_speed_dps: float = 80.0
    search_max_cycles: int = 2  # how many full 360° scans before giving up
    search_step_deg: float = 60.0  # degrees per search rotation step (6 steps = 360°)

    # --- Control loop ---
    loop_hz: float = 10.0  # target control loop rate


# ---------------------------------------------------------------------------
# PD Controller with EMA smoothing and derivative filtering
# ---------------------------------------------------------------------------


class PDController:
    """PD controller for differential-drive person following.

    Computes (left_speed, right_speed) from tracked person position.
    Turn: PD on horizontal offset from image center.
    Drive: PD on bbox height error (target height = follow distance proxy).
    Includes EMA smoothing on output and low-pass filter on derivative.
    """

    def __init__(self, config: FollowConfig) -> None:
        self._cfg = config
        self._prev_error_x: float = 0.0
        self._prev_error_h: float = 0.0
        self._prev_time: float = 0.0
        # Filtered derivatives
        self._filtered_d_error_x: float = 0.0
        self._filtered_d_error_h: float = 0.0
        # EMA output state
        self._ema_left: float = 0.0
        self._ema_right: float = 0.0

    def reset(self) -> None:
        """Reset derivative state (call on state transitions)."""
        self._prev_error_x = 0.0
        self._prev_error_h = 0.0
        self._prev_time = 0.0
        self._filtered_d_error_x = 0.0
        self._filtered_d_error_h = 0.0
        self._ema_left = 0.0
        self._ema_right = 0.0

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
        raw_d_error_x = (error_x - self._prev_error_x) / dt

        # --- Drive error: height difference (positive = too far away) ---
        error_h = cfg.target_height - bbox_height
        raw_d_error_h = (error_h - self._prev_error_h) / dt

        # Low-pass filter on derivatives to reject spikes
        d_alpha = cfg.derivative_filter_alpha
        self._filtered_d_error_x = (
            d_alpha * raw_d_error_x + (1.0 - d_alpha) * self._filtered_d_error_x
        )
        self._filtered_d_error_h = (
            d_alpha * raw_d_error_h + (1.0 - d_alpha) * self._filtered_d_error_h
        )

        # PD outputs with filtered derivatives
        turn_speed = cfg.kp_turn * error_x + cfg.kd_turn * self._filtered_d_error_x
        drive_speed = cfg.kp_drive * error_h + cfg.kd_drive * self._filtered_d_error_h

        # Dead zones
        if abs(error_x) < cfg.dead_zone_x:
            turn_speed = 0.0
        if abs(error_h) < cfg.dead_zone_h:
            drive_speed = 0.0

        # Asymmetric speed: limit backward drive to 40% of max
        # (Vector should approach eagerly but retreat gently)
        if drive_speed < 0:
            max_reverse = cfg.max_wheel_speed * 0.4
            drive_speed = max(-max_reverse, drive_speed)

        # Differential drive: turn adds to one side, subtracts from the other
        raw_left = drive_speed + turn_speed
        raw_right = drive_speed - turn_speed

        # Clamp
        max_spd = cfg.max_wheel_speed
        raw_left = max(-max_spd, min(max_spd, raw_left))
        raw_right = max(-max_spd, min(max_spd, raw_right))

        # EMA smoothing — prevents jerky motor commands
        alpha = cfg.ema_alpha
        self._ema_left = alpha * raw_left + (1.0 - alpha) * self._ema_left
        self._ema_right = alpha * raw_right + (1.0 - alpha) * self._ema_right

        # Save state for derivative
        self._prev_error_x = error_x
        self._prev_error_h = error_h

        return self._ema_left, self._ema_right


# ---------------------------------------------------------------------------
# Follow Planner
# ---------------------------------------------------------------------------


class FollowPlanner:
    """Person-following planner with state machine and event-bus integration.

    Subscribes to ``TRACKED_PERSON`` events (from Kalman tracker) and emits
    ``FOLLOW_STATE_CHANGED`` + ``MOTOR_COMMAND`` events.

    Features:
    - Track ID lock: locks to a specific track_id once following begins,
      preventing target switching in multi-person scenarios.
    - Velocity-biased search: when target is lost, first checks the
      direction the person was last moving before doing a full scan.
    - Multi-cycle search: does search_max_cycles full rotations before
      giving up (default 2).
    - EMA motor smoothing: prevents jerky commands.

    Thread-safe: the control loop runs in a daemon thread; start/stop can
    be called from any thread.
    """

    def __init__(
        self,
        motor_controller: MotorController,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
        config: FollowConfig | None = None,
        obstacle_detector: ObstacleDetector | None = None,
        say_func: Any | None = None,
    ) -> None:
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus
        self._cfg = config or FollowConfig()
        self._pd = PDController(self._cfg)
        self._obstacle = obstacle_detector
        self._say = say_func  # optional callable(text) for speech feedback

        # Head tracker — delegates vertical pitch control
        head_cfg = HeadTrackerConfig(
            kp=self._cfg.kp_head,
        )
        self._head_tracker = HeadTracker(head_controller, nuc_bus, head_cfg)

        # State
        self._state = State.IDLE
        self._state_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        # Latest tracked person from event bus
        self._latest_track: TrackedPersonEvent | None = None
        self._track_lock = threading.Lock()
        self._frames_without_track: int = 0

        # Track ID lock — prevents target switching
        self._locked_track_id: int | None = None

        # Last known velocity for search direction bias
        self._last_vx: float = 0.0  # positive = moving right in frame

        # Track movement for stuck detection
        self._prev_track_cx: float | None = None
        self._prev_track_cy: float | None = None

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

    @property
    def head_tracker(self) -> HeadTracker:
        """Access the underlying head tracker for direct configuration."""
        return self._head_tracker

    @property
    def locked_track_id(self) -> int | None:
        """Currently locked track ID, or None if not locked."""
        return self._locked_track_id

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start following — transitions to SEARCHING."""
        with self._state_lock:
            if self._running:
                logger.warning("FollowPlanner already running")
                return
            self._running = True

        self._locked_track_id = None
        self._last_vx = 0.0

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

        self._locked_track_id = None
        self._transition(State.IDLE)

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        logger.info("FollowPlanner stopped")

    # -- Event callbacks -----------------------------------------------------

    def _on_tracked_person(self, event: TrackedPersonEvent) -> None:
        """Receive smoothed track from Kalman tracker.

        If a track ID is locked, prefer events matching that ID.
        If the locked track hasn't been seen for a while, accept the
        new track (the Kalman tracker may have re-created it with a new ID
        after a brief detection gap).
        """
        with self._track_lock:
            if self._locked_track_id is not None:
                if event.track_id != self._locked_track_id:
                    # Accept new track if we haven't seen the locked one recently
                    if self._frames_without_track < 10:
                        return  # still expecting locked track
                    # Locked track is gone — accept new one and re-lock
                    logger.info(
                        "Re-locking from track_id=%d to %d (old track lost)",
                        self._locked_track_id, event.track_id,
                    )
                    self._locked_track_id = event.track_id
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

        # Release track lock when going to SEARCHING
        if new_state == State.SEARCHING:
            self._locked_track_id = None

        # Voice feedback on state transitions
        if self._say is not None:
            try:
                if new_state == State.SEARCHING:
                    threading.Thread(
                        target=self._say, args=("searching",), daemon=True
                    ).start()
                elif new_state == State.FOLLOWING and old != State.FOLLOWING:
                    threading.Thread(
                        target=self._say, args=("I see you",), daemon=True
                    ).start()
            except Exception:
                pass  # don't let TTS errors affect control loop

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
        """SEARCHING: head sweep (no movement) → velocity-biased turn → body scan.

        Strategy: look before you move. Head sweep is instant and catches
        nearby targets. Only then rotate the body.
        """
        # Check if Kalman tracker already has a target
        track = self._consume_track()
        if track is not None:
            self._transition(State.TRACKING)
            return

        # --- Phase 1: Head sweep (no body movement) ---
        # Fast check without spinning — person may still be in view
        logger.info("Search: head sweep")
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

        # Reset head to neutral for body scan
        try:
            self._head.set_angle(10.0)
        except Exception:
            pass

        # --- Phase 2: Velocity-biased quick look ---
        # If we know the person was moving in a direction, check there first
        if abs(self._last_vx) > 5.0:
            bias_angle = -60.0 if self._last_vx > 0 else 60.0
            logger.info(
                "Search: velocity-biased look (vx=%.0f → turn %.0f°)",
                self._last_vx, bias_angle,
            )
            try:
                self._motor.turn_in_place(
                    bias_angle, speed_dps=self._cfg.search_turn_speed_dps
                )
            except Exception:
                logger.exception("Velocity-biased turn failed")

            time.sleep(self._cfg.search_body_dwell_s)

            track = self._consume_track()
            if track is not None:
                self._transition(State.TRACKING)
                return

        # --- Phase 3: Gradual body rotation scan ---
        # Rotate in steps, checking for target at each position
        step_deg = self._cfg.search_step_deg
        steps_per_cycle = max(1, int(360.0 / step_deg))

        for cycle in range(self._cfg.search_max_cycles):
            logger.info("Search: body scan cycle %d/%d", cycle + 1, self._cfg.search_max_cycles)

            for step in range(steps_per_cycle):
                if not self._running:
                    return
                try:
                    self._motor.turn_in_place(
                        step_deg, speed_dps=self._cfg.search_turn_speed_dps
                    )
                except Exception:
                    logger.exception("Turn failed during search scan")
                    return

                time.sleep(self._cfg.search_body_dwell_s)

                track = self._consume_track()
                if track is not None:
                    self._transition(State.TRACKING)
                    return

        # All cycles exhausted — no target found
        logger.info(
            "Search complete — no target found after %d cycle(s), returning to IDLE",
            self._cfg.search_max_cycles,
        )
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
            # Lock to this track ID
            self._locked_track_id = track.track_id
            logger.info("Locked to track_id=%d", track.track_id)
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

        # Store velocity for search bias (vx from Kalman tracker)
        # TrackedPersonEvent doesn't have vx directly, but we can
        # estimate from consecutive cx values
        if self._prev_track_cx is not None:
            self._last_vx = track.cx - self._prev_track_cx

    def _apply_head_tracking(self, track: TrackedPersonEvent) -> None:
        """Adjust head pitch to keep person vertically centered.

        Delegates to the HeadTracker module for P-controller logic,
        slew-rate limiting, and neutral return.
        """
        self._head_tracker.update(track)

    def _apply_motor_commands(self, track: TrackedPersonEvent) -> None:
        """Compute and send motor commands for following."""
        if track.confidence < self._cfg.min_tracking_confidence:
            return

        left, right = self._pd.compute(track.cx, track.height)

        # Scale by obstacle proximity (camera-based soft slowdown)
        if self._obstacle is not None:
            scale = self._obstacle.speed_scale
            left *= scale
            right *= scale

            # Reset stuck timer when track position changes significantly
            self._check_track_movement(track)

            # Check for stuck condition and trigger escape if needed
            self._obstacle.check_stuck()

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

    def _check_track_movement(self, track: TrackedPersonEvent) -> None:
        """Reset obstacle stuck timer when track position changes."""
        if self._obstacle is None:
            return
        moved = False
        if self._prev_track_cx is not None:
            dx = abs(track.cx - self._prev_track_cx)
            dy = abs(track.cy - self._prev_track_cy)
            if dx > 10.0 or dy > 10.0:
                moved = True
        self._prev_track_cx = track.cx
        self._prev_track_cy = track.cy
        if moved:
            self._obstacle.reset_stuck()

    def _consume_track(self) -> TrackedPersonEvent | None:
        """Get and clear the latest tracked person event."""
        with self._track_lock:
            track = self._latest_track
            self._latest_track = None
            return track
