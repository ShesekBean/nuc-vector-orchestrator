"""Person-following planner for Vector's differential drive.

State machine:  IDLE → SEARCHING → FOLLOWING → SEARCHING
P controller:   camera detection → proportional motor commands
Head tracking:  vertical P-controller on person center-y
Body rotation:  horizontal P on person center-x (turn-first-then-drive)
Search:         head sweep → velocity-biased turn → body scan → IDLE

Key design: TURN FIRST, THEN DRIVE. Never mix turning and driving —
mixing them on differential drive causes arcs and circles.

Usage::

    from apps.vector.src.planner.follow_planner import FollowPlanner

    planner = FollowPlanner(motor_controller, head_controller, nuc_bus)
    planner.start()   # begins SEARCHING state
    # ... planner subscribes to TRACKED_PERSON events
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

logger = logging.getLogger(__name__)


class State(enum.Enum):
    """Follow planner states."""

    IDLE = "idle"
    SEARCHING = "searching"
    FOLLOWING = "following"


@dataclass
class FollowConfig:
    """Tunable follow parameters."""

    # --- P gain for turning (horizontal centering) ---
    kp_turn: float = 0.2
    turn_dead_zone_frac: float = 0.15  # fraction of frame width — no turn if error < this (±120px)

    # --- P gain for driving (distance control via bbox height) ---
    kp_drive: float = 2.5
    target_height_frac: float = 0.70  # target bbox height as fraction of frame height (420px — close)
    drive_dead_zone_frac: float = 0.03  # fraction of frame height — no drive if error < this
    too_close_frac: float = 0.85  # bbox height > this fraction → stop (person filling frame)

    # --- P gain for head pitch (vertical tracking) ---
    kp_head: float = 0.10

    # --- Speed limits ---
    max_wheel_speed: float = 140.0  # mm/s (conservative — Vector max ~200)
    max_turn_speed: float = 120.0  # mm/s for turning
    min_tracking_confidence: float = 0.10

    # --- Turn-first threshold ---
    # If horizontal error exceeds this fraction of frame width, ONLY turn (no drive)
    turn_first_threshold_frac: float = 0.12

    # --- Tracking thresholds ---
    target_lost_frames: int = 30  # ticks without detection → SEARCHING (~2s at 15Hz)

    # --- Search behaviour ---
    search_head_angles: tuple[float, ...] = (10.0, 30.0, -10.0, 0.0)
    search_head_dwell_s: float = 0.8
    search_body_dwell_s: float = 1.0
    search_turn_speed_dps: float = 80.0
    search_max_cycles: int = 2
    search_step_deg: float = 60.0

    # --- Control loop ---
    loop_hz: float = 15.0


class FollowPlanner:
    """Person-following planner with turn-first-then-drive control.

    Subscribes to ``TRACKED_PERSON`` events and drives motors using simple
    proportional control with a turn-first-then-drive strategy:

    - If person is off-center: TURN ONLY (no forward drive)
    - If person is centered: DRIVE ONLY (no turning)

    This prevents the arcs/circles caused by mixing turn+drive on
    differential drive.
    """

    def __init__(
        self,
        motor_controller: MotorController,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
        config: FollowConfig | None = None,
        obstacle_detector: Any | None = None,
        say_func: Any | None = None,
    ) -> None:
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus
        self._cfg = config or FollowConfig()
        self._obstacle = obstacle_detector
        self._say = say_func

        # Head tracker
        head_cfg = HeadTrackerConfig(kp=self._cfg.kp_head)
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
        self._last_vx: float = 0.0
        self._prev_track_cx: float | None = None

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
        return self._head_tracker

    @property
    def locked_track_id(self) -> int | None:
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
        self._prev_track_cx = None

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
        """Receive detection from pipeline.

        If a track ID is locked, prefer events matching that ID.
        Accept new track after 2 missed frames.
        """
        with self._track_lock:
            if self._locked_track_id is not None:
                if event.track_id != self._locked_track_id:
                    if self._frames_without_track < 2:
                        return
                    logger.info(
                        "Re-locking from track_id=%d to %d (old track lost)",
                        self._locked_track_id, event.track_id,
                    )
                    self._locked_track_id = event.track_id
            self._latest_track = event
            self._frames_without_track = 0

    def _on_emergency_stop(self, _event: Any) -> None:
        logger.warning("Emergency stop received — halting follow planner")
        with self._state_lock:
            self._running = False
        self._transition(State.IDLE)

    # -- State transitions ---------------------------------------------------

    def _transition(self, new_state: State) -> None:
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

        self._frames_without_track = 0

        if new_state == State.SEARCHING:
            self._locked_track_id = None

        # Voice feedback
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
                pass

    # -- Control loop --------------------------------------------------------

    def _control_loop(self) -> None:
        period = 1.0 / self._cfg.loop_hz

        while self._running:
            loop_start = time.monotonic()

            try:
                state = self.state
                if state == State.SEARCHING:
                    self._tick_searching()
                elif state == State.FOLLOWING:
                    self._tick_following()
            except Exception:
                logger.exception("Error in follow planner control loop")

            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # -- State tick handlers -------------------------------------------------

    def _tick_searching(self) -> None:
        """SEARCHING: head sweep → velocity-biased turn → body scan."""
        track = self._consume_track()
        if track is not None:
            self._locked_track_id = track.track_id
            logger.info("Locked to track_id=%d", track.track_id)
            self._transition(State.FOLLOWING)
            self._apply_tracking(track)
            return

        # --- Phase 1: Head sweep (no body movement) ---
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
                self._locked_track_id = track.track_id
                self._transition(State.FOLLOWING)
                return

        # Reset head to neutral
        try:
            self._head.set_angle(10.0)
        except Exception:
            pass

        # --- Phase 2: Velocity-biased quick look ---
        if abs(self._last_vx) > 5.0:
            bias_angle = -60.0 if self._last_vx > 0 else 60.0
            logger.info("Search: velocity-biased look (vx=%.0f → turn %.0f°)",
                        self._last_vx, bias_angle)
            try:
                self._motor.turn_in_place(
                    bias_angle, speed_dps=self._cfg.search_turn_speed_dps
                )
            except Exception:
                logger.exception("Velocity-biased turn failed")

            time.sleep(self._cfg.search_body_dwell_s)

            track = self._consume_track()
            if track is not None:
                self._locked_track_id = track.track_id
                self._transition(State.FOLLOWING)
                return

        # --- Phase 3: Body rotation scan ---
        step_deg = self._cfg.search_step_deg
        steps_per_cycle = max(1, int(360.0 / step_deg))

        for cycle in range(self._cfg.search_max_cycles):
            logger.info("Search: body scan cycle %d/%d",
                        cycle + 1, self._cfg.search_max_cycles)

            for _step in range(steps_per_cycle):
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
                    self._locked_track_id = track.track_id
                    self._transition(State.FOLLOWING)
                    return

        logger.info("Search complete — no target found, returning to IDLE")
        self._transition(State.IDLE)

    def _tick_following(self) -> None:
        """FOLLOWING: turn-first-then-drive person following."""
        track = self._consume_track()
        if track is None:
            self._frames_without_track += 1
            if self._frames_without_track >= self._cfg.target_lost_frames:
                try:
                    self._motor.drive_wheels(0, 0)
                except Exception:
                    pass
                self._transition(State.SEARCHING)
            return

        self._apply_tracking(track)

    # -- Tracking helpers ----------------------------------------------------

    def _apply_tracking(self, track: TrackedPersonEvent) -> None:
        """Apply turn-first-then-drive control."""
        # Head pitch tracking
        self._head_tracker.update(track)

        # Motor control
        if track.confidence < self._cfg.min_tracking_confidence:
            return

        cfg = self._cfg
        fw = float(track.frame_width)
        fh = float(track.frame_height)

        # Horizontal error: positive = person is right of center
        error_x = track.cx - (fw / 2.0)
        # Normalize to fraction of frame width
        error_x_frac = error_x / fw

        # Distance error: positive = person is too far
        target_h = cfg.target_height_frac * fh
        error_h = target_h - track.height
        error_h_frac = error_h / fh

        # Store velocity for search bias
        if self._prev_track_cx is not None:
            self._last_vx = track.cx - self._prev_track_cx
        self._prev_track_cx = track.cx

        # --- Drive forward + gentle steering ---
        # Always compute both drive and turn independently, then combine.
        # Drive: forward only when person is far, stop when close. Never reverse.
        # Turn: gentle proportional correction to keep person centered.
        # Too close (person fills frame): stop everything.

        # Drive component: forward only, never reverse
        drive_speed = 0.0
        if track.height > cfg.too_close_frac * fh:
            # Too close — stop
            drive_speed = 0.0
        elif error_h_frac > cfg.drive_dead_zone_frac:
            # Person is far — drive forward
            drive_speed = cfg.kp_drive * error_h * (cfg.max_wheel_speed / (fh / 2.0))
            drive_speed = max(0.0, min(cfg.max_wheel_speed, drive_speed))

        # Turn component: gentle correction
        turn_speed = 0.0
        if abs(error_x_frac) > cfg.turn_dead_zone_frac:
            turn_speed = cfg.kp_turn * error_x * (cfg.max_turn_speed / (fw / 2.0))
            turn_speed = max(-cfg.max_turn_speed, min(cfg.max_turn_speed, turn_speed))

        left = drive_speed + turn_speed
        right = drive_speed - turn_speed
        left = max(-cfg.max_wheel_speed, min(cfg.max_wheel_speed, left))
        right = max(-cfg.max_wheel_speed, min(cfg.max_wheel_speed, right))

        # Emit event
        self._bus.emit(
            MOTOR_COMMAND,
            MotorCommandEvent(left_speed_mmps=left, right_speed_mmps=right),
        )

        # Send to motors
        try:
            logger.info(
                "Motor cmd: L=%.0f R=%.0f (cx=%.0f cy=%.0f h=%.0f conf=%.2f frame=%dx%d)",
                left, right, track.cx, track.cy, track.height,
                track.confidence, track.frame_width, track.frame_height,
            )
            self._motor.drive_wheels(left, right)
        except Exception:
            logger.exception("Motor command failed during following")

    # -- Helpers -------------------------------------------------------------

    def _consume_track(self) -> TrackedPersonEvent | None:
        with self._track_lock:
            track = self._latest_track
            self._latest_track = None
            return track
