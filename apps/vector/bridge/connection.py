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
        self._media_service: Any | None = None
        self._nuc_bus: Any | None = None
        self._follow_pipeline: Any | None = None
        self._imu_poller: Any | None = None
        self._imu_fusion: Any | None = None
        self._visual_slam: Any | None = None
        self._map_store: Any | None = None
        self._waypoint_mgr: Any | None = None
        self._nav_controller: Any | None = None
        self._intercom: Any | None = None
        self._explorer: Any | None = None
        self._auto_charger: Any | None = None
        self._home_guardian: Any | None = None
        self._obstacle_map: Any | None = None
        self._vision_checker: Any | None = None
        self._floor_proximity: Any | None = None
        self._control_manager: Any | None = None

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
    def nav_controller(self) -> Any:
        """NavController instance, or None if not initialised."""
        return self._nav_controller

    @property
    def explorer(self) -> Any:
        """AutonomousExplorer instance, or None if not initialised."""
        return self._explorer

    @property
    def auto_charger(self) -> Any:
        """AutoCharger instance, or None if not initialised."""
        return self._auto_charger

    @property
    def control_manager(self) -> Any:
        """ControlManager singleton."""
        return self._control_manager

    @property
    def obstacle_map(self) -> Any:
        """ObstacleMap instance, or None if not initialised."""
        return self._obstacle_map

    @property
    def home_guardian(self) -> Any:
        """HomeGuardian instance, or None if not initialised."""
        return self._home_guardian

    @property
    def intercom(self) -> Any:
        """Intercom instance, or None if not initialised."""
        return self._intercom

    @property
    def media_service(self) -> Any:
        """MediaService instance, or None if not initialised."""
        return self._media_service

    @property
    def livekit_bridge(self) -> Any:
        """LiveKitBridge instance, or None if not initialised."""
        return self._livekit_bridge

    def request_override_control(self) -> None:
        """Temporarily escalate to OVERRIDE_BEHAVIORS_PRIORITY.

        Call this before commands that must interrupt Vector's internal
        behaviors (e.g. explore, patrol, follow).  Always call
        ``release_override_control()`` when done — Vector's firmware
        defaults to Wait, so releasing returns to idle automatically.
        """
        if self._control_manager is not None:
            self._control_manager.acquire("bridge")
        else:
            logger.warning("No ControlManager — cannot request override control")

    def release_override_control(self) -> None:
        """Release override priority — Vector returns to firmware Wait state."""
        if self._control_manager is not None:
            self._control_manager.release("bridge")

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
        self._robot = anki_vector.Robot(
            serial=self._serial,
            default_logging=False,
            behavior_control_level=None,  # no control — firmware defaults to Wait
            cache_animation_lists=False,
        )
        self._robot.connect()

        # Centralized control manager — all services use this instead of
        # calling robot.conn.request_control() directly
        from apps.vector.src.control_manager import ControlManager, set_control_manager
        self._control_manager = ControlManager(self._robot)
        set_control_manager(self._control_manager)

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
        # On-demand media service (all 4 channels)
        from apps.vector.src.media.service import MediaService
        self._media_service = MediaService(
            camera_client=self._camera_client,
            robot=self._robot,
        )

        self._livekit_bridge = LiveKitBridge(
            camera_client=self._camera_client,
            audio_client=self._audio_client,
            robot=self._robot,
            event_bus=self._nuc_bus,
            media_service=self._media_service,
            control_manager=self._control_manager,
        )

        from apps.vector.bridge.follow_pipeline import FollowPipeline

        self._follow_pipeline = FollowPipeline(
            camera_client=self._camera_client,
            motor_controller=self._motor_controller,
            head_controller=self._head_controller,
            nuc_bus=self._nuc_bus,
            robot=self._robot,
            obstacle_map=self._obstacle_map,
            floor_proximity=self._floor_proximity,
            control_manager=self._control_manager,
        )

        # Navigation system
        from apps.vector.src.planner.imu_fusion import ImuFusion, ImuPoller
        from apps.vector.src.planner.map_store import MapStore
        from apps.vector.src.planner.visual_slam import VisualSLAM
        from apps.vector.src.planner.waypoint_manager import WaypointManager
        from apps.vector.src.planner.nav_controller import NavController

        self._imu_poller = ImuPoller(self._robot, self._nuc_bus)
        self._imu_fusion = ImuFusion(self._nuc_bus)
        self._visual_slam = VisualSLAM(self._nuc_bus)
        self._map_store = MapStore()
        self._waypoint_mgr = WaypointManager(self._map_store, map_name="home")
        self._nav_controller = NavController(
            slam=self._visual_slam,
            motor=self._motor_controller,
            head=self._head_controller,
            nuc_bus=self._nuc_bus,
            map_store=self._map_store,
            waypoint_mgr=self._waypoint_mgr,
            obstacle_map=self._obstacle_map,
            control_manager=self._control_manager,
        )

        # Shared obstacle map (fuses all detection tiers)
        from apps.vector.src.planner.obstacle_map import ObstacleMap
        self._obstacle_map = ObstacleMap(nuc_bus=self._nuc_bus)
        self._obstacle_map.start()

        # Floor proximity detector (Tier 1)
        from apps.vector.src.detector.floor_proximity import FloorProximityDetector
        self._floor_proximity = FloorProximityDetector()

        # Async vision obstacle checker (Tier 3)
        from apps.vector.src.detector.vision_obstacle_checker import VisionObstacleChecker
        self._vision_checker = VisionObstacleChecker(
            camera_client=self._camera_client,
            obstacle_map=self._obstacle_map,
        )

        # Intercom for Signal messaging
        from apps.vector.src.intercom import Intercom
        self._intercom = Intercom(event_bus=self._nuc_bus)

        # Autonomous explorer
        from apps.vector.src.planner.exploration import AutonomousExplorer, AutoCharger
        self._explorer = AutonomousExplorer(
            slam=self._visual_slam,
            motor=self._motor_controller,
            head=self._head_controller,
            camera=self._camera_client,
            nuc_bus=self._nuc_bus,
            nav_controller=self._nav_controller,
            intercom=self._intercom,
            robot=self._robot,
            imu_poller=self._imu_poller,
            imu_fusion=self._imu_fusion,
            obstacle_map=self._obstacle_map,
            vision_checker=self._vision_checker,
            floor_proximity=self._floor_proximity,
            control_manager=self._control_manager,
        )

        # Home Guardian (patrol & security system)
        from apps.vector.src.planner.patrol import HomeGuardian
        self._home_guardian = HomeGuardian(
            nav_controller=self._nav_controller,
            motor=self._motor_controller,
            head=self._head_controller,
            camera=self._camera_client,
            nuc_bus=self._nuc_bus,
            intercom=self._intercom,
            robot=self._robot,
        )

        # Auto-charger (starts monitoring immediately)
        self._auto_charger = AutoCharger(
            robot=self._robot,
            nav_controller=self._nav_controller,
            nuc_bus=self._nuc_bus,
            intercom=self._intercom,
            control_manager=self._control_manager,
        )
        # Wire explorer ↔ charger so charger can pause/resume exploration
        self._auto_charger.explorer = self._explorer

        # Touch-to-sit: touching Vector's head stops everything and goes quiet
        from apps.vector.src.events.event_types import TOUCH_DETECTED
        self._nuc_bus.on(TOUCH_DETECTED, self._on_touch_sit)

        self._connected = True
        logger.info("Connected to Vector successfully (firmware defaults to Wait)")

    def start_monitor(self) -> None:
        """Start the connection monitor — connects with retry in background."""
        import threading
        import time as _time
        def _monitor():
            delay = 5.0
            while not self._connected:
                try:
                    self.connect()
                except Exception:
                    logger.warning("Connection failed — retrying in %.0fs", delay)
                    _time.sleep(delay)
                    delay = min(delay * 1.5, 30.0)
        self._monitor_thread = threading.Thread(
            target=_monitor, name="conn-monitor", daemon=True
        )
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        """Stop the connection monitor."""
        if hasattr(self, '_monitor_thread') and self._monitor_thread:
            self._monitor_thread.join(timeout=10.0)
            self._monitor_thread = None

    @property
    def mode(self) -> str:
        """Current behavior mode (quiet/playful)."""
        return getattr(self, '_mode', 'quiet')

    @property
    def playful_remaining(self) -> float:
        """Seconds remaining in playful mode, or 0 if quiet."""
        timer = getattr(self, '_playful_timer', None)
        if timer is None or self.mode != 'playful':
            return 0.0
        import time
        remaining = getattr(self, '_playful_end', 0) - time.monotonic()
        return max(0.0, remaining)

    def _on_touch_sit(self, event: Any) -> None:
        """Touch Vector's head → stop everything, go to quiet/sit mode.

        Stops exploration, follow, patrol, releases control, and sends
        quiet intent so Vector sits still.
        """
        import time as _time

        # Debounce — ignore if last touch was <3s ago
        now = _time.monotonic()
        last = getattr(self, '_last_touch_time', 0.0)
        if now - last < 3.0:
            return
        self._last_touch_time = now

        logger.info("Touch detected — stopping all activities, going to sit mode")

        # Stop active systems
        try:
            if self._explorer and self._explorer.state.name != "IDLE":
                self._explorer.stop()
                logger.info("Stopped exploration via touch")
        except Exception:
            pass
        try:
            if self._follow_pipeline and self._follow_pipeline.is_active:
                self._follow_pipeline.stop()
                logger.info("Stopped follow via touch")
        except Exception:
            pass
        try:
            if self._home_guardian and self._home_guardian.is_running:
                self._home_guardian.stop()
                logger.info("Stopped patrol via touch")
        except Exception:
            pass

        # Release control and go quiet
        if self._control_manager:
            self._control_manager.force_release()
        self.set_mode("quiet")

        # Say something so the user knows it worked
        try:
            self._robot.behavior.say_text("OK")
        except Exception:
            pass

    def set_mode(self, mode: str, duration_s: float = 480.0) -> None:
        """Set behavior mode.

        quiet: Release any override control — Vector returns to firmware Wait.
        playful: Request override control for *duration_s* seconds (default 8 min),
                 then auto-revert to quiet.
        """
        import threading
        import time

        # Cancel any existing playful timer
        old_timer = getattr(self, '_playful_timer', None)
        if old_timer is not None:
            old_timer.cancel()
            self._playful_timer = None

        self._mode = mode

        if mode == 'quiet':
            self.release_override_control()
            logger.info("Mode → quiet (firmware Wait)")
        elif mode == 'playful':
            self.request_override_control()
            self._playful_end = time.monotonic() + duration_s

            def _auto_quiet():
                logger.info("Playful mode expired (%.0fs) → quiet", duration_s)
                self._mode = 'quiet'
                self._playful_timer = None
                self.release_override_control()

            self._playful_timer = threading.Timer(duration_s, _auto_quiet)
            self._playful_timer.daemon = True
            self._playful_timer.start()
            logger.info("Mode → playful (%.0fs timer)", duration_s)

    def disconnect(self) -> None:
        """Disconnect from Vector and stop all controllers."""
        if not self._connected:
            return

        # Cancel playful timer if running
        timer = getattr(self, '_playful_timer', None)
        if timer is not None:
            timer.cancel()
            self._playful_timer = None

        logger.info("Disconnecting from Vector...")
        try:
            if self._home_guardian:
                self._home_guardian.stop()
            if self._auto_charger:
                self._auto_charger.stop()
            if self._explorer:
                self._explorer.stop()
            if self._nav_controller:
                self._nav_controller.stop()
            if self._imu_fusion:
                self._imu_fusion.stop()
            if self._imu_poller:
                self._imu_poller.stop()
            if self._vision_checker:
                self._vision_checker.stop()
            if self._obstacle_map:
                self._obstacle_map.stop()
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
        if self._media_service:
            self._media_service.stop()
        self._media_service = None
        self._follow_pipeline = None
        self._home_guardian = None
        self._auto_charger = None
        self._explorer = None
        self._intercom = None
        self._nav_controller = None
        self._waypoint_mgr = None
        self._map_store = None
        self._visual_slam = None
        self._imu_fusion = None
        self._imu_poller = None
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
