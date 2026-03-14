"""Companion dispatcher — throttling, night mode, and OpenClaw signalling.

Listens for ``PRESENCE_CHANGED`` events from the presence tracker, applies
engagement-adaptive throttling and quiet-hours logic, then formats a rich
context message and sends it to OpenClaw via the companion WebSocket client.

The dispatcher also runs proactive timers (goodnight, battery alerts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.vector.src.events.event_types import PRESENCE_CHANGED, PresenceChangedEvent

logger = logging.getLogger(__name__)

# Paths
COMPANION_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "companion-log.json"

# Throttle windows (seconds)
GREETING_MIN_INTERVAL_S = 120.0
TOUCH_MIN_INTERVAL_S = 30.0
DEPARTURE_MIN_SESSION_S = 300.0  # only say bye if session > 5 min

# Engagement-based check-in intervals
CHECKIN_HIGH_S = 1200.0     # 20 min
CHECKIN_MEDIUM_S = 2700.0   # 45 min
CHECKIN_LOW_S = 5400.0      # 90 min

# Quiet hours (fallback if no Oura data)
QUIET_HOUR_START = 23  # 11 PM
QUIET_HOUR_END = 7     # 7 AM

# Battery alert threshold
BATTERY_LOW_PERCENT = 20


def _format_duration(seconds: float) -> str:
    """Human-readable duration like '2 hours 14 minutes'."""
    if seconds < 60:
        return f"{int(seconds)} seconds"
    minutes = int(seconds // 60)
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0 and mins > 0:
        return f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"
    if hours > 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{mins} minute{'s' if mins != 1 else ''}"


def _now_str() -> str:
    """Current local time as a friendly string."""
    return datetime.now().strftime("%-I:%M %p %A")


class CompanionDispatcher:
    """Throttles presence signals and sends them to OpenClaw.

    Parameters
    ----------
    bus : NucEventBus
        Event bus for subscribing to PRESENCE_CHANGED.
    presence_tracker : PresenceTracker
        For reading current state and triggering goodnight.
    bridge_url : str
        Bridge HTTP URL for battery checks.
    """

    def __init__(self, bus: Any, presence_tracker: Any, bridge_url: str = "http://127.0.0.1:8081") -> None:
        self._bus = bus
        self._tracker = presence_tracker
        self._bridge_url = bridge_url
        self._running = False
        self._subscriptions: list[tuple[str, Any]] = []
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None

        # Throttle timestamps
        self._last_greeting_at: float = 0.0
        self._last_touch_at: float = 0.0
        self._last_checkin_at: float = 0.0
        self._last_departure_at: float = 0.0
        self._last_battery_alert_at: float = 0.0

        # Goodnight tracking
        self._goodnight_sent_today: str = ""

        # Last response from OpenClaw (for logging)
        self._last_said: str = ""
        self._last_said_at: float = 0.0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Start async event loop in background thread for WebSocket calls
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(
            target=self._async_loop.run_forever, daemon=True, name="companion-async",
        )
        self._async_thread.start()

        self._bus.on(PRESENCE_CHANGED, self._on_presence_changed)
        self._subscriptions.append((PRESENCE_CHANGED, self._on_presence_changed))

        logger.info("CompanionDispatcher started")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for event_name, handler in self._subscriptions:
            self._bus.off(event_name, handler)
        self._subscriptions.clear()

        if self._async_loop is not None:
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        logger.info("CompanionDispatcher stopped")

    # -- Event handler -------------------------------------------------------

    def _on_presence_changed(self, event: PresenceChangedEvent) -> None:
        """Route presence events through throttling and send to OpenClaw."""
        if not self._running:
            return

        # Night mode check
        if self._is_quiet_hours() and event.signal not in ("goodnight",):
            logger.debug("Suppressed %s signal during quiet hours", event.signal)
            return

        now = time.time()

        # Apply per-signal throttling
        if event.signal == "arrival":
            if now - self._last_greeting_at < GREETING_MIN_INTERVAL_S:
                logger.debug("Throttled arrival greeting (too recent)")
                return
            self._last_greeting_at = now

        elif event.signal == "touch":
            if now - self._last_touch_at < TOUCH_MIN_INTERVAL_S:
                logger.debug("Throttled touch response")
                return
            self._last_touch_at = now

        elif event.signal == "still_present":
            interval = self._checkin_interval(event.engagement_score)
            if now - self._last_checkin_at < interval:
                logger.debug("Throttled check-in (interval=%.0fs)", interval)
                return
            self._last_checkin_at = now

        elif event.signal == "departure":
            if event.session_duration_s < DEPARTURE_MIN_SESSION_S:
                logger.debug("Suppressed departure (session too short: %.0fs)", event.session_duration_s)
                return
            self._last_departure_at = now

        # Format and send
        message = self._format_signal(event)
        self._send_to_openclaw(message)

    # -- Throttle helpers ----------------------------------------------------

    def _checkin_interval(self, engagement: float) -> float:
        if engagement > 0.7:
            return CHECKIN_HIGH_S
        if engagement > 0.3:
            return CHECKIN_MEDIUM_S
        return CHECKIN_LOW_S

    def _is_quiet_hours(self) -> bool:
        """Check if we're in quiet hours (time-based)."""
        hour = datetime.now().hour
        return hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END

    # -- Message formatting --------------------------------------------------

    def _format_signal(self, event: PresenceChangedEvent) -> str:
        """Build the rich context message for OpenClaw."""
        parts = [
            "[COMPANION SIGNAL — activate companion skill]",
            f"Signal: {event.signal.upper()}",
            f"Person: {event.person_name}",
            f"Time: {_now_str()}",
        ]

        if event.signal == "arrival":
            if event.away_duration_s > 0:
                parts.append(f"Away: {_format_duration(event.away_duration_s)}")
            parts.append(f"First today: {'yes' if event.first_today else 'no'}")

        if event.signal == "still_present":
            parts.append(f"Present for: {_format_duration(event.session_duration_s)}")

        if event.signal == "departure":
            parts.append(f"Session lasted: {_format_duration(event.session_duration_s)}")

        parts.append(f"Engagement score: {event.engagement_score:.2f}")
        parts.append(f"Interactions today: {event.interactions_today}")

        # Battery status (best effort)
        battery_info = self._get_battery_info()
        if battery_info:
            parts.append(f"Battery: {battery_info}")

        # Last thing Vector said
        if self._last_said:
            ago = time.time() - self._last_said_at
            parts.append(f'Last said: "{self._last_said[:60]}" ({_format_duration(ago)} ago)')

        # Camera frame for unknown person
        if event.person_name == "unknown" and event.signal == "arrival":
            frame_b64 = self._capture_frame_b64()
            if frame_b64:
                parts.append(f"\n[Camera frame attached as base64 for identification]\nImage: {frame_b64}")

        return "\n".join(parts)

    def _get_battery_info(self) -> str:
        """Quick synchronous battery check via bridge."""
        try:
            import urllib.request
            with urllib.request.urlopen(f"{self._bridge_url}/health", timeout=2) as resp:
                data = json.loads(resp.read())
                battery = data.get("battery", {})
                pct = battery.get("percent", -1)
                charging = battery.get("is_charging", False)
                suffix = " (charging)" if charging else ""
                if pct >= 0:
                    return f"{pct}%{suffix}"
        except Exception:
            pass
        return ""

    def _capture_frame_b64(self) -> str:
        """Capture a camera frame as base64 for person identification."""
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"{self._bridge_url}/capture?format=base64", timeout=5,
            ) as resp:
                data = json.loads(resp.read())
                return data.get("image", "")
        except Exception:
            logger.debug("Failed to capture frame for identification")
            return ""

    # -- OpenClaw communication ----------------------------------------------

    def _send_to_openclaw(self, message: str) -> None:
        """Schedule an async OpenClaw chat.send on the background loop."""
        if self._async_loop is None or self._async_loop.is_closed():
            logger.warning("Async loop not available, skipping companion signal")
            return
        asyncio.run_coroutine_threadsafe(
            self._async_send(message), self._async_loop,
        )

    async def _async_send(self, message: str) -> None:
        """Send message to OpenClaw and log the response."""
        from apps.vector.src.companion.openclaw_client import openclaw_chat

        try:
            response = await openclaw_chat(message, session_key="hook:companion")
            if response and response not in ("OK", "timeout", "error"):
                self._last_said = response[:200]
                self._last_said_at = time.time()
                self._log_interaction(message, response)
            logger.info("OpenClaw companion response: %s", response[:120])
        except Exception:
            logger.exception("Failed to send companion signal to OpenClaw")

    def _log_interaction(self, signal: str, response: str) -> None:
        """Append to companion-log.json for memory continuity."""
        try:
            log_path = COMPANION_LOG_PATH
            log_path.parent.mkdir(parents=True, exist_ok=True)

            entries = []
            if log_path.exists():
                try:
                    entries = json.loads(log_path.read_text())
                except Exception:
                    entries = []

            entries.append({
                "timestamp": datetime.now().isoformat(),
                "signal": signal.split("\n")[1] if "\n" in signal else signal[:80],
                "response": response[:500],
            })

            # Keep last 200 entries
            if len(entries) > 200:
                entries = entries[-200:]

            log_path.write_text(json.dumps(entries, indent=2))
        except Exception:
            logger.debug("Failed to log companion interaction")

    # -- Proactive timers (called externally) --------------------------------

    def check_goodnight(self) -> None:
        """Check if it's time to send a goodnight signal.

        Should be called periodically (e.g. every 5 min) by the companion
        system's main timer.
        """
        today = time.strftime("%Y-%m-%d")
        if self._goodnight_sent_today == today:
            return

        hour = datetime.now().hour
        if hour == 22 and self._tracker.state.is_present:
            self._goodnight_sent_today = today
            self._tracker.emit_goodnight()

    def check_battery(self) -> None:
        """Check battery level and alert if low."""
        now = time.time()
        if now - self._last_battery_alert_at < 3600:  # max once per hour
            return

        try:
            import urllib.request
            with urllib.request.urlopen(f"{self._bridge_url}/health", timeout=2) as resp:
                data = json.loads(resp.read())
                pct = data.get("battery", {}).get("percent", 100)
                if pct < BATTERY_LOW_PERCENT:
                    self._last_battery_alert_at = now
                    message = (
                        "[COMPANION SIGNAL — activate companion skill]\n"
                        f"Signal: BATTERY_LOW\n"
                        f"Battery: {pct}%\n"
                        f"Time: {_now_str()}"
                    )
                    self._send_to_openclaw(message)
        except Exception:
            pass
