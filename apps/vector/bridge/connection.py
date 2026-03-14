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
from typing import Any

logger = logging.getLogger(__name__)

SERIAL = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")


class ConnectionManager:
    """Manages the Vector SDK connection and controller instances."""

    def __init__(self, serial: str = SERIAL) -> None:
        self._serial = serial
        self._robot: Any | None = None
        self._connected = False

        # Controllers — created on connect
        self._motor_controller: Any | None = None
        self._head_controller: Any | None = None
        self._lift_controller: Any | None = None
        self._led_controller: Any | None = None
        self._display_controller: Any | None = None
        self._camera_client: Any | None = None
        self._audio_client: Any | None = None
        self._livekit_bridge: Any | None = None
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
        from apps.vector.src.motor_controller import MotorController
        from apps.vector.src.voice.audio_client import AudioClient

        logger.info("Connecting to Vector (serial=%s)...", self._serial)
        self._robot = anki_vector.Robot(serial=self._serial, default_logging=False)
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
        # NOTE: AudioFeed NOT started — SDK only provides signal_power
        # (980Hz calibration tone), not real mic PCM.  The stall-reconnect
        # loop starves the camera feed of SDK event-loop time.
        # self._audio_client.start()
        self._livekit_bridge = LiveKitBridge(
            camera_client=self._camera_client,
            audio_client=self._audio_client,
            robot=self._robot,
            event_bus=self._nuc_bus,
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
        logger.info("Connected to Vector successfully")

        # Send quiet intent so Vector sits still by default (wake word still active)
        self._send_quiet_intent()

    def _send_quiet_intent(self) -> None:
        """Send imperative_quiet intent via wire-pod to keep Vector still."""
        import urllib.request
        import urllib.parse

        try:
            url = "http://localhost:8080/api-sdk/cloud_intent?" + urllib.parse.urlencode({
                "serial": self._serial,
                "intent": "intent_imperative_quiet",
            })
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            logger.info("Sent quiet intent via wire-pod")
        except Exception:
            logger.warning("Failed to send quiet intent via wire-pod", exc_info=True)

    def disconnect(self) -> None:
        """Disconnect from Vector and stop all controllers."""
        if not self._connected:
            return

        logger.info("Disconnecting from Vector...")
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
        self._follow_pipeline = None
        self._nuc_bus = None
        logger.info("Disconnected from Vector")

    def get_battery_state(self) -> dict:
        """Read battery state and return as dict."""
        batt = self.robot.get_battery_state()
        return {
            "voltage": round(batt.battery_volts, 2),
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
