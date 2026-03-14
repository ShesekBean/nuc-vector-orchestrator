"""Vector process supervisor — ordered startup, health monitoring, graceful shutdown.

Manages the full Vector component lifecycle on the NUC:
1. gRPC connection to Vector (with retry/backoff)
2. Event bus + SDK event bridge
3. All controllers and pipeline components
4. WiFi disconnect detection (SDK connection_lost event) + auto-reconnect
5. Component crash detection + auto-restart
6. Battery-aware functionality reduction
7. sd_notify READY=1 for systemd integration
8. Graceful SIGTERM shutdown (reverse order)

Usage::

    python3 -m apps.vector.supervisor [--serial 0dd1cdcf]

Or as a library::

    supervisor = VectorSupervisor(serial="0dd1cdcf")
    supervisor.run()  # blocks until SIGTERM/SIGINT
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from apps.vector.src.events.event_types import EMERGENCY_STOP, EmergencyStopEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VECTOR_SERIAL = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")

# Reconnection backoff
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0
RECONNECT_BACKOFF_FACTOR = 2.0

# Health check interval
HEALTH_CHECK_INTERVAL_S = 10.0

# Battery thresholds
BATTERY_LOW_LEVEL = 1  # battery_level enum: 1 = low
BATTERY_CHECK_INTERVAL_S = 60.0

# WiFi state event name
WIFI_STATE = "wifi_state"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class SupervisorState(Enum):
    """Top-level supervisor state."""

    INIT = auto()
    CONNECTING = auto()
    RUNNING = auto()
    RECONNECTING = auto()
    SHUTTING_DOWN = auto()
    STOPPED = auto()


@dataclass
class WifiStateEvent:
    """Payload for wifi_state events on the NUC bus."""

    connected: bool
    detail: str = ""


@dataclass
class ComponentInfo:
    """Metadata for a managed component."""

    name: str
    factory: Callable[[], Any]
    start_order: int
    is_critical: bool = False
    requires_connection: bool = True
    instance: Any = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)

    @property
    def is_started(self) -> bool:
        return self._started


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class VectorSupervisor:
    """Orchestrates Vector component lifecycle on the NUC.

    Parameters
    ----------
    serial:
        Vector serial number for SDK connection.
    """

    def __init__(self, serial: str = VECTOR_SERIAL) -> None:
        self._serial = serial
        self._state = SupervisorState.INIT
        self._state_lock = threading.Lock()

        # Core objects — created during startup
        self._robot: Any | None = None
        self._nuc_bus = NucEventBus()
        self._sdk_bridge: Any | None = None

        # Component registry (populated by _register_components)
        self._components: list[ComponentInfo] = []

        # Threads
        self._health_thread: threading.Thread | None = None
        self._battery_thread: threading.Thread | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()

        # Battery state
        self._low_battery = False

        # Signal handlers
        self._original_sigterm = None
        self._original_sigint = None

    # -- Public API ---------------------------------------------------------

    def run(self) -> None:
        """Block until SIGTERM/SIGINT.  Main entry point."""
        self._install_signal_handlers()
        try:
            if not self._connect_to_vector():
                logger.error("Failed to connect to Vector — exiting")
                return

            self._register_components()
            self._start_components()
            self._start_health_monitor()
            self._start_battery_monitor()
            self._set_state(SupervisorState.RUNNING)
            _sd_notify_ready()
            logger.info("Vector supervisor READY — all components started")

            # Block until shutdown signal
            self._shutdown_event.wait()
        finally:
            self._shutdown()
            self._restore_signal_handlers()

    # -- Connection ---------------------------------------------------------

    def _connect_to_vector(self) -> bool:
        """Connect to Vector with exponential backoff.

        Returns True on success, False if shutdown was requested during retries.
        """
        self._set_state(SupervisorState.CONNECTING)
        delay = RECONNECT_INITIAL_DELAY_S

        while not self._shutdown_event.is_set():
            try:
                import anki_vector

                logger.info("Connecting to Vector (serial=%s)...", self._serial)
                self._robot = anki_vector.Robot(
                    serial=self._serial, default_logging=False
                )
                self._robot.connect()

                # Set up SDK event bridge for connection_lost detection
                from apps.vector.src.events.sdk_events import SdkEventBridge

                self._sdk_bridge = SdkEventBridge(self._robot, self._nuc_bus)
                self._sdk_bridge.setup()

                # Subscribe to connection_lost via emergency_stop events
                self._nuc_bus.on(EMERGENCY_STOP, self._on_emergency_stop)

                logger.info("Connected to Vector successfully")
                return True
            except Exception:
                logger.exception(
                    "Failed to connect to Vector — retrying in %.1fs", delay
                )
                if self._shutdown_event.wait(timeout=delay):
                    return False
                delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY_S)

        return False

    def _reconnect(self) -> None:
        """Reconnect to Vector after WiFi drop (runs in dedicated thread)."""
        self._set_state(SupervisorState.RECONNECTING)
        self._nuc_bus.emit(WIFI_STATE, WifiStateEvent(connected=False))

        # Pause connection-dependent components
        self._pause_connected_components()

        # Disconnect stale robot
        self._disconnect_robot()

        delay = RECONNECT_INITIAL_DELAY_S
        while not self._shutdown_event.is_set():
            try:
                import anki_vector

                logger.info("Reconnecting to Vector (serial=%s)...", self._serial)
                self._robot = anki_vector.Robot(
                    serial=self._serial, default_logging=False
                )
                self._robot.connect()

                from apps.vector.src.events.sdk_events import SdkEventBridge

                self._sdk_bridge = SdkEventBridge(self._robot, self._nuc_bus)
                self._sdk_bridge.setup()

                logger.info("Reconnected to Vector")
                self._nuc_bus.emit(WIFI_STATE, WifiStateEvent(connected=True))

                # Resume components
                self._resume_connected_components()
                self._set_state(SupervisorState.RUNNING)
                return
            except Exception:
                logger.warning(
                    "Reconnect attempt failed — retrying in %.1fs", delay
                )
                if self._shutdown_event.wait(timeout=delay):
                    return
                delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY_S)

    def _disconnect_robot(self) -> None:
        """Safely disconnect the current robot instance."""
        if self._sdk_bridge:
            try:
                self._sdk_bridge.teardown()
            except Exception:
                logger.exception("Error tearing down SDK bridge")
            self._sdk_bridge = None

        if self._robot:
            try:
                self._robot.disconnect()
            except Exception:
                logger.exception("Error disconnecting robot")
            self._robot = None

    # -- Component lifecycle ------------------------------------------------

    def _register_components(self) -> None:
        """Build the ordered component list.

        Each component is a name + factory callable.  Factories are closures
        that capture self._robot and self._nuc_bus so components get the
        current robot instance.
        """
        from apps.vector.src.camera.camera_client import CameraClient
        from apps.vector.src.companion import CompanionSystem
        from apps.vector.src.companion.presence_loop import PresenceDetectionLoop
        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.display_controller import DisplayController
        from apps.vector.src.face_recognition.face_detector import FaceDetector
        from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer
        from apps.vector.src.led_controller import LedController
        from apps.vector.src.lift_controller import LiftController
        from apps.vector.src.motor_controller import MotorController
        from apps.vector.src.planner.follow_planner import FollowPlanner
        from apps.vector.src.sensor_handler import SensorHandler
        from apps.vector.src.voice.audio_client import AudioClient
        from apps.vector.src.voice.echo_cancel import EchoSuppressor
        from apps.vector.src.voice.openclaw_voice_bridge import OpenClawVoiceBridge
        from apps.vector.src.voice.speech_output import SpeechOutput
        from apps.vector.src.voice.wake_word import WakeWordDetector

        robot = self._robot
        bus = self._nuc_bus

        # Helper to build components lazily with current robot
        def _make_motor() -> MotorController:
            return MotorController(robot, bus)

        def _make_sensor() -> SensorHandler:
            return SensorHandler(robot, bus)

        def _make_led() -> LedController:
            return LedController(robot, bus)

        def _make_lift() -> LiftController:
            return LiftController(robot, bus)

        def _make_display() -> DisplayController:
            return DisplayController(robot, event_bus=bus)

        def _make_camera() -> CameraClient:
            return CameraClient(robot)

        def _make_detector() -> PersonDetector:
            return PersonDetector(bus)

        def _make_presence_loop() -> PresenceDetectionLoop:
            camera = self._get_component("camera_client")
            detector = self._get_component("person_detector")
            return PresenceDetectionLoop(
                camera_client=camera,
                person_detector=detector,
                face_detector=FaceDetector(),
                face_recognizer=FaceRecognizer(event_bus=bus),
                event_bus=bus,
            )

        def _make_audio() -> AudioClient:
            return AudioClient(robot, bus)

        def _make_wake_word() -> WakeWordDetector:
            return WakeWordDetector(bus)

        def _make_speech_output() -> SpeechOutput:
            return SpeechOutput(robot, bus)

        def _make_echo_suppressor() -> EchoSuppressor:
            audio = self._get_component("audio_client")
            return EchoSuppressor(bus, audio)

        def _make_voice_bridge() -> OpenClawVoiceBridge:
            audio = self._get_component("audio_client")
            return OpenClawVoiceBridge(robot, bus, audio)

        def _make_follow_planner() -> FollowPlanner:
            motor = self._get_component("motor_controller")
            return FollowPlanner(robot, bus, motor)

        def _make_companion() -> CompanionSystem:
            return CompanionSystem(bus)

        self._components = [
            # Order 1-2: connection + event bus handled before components
            # Order 3: Motor controller (needed for safety — cliff reactions)
            ComponentInfo("motor_controller", _make_motor, 3, is_critical=True),
            # Order 4: Sensor handler (cliff/touch safety)
            ComponentInfo("sensor_handler", _make_sensor, 4, is_critical=True),
            # Order 5: Camera stream
            ComponentInfo("camera_client", _make_camera, 5),
            # Order 6: LED controller
            ComponentInfo("led_controller", _make_led, 6),
            # Order 7: Lift controller
            ComponentInfo("lift_controller", _make_lift, 7),
            # Order 8: Display controller
            ComponentInfo("display_controller", _make_display, 8),
            # Order 9: Person detection pipeline
            ComponentInfo(
                "person_detector", _make_detector, 9, requires_connection=False
            ),
            # Order 10: Background presence detection (camera → YOLO → face)
            ComponentInfo(
                "presence_loop", _make_presence_loop, 10,
                requires_connection=False,
            ),
            # Order 11: Audio client (mic stream)
            ComponentInfo("audio_client", _make_audio, 11),
            # Order 12: Wake word detector
            ComponentInfo(
                "wake_word", _make_wake_word, 12, requires_connection=False
            ),
            # Order 13: Speech output (TTS)
            ComponentInfo("speech_output", _make_speech_output, 13),
            # Order 14: Echo suppressor
            ComponentInfo(
                "echo_suppressor", _make_echo_suppressor, 14,
                requires_connection=False,
            ),
            # Order 15: Voice bridge (wake word → STT → agent → TTS)
            ComponentInfo("voice_bridge", _make_voice_bridge, 15),
            # Order 16: Follow planner (idle, waiting for trigger)
            ComponentInfo("follow_planner", _make_follow_planner, 16),
            # Order 17: Companion system (presence tracking + OpenClaw signals)
            ComponentInfo(
                "companion", _make_companion, 17, requires_connection=False,
            ),
        ]

        # Sort by start_order
        self._components.sort(key=lambda c: c.start_order)

    def _start_components(self) -> None:
        """Start all registered components in order."""
        for comp in self._components:
            self._start_component(comp)

    def _start_component(self, comp: ComponentInfo) -> bool:
        """Start a single component.  Returns True on success."""
        try:
            logger.info("Starting component: %s", comp.name)
            comp.instance = comp.factory()
            if hasattr(comp.instance, "start"):
                comp.instance.start()
            comp._started = True
            logger.info("Started component: %s", comp.name)
            return True
        except Exception:
            logger.exception("Failed to start component: %s", comp.name)
            if comp.is_critical:
                raise
            return False

    def _stop_component(self, comp: ComponentInfo) -> None:
        """Stop a single component."""
        if not comp._started or comp.instance is None:
            return
        try:
            logger.info("Stopping component: %s", comp.name)
            if hasattr(comp.instance, "stop"):
                comp.instance.stop()
            comp._started = False
            comp.instance = None
            logger.info("Stopped component: %s", comp.name)
        except Exception:
            logger.exception("Error stopping component: %s", comp.name)
            comp._started = False
            comp.instance = None

    def _get_component(self, name: str) -> Any:
        """Get a started component instance by name."""
        for comp in self._components:
            if comp.name == name:
                return comp.instance
        return None

    def _pause_connected_components(self) -> None:
        """Stop components that require a live Vector connection."""
        for comp in reversed(self._components):
            if comp.requires_connection and comp._started:
                self._stop_component(comp)

    def _resume_connected_components(self) -> None:
        """Re-create and start connection-dependent components with new robot."""
        # Preserve non-connection component state
        preserved: dict[str, tuple[Any, bool]] = {}
        for comp in self._components:
            if not comp.requires_connection:
                preserved[comp.name] = (comp.instance, comp._started)

        # Re-register to get fresh factories with new robot reference
        self._register_components()

        # Restore non-connection components
        for comp in self._components:
            if comp.name in preserved:
                comp.instance, comp._started = preserved[comp.name]

        # Start connection-dependent components
        for comp in self._components:
            if comp.requires_connection and not comp._started:
                self._start_component(comp)

    # -- Health monitoring --------------------------------------------------

    def _start_health_monitor(self) -> None:
        """Start background thread that checks component health."""
        self._health_thread = threading.Thread(
            target=self._health_loop, name="supervisor-health", daemon=True
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        """Periodically check component health and restart crashed ones."""
        while not self._shutdown_event.wait(timeout=HEALTH_CHECK_INTERVAL_S):
            if self._state != SupervisorState.RUNNING:
                continue
            self._check_component_health()

    def _check_component_health(self) -> None:
        """Check each component and restart if crashed."""
        for comp in self._components:
            if not comp._started:
                continue

            alive = True
            instance = comp.instance
            if instance is None:
                alive = False
            elif hasattr(instance, "is_alive"):
                alive = instance.is_alive()
            elif hasattr(instance, "_thread") and isinstance(
                getattr(instance, "_thread", None), threading.Thread
            ):
                alive = instance._thread.is_alive()

            if not alive:
                logger.warning(
                    "Component %s appears dead — restarting", comp.name
                )
                self._stop_component(comp)
                self._start_component(comp)

    # -- Battery monitoring -------------------------------------------------

    def _start_battery_monitor(self) -> None:
        """Start background thread that monitors battery level."""
        self._battery_thread = threading.Thread(
            target=self._battery_loop, name="supervisor-battery", daemon=True
        )
        self._battery_thread.start()

    def _battery_loop(self) -> None:
        """Periodically check battery and adjust functionality."""
        while not self._shutdown_event.wait(timeout=BATTERY_CHECK_INTERVAL_S):
            if self._state != SupervisorState.RUNNING:
                continue
            self._check_battery()

    def _check_battery(self) -> None:
        """Read battery state and reduce functionality if low."""
        if self._robot is None:
            return
        try:
            batt = self._robot.get_battery_state()
            level = batt.battery_level
            was_low = self._low_battery

            if level <= BATTERY_LOW_LEVEL and not was_low:
                logger.warning(
                    "Battery LOW (level=%d, %.2fV) — reducing functionality",
                    level,
                    batt.battery_volts,
                )
                self._low_battery = True
                self._enter_low_battery_mode()
            elif level > BATTERY_LOW_LEVEL and was_low:
                logger.info(
                    "Battery OK (level=%d, %.2fV) — restoring full functionality",
                    level,
                    batt.battery_volts,
                )
                self._low_battery = False
                self._exit_low_battery_mode()
        except Exception:
            logger.exception("Error checking battery")

    def _enter_low_battery_mode(self) -> None:
        """Reduce functionality to conserve battery."""
        # Stop non-essential components: detection, follow planner, voice
        for name in ("follow_planner", "presence_loop", "person_detector", "voice_bridge"):
            comp = self._find_component(name)
            if comp and comp._started:
                logger.info("Low battery — stopping %s", name)
                self._stop_component(comp)

        # Set LED to low-battery indicator
        led = self._get_component("led_controller")
        if led and hasattr(led, "set_state"):
            led.set_state("low_battery")

    def _exit_low_battery_mode(self) -> None:
        """Restore full functionality after battery recovers."""
        for name in ("person_detector", "presence_loop", "voice_bridge", "follow_planner"):
            comp = self._find_component(name)
            if comp and not comp._started:
                logger.info("Battery OK — restarting %s", name)
                self._start_component(comp)

        led = self._get_component("led_controller")
        if led and hasattr(led, "set_state"):
            led.set_state("idle")

    def _find_component(self, name: str) -> ComponentInfo | None:
        """Find a ComponentInfo by name."""
        for comp in self._components:
            if comp.name == name:
                return comp
        return None

    # -- Event handlers -----------------------------------------------------

    def _on_emergency_stop(self, event: Any) -> None:
        """Handle emergency stop events — trigger reconnect on connection loss."""
        if not isinstance(event, EmergencyStopEvent):
            return
        if event.source != "connection_lost":
            return
        if self._state in (
            SupervisorState.RECONNECTING,
            SupervisorState.SHUTTING_DOWN,
            SupervisorState.STOPPED,
        ):
            return

        logger.warning("Connection lost to Vector — starting reconnect")
        # Run reconnect in a separate thread to avoid blocking event bus
        self._reconnect_thread = threading.Thread(
            target=self._reconnect, name="supervisor-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    # -- Shutdown -----------------------------------------------------------

    def _shutdown(self) -> None:
        """Graceful shutdown — reverse component order."""
        if self._state == SupervisorState.STOPPED:
            return
        self._set_state(SupervisorState.SHUTTING_DOWN)
        logger.info("Shutting down Vector supervisor...")

        # 1. Stop components in reverse order
        for comp in reversed(self._components):
            self._stop_component(comp)

        # 2. Stow lift (if robot still connected)
        if self._robot:
            try:
                from anki_vector.util import degrees

                self._robot.behavior.set_lift_height(0.0)
                self._robot.behavior.set_head_angle(degrees(0))
                logger.info("Lift stowed, head centered")
            except Exception:
                logger.exception("Error stowing lift/head during shutdown")

        # 3. Disconnect SDK bridge + robot
        self._nuc_bus.off(EMERGENCY_STOP, self._on_emergency_stop)
        self._disconnect_robot()

        # 4. Clear event bus
        self._nuc_bus.clear()

        self._set_state(SupervisorState.STOPPED)
        logger.info("Vector supervisor stopped")

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        """Handle SIGTERM/SIGINT — trigger graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — initiating shutdown", sig_name)
        self._shutdown_event.set()

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers."""
        self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
        self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)

    # -- State management ---------------------------------------------------

    def _set_state(self, new_state: SupervisorState) -> None:
        """Thread-safe state transition."""
        with self._state_lock:
            old = self._state
            self._state = new_state
        logger.info("Supervisor state: %s → %s", old.name, new_state.name)

    @property
    def state(self) -> SupervisorState:
        with self._state_lock:
            return self._state

    @property
    def nuc_bus(self) -> NucEventBus:
        return self._nuc_bus

    @property
    def robot(self) -> Any | None:
        return self._robot

    @property
    def components(self) -> list[ComponentInfo]:
        return list(self._components)


# ---------------------------------------------------------------------------
# sd_notify helper
# ---------------------------------------------------------------------------


def _sd_notify_ready() -> None:
    """Send READY=1 to systemd via sd_notify socket.

    If NOTIFY_SOCKET is not set (not running under systemd), this is a no-op.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        logger.debug("NOTIFY_SOCKET not set — skipping sd_notify")
        return

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            if sock_path.startswith("@"):
                sock_path = "\0" + sock_path[1:]
            sock.sendto(b"READY=1", sock_path)
            logger.info("Sent sd_notify READY=1")
        finally:
            sock.close()
    except Exception:
        logger.exception("Failed to send sd_notify")
