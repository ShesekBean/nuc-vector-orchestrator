"""Battery monitor for Vector — tracks state and triggers safety actions.

Subscribes to ``battery_state`` events on the NUC bus (emitted by
SdkEventBridge from SDK robot_state) and:

- Tracks voltage, level, and charging state
- Emits ``battery_low`` when battery drops below warning threshold
- Emits ``emergency_stop`` when battery reaches critical level
- Only fires on *transitions* to avoid spamming at ~15 Hz event rate
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    BATTERY_LOW,
    BATTERY_STATE,
    EMERGENCY_STOP,
    BatteryLowEvent,
    BatteryStateEvent,
    EmergencyStopEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Voltage thresholds (configurable via constructor).
DEFAULT_LOW_VOLTAGE = 3.6
DEFAULT_CRITICAL_VOLTAGE = 3.5

# Severity constants
SEVERITY_NORMAL = "normal"
SEVERITY_LOW = "low"
SEVERITY_CRITICAL = "critical"


class BatteryMonitor:
    """Monitors battery state and triggers low/critical actions.

    Usage::

        monitor = BatteryMonitor(robot, nuc_bus)
        monitor.start()
        # ... robot is running ...
        monitor.stop()
    """

    def __init__(
        self,
        robot: Any,
        nuc_bus: NucEventBus,
        low_voltage: float = DEFAULT_LOW_VOLTAGE,
        critical_voltage: float = DEFAULT_CRITICAL_VOLTAGE,
    ) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._low_voltage = low_voltage
        self._critical_voltage = critical_voltage
        self._running = False
        self._lock = threading.Lock()

        # Current state — protected by _lock
        self._severity: str = SEVERITY_NORMAL
        self._last_voltage: float = 0.0
        self._last_level: int = 0
        self._is_charging: bool = False
        self._is_on_charger: bool = False
        self._update_count: int = 0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to battery_state events on the NUC bus."""
        if self._running:
            return
        self._bus.on(BATTERY_STATE, self._on_battery_state)
        self._running = True
        logger.info(
            "BatteryMonitor started (low=%.2fV, critical=%.2fV)",
            self._low_voltage,
            self._critical_voltage,
        )

    def stop(self) -> None:
        """Unsubscribe from battery_state events."""
        if not self._running:
            return
        self._bus.off(BATTERY_STATE, self._on_battery_state)
        self._running = False
        logger.info(
            "BatteryMonitor stopped (updates=%d, last=%.2fV)",
            self._update_count,
            self._last_voltage,
        )

    # -- Public state accessors ----------------------------------------------

    @property
    def voltage(self) -> float:
        """Last reported battery voltage."""
        return self._last_voltage

    @property
    def level(self) -> int:
        """Last reported SDK battery level (0-3)."""
        return self._last_level

    @property
    def is_charging(self) -> bool:
        """Whether the battery is currently charging."""
        return self._is_charging

    @property
    def is_on_charger(self) -> bool:
        """Whether Vector is on the charger platform."""
        return self._is_on_charger

    @property
    def severity(self) -> str:
        """Current battery severity: 'normal', 'low', or 'critical'."""
        return self._severity

    @property
    def update_count(self) -> int:
        """Number of battery state updates received."""
        return self._update_count

    # -- Event handler -------------------------------------------------------

    def _on_battery_state(self, event: BatteryStateEvent) -> None:
        """Process a battery_state event and check for transitions."""
        with self._lock:
            self._last_voltage = event.voltage
            self._last_level = event.level
            self._is_charging = event.is_charging
            self._is_on_charger = event.is_on_charger
            self._update_count += 1

            new_severity = self._classify_voltage(event.voltage)

            # Skip if charging — don't trigger warnings while on charger
            if event.is_charging:
                if self._severity != SEVERITY_NORMAL:
                    logger.info(
                        "Battery charging (%.2fV) — clearing %s state",
                        event.voltage,
                        self._severity,
                    )
                    self._severity = SEVERITY_NORMAL
                return

            # Only act on transitions (not every ~15 Hz update)
            if new_severity == self._severity:
                return

            old_severity = self._severity
            self._severity = new_severity

        # Release lock before emitting events (callbacks may be slow)
        if new_severity == SEVERITY_LOW:
            logger.warning(
                "Battery LOW (%.2fV, level=%d) — was %s",
                event.voltage,
                event.level,
                old_severity,
            )
            self._bus.emit(
                BATTERY_LOW,
                BatteryLowEvent(
                    voltage=event.voltage,
                    level=event.level,
                    severity=SEVERITY_LOW,
                ),
            )
        elif new_severity == SEVERITY_CRITICAL:
            logger.critical(
                "Battery CRITICAL (%.2fV, level=%d) — stopping motors",
                event.voltage,
                event.level,
            )
            self._bus.emit(
                BATTERY_LOW,
                BatteryLowEvent(
                    voltage=event.voltage,
                    level=event.level,
                    severity=SEVERITY_CRITICAL,
                ),
            )
            self._bus.emit(
                EMERGENCY_STOP,
                EmergencyStopEvent(
                    source="battery_critical",
                    details=f"voltage={event.voltage:.2f}V",
                ),
            )
        elif new_severity == SEVERITY_NORMAL and old_severity != SEVERITY_NORMAL:
            logger.info(
                "Battery recovered to normal (%.2fV, level=%d)",
                event.voltage,
                event.level,
            )

    def _classify_voltage(self, voltage: float) -> str:
        """Classify voltage into severity level."""
        if voltage <= self._critical_voltage:
            return SEVERITY_CRITICAL
        if voltage <= self._low_voltage:
            return SEVERITY_LOW
        return SEVERITY_NORMAL
