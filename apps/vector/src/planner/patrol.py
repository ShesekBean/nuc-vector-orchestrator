"""Smart Patrol & Home Guardian — autonomous waypoint patrol with detection.

Ties together navigation, person detection, face recognition, scene
description (Claude Vision), Signal alerts, and voice narration into a
cohesive autonomous behavior.

Modes:
    **patrol** — cycle through saved waypoints in a loop, scanning at each.
    **sentry** — park at one spot and continuously monitor.

At each waypoint (or continuously in sentry mode):
1. Look around (head sweep)
2. Run person detection (YOLO + Kalman tracker)
3. If person found → face recognition (known vs unknown)
4. Unknown person → Signal alert with photo + scene description
5. Known person → log but don't alert (configurable)
6. Voice narration throughout ("Checking the kitchen... all clear!")
7. Activity log accessible via API

Usage::

    guardian = HomeGuardian(
        nav, motor, head, camera, nuc_bus, intercom, robot,
    )
    guardian.start(mode="patrol")   # begin patrol loop
    guardian.start(mode="sentry")   # sentry at current position
    guardian.stop()
    guardian.get_activity_log()     # list of events
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:

    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.intercom import Intercom
    from apps.vector.src.motor_controller import MotorController
    from apps.vector.src.planner.nav_controller import NavController

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PatrolMode(Enum):
    """Patrol operating mode."""

    PATROL = auto()   # cycle through waypoints
    SENTRY = auto()   # park and watch


class PatrolState(Enum):
    """Guardian state machine."""

    IDLE = auto()
    NAVIGATING = auto()
    SCANNING = auto()
    ALERT = auto()
    PAUSED = auto()


@dataclass
class PatrolEvent:
    """A single logged event during patrol."""

    timestamp: float
    event_type: str          # "patrol_start", "waypoint_arrived", "person_detected",
                             # "face_recognized", "unknown_person", "all_clear",
                             # "scene_described", "patrol_complete", "alert_sent"
    waypoint: str = ""       # which waypoint this occurred at
    details: str = ""        # human-readable details
    person_name: str = ""    # face recognition result, if any
    confidence: float = 0.0  # detection confidence


@dataclass
class PatrolConfig:
    """Patrol configuration."""

    # Time to scan at each waypoint (seconds)
    dwell_time_s: float = 8.0

    # Detection scan rate (Hz)
    scan_hz: float = 5.0

    # Head sweep angles during scan (degrees)
    head_angles: tuple[float, ...] = (-15.0, 0.0, 15.0, 0.0)

    # Time to hold each head angle (seconds)
    head_hold_s: float = 2.0

    # Person detection confidence threshold
    person_confidence: float = 0.30

    # Alert cooldown — don't re-alert for same person within this window (seconds)
    alert_cooldown_s: float = 120.0

    # Whether to use Claude Vision for scene descriptions
    scene_description_enabled: bool = True

    # Whether to alert on known faces
    alert_on_known_faces: bool = False

    # Maximum activity log entries
    max_log_entries: int = 500

    # Pause between patrol loops (seconds)
    loop_pause_s: float = 5.0

    # Sentry scan interval (seconds) — how often to re-scan in sentry mode
    sentry_interval_s: float = 15.0

    # Voice narration enabled
    voice_enabled: bool = True


# ---------------------------------------------------------------------------
# Home Guardian
# ---------------------------------------------------------------------------


class HomeGuardian:
    """Autonomous patrol and home security system.

    Args:
        nav_controller: NavController for waypoint navigation.
        motor: MotorController for emergency stops.
        head: HeadController for looking around.
        camera: CameraClient for frames.
        nuc_bus: Event bus.
        intercom: Signal messaging.
        robot: Vector SDK robot for say_text() and face recognition.
        config: Patrol configuration.
    """

    def __init__(
        self,
        nav_controller: NavController,
        motor: MotorController,
        head: HeadController,
        camera: CameraClient,
        nuc_bus: NucEventBus,
        intercom: Intercom,
        robot: Any = None,
        config: PatrolConfig | None = None,
    ) -> None:
        self._nav = nav_controller
        self._motor = motor
        self._head = head
        self._camera = camera
        self._bus = nuc_bus
        self._intercom = intercom
        self._robot = robot
        self._cfg = config or PatrolConfig()

        self._state = PatrolState.IDLE
        self._mode = PatrolMode.PATROL
        self._running = False
        self._paused = False
        self._thread: threading.Thread | None = None

        # Detection subsystems (lazy-initialized)
        self._person_detector: Any = None
        self._kalman_tracker: Any = None
        self._face_detector: Any = None
        self._face_recognizer: Any = None
        self._scene_describer: Any = None

        # Activity log
        self._log: list[PatrolEvent] = []
        self._log_lock = threading.Lock()

        # Alert cooldown tracking: person_name -> last_alert_time
        self._alert_cooldown: dict[str, float] = {}

        # Patrol stats
        self._patrol_count: int = 0
        self._waypoints_visited: int = 0
        self._persons_detected: int = 0
        self._alerts_sent: int = 0
        self._current_waypoint: str = ""
        self._start_time: float = 0.0

    # -- Properties ----------------------------------------------------------

    @property
    def state(self) -> PatrolState:
        return self._state

    @property
    def mode(self) -> PatrolMode:
        return self._mode

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    # -- Lifecycle -----------------------------------------------------------

    def start(self, mode: str = "patrol", waypoints: list[str] | None = None) -> None:
        """Start the home guardian.

        Args:
            mode: "patrol" or "sentry".
            waypoints: Optional list of waypoint names to patrol.
                If None, patrols all saved waypoints.
        """
        if self._running:
            logger.warning("HomeGuardian already running")
            return

        self._mode = PatrolMode.PATROL if mode == "patrol" else PatrolMode.SENTRY
        self._running = True
        self._paused = False
        self._start_time = time.time()

        # Initialize detection subsystems
        self._init_detectors()

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(waypoints,),
            name="home-guardian",
            daemon=True,
        )
        self._thread.start()

        mode_label = self._mode.name.lower()
        self._say(f"Home guardian activated. {mode_label} mode.")
        self._intercom.send_text(
            f"Home Guardian activated in {mode_label} mode."
        )
        self._log_event("patrol_start", details=f"Mode: {mode_label}")
        logger.info("HomeGuardian started in %s mode", mode_label)

    def stop(self) -> None:
        """Stop the guardian and generate a summary."""
        if not self._running:
            return

        self._running = False
        self._state = PatrolState.IDLE

        try:
            self._motor.drive_wheels(0, 0)
        except Exception:
            pass

        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None

        # Generate summary
        duration = time.time() - self._start_time if self._start_time else 0
        summary = (
            f"Home Guardian stopped. "
            f"Ran for {_format_duration(duration)}. "
            f"Patrols: {self._patrol_count}, "
            f"Waypoints visited: {self._waypoints_visited}, "
            f"Persons detected: {self._persons_detected}, "
            f"Alerts sent: {self._alerts_sent}."
        )
        self._intercom.send_text(summary)
        self._say("Home guardian deactivated.")
        self._log_event("patrol_complete", details=summary)
        logger.info("HomeGuardian stopped: %s", summary)

    def pause(self) -> None:
        """Pause patrol (stays in place, stops scanning)."""
        self._paused = True
        self._state = PatrolState.PAUSED
        self._say("Patrol paused.")

    def resume(self) -> None:
        """Resume patrol after pause."""
        self._paused = False
        self._say("Patrol resumed.")

    # -- Status --------------------------------------------------------------

    def get_status(self) -> dict:
        """Return current guardian status."""
        duration = time.time() - self._start_time if self._start_time and self._running else 0
        return {
            "state": self._state.name.lower(),
            "mode": self._mode.name.lower(),
            "running": self._running,
            "paused": self._paused,
            "current_waypoint": self._current_waypoint,
            "patrol_count": self._patrol_count,
            "waypoints_visited": self._waypoints_visited,
            "persons_detected": self._persons_detected,
            "alerts_sent": self._alerts_sent,
            "uptime_s": round(duration, 1),
            "recent_events": self._get_recent_events(5),
        }

    def get_activity_log(self, limit: int = 50) -> list[dict]:
        """Return the activity log as a list of dicts."""
        with self._log_lock:
            entries = self._log[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "time": time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
                "type": e.event_type,
                "waypoint": e.waypoint,
                "details": e.details,
                "person": e.person_name,
                "confidence": round(e.confidence, 2) if e.confidence else None,
            }
            for e in entries
        ]

    # -- Main loops ----------------------------------------------------------

    def _run_loop(self, waypoints: list[str] | None) -> None:
        """Main guardian loop dispatches to patrol or sentry."""
        try:
            if self._mode == PatrolMode.PATROL:
                self._patrol_loop(waypoints)
            else:
                self._sentry_loop()
        except Exception:
            logger.exception("HomeGuardian loop crashed")
            self._intercom.send_text(
                "Home Guardian encountered an error and stopped."
            )
        finally:
            self._running = False
            self._state = PatrolState.IDLE

    def _patrol_loop(self, waypoint_names: list[str] | None) -> None:
        """Cycle through waypoints, scanning at each one."""
        while self._running:
            # Get waypoint list
            wp_list = self._get_patrol_waypoints(waypoint_names)
            if not wp_list:
                self._say("No waypoints to patrol.")
                self._intercom.send_text(
                    "Home Guardian: no waypoints saved to patrol. "
                    "Explore the house first or save waypoints manually."
                )
                break

            self._patrol_count += 1
            self._say(f"Starting patrol round {self._patrol_count}.")
            logger.info("Patrol round %d: %d waypoints", self._patrol_count, len(wp_list))

            for wp_name in wp_list:
                if not self._running:
                    break

                # Wait while paused
                while self._paused and self._running:
                    time.sleep(0.5)

                if not self._running:
                    break

                self._current_waypoint = wp_name
                self._state = PatrolState.NAVIGATING

                # Navigate to waypoint
                self._say(f"Heading to the {wp_name}.")
                logger.info("Navigating to waypoint: %s", wp_name)

                arrived = self._navigate_to(wp_name)
                if not arrived:
                    self._log_event(
                        "navigation_failed",
                        waypoint=wp_name,
                        details=f"Could not reach {wp_name}",
                    )
                    continue

                self._waypoints_visited += 1
                self._log_event("waypoint_arrived", waypoint=wp_name)

                # Scan at this waypoint
                self._state = PatrolState.SCANNING
                self._scan_location(wp_name)

            # Pause between loops
            if self._running:
                self._say("Patrol round complete. Resting briefly.")
                self._sleep_interruptible(self._cfg.loop_pause_s)

    def _sentry_loop(self) -> None:
        """Stay in place and continuously monitor."""
        self._current_waypoint = "sentry_position"
        self._say("Sentry mode. Watching this position.")

        while self._running:
            while self._paused and self._running:
                time.sleep(0.5)

            if not self._running:
                break

            self._state = PatrolState.SCANNING
            self._scan_location("sentry_position")
            self._sleep_interruptible(self._cfg.sentry_interval_s)

    # -- Navigation ----------------------------------------------------------

    def _navigate_to(self, waypoint_name: str) -> bool:
        """Navigate to a waypoint. Returns True if arrived."""
        from apps.vector.src.planner.nav_controller import NavState

        started = self._nav.navigate_to_waypoint(waypoint_name)
        if not started:
            logger.warning("Navigation to '%s' failed to start", waypoint_name)
            return False

        # Wait for navigation to complete (up to 2 minutes)
        deadline = time.monotonic() + 120.0
        while self._running and time.monotonic() < deadline:
            state = self._nav.state
            if state == NavState.ARRIVED:
                return True
            if state in (NavState.IDLE, NavState.BLOCKED):
                return state == NavState.ARRIVED
            time.sleep(0.5)

        return False

    # -- Scanning & Detection ------------------------------------------------

    def _scan_location(self, waypoint_name: str) -> None:
        """Scan current location: head sweep + person detection + face recognition."""
        if self._cfg.voice_enabled:
            self._say(f"Checking the {waypoint_name}.")

        persons_found = []

        # Head sweep with detection at each angle
        for angle in self._cfg.head_angles:
            if not self._running:
                return

            # Move head
            try:
                self._head.set_angle(angle)
            except Exception:
                logger.debug("Head angle set failed")

            # Detection loop at this angle
            angle_end = time.monotonic() + self._cfg.head_hold_s
            while self._running and time.monotonic() < angle_end:
                frame = self._camera.get_latest_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue

                detections = self._detect_persons(frame)
                if detections:
                    for det in detections:
                        persons_found.append((det, frame.copy()))

                time.sleep(1.0 / self._cfg.scan_hz)

        # Reset head to center
        try:
            self._head.set_angle(0.0)
        except Exception:
            pass

        # Process findings
        if persons_found:
            self._handle_person_detections(persons_found, waypoint_name)
        else:
            self._log_event("all_clear", waypoint=waypoint_name, details="No persons detected")
            if self._cfg.voice_enabled:
                self._say(f"{waypoint_name}. All clear.")
            logger.info("Scan complete at '%s': all clear", waypoint_name)

    def _detect_persons(self, frame: Any) -> list[Any]:
        """Run person detection on a frame. Returns confirmed tracks."""
        if self._person_detector is None:
            return []

        try:
            detections = self._person_detector.detect(frame)
            if self._kalman_tracker is not None:
                confirmed = self._kalman_tracker.update(detections)
                return [t for t in confirmed if t.confidence >= self._cfg.person_confidence]
            return [d for d in detections if d.confidence >= self._cfg.person_confidence]
        except Exception:
            logger.debug("Person detection error", exc_info=True)
            return []

    def _handle_person_detections(
        self,
        detections: list[tuple[Any, Any]],
        waypoint_name: str,
    ) -> None:
        """Process person detections: face recognition + alerting."""
        self._state = PatrolState.ALERT
        self._persons_detected += len(detections)

        # Use the best detection (highest confidence)
        best_det, best_frame = max(detections, key=lambda x: x[0].confidence)

        # Try face recognition
        face_name = self._try_face_recognition(best_frame)

        if face_name and face_name != "unknown":
            # Known person
            self._log_event(
                "face_recognized",
                waypoint=waypoint_name,
                person_name=face_name,
                confidence=best_det.confidence,
                details=f"Recognized {face_name} in {waypoint_name}",
            )
            if self._cfg.voice_enabled:
                self._say(f"I see {face_name} in the {waypoint_name}.")

            if self._cfg.alert_on_known_faces:
                self._send_alert(
                    waypoint_name, best_frame, face_name, best_det.confidence
                )
        else:
            # Unknown person — always alert
            self._log_event(
                "unknown_person",
                waypoint=waypoint_name,
                person_name="unknown",
                confidence=best_det.confidence,
                details=f"Unknown person detected in {waypoint_name}",
            )
            if self._cfg.voice_enabled:
                self._say(f"I see someone in the {waypoint_name}!")

            self._send_alert(waypoint_name, best_frame, "unknown", best_det.confidence)

    def _try_face_recognition(self, frame: Any) -> str | None:
        """Try to recognize a face in the frame. Returns name or None."""
        if self._face_detector is None or self._face_recognizer is None:
            return None

        try:
            face_detections = self._face_detector.detect(frame)
            if not face_detections:
                return None

            matches = self._face_recognizer.recognize(frame, face_detections)
            for match in matches:
                if match.name != "unknown":
                    return match.name
            return "unknown"
        except Exception:
            logger.debug("Face recognition error", exc_info=True)
            return None

    def _send_alert(
        self,
        waypoint_name: str,
        frame: Any,
        person_name: str,
        confidence: float,
    ) -> None:
        """Send a Signal alert with photo and optional scene description."""
        # Check cooldown
        now = time.monotonic()
        cooldown_key = f"{waypoint_name}:{person_name}"
        last_alert = self._alert_cooldown.get(cooldown_key, 0)
        if now - last_alert < self._cfg.alert_cooldown_s:
            logger.debug("Alert cooldown active for %s", cooldown_key)
            return

        self._alert_cooldown[cooldown_key] = now
        self._alerts_sent += 1

        # Build alert message
        if person_name == "unknown":
            msg = f"Unknown person detected in {waypoint_name}! (confidence: {confidence:.0%})"
        else:
            msg = f"{person_name} spotted in {waypoint_name} (confidence: {confidence:.0%})"

        # Get scene description if enabled
        scene_desc = ""
        if self._cfg.scene_description_enabled and self._scene_describer is not None:
            scene_desc = self._describe_scene()
            if scene_desc:
                msg += f"\n\nScene: {scene_desc}"

        # Send photo + message via Signal
        try:
            self._intercom.send_photo(msg)
        except Exception:
            logger.exception("Failed to send alert photo")
            try:
                self._intercom.send_text(msg)
            except Exception:
                pass

        self._log_event(
            "alert_sent",
            waypoint=waypoint_name,
            person_name=person_name,
            confidence=confidence,
            details=msg[:200],
        )
        logger.info("Alert sent: %s", msg[:100])

    def _describe_scene(self) -> str:
        """Get a Claude Vision scene description of the current view."""
        if self._scene_describer is None:
            return ""

        try:
            result = self._scene_describer.capture_scene()
            return result.description
        except Exception:
            logger.debug("Scene description failed", exc_info=True)
            return ""

    # -- Detector initialization ---------------------------------------------

    def _init_detectors(self) -> None:
        """Lazy-initialize detection subsystems."""
        # Person detector (YOLO)
        try:
            from apps.vector.src.detector.person_detector import PersonDetector

            self._person_detector = PersonDetector(
                confidence_threshold=self._cfg.person_confidence,
                event_bus=self._bus,
            )
            logger.info("Person detector initialized")
        except Exception:
            logger.warning("Person detector not available", exc_info=True)

        # Kalman tracker
        try:
            from apps.vector.src.detector.kalman_tracker import KalmanTracker

            self._kalman_tracker = KalmanTracker()
            logger.info("Kalman tracker initialized")
        except Exception:
            logger.warning("Kalman tracker not available", exc_info=True)

        # Face detector + recognizer
        try:
            from apps.vector.src.face_recognition.face_detector import FaceDetector
            from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer

            self._face_detector = FaceDetector()
            self._face_recognizer = FaceRecognizer(event_bus=self._bus)
            self._face_recognizer.load_database()
            logger.info("Face recognition initialized")
        except Exception:
            logger.warning("Face recognition not available", exc_info=True)

        # Scene describer (Claude Vision)
        if self._cfg.scene_description_enabled:
            try:
                from apps.vector.src.camera.scene_describer import SceneDescriber

                self._scene_describer = SceneDescriber(
                    camera_client=self._camera,
                    event_bus=self._bus,
                )
                logger.info("Scene describer initialized")
            except Exception:
                logger.warning("Scene describer not available", exc_info=True)

    # -- Helpers -------------------------------------------------------------

    def _say(self, text: str) -> None:
        """Have Vector say something (non-blocking)."""
        if self._robot is None or not self._cfg.voice_enabled:
            return

        def _speak():
            try:
                self._robot.behavior.say_text(text)
            except Exception:
                logger.debug("say_text failed: %s", text)

        threading.Thread(target=_speak, name="guardian-tts", daemon=True).start()

    def _log_event(
        self,
        event_type: str,
        waypoint: str = "",
        details: str = "",
        person_name: str = "",
        confidence: float = 0.0,
    ) -> None:
        """Add an event to the activity log."""
        event = PatrolEvent(
            timestamp=time.time(),
            event_type=event_type,
            waypoint=waypoint or self._current_waypoint,
            details=details,
            person_name=person_name,
            confidence=confidence,
        )
        with self._log_lock:
            self._log.append(event)
            if len(self._log) > self._cfg.max_log_entries:
                self._log = self._log[-self._cfg.max_log_entries:]

        # Emit event on bus
        try:
            from apps.vector.src.events.event_types import (
                PATROL_EVENT,
                PatrolEventPayload,
            )
            self._bus.emit(PATROL_EVENT, PatrolEventPayload(
                event_type=event_type,
                waypoint=waypoint or self._current_waypoint,
                details=details,
                person_name=person_name,
            ))
        except (ImportError, AttributeError):
            pass  # Event type not registered yet

    def _get_recent_events(self, n: int) -> list[dict]:
        """Get the N most recent log events."""
        with self._log_lock:
            entries = self._log[-n:]
        return [
            {
                "time": time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
                "type": e.event_type,
                "waypoint": e.waypoint,
                "details": e.details[:100],
            }
            for e in entries
        ]

    def _get_patrol_waypoints(self, names: list[str] | None) -> list[str]:
        """Get list of waypoint names to patrol."""
        if names:
            return names

        # Get all saved waypoints from nav controller's waypoint manager
        try:
            all_wps = self._nav._waypoint_mgr.list_waypoints()
            # Exclude "charger" from patrol — it's a utility waypoint
            return [wp.name for wp in all_wps if wp.name.lower() != "charger"]
        except Exception:
            return []

    def _sleep_interruptible(self, duration: float) -> None:
        """Sleep for duration, but wake early if stopped."""
        deadline = time.monotonic() + duration
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    remaining_min = int(minutes) % 60
    return f"{hours:.0f}h {remaining_min}m"
