"""Head pitch tracker for person following.

Keeps the detected person centered vertically in Vector's camera frame
using a P-controller with dead zone and slew-rate limiting for smooth motion.
Returns to neutral gradually when no person is detected.

Usage::

    from apps.vector.src.planner.head_tracker import HeadTracker

    tracker = HeadTracker(head_controller, nuc_bus)
    tracker.start()   # subscribes to TRACKED_PERSON events
    # ... tracker adjusts head pitch automatically
    tracker.stop()    # unsubscribes, returns head to neutral
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.vector.src.events.event_types import (
    HEAD_ANGLE_COMMAND,
    TRACKED_PERSON,
    HeadAngleCommandEvent,
    TrackedPersonEvent,
)
from apps.vector.src.head_controller import NEUTRAL_ANGLE

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController

logger = logging.getLogger(__name__)

FRAME_H = 600


@dataclass
class HeadTrackerConfig:
    """Tunable head tracking parameters.

    All gains, limits, and timing live here so they can be adjusted
    without touching tracker logic.
    """

    # P-gain: degrees of head adjustment per pixel of vertical offset.
    kp: float = 0.15

    # Dead zone: ignore vertical offsets smaller than this (pixels).
    dead_zone_px: float = 15.0

    # Max angle change per update (degrees) — prevents jerky motion.
    max_slew_rate: float = 5.0

    # Speed for head servo commands (degrees per second).
    head_speed_dps: float = 90.0

    # Seconds without a detection before starting neutral return.
    neutral_timeout_s: float = 1.5

    # Control loop rate (Hz).
    loop_hz: float = 10.0


class HeadTracker:
    """Event-driven head pitch tracker.

    Subscribes to ``TRACKED_PERSON`` events and adjusts head pitch via
    the ``HeadController`` to keep the person vertically centered.

    Parameters
    ----------
    head_controller : HeadController
        Head servo controller (provides ``set_angle``, ``last_angle``).
    nuc_bus : NucEventBus
        Event bus for subscribing to tracks and emitting head commands.
    config : HeadTrackerConfig | None
        Tunable parameters.  Defaults are used if *None*.
    """

    def __init__(
        self,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
        config: HeadTrackerConfig | None = None,
    ) -> None:
        self._head = head_controller
        self._bus = nuc_bus
        self._cfg = config or HeadTrackerConfig()

        # Thread-safe latest track
        self._latest_track: TrackedPersonEvent | None = None
        self._track_lock = threading.Lock()
        self._last_track_time: float = 0.0

        # Control loop
        self._running = False
        self._thread: threading.Thread | None = None

        # Neutral return state
        self._returning_to_neutral = False

    # -- Properties ----------------------------------------------------------

    @property
    def config(self) -> HeadTrackerConfig:
        return self._cfg

    @property
    def is_running(self) -> bool:
        return self._running

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start head tracking — subscribes to TRACKED_PERSON events."""
        if self._running:
            logger.warning("HeadTracker already running")
            return

        self._running = True
        self._returning_to_neutral = False
        self._bus.on(TRACKED_PERSON, self._on_tracked_person)

        self._thread = threading.Thread(
            target=self._control_loop, name="head-tracker", daemon=True
        )
        self._thread.start()
        logger.info("HeadTracker started")

    def stop(self) -> None:
        """Stop head tracking and return head to neutral."""
        if not self._running:
            return

        self._running = False
        self._bus.off(TRACKED_PERSON, self._on_tracked_person)

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        # Return to neutral on stop
        try:
            self._head.set_angle(NEUTRAL_ANGLE, speed_dps=self._cfg.head_speed_dps)
        except Exception:
            logger.exception("Failed to return head to neutral on stop")

        logger.info("HeadTracker stopped")

    # -- Event callback ------------------------------------------------------

    def _on_tracked_person(self, event: TrackedPersonEvent) -> None:
        """Receive smoothed track from Kalman tracker."""
        with self._track_lock:
            self._latest_track = event
            self._last_track_time = time.monotonic()

    # -- External update (for FollowPlanner integration) ---------------------

    def update(self, track: TrackedPersonEvent) -> None:
        """Directly update head tracking from a TrackedPersonEvent.

        This allows the FollowPlanner to feed tracks synchronously
        instead of relying on the event bus, avoiding double-subscription.
        """
        with self._track_lock:
            self._latest_track = track
            self._last_track_time = time.monotonic()

        # If not running our own loop, apply immediately
        if not self._running:
            self._apply_tracking(track)

    # -- Control loop --------------------------------------------------------

    def _control_loop(self) -> None:
        """Main control loop — runs at config.loop_hz in daemon thread."""
        period = 1.0 / self._cfg.loop_hz

        while self._running:
            loop_start = time.monotonic()

            try:
                self._tick()
            except Exception:
                logger.exception("Error in head tracker control loop")

            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _tick(self) -> None:
        """Single control loop tick."""
        track = self._consume_track()
        now = time.monotonic()

        if track is not None:
            self._returning_to_neutral = False
            self._apply_tracking(track)
        elif self._last_track_time > 0:
            # No track — check if we should return to neutral
            elapsed_since_track = now - self._last_track_time
            if elapsed_since_track >= self._cfg.neutral_timeout_s:
                self._return_to_neutral()

    # -- Tracking logic ------------------------------------------------------

    def _apply_tracking(self, track: TrackedPersonEvent) -> None:
        """Adjust head pitch to keep person vertically centered.

        Error: positive = person is below center → need to look down
        (decrease angle). Slew-rate limited for smooth motion.
        """
        error_y = track.cy - (FRAME_H / 2)

        # Dead zone
        if abs(error_y) < self._cfg.dead_zone_px:
            return

        # P-controller: negative because person below → decrease angle
        angle_adjust = -self._cfg.kp * error_y

        # Slew-rate limit
        angle_adjust = max(
            -self._cfg.max_slew_rate,
            min(self._cfg.max_slew_rate, angle_adjust),
        )

        current_angle = self._head.last_angle
        if current_angle is None:
            current_angle = NEUTRAL_ANGLE

        new_angle = current_angle + angle_adjust
        self._command_head(new_angle)

    def _return_to_neutral(self) -> None:
        """Gradually return head to neutral when no person detected."""
        current_angle = self._head.last_angle
        if current_angle is None:
            return

        diff = NEUTRAL_ANGLE - current_angle
        if abs(diff) < 0.5:
            # Close enough — snap to neutral
            if not self._returning_to_neutral:
                self._command_head(NEUTRAL_ANGLE)
                self._returning_to_neutral = True
            return

        # Slew toward neutral
        step = max(
            -self._cfg.max_slew_rate,
            min(self._cfg.max_slew_rate, diff),
        )
        self._command_head(current_angle + step)
        self._returning_to_neutral = False

    def _command_head(self, angle_deg: float) -> None:
        """Send a head angle command and emit event."""
        try:
            actual = self._head.set_angle(
                angle_deg, speed_dps=self._cfg.head_speed_dps
            )
        except Exception:
            logger.exception("Head angle command failed")
            return

        self._bus.emit(
            HEAD_ANGLE_COMMAND,
            HeadAngleCommandEvent(angle_deg=actual, source="head_tracker"),
        )

    # -- Helpers -------------------------------------------------------------

    def _consume_track(self) -> TrackedPersonEvent | None:
        """Get and clear the latest tracked person event."""
        with self._track_lock:
            track = self._latest_track
            self._latest_track = None
            return track
