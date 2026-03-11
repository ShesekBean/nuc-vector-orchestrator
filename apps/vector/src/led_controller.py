"""LED state manager for Vector's eye color display.

Priority-based state machine that drives ``set_eye_color(hue, saturation)``
with animated patterns (breathing, blinking, hue cycling).  Integrates with
the NUC event bus for state-driven transitions and emergency-stop handling.

Note: Vector's physical backpack LEDs are only controllable through the
internal animation engine and are not exposed via the external gRPC API.
This controller uses ``set_eye_color`` as the primary visual state indicator.

Usage::

    from apps.vector.src.led_controller import LedController

    ctrl = LedController(robot, nuc_bus)
    ctrl.start()
    ctrl.set_state("person_detected")
    ctrl.set_state("idle")
    ctrl.override(hue=0.5, saturation=1.0, duration_s=5.0)
    ctrl.stop()
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    LED_STATE_CHANGED,
    LedStateChangedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# --- State definitions -------------------------------------------------------

PRIORITY_ORDER: list[str] = [
    "idle",
    "searching",
    "person_detected",
    "following",
    "low_battery",
    "battery_shutdown",
]
"""States ordered by ascending priority — last element has highest priority."""

PRIORITY: dict[str, int] = {s: i for i, s in enumerate(PRIORITY_ORDER)}


@dataclass(frozen=True)
class LedPattern:
    """Defines the visual pattern for an LED state."""

    hue: float
    saturation: float
    mode: str  # "solid", "breathing", "blinking", "cycling"


PATTERNS: dict[str, LedPattern] = {
    "idle": LedPattern(hue=0.33, saturation=1.0, mode="solid"),
    "searching": LedPattern(hue=0.78, saturation=1.0, mode="cycling"),
    "person_detected": LedPattern(hue=0.67, saturation=1.0, mode="solid"),
    "following": LedPattern(hue=0.67, saturation=1.0, mode="breathing"),
    "low_battery": LedPattern(hue=0.0, saturation=1.0, mode="blinking"),
    "battery_shutdown": LedPattern(hue=0.0, saturation=1.0, mode="solid"),
}

# Animation timing
ANIMATION_INTERVAL_S = 0.05  # 20 Hz animation tick
BREATHING_PERIOD_S = 2.0  # full inhale + exhale cycle
BREATHING_MIN_SAT = 0.2  # minimum saturation during breathing
BLINKING_PERIOD_S = 0.8  # on + off cycle
CYCLING_PERIOD_S = 3.0  # full hue rotation

DEFAULT_OVERRIDE_DURATION_S = 10.0


class LedController:
    """Priority-based LED state manager with animated patterns.

    Parameters
    ----------
    robot:
        Connected ``anki_vector.Robot`` instance.
    nuc_bus:
        NUC event bus for emergency-stop subscription and LED notifications.
    override_duration_s:
        Default duration for manual override before auto-revert.
    """

    def __init__(
        self,
        robot: Any,
        nuc_bus: NucEventBus,
        override_duration_s: float = DEFAULT_OVERRIDE_DURATION_S,
    ) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._override_duration_s = override_duration_s

        self._lock = threading.Lock()
        self._active_states: set[str] = set()
        self._current_state: str = "idle"
        self._running = False
        self._e_stop_active = False

        # Override state
        self._override_hue: float | None = None
        self._override_sat: float | None = None
        self._override_timer: threading.Timer | None = None

        # Animation thread
        self._stop_event = threading.Event()
        self._anim_thread: threading.Thread | None = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to events and start the animation loop."""
        if self._running:
            return
        self._bus.on(EMERGENCY_STOP, self._on_emergency_stop)
        self._running = True
        self._e_stop_active = False
        self._stop_event.clear()
        self._anim_thread = threading.Thread(
            target=self._animation_loop, daemon=True, name="led-anim",
        )
        self._anim_thread.start()
        # Set initial idle colour
        self._apply_state("idle")
        logger.info("LedController started")

    def stop(self) -> None:
        """Unsubscribe from events and stop the animation loop."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._cancel_override_timer()
        self._bus.off(EMERGENCY_STOP, self._on_emergency_stop)
        if self._anim_thread is not None:
            self._anim_thread.join(timeout=2.0)
            self._anim_thread = None
        logger.info("LedController stopped")

    # -- Public API ----------------------------------------------------------

    def set_state(self, state: str) -> None:
        """Activate an LED state.

        The highest-priority active state determines the visual output.

        Parameters
        ----------
        state:
            One of the defined LED states (see ``PRIORITY_ORDER``).

        Raises
        ------
        ValueError
            If *state* is not a recognised state name.
        """
        if state not in PRIORITY:
            raise ValueError(
                f"Unknown LED state {state!r}; "
                f"choose from {sorted(PRIORITY, key=PRIORITY.get)}"  # type: ignore[arg-type]
            )
        with self._lock:
            self._active_states.add(state)
        self._resolve_state()

    def clear_state(self, state: str) -> None:
        """Deactivate an LED state.

        If the cleared state was the current display state, the controller
        falls back to the next highest-priority active state (or ``idle``).
        """
        with self._lock:
            self._active_states.discard(state)
        self._resolve_state()

    def override(
        self,
        hue: float,
        saturation: float,
        duration_s: float | None = None,
    ) -> None:
        """Temporarily override the LED output with a custom colour.

        Parameters
        ----------
        hue:
            Hue value in range ``[0.0, 1.0]``.
        saturation:
            Saturation value in range ``[0.0, 1.0]``.
        duration_s:
            Override duration in seconds.  ``None`` uses the default
            (``override_duration_s`` from constructor).
        """
        if duration_s is None:
            duration_s = self._override_duration_s

        with self._lock:
            self._override_hue = hue
            self._override_sat = saturation

        self._cancel_override_timer()
        if duration_s > 0:
            self._override_timer = threading.Timer(
                duration_s, self._clear_override,
            )
            self._override_timer.daemon = True
            self._override_timer.start()

        self._send_eye_color(hue, saturation)
        logger.info(
            "LED override: hue=%.2f sat=%.2f for %.1fs", hue, saturation, duration_s,
        )

    # -- Properties ----------------------------------------------------------

    @property
    def current_state(self) -> str:
        """The currently displayed LED state name."""
        with self._lock:
            return self._current_state

    @property
    def active_states(self) -> frozenset[str]:
        """All currently active LED states."""
        with self._lock:
            return frozenset(self._active_states)

    @property
    def is_overridden(self) -> bool:
        """Whether a manual override is active."""
        with self._lock:
            return self._override_hue is not None

    # -- Internal ------------------------------------------------------------

    def _resolve_state(self) -> None:
        """Determine the highest-priority active state and switch to it."""
        with self._lock:
            if not self._active_states:
                new_state = "idle"
            else:
                new_state = max(self._active_states, key=lambda s: PRIORITY[s])

            old_state = self._current_state
            if new_state == old_state:
                return
            self._current_state = new_state

        self._apply_state(new_state, old_state)

    def _apply_state(self, state: str, previous: str | None = None) -> None:
        """Apply a state's pattern and emit a change event."""
        pattern = PATTERNS[state]
        if pattern.mode == "solid":
            self._send_eye_color(pattern.hue, pattern.saturation)
        # Animated modes are handled by _animation_loop

        self._bus.emit(
            LED_STATE_CHANGED,
            LedStateChangedEvent(state=state, previous_state=previous),
        )
        logger.info("LED state: %s → %s", previous, state)

    def _animation_loop(self) -> None:
        """Background thread that drives animated patterns."""
        while not self._stop_event.is_set():
            with self._lock:
                state = self._current_state
                override_active = self._override_hue is not None
                e_stop = self._e_stop_active

            if e_stop:
                self._send_eye_color(0.0, 1.0)  # solid red
                self._stop_event.wait(ANIMATION_INTERVAL_S)
                continue

            if override_active:
                self._stop_event.wait(ANIMATION_INTERVAL_S)
                continue

            pattern = PATTERNS.get(state)
            if pattern is None or pattern.mode == "solid":
                self._stop_event.wait(ANIMATION_INTERVAL_S)
                continue

            t = time.monotonic()

            if pattern.mode == "breathing":
                phase = (t % BREATHING_PERIOD_S) / BREATHING_PERIOD_S
                sat = BREATHING_MIN_SAT + (
                    pattern.saturation - BREATHING_MIN_SAT
                ) * (0.5 + 0.5 * math.cos(2 * math.pi * phase))
                self._send_eye_color(pattern.hue, sat)

            elif pattern.mode == "blinking":
                phase = (t % BLINKING_PERIOD_S) / BLINKING_PERIOD_S
                on = phase < 0.5
                self._send_eye_color(pattern.hue, pattern.saturation if on else 0.0)

            elif pattern.mode == "cycling":
                phase = (t % CYCLING_PERIOD_S) / CYCLING_PERIOD_S
                hue = (pattern.hue + phase) % 1.0
                self._send_eye_color(hue, pattern.saturation)

            self._stop_event.wait(ANIMATION_INTERVAL_S)

    def _send_eye_color(self, hue: float, saturation: float) -> None:
        """Send eye colour to Vector. Errors are logged, not raised."""
        try:
            self._robot.behavior.set_eye_color(hue, saturation)
        except Exception:
            logger.exception("Failed to set eye color (hue=%.2f, sat=%.2f)", hue, saturation)

    # -- Event handlers ------------------------------------------------------

    def _on_emergency_stop(self, event: Any) -> None:
        """Switch to solid red on emergency stop."""
        with self._lock:
            self._e_stop_active = True
        logger.warning("LedController: emergency stop — solid red")

    def clear_emergency_stop(self) -> None:
        """Re-enable normal LED states after emergency-stop clears."""
        with self._lock:
            self._e_stop_active = False
        self._resolve_state()
        logger.info("LedController: emergency stop cleared")

    # -- Override timer ------------------------------------------------------

    def _clear_override(self) -> None:
        """Timer callback — revert to priority-based state."""
        with self._lock:
            self._override_hue = None
            self._override_sat = None
        # Re-apply current state
        with self._lock:
            state = self._current_state
        pattern = PATTERNS[state]
        if pattern.mode == "solid":
            self._send_eye_color(pattern.hue, pattern.saturation)
        logger.info("LED override expired — reverted to %s", state)

    def _cancel_override_timer(self) -> None:
        """Cancel the pending override revert timer, if any."""
        if self._override_timer is not None:
            self._override_timer.cancel()
            self._override_timer = None
