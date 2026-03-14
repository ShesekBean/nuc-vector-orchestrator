"""Presence state machine — tracks who is near Vector and for how long.

Subscribes to face recognition, person detection, touch, and wake word
events on the NUC event bus.  Emits ``PRESENCE_CHANGED`` events that the
companion dispatcher uses to decide when to greet, check in, or say goodbye.

State file: ``~/.openclaw/workspace/memory/companion-state.json``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from apps.vector.src.events.event_types import (
    FACE_RECOGNIZED,
    PRESENCE_CHANGED,
    TOUCH_DETECTED,
    WAKE_WORD_DETECTED,
    YOLO_PERSON_DETECTED,
    PresenceChangedEvent,
)

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "companion-state.json"

# Seconds without any detection before we consider the person gone
ABSENCE_TIMEOUT_S = 120.0

# Minimum seconds between "still_present" check-in signals
CHECKIN_MIN_INTERVAL_S = 1200.0  # 20 min (dispatcher further throttles)

# Engagement score decay per second of no interaction
ENGAGEMENT_DECAY_PER_S = 0.0005  # ~0.03/min → drops from 1.0 to 0 in ~33 min


@dataclass
class PresenceState:
    """Mutable presence state for one tracked context (whole room)."""

    is_present: bool = False
    person_name: str = "unknown"
    last_seen_at: float = 0.0
    first_seen_today: float = 0.0
    session_start: float = 0.0
    away_since: float = 0.0
    engagement_score: float = 0.0
    interactions_today: int = 0
    last_greeting_at: float = 0.0
    last_checkin_at: float = 0.0
    last_interaction_at: float = 0.0
    today_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PresenceState:
        known = {f.name for f in field()} if False else set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


class PresenceTracker:
    """Event-bus-driven presence state machine.

    Parameters
    ----------
    bus : NucEventBus
        The NUC event bus to subscribe to and emit on.
    """

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self._state = PresenceState()
        self._running = False
        self._subscriptions: list[tuple[str, Any]] = []
        self._absence_timer: threading.Timer | None = None
        self._checkin_timer: threading.Timer | None = None
        self._load_state()

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        handlers = [
            (FACE_RECOGNIZED, self._on_face_recognized),
            (YOLO_PERSON_DETECTED, self._on_person_detected),
            (TOUCH_DETECTED, self._on_touch),
            (WAKE_WORD_DETECTED, self._on_wake_word),
        ]
        for event_name, handler in handlers:
            self._bus.on(event_name, handler)
            self._subscriptions.append((event_name, handler))

        # If we were present when we last saved, restart absence timer
        if self._state.is_present:
            self._reset_absence_timer()
            self._schedule_checkin()

        logger.info("PresenceTracker started (present=%s)", self._state.is_present)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._cancel_timer("_absence_timer")
        self._cancel_timer("_checkin_timer")
        for event_name, handler in self._subscriptions:
            self._bus.off(event_name, handler)
        self._subscriptions.clear()
        self._save_state()
        logger.info("PresenceTracker stopped")

    @property
    def state(self) -> PresenceState:
        with self._lock:
            return self._state

    # -- Event handlers ------------------------------------------------------

    def _on_face_recognized(self, event: Any) -> None:
        name = getattr(event, "name", "unknown")
        confidence = getattr(event, "confidence", 0.0)
        if confidence < 0.4:
            name = "unknown"
        self._record_detection(name, interaction=False)

    def _on_person_detected(self, event: Any) -> None:
        # YOLO gives us a person but no name
        self._record_detection(self._state.person_name or "unknown", interaction=False)

    def _on_touch(self, event: Any) -> None:
        is_pressed = getattr(event, "is_pressed", True)
        if is_pressed:
            self._record_detection(self._state.person_name or "unknown", interaction=True)
            self._emit_signal("touch")

    def _on_wake_word(self, event: Any) -> None:
        self._record_detection(self._state.person_name or "unknown", interaction=True)

    # -- Core state logic ----------------------------------------------------

    def _record_detection(self, person_name: str, interaction: bool) -> None:
        """Process a new detection — may trigger arrival or refresh timers."""
        now = time.time()
        today = time.strftime("%Y-%m-%d")

        with self._lock:
            # Update name if we got a better one (face vs generic person)
            if person_name != "unknown":
                self._state.person_name = person_name

            was_present = self._state.is_present

            # Day rollover
            if self._state.today_date != today:
                self._state.today_date = today
                self._state.first_seen_today = 0.0
                self._state.interactions_today = 0

            self._state.last_seen_at = now

            if interaction:
                # Boost engagement score on interaction
                self._state.engagement_score = min(
                    1.0, self._state.engagement_score + 0.15
                )
                self._state.interactions_today += 1
                self._state.last_interaction_at = now

            if not was_present:
                # --- ARRIVAL ---
                away_duration = now - self._state.away_since if self._state.away_since > 0 else 0
                first_today = self._state.first_seen_today == 0.0

                self._state.is_present = True
                self._state.session_start = now
                if first_today:
                    self._state.first_seen_today = now

                # Decay engagement while away
                if self._state.away_since > 0:
                    elapsed = now - self._state.away_since
                    decay = elapsed * ENGAGEMENT_DECAY_PER_S
                    self._state.engagement_score = max(0.0, self._state.engagement_score - decay)

                signal_data = PresenceChangedEvent(
                    signal="arrival",
                    person_name=self._state.person_name,
                    is_present=True,
                    first_today=first_today,
                    away_duration_s=away_duration,
                    engagement_score=self._state.engagement_score,
                    interactions_today=self._state.interactions_today,
                )
                # Release lock before emitting
                self._state_snapshot = signal_data

        # Outside lock: emit arrival if needed
        if not was_present:
            self._emit_event(self._state_snapshot)
            self._save_state()

        # Always reset absence timer on detection
        self._reset_absence_timer()

        if not was_present:
            self._schedule_checkin()

    def _on_absence_timeout(self) -> None:
        """Called when no detection for ABSENCE_TIMEOUT_S — person departed."""
        now = time.time()
        with self._lock:
            if not self._state.is_present:
                return
            session_duration = now - self._state.session_start
            self._state.is_present = False
            self._state.away_since = now

            signal_data = PresenceChangedEvent(
                signal="departure",
                person_name=self._state.person_name,
                is_present=False,
                session_duration_s=session_duration,
                engagement_score=self._state.engagement_score,
                interactions_today=self._state.interactions_today,
            )

        self._cancel_timer("_checkin_timer")
        self._emit_event(signal_data)
        self._save_state()

    def _on_checkin_timer(self) -> None:
        """Periodic check-in while person is present."""
        now = time.time()
        with self._lock:
            if not self._state.is_present:
                return

            # Decay engagement
            if self._state.last_interaction_at > 0:
                elapsed = now - self._state.last_interaction_at
                decay = elapsed * ENGAGEMENT_DECAY_PER_S
                self._state.engagement_score = max(0.0, self._state.engagement_score - decay)

            session_duration = now - self._state.session_start
            self._state.last_checkin_at = now

            signal_data = PresenceChangedEvent(
                signal="still_present",
                person_name=self._state.person_name,
                is_present=True,
                session_duration_s=session_duration,
                engagement_score=self._state.engagement_score,
                interactions_today=self._state.interactions_today,
            )

        self._emit_event(signal_data)
        self._schedule_checkin()

    def emit_goodnight(self) -> None:
        """Manually trigger a GOODNIGHT signal (called by dispatcher)."""
        with self._lock:
            signal_data = PresenceChangedEvent(
                signal="goodnight",
                person_name=self._state.person_name,
                is_present=self._state.is_present,
                engagement_score=self._state.engagement_score,
                interactions_today=self._state.interactions_today,
            )
        self._emit_event(signal_data)

    # -- Helpers -------------------------------------------------------------

    def _emit_signal(self, signal_type: str) -> None:
        """Emit a presence signal for the current state."""
        with self._lock:
            signal_data = PresenceChangedEvent(
                signal=signal_type,
                person_name=self._state.person_name,
                is_present=self._state.is_present,
                session_duration_s=time.time() - self._state.session_start if self._state.is_present else 0,
                engagement_score=self._state.engagement_score,
                interactions_today=self._state.interactions_today,
            )
        self._emit_event(signal_data)

    def _emit_event(self, event: PresenceChangedEvent) -> None:
        """Emit a PRESENCE_CHANGED event on the bus."""
        logger.info(
            "Presence: %s (%s, engagement=%.2f)",
            event.signal, event.person_name, event.engagement_score,
        )
        self._bus.emit(PRESENCE_CHANGED, event)

    def _reset_absence_timer(self) -> None:
        self._cancel_timer("_absence_timer")
        timer = threading.Timer(ABSENCE_TIMEOUT_S, self._on_absence_timeout)
        timer.daemon = True
        timer.start()
        self._absence_timer = timer

    def _schedule_checkin(self) -> None:
        self._cancel_timer("_checkin_timer")
        timer = threading.Timer(CHECKIN_MIN_INTERVAL_S, self._on_checkin_timer)
        timer.daemon = True
        timer.start()
        self._checkin_timer = timer

    def _cancel_timer(self, attr: str) -> None:
        timer = getattr(self, attr, None)
        if timer is not None:
            timer.cancel()
            setattr(self, attr, None)

    # -- Persistence ---------------------------------------------------------

    def _load_state(self) -> None:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
                self._state = PresenceState.from_dict(data)
                logger.info("Loaded companion state from %s", STATE_PATH)
            except Exception:
                logger.warning("Failed to load companion state, using defaults")

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self._state.to_dict(), indent=2))
        except Exception:
            logger.exception("Failed to save companion state")
