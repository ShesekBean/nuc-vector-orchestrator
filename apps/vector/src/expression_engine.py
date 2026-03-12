"""Coordinated expression engine for Vector personality.

Orchestrates face display, LED patterns, and sound effects into synchronized
emotion expressions.  Sits on top of existing controllers — does not modify
their internal logic.

Usage::

    from apps.vector.src.expression_engine import ExpressionEngine

    engine = ExpressionEngine(display_ctrl, led_ctrl, speech, bus)
    engine.start()           # subscribe to events
    engine.express("happy")  # trigger an emotion
    engine.stop()

**Important:** When using ExpressionEngine, construct ``DisplayController``
WITHOUT ``event_bus`` — the expression engine drives face expressions directly.
Otherwise both will fight over ``set_expression()``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    COMMAND_RECEIVED,
    EXPRESSION_CHANGED,
    ExpressionChangedEvent,
    FACE_RECOGNIZED,
    TOUCH_DETECTED,
    WAKE_WORD_DETECTED,
    YOLO_PERSON_DETECTED,
)

if TYPE_CHECKING:
    from apps.vector.src.display_controller import DisplayController
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.led_controller import LedController
    from apps.vector.src.voice.speech_output import SpeechOutput

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Emotion definitions
# ---------------------------------------------------------------------------

# Default durations (seconds) before auto-decay to idle
DEFAULT_DECAY_S = 4.0
STARTLED_DECAY_S = 2.0
SLEEPY_DECAY_S = 10.0

# LED override duration used to quickly revert to LedController's state machine
_IDLE_OVERRIDE_DURATION_S = 0.05

# Priority order — higher value wins when multiple emotions compete
EMOTION_PRIORITY: dict[str, int] = {
    "idle": 0,
    "sleepy": 1,
    "sad": 2,
    "curious": 3,
    "happy": 4,
    "excited": 5,
    "startled": 6,
}


@dataclass(frozen=True)
class EmotionDef:
    """Defines the coordinated expression for one emotion."""

    face: str  # DisplayController expression name
    led_hue: float  # LedController override hue
    led_sat: float  # LedController override saturation
    sound: str  # Short text for say_text() — empty = silent
    decay_s: float  # Auto-revert duration (0 = no auto-decay)
    priority: int  # Higher wins in conflicts


EMOTIONS: dict[str, EmotionDef] = {
    "idle": EmotionDef(
        face="idle", led_hue=0.33, led_sat=1.0,
        sound="", decay_s=0, priority=0,
    ),
    "happy": EmotionDef(
        face="happy", led_hue=0.33, led_sat=1.0,
        sound="ha ha!", decay_s=DEFAULT_DECAY_S, priority=4,
    ),
    "curious": EmotionDef(
        face="curious", led_hue=0.50, led_sat=1.0,
        sound="hmm?", decay_s=DEFAULT_DECAY_S, priority=3,
    ),
    "sad": EmotionDef(
        face="sad", led_hue=0.67, led_sat=0.5,
        sound="aww", decay_s=DEFAULT_DECAY_S, priority=2,
    ),
    "excited": EmotionDef(
        face="excited", led_hue=0.17, led_sat=1.0,
        sound="woo hoo!", decay_s=DEFAULT_DECAY_S, priority=5,
    ),
    "sleepy": EmotionDef(
        face="sleepy", led_hue=0.08, led_sat=0.6,
        sound="", decay_s=SLEEPY_DECAY_S, priority=1,
    ),
    "startled": EmotionDef(
        face="startled", led_hue=0.0, led_sat=1.0,
        sound="oh!", decay_s=STARTLED_DECAY_S, priority=6,
    ),
}


# ---------------------------------------------------------------------------
# ExpressionEngine
# ---------------------------------------------------------------------------

class ExpressionEngine:
    """Coordinated emotion expression engine for Vector.

    Parameters
    ----------
    display:
        DisplayController instance (constructed WITHOUT event_bus).
    leds:
        LedController instance.
    speech:
        SpeechOutput instance (may be ``None`` to disable sounds).
    bus:
        NucEventBus for event subscriptions and expression-change notifications.
    """

    def __init__(
        self,
        display: DisplayController,
        leds: LedController,
        speech: SpeechOutput | None,
        bus: NucEventBus,
    ) -> None:
        self._display = display
        self._leds = leds
        self._speech = speech
        self._bus = bus

        self._lock = threading.Lock()
        self._current: str = "idle"
        self._decay_timer: threading.Timer | None = None
        self._running = False

        # Event subscriptions for teardown
        self._subscriptions: list[tuple[str, Any]] = []

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to events that trigger emotions."""
        if self._running:
            return
        self._running = True
        handlers = [
            (YOLO_PERSON_DETECTED, self._on_person_detected),
            (FACE_RECOGNIZED, self._on_face_recognized),
            (TOUCH_DETECTED, self._on_touch),
            (COMMAND_RECEIVED, self._on_command),
            (CLIFF_TRIGGERED, self._on_cliff),
            (WAKE_WORD_DETECTED, self._on_wake_word),
        ]
        for event_name, handler in handlers:
            self._bus.on(event_name, handler)
            self._subscriptions.append((event_name, handler))
        logger.info("ExpressionEngine started")

    def stop(self) -> None:
        """Unsubscribe from events and cancel pending timers."""
        if not self._running:
            return
        self._running = False
        self._cancel_decay_timer()
        for event_name, handler in self._subscriptions:
            self._bus.off(event_name, handler)
        self._subscriptions.clear()
        logger.info("ExpressionEngine stopped")

    # -- Public API ----------------------------------------------------------

    def express(self, emotion: str, duration_s: float | None = None,
                _trigger: str = "api") -> None:
        """Trigger an emotion expression.

        Parameters
        ----------
        emotion:
            One of the defined emotion names (see ``EMOTIONS``).
        duration_s:
            Override the default decay duration. ``None`` uses the emotion's
            default. ``0`` means no auto-decay (stays until replaced).

        Raises
        ------
        ValueError
            If *emotion* is not a recognised emotion name.
        """
        if emotion not in EMOTIONS:
            raise ValueError(
                f"Unknown emotion {emotion!r}; "
                f"choose from {sorted(EMOTIONS)}"
            )

        defn = EMOTIONS[emotion]
        decay = duration_s if duration_s is not None else defn.decay_s

        with self._lock:
            # Priority check: don't downgrade a higher-priority active emotion
            current_defn = EMOTIONS.get(self._current, EMOTIONS["idle"])
            if emotion != "idle" and defn.priority < current_defn.priority:
                logger.debug(
                    "Suppressed %s (pri %d) — %s active (pri %d)",
                    emotion, defn.priority, self._current, current_defn.priority,
                )
                return

            previous = self._current
            if emotion == previous:
                # Same emotion — just reset the decay timer
                self._reset_decay_timer_locked(decay)
                return

            self._current = emotion
            # Cancel old timer under lock to prevent race with _on_decay
            old_timer = self._decay_timer
            self._decay_timer = None

        if old_timer is not None:
            old_timer.cancel()

        # Drive the three subsystems
        self._display.set_expression(defn.face)

        if emotion == "idle":
            # Short-lived override that auto-reverts to LedController's state
            self._leds.override(
                hue=defn.led_hue,
                saturation=defn.led_sat,
                duration_s=_IDLE_OVERRIDE_DURATION_S,
            )
        else:
            self._leds.override(
                hue=defn.led_hue,
                saturation=defn.led_sat,
                duration_s=decay if decay > 0 else None,
            )

        if defn.sound and self._speech is not None:
            try:
                self._speech.speak(defn.sound)
            except Exception:
                logger.exception("Expression sound failed for %s", emotion)

        # Emit change event
        self._bus.emit(
            EXPRESSION_CHANGED,
            ExpressionChangedEvent(
                emotion=emotion,
                previous_emotion=previous,
                trigger=_trigger,
            ),
        )
        logger.info("Expression: %s → %s (trigger=%s)", previous, emotion, _trigger)

        # Set up decay timer
        if emotion != "idle" and decay > 0:
            self._set_decay_timer(decay)

    @property
    def current_emotion(self) -> str:
        """The currently active emotion."""
        with self._lock:
            return self._current

    # -- Event handlers ------------------------------------------------------

    def _on_person_detected(self, event: Any) -> None:
        self.express("curious", _trigger="person_detected")

    def _on_face_recognized(self, event: Any) -> None:
        self.express("happy", _trigger="face_recognized")

    def _on_touch(self, event: Any) -> None:
        is_pressed = getattr(event, "is_pressed", True)
        if is_pressed:
            self.express("happy", _trigger="touch")

    def _on_command(self, event: Any) -> None:
        self.express("curious", _trigger="command")

    def _on_cliff(self, event: Any) -> None:
        self.express("startled", _trigger="cliff")

    def _on_wake_word(self, event: Any) -> None:
        self.express("curious", _trigger="wake_word")

    # -- Decay timer ---------------------------------------------------------

    def _set_decay_timer(self, duration_s: float) -> None:
        """Start a timer that reverts to idle after *duration_s*."""
        with self._lock:
            self._decay_timer = threading.Timer(duration_s, self._on_decay)
            self._decay_timer.daemon = True
            self._decay_timer.start()

    def _reset_decay_timer_locked(self, decay_s: float) -> None:
        """Reset the decay timer (must be called while holding ``_lock``)."""
        timer = self._decay_timer
        self._decay_timer = None
        if timer is not None:
            timer.cancel()

        if decay_s > 0:
            self._decay_timer = threading.Timer(decay_s, self._on_decay)
            self._decay_timer.daemon = True
            self._decay_timer.start()

    def _cancel_decay_timer(self) -> None:
        """Cancel the pending decay timer, if any."""
        with self._lock:
            timer = self._decay_timer
            self._decay_timer = None
        if timer is not None:
            timer.cancel()

    def _on_decay(self) -> None:
        """Timer callback — revert to idle."""
        with self._lock:
            previous = self._current
            if previous == "idle":
                return
            self._current = "idle"
            self._decay_timer = None

        self._display.set_expression("idle")
        # LED override auto-expires via LedController's own timer

        self._bus.emit(
            EXPRESSION_CHANGED,
            ExpressionChangedEvent(
                emotion="idle",
                previous_emotion=previous,
                trigger="decay",
            ),
        )
        logger.info("Expression decayed: %s → idle", previous)
