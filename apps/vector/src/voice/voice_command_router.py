"""Voice command router — dual-path routing for voice commands.

Implements the dual strategy from issue #22:

1. **Wire-pod intents** — simple built-in commands (forward, stop, volume)
   are handled locally by mapping SDK ``user_intent`` events to HTTP bridge
   endpoints on ``localhost:8080``.

2. **OpenClaw agent** — custom commands ("follow me", "what do you see",
   financial queries) are handled by ``OpenClawVoiceBridge`` via the
   wake-word → STT → OpenClaw hooks pipeline.

Both paths emit ``COMMAND_RECEIVED`` on the NUC event bus and speak
confirmations through ``SpeechOutput`` / ``say_text()``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    USER_INTENT,
    CommandReceivedEvent,
    UserIntentEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.voice.speech_output import SpeechOutput

logger = logging.getLogger(__name__)

DEFAULT_BRIDGE_URL = "http://localhost:8080"
DEFAULT_BRIDGE_TIMEOUT_SEC = 10

# Default movement parameters for wire-pod intent commands.
DEFAULT_DRIVE_DISTANCE_MM = 200
DEFAULT_DRIVE_SPEED_MMPS = 100
DEFAULT_TURN_ANGLE_DEG = 90
DEFAULT_TURN_SPEED_DPS = 100

# Volume ladder for up/down commands.
_VOLUME_LADDER = ("mute", "low", "medium_low", "medium", "medium_high", "high")


# ---------------------------------------------------------------------------
# Intent → bridge action mapping
# ---------------------------------------------------------------------------

class IntentAction:
    """A bridge HTTP call + spoken confirmation for a wire-pod intent."""

    __slots__ = ("method", "path", "body", "confirmation")

    def __init__(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        confirmation: str = "",
    ) -> None:
        self.method = method
        self.path = path
        self.body = body
        self.confirmation = confirmation


def _build_intent_map() -> dict[str, IntentAction]:
    """Build the default wire-pod intent → bridge action mapping."""
    return {
        "intent_imperative_forward": IntentAction(
            "POST", "/move",
            {"type": "straight", "distance_mm": DEFAULT_DRIVE_DISTANCE_MM,
             "speed_mmps": DEFAULT_DRIVE_SPEED_MMPS},
            "Moving forward",
        ),
        "intent_imperative_reverse": IntentAction(
            "POST", "/move",
            {"type": "straight", "distance_mm": -DEFAULT_DRIVE_DISTANCE_MM,
             "speed_mmps": DEFAULT_DRIVE_SPEED_MMPS},
            "Moving backward",
        ),
        "intent_imperative_turn_left": IntentAction(
            "POST", "/move",
            {"type": "turn", "angle_deg": DEFAULT_TURN_ANGLE_DEG,
             "speed_dps": DEFAULT_TURN_SPEED_DPS},
            "Turning left",
        ),
        "intent_imperative_turn_right": IntentAction(
            "POST", "/move",
            {"type": "turn", "angle_deg": -DEFAULT_TURN_ANGLE_DEG,
             "speed_dps": DEFAULT_TURN_SPEED_DPS},
            "Turning right",
        ),
        "intent_imperative_stop": IntentAction(
            "POST", "/stop", None, "Stopping",
        ),
        "intent_imperative_come": IntentAction(
            "POST", "/move",
            {"type": "straight", "distance_mm": DEFAULT_DRIVE_DISTANCE_MM * 2,
             "speed_mmps": DEFAULT_DRIVE_SPEED_MMPS},
            "Coming to you",
        ),
        "intent_imperative_lookatme": IntentAction(
            "POST", "/head", {"angle_deg": 0}, "Looking at you",
        ),
        "intent_greeting_hello": IntentAction(
            "POST", "/display", {"expression": "happy"}, "Hello!",
        ),
    }


# Volume intents are handled specially (not bridge calls).
_VOLUME_INTENTS = {
    "intent_imperative_volumeup": 1,
    "intent_imperative_volumedown": -1,
}


class VoiceCommandRouter:
    """Routes wire-pod intents to HTTP bridge endpoints.

    Args:
        nuc_bus: NUC event bus for ``COMMAND_RECEIVED`` emission.
        speech: ``SpeechOutput`` instance for spoken confirmations.
        bridge_url: Base URL of the HTTP→gRPC bridge.
        bridge_timeout: HTTP request timeout in seconds.
        intent_map: Override the default intent→action mapping.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        speech: SpeechOutput,
        *,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        bridge_timeout: float = DEFAULT_BRIDGE_TIMEOUT_SEC,
        intent_map: dict[str, IntentAction] | None = None,
    ) -> None:
        self._bus = nuc_bus
        self._speech = speech
        self._bridge_url = bridge_url.rstrip("/")
        self._bridge_timeout = bridge_timeout
        self._intent_map = intent_map if intent_map is not None else _build_intent_map()

        # Metrics
        self._total_handled = 0
        self._total_errors = 0
        self._total_unknown = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to USER_INTENT events on the NUC bus."""
        self._bus.on(USER_INTENT, self._on_user_intent)
        logger.info(
            "VoiceCommandRouter started (bridge=%s, intents=%d)",
            self._bridge_url,
            len(self._intent_map),
        )

    def stop(self) -> None:
        """Unsubscribe from events."""
        self._bus.off(USER_INTENT, self._on_user_intent)
        logger.info(
            "VoiceCommandRouter stopped (handled=%d, errors=%d, unknown=%d)",
            self._total_handled,
            self._total_errors,
            self._total_unknown,
        )

    def start_sdk_listener(self, robot: Any) -> None:
        """Subscribe to Vector SDK ``user_intent`` events.

        Bridges SDK events to the NUC bus as ``USER_INTENT`` events.

        Args:
            robot: Connected ``anki_vector.Robot`` instance.
        """
        try:
            from anki_vector.events import Events
        except ImportError:
            logger.warning(
                "anki_vector not installed — SDK user_intent listener disabled"
            )
            return

        robot.events.subscribe(self._on_sdk_user_intent, Events.user_intent)
        logger.info("SDK user_intent listener started")

    @property
    def total_handled(self) -> int:
        return self._total_handled

    @property
    def total_errors(self) -> int:
        return self._total_errors

    @property
    def total_unknown(self) -> int:
        return self._total_unknown

    # ------------------------------------------------------------------
    # Internal: SDK event handler
    # ------------------------------------------------------------------

    def _on_sdk_user_intent(self, _robot: Any, _name: str, msg: Any) -> None:
        """Handle SDK user_intent events — re-emit on NUC bus."""
        intent_name = getattr(msg, "intent", "")
        if not intent_name:
            # Try alternative attribute names from wire-pod SDK
            intent_name = getattr(msg, "intent_type", "")
        if not intent_name:
            intent_name = str(msg)

        params: dict = {}
        for attr in ("param", "params", "metadata"):
            val = getattr(msg, attr, None)
            if val is not None:
                if isinstance(val, dict):
                    params = val
                break

        event = UserIntentEvent(intent=intent_name, params=params)
        self._bus.emit(USER_INTENT, event)

    # ------------------------------------------------------------------
    # Internal: NUC bus event handler
    # ------------------------------------------------------------------

    def _on_user_intent(self, event: Any) -> None:
        """Handle USER_INTENT events from the NUC bus."""
        intent = getattr(event, "intent", "")
        if not intent:
            return

        logger.info("Routing intent: %s", intent)

        # Emit COMMAND_RECEIVED for downstream listeners (expression engine, etc.)
        self._bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(
                command=intent,
                source="sdk_intent",
                args=getattr(event, "params", {}),
            ),
        )

        # Volume intents are handled via SpeechOutput, not bridge.
        direction = _VOLUME_INTENTS.get(intent)
        if direction is not None:
            self._handle_volume(direction)
            return

        # Look up bridge action.
        action = self._intent_map.get(intent)
        if action is None:
            self._total_unknown += 1
            logger.info("Unknown intent (skipped): %s", intent)
            return

        # Execute bridge call.
        ok = self._call_bridge(action.method, action.path, action.body)
        if ok:
            self._total_handled += 1
            if action.confirmation:
                self._speech.speak(action.confirmation)
        else:
            self._total_errors += 1
            self._speech.speak("Command failed")

    # ------------------------------------------------------------------
    # Internal: bridge HTTP calls
    # ------------------------------------------------------------------

    def _call_bridge(
        self, method: str, path: str, body: dict | None = None
    ) -> bool:
        """Call the HTTP bridge and return True on success."""
        url = f"{self._bridge_url}{path}"
        data = json.dumps(body).encode("utf-8") if body else None

        req = Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=self._bridge_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                status = result.get("status", "")
                if status == "ok":
                    logger.info("Bridge %s %s → ok", method, path)
                    return True
                logger.warning(
                    "Bridge %s %s → status=%s", method, path, status
                )
                return False
        except HTTPError as exc:
            logger.error(
                "Bridge %s %s HTTP error %d: %s",
                method, path, exc.code, exc.reason,
            )
        except URLError as exc:
            logger.error(
                "Bridge %s %s connection error: %s",
                method, path, exc.reason,
            )
        except Exception:
            logger.exception("Bridge %s %s unexpected error", method, path)
        return False

    # ------------------------------------------------------------------
    # Internal: volume handling
    # ------------------------------------------------------------------

    def _handle_volume(self, direction: int) -> None:
        """Adjust volume up (+1) or down (-1) on the volume ladder."""
        current = self._speech.volume
        try:
            idx = _VOLUME_LADDER.index(current)
        except ValueError:
            idx = 3  # default to "medium"

        new_idx = max(0, min(len(_VOLUME_LADDER) - 1, idx + direction))
        new_level = _VOLUME_LADDER[new_idx]

        self._speech.set_volume(new_level)
        self._total_handled += 1

        if direction > 0:
            self._speech.speak("Volume raised")
        else:
            self._speech.speak("Volume lowered")
