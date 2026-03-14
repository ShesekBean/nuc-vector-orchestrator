"""Autonomous room-by-room exploration with Signal-based room naming.

Drives Vector through the house, building the occupancy grid via SLAM.
When the robot detects it has entered a new area (significant movement since
last waypoint), it:
1. Stops and takes a photo
2. Sends a Signal message to Ophir: "I found a new room! What should I call it?"
3. Waits for Ophir's reply (reads from signal-inbox.jsonl)
4. Saves the position as a named waypoint

Exploration strategy: frontier-based — drive toward the nearest boundary
between explored (FREE) and unexplored (UNKNOWN) cells.

Usage::

    explorer = AutonomousExplorer(
        slam, motor, head, camera, nuc_bus,
        nav_controller, intercom,
    )
    explorer.start()   # begins exploration
    explorer.stop()    # saves map, stops
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.intercom import Intercom
    from apps.vector.src.motor_controller import MotorController
    from apps.vector.src.planner.nav_controller import NavController
    from apps.vector.src.planner.visual_slam import VisualSLAM

logger = logging.getLogger(__name__)

# Signal inbox file path (shared with signal-group-monitor.sh)
INBOX_PATH = Path(
    os.environ.get(
        "SIGNAL_INBOX",
        os.path.expanduser(
            "~/Documents/claude/nuc-vector-orchestrator/.claude/state/signal-inbox.jsonl"
        ),
    )
)

# Ophir's phone number (replies come from this number)
OPHIR_PHONE = os.environ.get("OPHIR_PHONE", "+14084758230")


class ExploreState(Enum):
    """Explorer state machine."""

    IDLE = auto()
    EXPLORING = auto()
    ASKING_ROOM_NAME = auto()
    WAITING_REPLY = auto()
    NAVIGATING_TO_FRONTIER = auto()


@dataclass
class ExploreConfig:
    """Exploration configuration."""

    # How far to travel before checking for a new room (mm)
    room_check_distance_mm: float = 1500.0

    # SLAM frame processing rate during exploration
    slam_hz: float = 10.0

    # Exploration drive speed
    drive_speed_mmps: float = 80.0
    turn_speed_dps: float = 60.0

    # Frontier detection
    frontier_min_cells: int = 3  # minimum frontier cluster size
    frontier_search_radius_cells: int = 40  # how far to look for frontiers

    # How long to wait for Ophir's room name reply (seconds)
    reply_timeout_s: float = 300.0  # 5 minutes

    # Minimum time between room-name prompts (seconds)
    min_prompt_interval_s: float = 30.0

    # Exploration step size (mm) — drive this far toward frontier
    step_distance_mm: float = 400.0

    # Maximum exploration time (seconds) before auto-stop
    max_explore_time_s: float = 1800.0  # 30 minutes

    # Auto-save map every N seconds during exploration
    auto_save_interval_s: float = 60.0

    # Map name for persistence
    map_name: str = "exploration"


class AutonomousExplorer:
    """Autonomous frontier-based exploration with Signal room naming.

    Drives toward unexplored areas, processes camera frames through SLAM,
    and asks Ophir for room names when it enters new areas.

    Args:
        slam: VisualSLAM for pose estimation and map building.
        motor: MotorController for driving.
        head: HeadController for looking around.
        camera: CameraClient for SLAM frames.
        nuc_bus: Event bus.
        nav_controller: NavController for map/waypoint management.
        intercom: Intercom for Signal messaging.
        config: Exploration parameters.
    """

    def __init__(
        self,
        slam: VisualSLAM,
        motor: MotorController,
        head: HeadController,
        camera: CameraClient,
        nuc_bus: NucEventBus,
        nav_controller: NavController,
        intercom: Intercom,
        robot: Any = None,
        imu_poller: Any = None,
        imu_fusion: Any = None,
        config: ExploreConfig | None = None,
    ) -> None:
        self._slam = slam
        self._motor = motor
        self._head = head
        self._camera = camera
        self._bus = nuc_bus
        self._nav = nav_controller
        self._intercom = intercom
        self._robot = robot
        self._imu_poller = imu_poller
        self._imu_fusion = imu_fusion
        self._cfg = config or ExploreConfig()

        self._state = ExploreState.IDLE
        self._running = False
        self._explore_thread: threading.Thread | None = None
        self._slam_thread: threading.Thread | None = None

        # Obstacle detector + YOLO — lazy init
        self._obstacle_detector: Any | None = None
        self._person_detector: Any | None = None

        # Track distance since last room prompt
        self._last_room_x: float = 0.0
        self._last_room_y: float = 0.0
        self._last_prompt_time: float = 0.0
        self._rooms_discovered: int = 0
        self._last_save_time: float = 0.0

    def _say(self, text: str) -> None:
        """Have Vector say something via built-in TTS (non-blocking)."""
        if self._robot is None:
            return
        def _speak():
            try:
                self._robot.behavior.say_text(text)
            except Exception:
                logger.debug("say_text failed: %s", text)
        threading.Thread(target=_speak, name="explorer-tts", daemon=True).start()

    def _request_control(self) -> None:
        """Request override behavior control so we can interrupt charger sit etc."""
        if self._robot is None:
            return
        try:
            from anki_vector.connection import ControlPriorityLevel
            self._robot.conn.request_control(
                behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
            )
            logger.info("Override behavior control granted for exploration")
        except Exception:
            logger.warning("Failed to request override control", exc_info=True)

    def _release_control(self) -> None:
        """Release override priority back to default."""
        if self._robot is None:
            return
        try:
            self._robot.conn.release_control()
            logger.info("Released override control")
        except Exception:
            pass

    def _drive_off_charger(self) -> None:
        """Drive off charger if Vector is currently docked.

        Also saves the charger position as a waypoint so AutoCharger
        can navigate back later.
        """
        if self._robot is None:
            return
        try:
            batt = self._robot.get_battery_state()
            if batt.is_on_charger_platform:
                # Save charger position BEFORE driving off
                # (this is where the charger physically is)
                try:
                    self._nav.save_current_position("charger")
                    logger.info("Saved charger waypoint at current position")
                except Exception:
                    logger.debug("Could not save charger waypoint", exc_info=True)

                logger.info("Vector is on charger — driving off")
                self._say("Driving off charger.")
                self._robot.behavior.drive_off_charger()
                time.sleep(2.0)  # wait for him to clear the charger
                logger.info("Drove off charger successfully")
        except Exception:
            logger.warning("drive_off_charger failed", exc_info=True)

    @property
    def state(self) -> ExploreState:
        return self._state

    @property
    def rooms_discovered(self) -> int:
        return self._rooms_discovered

    def start(self) -> None:
        """Start autonomous exploration."""
        if self._running:
            logger.warning("Explorer already running")
            return

        self._running = True
        self._state = ExploreState.EXPLORING

        # Override behavior control so we can move even if Vector is on charger
        self._request_control()

        # Drive off charger if needed
        self._drive_off_charger()

        # Start IMU poller + fusion for better heading estimation
        if self._imu_poller is not None:
            try:
                self._imu_poller.start()
                logger.info("IMU poller started for exploration")
            except Exception:
                logger.warning("Failed to start IMU poller", exc_info=True)
        if self._imu_fusion is not None:
            try:
                self._imu_fusion.start()
                logger.info("IMU fusion started for exploration")
            except Exception:
                logger.warning("Failed to start IMU fusion", exc_info=True)

        # Start obstacle detector + YOLO for obstacle scanning
        try:
            from apps.vector.src.planner.obstacle_detector import ObstacleDetector
            self._obstacle_detector = ObstacleDetector(self._motor, self._bus)
            self._obstacle_detector.start()
            logger.info("ObstacleDetector started for exploration")
        except Exception:
            logger.warning("Failed to start ObstacleDetector", exc_info=True)
        try:
            from apps.vector.src.detector.person_detector import PersonDetector
            self._person_detector = PersonDetector(confidence_threshold=0.25)
            self._person_detector.load_model()
            logger.info("YOLO detector loaded for obstacle scanning")
        except Exception:
            logger.warning("Failed to load YOLO detector", exc_info=True)

        # Start SLAM if not already running
        self._slam.start()

        # Load existing map if available
        try:
            if self._nav._map_store.exists(self._cfg.map_name):
                self._nav._load_map(self._cfg.map_name)
                logger.info("Loaded existing map '%s'", self._cfg.map_name)
            else:
                logger.info("No existing map to load — starting fresh")
        except Exception:
            logger.info("Failed to load map — starting fresh", exc_info=True)

        # Start SLAM frame processing thread
        self._slam_thread = threading.Thread(
            target=self._slam_loop, name="explorer-slam", daemon=True
        )
        self._slam_thread.start()

        # Start exploration thread
        self._explore_thread = threading.Thread(
            target=self._explore_loop, name="explorer", daemon=True
        )
        self._explore_thread.start()

        # Reset room tracking
        pose = self._slam.get_pose()
        self._last_room_x = pose.x
        self._last_room_y = pose.y
        self._last_save_time = time.monotonic()

        # Check intercom health
        if not self._intercom.health_check():
            logger.warning(
                "Intercom server not reachable — room naming via Signal will not work. "
                "Start it with: python3 scripts/intercom-server.py"
            )

        self._intercom.send_text(
            "Starting autonomous exploration! "
            "I'll map your house and ask you to name rooms as I discover them."
        )
        logger.info("AutonomousExplorer started")

    def stop(self) -> None:
        """Stop exploration and save map."""
        if not self._running:
            return

        self._running = False
        self._state = ExploreState.IDLE

        # Stop motors
        try:
            self._motor.drive_wheels(0, 0)
        except Exception:
            pass

        # Wait for threads
        if self._explore_thread:
            self._explore_thread.join(timeout=5.0)
            self._explore_thread = None
        if self._slam_thread:
            self._slam_thread.join(timeout=5.0)
            self._slam_thread = None

        # Stop obstacle detector + YOLO
        if self._obstacle_detector:
            self._obstacle_detector.stop()
            self._obstacle_detector = None
        self._person_detector = None

        # Stop IMU
        if self._imu_fusion is not None:
            self._imu_fusion.stop()
        if self._imu_poller is not None:
            self._imu_poller.stop()

        # Save map
        self._nav._save_map()

        # Release override control
        self._release_control()

        grid = self._slam.get_grid()
        self._intercom.send_text(
            f"Exploration complete! Mapped {grid.free_cell_count} cells, "
            f"discovered {self._rooms_discovered} rooms."
        )
        logger.info("AutonomousExplorer stopped")

    def get_status(self) -> dict:
        """Return exploration status."""
        pose = self._slam.get_pose()
        grid = self._slam.get_grid()
        return {
            "state": self._state.name.lower(),
            "rooms_discovered": self._rooms_discovered,
            "pose": {
                "x": round(pose.x, 1),
                "y": round(pose.y, 1),
                "theta_deg": round(math.degrees(pose.theta), 1),
            },
            "map": {
                "free_cells": grid.free_cell_count,
                "occupied_cells": grid.occupied_cell_count,
            },
            "slam_frames": self._slam.frames_processed,
        }

    # -- SLAM frame loop -----------------------------------------------------

    def _slam_loop(self) -> None:
        """Continuously feed camera frames to SLAM."""
        period = 1.0 / self._cfg.slam_hz

        while self._running:
            start = time.monotonic()
            try:
                frame = self._camera.get_latest_frame()
                if frame is not None:
                    self._slam.process_frame(frame)
            except Exception:
                logger.exception("SLAM frame processing error")

            elapsed = time.monotonic() - start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # -- Exploration loop ----------------------------------------------------

    def _explore_loop(self) -> None:
        """Main exploration loop: find frontiers, drive to them, check rooms."""
        start_time = time.monotonic()

        # --- Warmup phase: seed the map and do an initial scan ---------------
        self._say("Starting exploration.")
        self._seed_start_area()

        # Wait for SLAM to process a few frames
        warmup_end = time.monotonic() + 3.0
        while self._running and time.monotonic() < warmup_end:
            time.sleep(0.5)

        # Initial 360° scan to build map around starting position
        if self._running:
            self._initial_scan()

        # --- Main exploration loop -------------------------------------------
        no_frontier_count = 0

        while self._running:
            # Check time limit
            if time.monotonic() - start_time > self._cfg.max_explore_time_s:
                logger.info("Exploration time limit reached")
                self._intercom.send_text(
                    "I've been exploring for 30 minutes. Stopping to save the map!"
                )
                break

            try:
                self._state = ExploreState.EXPLORING

                # Sync SLAM pose with IMU-fused pose for better accuracy
                self._sync_pose_from_imu()

                # Auto-save map periodically
                now = time.monotonic()
                if now - self._last_save_time > self._cfg.auto_save_interval_s:
                    self._auto_save_map()
                    self._last_save_time = now

                # Check obstacle detector for stuck condition
                if self._obstacle_detector and self._obstacle_detector.check_stuck():
                    logger.info("Obstacle detector triggered escape maneuver")
                    time.sleep(1.0)
                    continue

                # Check if we've moved enough to potentially be in a new room
                self._check_room_transition()

                # Find nearest frontier
                frontier = self._find_frontier()
                if frontier is None:
                    no_frontier_count += 1
                    if no_frontier_count >= 3:
                        logger.info("No more frontiers — exploration complete!")
                        self._say("Exploration complete!")
                        self._intercom.send_text(
                            "I've explored everywhere I can reach! No more unexplored areas."
                        )
                        break
                    # Sometimes SLAM just needs more frames — wait and retry
                    time.sleep(1.0)
                    continue

                no_frontier_count = 0

                # Scan for obstacles with YOLO before driving
                self._scan_for_obstacles()
                if self._obstacle_detector:
                    zone = self._obstacle_detector.zone
                    if zone == "danger":
                        logger.info("Obstacle in danger zone — backing up and turning")
                        try:
                            self._motor.turn_then_drive(
                                angle_deg=0,
                                distance_mm=-50,
                                drive_speed_mmps=40,
                                turn_speed_dps=self._cfg.turn_speed_dps,
                            )
                        except Exception:
                            pass
                        try:
                            self._motor.turn_in_place(
                                90.0, speed_dps=self._cfg.turn_speed_dps
                            )
                        except Exception:
                            pass
                        time.sleep(0.5)
                        continue

                # Drive toward frontier
                self._state = ExploreState.NAVIGATING_TO_FRONTIER
                logger.info(
                    "Driving toward frontier (%.0f, %.0f) from pose (%.0f, %.0f)",
                    frontier[0], frontier[1],
                    self._slam.get_pose().x, self._slam.get_pose().y,
                )
                self._drive_toward(frontier[0], frontier[1])

                # Brief pause to let SLAM process
                time.sleep(0.5)

            except Exception:
                logger.exception("Exploration loop error")
                time.sleep(1.0)

        self._running = False
        self._state = ExploreState.IDLE

    def _scan_for_obstacles(self) -> None:
        """Run YOLO on the current camera frame and feed results to ObstacleDetector.

        This gives the obstacle detector real vision data to determine
        zone (clear/caution/danger) and speed_scale.
        """
        if self._person_detector is None or self._obstacle_detector is None:
            return
        try:
            frame = self._camera.get_latest_frame()
            if frame is None:
                return
            detections = self._person_detector.detect(frame)
            self._obstacle_detector.update(detections)
            zone = self._obstacle_detector.zone
            if zone != "clear":
                logger.info(
                    "Obstacle scan: zone=%s scale=%.2f (%d detections)",
                    zone, self._obstacle_detector.speed_scale, len(detections),
                )
        except Exception:
            logger.debug("Obstacle scan failed", exc_info=True)

    def _sync_pose_from_imu(self) -> None:
        """Sync SLAM pose with IMU-fused heading for drift correction.

        IMU fusion uses gyro (50Hz) + visual odometry complementary filter
        for heading, which is much more accurate than pure dead reckoning.
        """
        if self._imu_fusion is None:
            return
        try:
            fused = self._imu_fusion.get_fused_pose()
            slam_pose = self._slam.get_pose()
            # Correct SLAM heading with IMU-fused heading
            heading_error = fused.theta - slam_pose.theta
            if abs(heading_error) > 0.01:  # ~0.5°
                self._slam.update_pose_dead_reckoning(delta_theta=heading_error)
        except Exception:
            logger.debug("IMU pose sync failed", exc_info=True)

    def _auto_save_map(self) -> None:
        """Save the current map to disk periodically."""
        try:
            self._nav._save_map()
            grid = self._slam.get_grid()
            logger.info(
                "Auto-saved map: %d free cells, %d occupied",
                grid.free_cell_count, grid.occupied_cell_count,
            )
        except Exception:
            logger.warning("Auto-save map failed", exc_info=True)

    def _mark_area_free(self) -> None:
        """Mark a small area around the current robot position as FREE.

        This supplements the SLAM mark_line_free with a radius around the
        robot, ensuring frontiers advance as the robot moves.
        """
        from apps.vector.src.planner.visual_slam import CellState

        grid = self._slam.get_grid()
        pose = self._slam.get_pose()

        # Mark 150mm radius around robot as free (robot is ~100mm wide)
        for dx in range(-150, 151, 50):
            for dy in range(-150, 151, 50):
                if math.hypot(dx, dy) <= 150:
                    grid.set_cell(int(pose.x + dx), int(pose.y + dy), CellState.FREE)

    def _seed_start_area(self) -> None:
        """Mark the area around the starting position as FREE.

        Without this, the grid is entirely UNKNOWN and frontier detection
        can't find any FREE cells to anchor frontiers on.
        """
        from apps.vector.src.planner.visual_slam import CellState

        grid = self._slam.get_grid()
        pose = self._slam.get_pose()

        # Mark a small area around the robot as free (200mm radius)
        for dx in range(-200, 201, 50):
            for dy in range(-200, 201, 50):
                if math.hypot(dx, dy) <= 200:
                    grid.set_cell(int(pose.x + dx), int(pose.y + dy), CellState.FREE)

        logger.info("Seeded start area with FREE cells at (%.0f, %.0f)", pose.x, pose.y)

    def _initial_scan(self) -> None:
        """Do a slow 360° turn to build initial map around the robot."""
        logger.info("Performing initial 360° scan")
        try:
            # Head level for best field of view
            self._head.set_angle(0.0)
            time.sleep(0.3)

            # Turn in place: 4 × 90°, pausing to let SLAM process
            for i in range(4):
                if not self._running:
                    return
                self._motor.turn_in_place(
                    90.0, speed_dps=self._cfg.turn_speed_dps
                )
                time.sleep(1.0)  # let SLAM process frames during pause

        except Exception:
            logger.warning("Initial scan failed — continuing anyway", exc_info=True)

    def _check_room_transition(self) -> None:
        """Check if robot has moved far enough to be in a new area."""
        pose = self._slam.get_pose()
        dist = math.hypot(
            pose.x - self._last_room_x,
            pose.y - self._last_room_y,
        )

        now = time.monotonic()
        if (
            dist >= self._cfg.room_check_distance_mm
            and now - self._last_prompt_time > self._cfg.min_prompt_interval_s
        ):
            self._ask_room_name(pose)

    def _ask_room_name(self, pose: Any) -> None:
        """Stop, take a photo, ask Ophir for the room name via Signal."""
        # Stop motors
        try:
            self._motor.drive_wheels(0, 0)
        except Exception:
            pass

        self._state = ExploreState.ASKING_ROOM_NAME
        self._rooms_discovered += 1

        # Voice feedback
        self._say("I found a new room! Let me take a look around.")

        # Send photo of what Vector sees
        self._intercom.send_photo(
            f"I'm in a new area (room #{self._rooms_discovered})! "
            "What should I call this room?"
        )

        self._last_prompt_time = time.monotonic()

        # Wait for reply
        self._state = ExploreState.WAITING_REPLY
        room_name = self._wait_for_reply(self._cfg.reply_timeout_s)

        if room_name:
            # Save waypoint with the name Ophir gave
            self._nav.save_current_position(room_name)
            self._say(f"Got it! This is the {room_name}.")
            self._intercom.send_text(
                f"Finished mapping room! Saved this spot as '{room_name}'."
            )
            logger.info("Room named: '%s' at (%.0f, %.0f)", room_name, pose.x, pose.y)
        else:
            # No reply — save with auto-name
            auto_name = f"room-{self._rooms_discovered}"
            self._nav.save_current_position(auto_name)
            self._say(f"Finished mapping room {self._rooms_discovered}.")
            self._intercom.send_text(
                f"Finished mapping room! Saved as '{auto_name}' — "
                "you can rename it later!"
            )
            logger.info("Auto-named room '%s' at (%.0f, %.0f)", auto_name, pose.x, pose.y)

        # Update last room position
        self._last_room_x = pose.x
        self._last_room_y = pose.y

    def _wait_for_reply(self, timeout_s: float) -> str | None:
        """Wait for a reply from Ophir on Signal.

        Reads from signal-inbox.jsonl for new messages from Ophir's number.
        Returns the reply text, or None on timeout.
        """
        # Record position in inbox before waiting
        start_pos = _get_inbox_size()
        deadline = time.monotonic() + timeout_s

        while self._running and time.monotonic() < deadline:
            reply = _check_inbox_for_reply(start_pos)
            if reply:
                return reply.strip()
            time.sleep(2.0)

        return None

    # -- Frontier detection --------------------------------------------------

    def _find_frontier(self) -> tuple[float, float] | None:
        """Find the nearest frontier cell (boundary between FREE and UNKNOWN).

        A frontier is a FREE cell adjacent to at least one UNKNOWN cell.
        Returns world coordinates (x_mm, y_mm) of the nearest frontier,
        or None if no frontiers exist.
        """
        from apps.vector.src.planner.visual_slam import CellState

        grid = self._slam.get_grid()
        pose = self._slam.get_pose()
        robot_r, robot_c = grid.world_to_cell(pose.x, pose.y)

        raw = grid.grid
        dim = grid.grid_dim
        radius = self._cfg.frontier_search_radius_cells

        best_dist = float("inf")
        best_r, best_c = -1, -1

        # Search in a radius around robot position
        r_min = max(0, robot_r - radius)
        r_max = min(dim, robot_r + radius)
        c_min = max(0, robot_c - radius)
        c_max = min(dim, robot_c + radius)

        for r in range(r_min, r_max):
            for c in range(c_min, c_max):
                if raw[r, c] != int(CellState.FREE):
                    continue

                # Check if this FREE cell borders any UNKNOWN cell
                is_frontier = False
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < dim and 0 <= nc < dim:
                        if raw[nr, nc] == int(CellState.UNKNOWN):
                            is_frontier = True
                            break

                if is_frontier:
                    dist = math.hypot(r - robot_r, c - robot_c)
                    if dist < best_dist and dist > 2:  # don't pick self
                        best_dist = dist
                        best_r, best_c = r, c

        if best_r < 0:
            return None

        # Convert back to world coordinates
        origin = dim // 2
        x_mm = (best_c - origin) * grid.cell_size_mm
        y_mm = (best_r - origin) * grid.cell_size_mm
        return (float(x_mm), float(y_mm))

    def _drive_toward(self, target_x: float, target_y: float) -> None:
        """Drive one step toward a target position."""
        from apps.vector.src.motor_controller import CliffSafetyError

        pose = self._slam.get_pose()
        dx = target_x - pose.x
        dy = target_y - pose.y
        distance = math.hypot(dx, dy)

        # Limit step distance
        distance = min(distance, self._cfg.step_distance_mm)

        # Fresh obstacle scan before driving
        self._scan_for_obstacles()

        # Apply obstacle detector speed scaling
        if self._obstacle_detector:
            scale = self._obstacle_detector.speed_scale
            if scale <= 0.0:
                logger.info("Obstacle ahead — full stop, turning away")
                try:
                    self._motor.turn_in_place(
                        90.0, speed_dps=self._cfg.turn_speed_dps
                    )
                except Exception:
                    pass
                return
            if scale < 1.0:
                distance *= scale
                logger.info("Obstacle speed scale %.2f → distance %.0fmm", scale, distance)

        # Compute bearing
        bearing = math.atan2(dy, dx)
        turn_angle = _normalise_angle(bearing - pose.theta)
        turn_deg = math.degrees(turn_angle)

        logger.info(
            "turn_then_drive: turn=%.1f° dist=%.0fmm bearing=%.1f°",
            turn_deg, distance, math.degrees(bearing),
        )
        try:
            self._motor.turn_then_drive(
                angle_deg=turn_deg,
                distance_mm=distance,
                drive_speed_mmps=self._cfg.drive_speed_mmps,
                turn_speed_dps=self._cfg.turn_speed_dps,
            )
            logger.info("Drive command completed successfully")
            # Dead-reckoning: update SLAM pose with commanded movement
            # Visual SLAM only tracks rotation — we need to manually
            # update position so the map builds and frontiers advance.
            self._slam.update_pose_dead_reckoning(
                delta_x=distance * math.cos(bearing),
                delta_y=distance * math.sin(bearing),
            )
            # Mark area around new position as free (robot clearance)
            self._mark_area_free()
            # Reset stuck detection on successful movement
            if self._obstacle_detector:
                self._obstacle_detector.reset_stuck()
        except CliffSafetyError:
            logger.warning("Cliff detected during exploration — turning away")
            try:
                self._motor.turn_in_place(90.0, speed_dps=self._cfg.turn_speed_dps)
            except Exception:
                pass
        except Exception:
            logger.exception("Drive failed during exploration")


# ---------------------------------------------------------------------------
# Auto-charge: navigate to charger when battery is low
# ---------------------------------------------------------------------------


class AutoCharger:
    """Monitors battery and navigates to charger when low.

    When battery drops below the threshold:
    1. Navigate to the "charger" waypoint (if saved)
    2. Once near, call SDK `drive_on_charger()` for visual docking

    The charger has a visual marker that Vector's native SDK recognizes
    for precise docking alignment.

    Args:
        robot: Connected Vector SDK robot.
        nav_controller: NavController for waypoint navigation.
        nuc_bus: Event bus.
        intercom: For notifying Ophir.
        battery_threshold_pct: Battery voltage percentage to trigger charging.
    """

    # Voltage-to-percentage mapping for Vector's LiPo
    # Based on typical 1S LiPo discharge curve
    VOLTAGE_TABLE = [
        (4.20, 100), (4.10, 90), (4.00, 80), (3.90, 70),
        (3.80, 60), (3.75, 50), (3.70, 40), (3.65, 30),
        (3.60, 20), (3.55, 15), (3.50, 10), (3.40, 5),
        (3.30, 0),
    ]

    def __init__(
        self,
        robot: Any,
        nav_controller: NavController,
        nuc_bus: NucEventBus,
        intercom: Intercom | None = None,
        battery_threshold_pct: float = 18.0,
        resume_threshold_pct: float = 90.0,
        check_interval_s: float = 30.0,
    ) -> None:
        self._robot = robot
        self._nav = nav_controller
        self._bus = nuc_bus
        self._intercom = intercom
        self._threshold_pct = battery_threshold_pct
        self._resume_threshold_pct = resume_threshold_pct
        self._check_interval = check_interval_s

        self._running = False
        self._thread: threading.Thread | None = None
        self._returning_to_charger = False
        self._waiting_for_charge = False  # waiting on charger to hit resume threshold

        # Explorer reference — set by ConnectionManager so we can
        # pause exploration before docking and resume after charging
        self.explorer: AutonomousExplorer | None = None
        self._was_exploring = False  # True if we interrupted exploration

    def start(self) -> None:
        """Start battery monitoring."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, name="auto-charger", daemon=True
        )
        self._thread.start()
        logger.info(
            "AutoCharger started (threshold=%.0f%%)", self._threshold_pct
        )

    def stop(self) -> None:
        """Stop battery monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("AutoCharger stopped")

    @property
    def is_returning(self) -> bool:
        return self._returning_to_charger

    def _monitor_loop(self) -> None:
        """Periodically check battery and trigger return-to-charger."""
        while self._running:
            try:
                self._check_battery()
            except Exception:
                logger.exception("Battery check failed")

            # Sleep in small increments so we can stop quickly
            deadline = time.monotonic() + self._check_interval
            while self._running and time.monotonic() < deadline:
                time.sleep(1.0)

    def _check_battery(self) -> None:
        """Check battery level and navigate to charger if low.

        Also checks if battery has recharged enough to resume exploration.
        """
        try:
            batt = self._robot.get_battery_state()
        except Exception:
            return

        pct = self._voltage_to_percent(batt.battery_volts)
        logger.debug("Battery: %.2fV (%.0f%%)", batt.battery_volts, pct)

        # --- Resume after charging ---
        if self._waiting_for_charge:
            if batt.is_charging or batt.is_on_charger_platform:
                if pct >= self._resume_threshold_pct:
                    logger.info("Battery charged to %.0f%% — resuming", pct)
                    self._waiting_for_charge = False
                    self._resume_exploration(pct)
                return  # still charging, keep waiting
            # Fell off charger before reaching threshold — stop waiting
            self._waiting_for_charge = False
            return

        if self._returning_to_charger:
            return  # Already heading to charger

        if batt.is_charging or batt.is_on_charger_platform:
            return  # Already charging

        if pct <= self._threshold_pct:
            logger.warning(
                "Battery LOW: %.2fV (%.0f%%) — returning to charger",
                batt.battery_volts, pct,
            )
            self._return_to_charger(batt.battery_volts, pct)

    def _return_to_charger(self, voltage: float, pct: float) -> None:
        """Navigate to charger waypoint, then dock."""
        self._returning_to_charger = True

        # Stop exploration if running
        if self.explorer and self.explorer._running:
            self._was_exploring = True
            logger.info("Pausing exploration for charging")
            self.explorer.stop()
        else:
            self._was_exploring = False

        if self._intercom:
            self._intercom.send_text(
                f"Battery low ({pct:.0f}%, {voltage:.2f}V). "
                "Heading back to the charger!"
            )

        # Check if "charger" waypoint exists
        charger_wp = self._nav._waypoint_mgr.get("charger")
        if charger_wp is None:
            logger.warning(
                "No 'charger' waypoint saved — trying SDK drive_on_charger() directly"
            )
            self._dock_with_charger()
            return

        # Navigate to charger waypoint
        logger.info("Navigating to charger waypoint at (%.0f, %.0f)", charger_wp.x, charger_wp.y)
        started = self._nav.navigate_to_waypoint("charger")

        if started:
            # Wait for navigation to complete
            from apps.vector.src.planner.nav_controller import NavState

            timeout = time.monotonic() + 120.0  # 2 min max
            while self._running and time.monotonic() < timeout:
                state = self._nav.state
                if state in (NavState.ARRIVED, NavState.IDLE, NavState.BLOCKED):
                    break
                time.sleep(1.0)

            if self._nav.state == NavState.ARRIVED:
                logger.info("Arrived near charger — attempting SDK dock")
            else:
                logger.warning("Navigation to charger did not arrive, trying dock anyway")

        # Use SDK's native visual docking
        self._dock_with_charger()

    def _dock_with_charger(self) -> None:
        """Use the SDK's drive_on_charger() for visual docking.

        Vector's charger has a visual marker that the SDK recognizes.
        The SDK handles the final approach alignment and backing onto
        the charging contacts.
        """
        try:
            from anki_vector.connection import ControlPriorityLevel
            self._robot.conn.request_control(
                behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
            )
        except Exception:
            logger.warning("Failed to request control for docking")

        try:
            logger.info("Calling SDK drive_on_charger()...")
            self._robot.behavior.drive_on_charger()
            logger.info("Successfully docked on charger!")

            if self._intercom:
                msg = "I'm on the charger! Charging up."
                if self._was_exploring:
                    msg += f" I'll resume exploring when battery reaches {self._resume_threshold_pct:.0f}%."
                self._intercom.send_text(msg)

            # Wait for battery to recharge before resuming
            if self._was_exploring:
                self._waiting_for_charge = True
        except Exception:
            logger.exception("drive_on_charger() failed")
            if self._intercom:
                self._intercom.send_text(
                    "I couldn't find or reach the charger. "
                    "Can you put me on it?"
                )
        finally:
            self._returning_to_charger = False
            try:
                self._robot.conn.release_control()
            except Exception:
                pass

    def _resume_exploration(self, pct: float) -> None:
        """Resume exploration after charging if we were exploring before."""
        if not self._was_exploring or self.explorer is None:
            return

        self._was_exploring = False
        logger.info("Resuming exploration after charging (battery %.0f%%)", pct)

        if self._intercom:
            self._intercom.send_text(
                f"Battery at {pct:.0f}%! Resuming exploration."
            )

        try:
            self.explorer.start()
        except Exception:
            logger.exception("Failed to resume exploration after charging")

    @classmethod
    def _voltage_to_percent(cls, voltage: float) -> float:
        """Convert battery voltage to percentage using lookup table."""
        if voltage >= cls.VOLTAGE_TABLE[0][0]:
            return 100.0
        if voltage <= cls.VOLTAGE_TABLE[-1][0]:
            return 0.0

        # Linear interpolation between table entries
        for i in range(len(cls.VOLTAGE_TABLE) - 1):
            v_high, p_high = cls.VOLTAGE_TABLE[i]
            v_low, p_low = cls.VOLTAGE_TABLE[i + 1]
            if v_low <= voltage <= v_high:
                frac = (voltage - v_low) / (v_high - v_low)
                return p_low + frac * (p_high - p_low)

        return 50.0  # fallback


# ---------------------------------------------------------------------------
# Signal inbox helpers
# ---------------------------------------------------------------------------


def _get_inbox_size() -> int:
    """Get current size of Signal inbox file."""
    try:
        return INBOX_PATH.stat().st_size
    except FileNotFoundError:
        return 0


def _check_inbox_for_reply(start_pos: int) -> str | None:
    """Check Signal inbox for a new message from Ophir after start_pos.

    Reads lines added to signal-inbox.jsonl after the given byte position.
    Returns the first unreplied message text from Ophir, or None.
    """
    try:
        if not INBOX_PATH.exists():
            return None

        with open(INBOX_PATH, "r") as f:
            f.seek(start_pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check if from Ophir and not already replied
                sender = entry.get("from", "")
                if OPHIR_PHONE not in sender:
                    continue
                if entry.get("replied", False):
                    continue

                msg = entry.get("msg", "").strip()
                if msg:
                    return msg
    except Exception:
        logger.exception("Error reading Signal inbox")

    return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _normalise_angle(angle: float) -> float:
    """Normalise angle to [-pi, pi]."""
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle
