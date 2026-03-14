"""Vector SDK connection manager — singleton lifecycle for the bridge server.

Lazily connects to Vector on first use, provides the robot instance and
all controller objects.  Handles disconnect and reconnect.

Usage::

    mgr = ConnectionManager()
    mgr.connect()              # call once at startup
    robot = mgr.robot           # anki_vector.Robot
    mgr.motor_controller        # MotorController (cliff-safe)
    mgr.head_controller         # HeadController
    ...
    mgr.disconnect()
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

SERIAL = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")


class ConnectionManager:
    """Manages the Vector SDK connection and controller instances."""

    def __init__(self, serial: str = SERIAL) -> None:
        self._serial = serial
        self._robot: Any | None = None
        self._connected = False
        self._mode: str = "quiet"  # "quiet" or "playful"
        self._control_watchdog: threading.Thread | None = None
        self._control_watchdog_stop = threading.Event()

        # Controllers — created on connect
        self._motor_controller: Any | None = None
        self._head_controller: Any | None = None
        self._lift_controller: Any | None = None
        self._led_controller: Any | None = None
        self._display_controller: Any | None = None
        self._camera_client: Any | None = None
        self._audio_client: Any | None = None
        self._livekit_bridge: Any | None = None
        self._media_service: Any | None = None
        self._nuc_bus: Any | None = None
        self._follow_pipeline: Any | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def robot(self) -> Any:
        if not self._connected or self._robot is None:
            raise ConnectionError("Not connected to Vector")
        return self._robot

    @property
    def motor_controller(self) -> Any:
        if self._motor_controller is None:
            raise ConnectionError("Not connected to Vector")
        return self._motor_controller

    @property
    def head_controller(self) -> Any:
        if self._head_controller is None:
            raise ConnectionError("Not connected to Vector")
        return self._head_controller

    @property
    def lift_controller(self) -> Any:
        if self._lift_controller is None:
            raise ConnectionError("Not connected to Vector")
        return self._lift_controller

    @property
    def led_controller(self) -> Any:
        if self._led_controller is None:
            raise ConnectionError("Not connected to Vector")
        return self._led_controller

    @property
    def display_controller(self) -> Any:
        if self._display_controller is None:
            raise ConnectionError("Not connected to Vector")
        return self._display_controller

    @property
    def camera_client(self) -> Any:
        if self._camera_client is None:
            raise ConnectionError("Not connected to Vector")
        return self._camera_client

    @property
    def audio_client(self) -> Any:
        if self._audio_client is None:
            raise ConnectionError("Not connected to Vector")
        return self._audio_client

    @property
    def media_service(self) -> Any:
        """MediaService instance, or None if not initialised."""
        return self._media_service

    @property
    def follow_pipeline(self) -> Any:
        """FollowPipeline instance, or None if not initialised."""
        return self._follow_pipeline

    @property
    def livekit_bridge(self) -> Any:
        """LiveKitBridge instance, or None if not initialised."""
        return self._livekit_bridge

    def connect(self) -> None:
        """Connect to Vector and initialise all controllers."""
        if self._connected:
            logger.warning("Already connected to Vector")
            return

        import anki_vector

        from apps.vector.src.camera.camera_client import CameraClient
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.head_controller import HeadController
        from apps.vector.src.led_controller import LedController
        from apps.vector.src.livekit_bridge import LiveKitBridge
        from apps.vector.src.lift_controller import LiftController
        from apps.vector.src.media.service import MediaService
        from apps.vector.src.motor_controller import MotorController
        from apps.vector.src.voice.audio_client import AudioClient

        from anki_vector.connection import ControlPriorityLevel

        logger.info("Connecting to Vector (serial=%s) with OVERRIDE_BEHAVIORS...", self._serial)
        self._robot = anki_vector.Robot(
            serial=self._serial,
            default_logging=False,
            behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
        )
        self._robot.connect()

        self._nuc_bus = NucEventBus()
        self._motor_controller = MotorController(self._robot, self._nuc_bus)
        self._motor_controller.start()
        self._head_controller = HeadController(self._robot)
        self._lift_controller = LiftController(self._robot, self._nuc_bus)
        self._lift_controller.start()
        self._led_controller = LedController(self._robot, self._nuc_bus)
        self._led_controller.start()
        # NOTE: DisplayController disabled — it uses DisplayFaceImageRGB which
        # crashes vic-anim due to race condition (issue #129).
        # self._display_controller = DisplayController(self._robot, event_bus=self._nuc_bus)
        # self._display_controller.start()
        self._camera_client = CameraClient(self._robot)
        self._camera_client.start()
        self._audio_client = AudioClient(self._robot)
        # NOTE: AudioFeed NOT started -- SDK only provides signal_power
        # (980Hz calibration tone), not real mic PCM.  The stall-reconnect
        # loop starves the camera feed of SDK event-loop time.
        # self._audio_client.start()

        # Start MediaService (vector-streamer mic channel)
        self._media_service = MediaService()
        self._media_service.start()

        self._livekit_bridge = LiveKitBridge(
            camera_client=self._camera_client,
            audio_client=self._audio_client,
            robot=self._robot,
            event_bus=self._nuc_bus,
            media_service=self._media_service,
        )

        from apps.vector.bridge.follow_pipeline import FollowPipeline

        self._follow_pipeline = FollowPipeline(
            camera_client=self._camera_client,
            motor_controller=self._motor_controller,
            head_controller=self._head_controller,
            nuc_bus=self._nuc_bus,
            robot=self._robot,
        )

        self._connected = True

        # Start behavior control watchdog — re-requests OVERRIDE_BEHAVIORS
        # whenever the SDK signals control_lost while we're in quiet mode.
        self._control_watchdog_stop.clear()
        self._control_watchdog = threading.Thread(
            target=self._behavior_control_watchdog,
            daemon=True,
            name="behavior-control-watchdog",
        )
        self._control_watchdog.start()

        logger.info("Connected to Vector successfully")

    def _behavior_control_watchdog(self) -> None:
        """Background thread: re-request OVERRIDE_BEHAVIORS when control is lost.

        The SDK's BehaviorControl gRPC stream can lose control if another client
        connects or if the stream hiccups. This watchdog waits for the
        control_lost_event and immediately re-requests control when in quiet mode.
        """
        while not self._control_watchdog_stop.is_set():
            try:
                if not self._connected or self._robot is None:
                    self._control_watchdog_stop.wait(5)
                    continue
                # Block until control is lost (or stop is signaled)
                lost_event = self._robot.conn.control_lost_event
                # Poll with timeout so we can check stop flag
                while not self._control_watchdog_stop.is_set():
                    if lost_event.is_set():
                        break
                    self._control_watchdog_stop.wait(1)
                if self._control_watchdog_stop.is_set():
                    break
                if self._mode == "quiet":
                    logger.warning("Behavior control lost — re-requesting OVERRIDE_BEHAVIORS")
                    from anki_vector.connection import ControlPriorityLevel
                    try:
                        self._robot.conn.request_control(
                            behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                        )
                        logger.info("Behavior control re-acquired")
                    except Exception:
                        logger.exception("Failed to re-request behavior control")
                else:
                    logger.debug("Control lost in playful mode — not re-requesting")
            except Exception:
                logger.exception("Behavior control watchdog error")
                self._control_watchdog_stop.wait(5)

    def disconnect(self) -> None:
        """Disconnect from Vector and stop all controllers."""
        if not self._connected:
            return

        logger.info("Disconnecting from Vector...")

        # Stop behavior control watchdog
        self._control_watchdog_stop.set()
        if self._control_watchdog and self._control_watchdog.is_alive():
            self._control_watchdog.join(timeout=3)
        self._control_watchdog = None

        try:
            if self._follow_pipeline:
                self._follow_pipeline.stop()
            if self._motor_controller:
                self._motor_controller.stop()
            if self._lift_controller:
                self._lift_controller.stop()
            if self._led_controller:
                self._led_controller.stop()
            if self._display_controller:
                self._display_controller.stop()
            if self._camera_client:
                self._camera_client.stop()
            if self._audio_client:
                self._audio_client.stop()
            if self._media_service:
                self._media_service.stop()
        except Exception:
            logger.exception("Error stopping controllers")

        try:
            if self._robot:
                self._robot.disconnect()
        except Exception:
            logger.exception("Error disconnecting from Vector")

        self._connected = False
        self._robot = None
        self._motor_controller = None
        self._head_controller = None
        self._lift_controller = None
        self._led_controller = None
        self._display_controller = None
        self._camera_client = None
        self._audio_client = None
        self._livekit_bridge = None
        self._media_service = None
        self._follow_pipeline = None
        self._nuc_bus = None
        logger.info("Disconnected from Vector")

    @property
    def mode(self) -> str:
        """Current behavior mode: 'quiet' or 'playful'."""
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Switch between 'quiet' (still) and 'playful' (autonomous behaviors).

        In quiet mode, the bridge holds OVERRIDE_BEHAVIORS_PRIORITY so Vector
        stays still. In playful mode, control is released and vic-engine runs
        its behavior tree (exploring, looking around, reacting).
        """
        if mode not in ("quiet", "playful"):
            raise ValueError(f"Unknown mode: {mode!r} (expected 'quiet' or 'playful')")
        if not self._connected or self._robot is None:
            raise ConnectionError("Not connected to Vector")

        from anki_vector.connection import ControlPriorityLevel

        if mode == "quiet":
            logger.info("Switching to quiet mode (OVERRIDE_BEHAVIORS)")
            self._robot.conn.request_control(
                behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
            )
            self._robot.motors.set_wheel_motors(0, 0)
        else:  # playful
            logger.info("Switching to playful mode (releasing control)")
            self._robot.conn.release_control()

        self._mode = mode

    @staticmethod
    def _voltage_to_percent(volts: float) -> int:
        """Estimate battery percentage from LiPo voltage (3.4V–4.2V)."""
        pct = (volts - 3.4) / (4.2 - 3.4) * 100
        return max(0, min(100, int(round(pct))))

    def get_battery_state(self) -> dict:
        """Read battery state and return as dict."""
        batt = self.robot.get_battery_state()
        volts = round(batt.battery_volts, 2)
        return {
            "voltage": volts,
            "percent": self._voltage_to_percent(volts),
            "level": batt.battery_level,
            "is_charging": batt.is_charging,
            "is_on_charger": batt.is_on_charger_platform,
        }

    def get_robot_state(self) -> dict:
        """Read robot state (sensors, accel, gyro) and return as dict."""
        robot = self.robot
        accel = robot.accel
        gyro = robot.gyro
        touch = robot.touch.last_sensor_reading
        return {
            "accel": {"x": round(accel.x, 2), "y": round(accel.y, 2), "z": round(accel.z, 2)},
            "gyro": {"x": round(gyro.x, 2), "y": round(gyro.y, 2), "z": round(gyro.z, 2)},
            "touch": bool(touch) if touch is not None else None,
            "head_angle_deg": round(robot.head_angle_rad * 57.2958, 1) if hasattr(robot, "head_angle_rad") else None,
            "lift_height_mm": round(robot.lift_height_mm, 1) if hasattr(robot, "lift_height_mm") else None,
        }
