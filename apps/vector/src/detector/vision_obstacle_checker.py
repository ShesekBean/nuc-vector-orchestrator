"""Async vision obstacle checker — Claude Vision via CLI in background thread.

Periodically captures a camera frame and asks Claude Haiku to assess
obstacles. Results are written to the shared ObstacleMap.

Runs in a background daemon thread so it never blocks movement control loops.

Usage::

    checker = VisionObstacleChecker(camera_client, obstacle_map)
    checker.start()      # begins background checking
    checker.check_now()  # trigger an immediate check (blocking)
    checker.stop()
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.planner.obstacle_map import ObstacleMap

logger = logging.getLogger(__name__)

# Default check interval (seconds)
DEFAULT_INTERVAL_S = 5.0

# Subprocess timeout
CLI_TIMEOUT_S = 15


class VisionObstacleChecker:
    """Background vision obstacle checker using Claude Haiku.

    Args:
        camera_client: Camera for frame capture.
        obstacle_map: Shared obstacle map to write results to.
        interval_s: Seconds between background checks.
    """

    def __init__(
        self,
        camera_client: CameraClient,
        obstacle_map: ObstacleMap,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._camera = camera_client
        self._map = obstacle_map
        self._interval = interval_s
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._check_now_event = threading.Event()
        self._running = False
        self._checks_done = 0

    @property
    def checks_done(self) -> int:
        return self._checks_done

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start background vision checking thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="vision-obstacle",
            daemon=True,
        )
        self._thread.start()
        logger.info("VisionObstacleChecker started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        """Stop background checking."""
        self._running = False
        self._stop_event.set()
        self._check_now_event.set()  # wake up if sleeping
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=CLI_TIMEOUT_S + 2)
        self._thread = None
        logger.info("VisionObstacleChecker stopped (%d checks done)", self._checks_done)

    def check_now(self) -> tuple[bool, str]:
        """Trigger an immediate vision check (blocking).

        Returns:
            (blocked, direction) — same as a background check result.
        """
        return self._do_check()

    def trigger_async(self) -> None:
        """Wake up the background thread for an early check."""
        self._check_now_event.set()

    # -- Internal -----------------------------------------------------------

    def _run_loop(self) -> None:
        """Background loop: check periodically or on trigger."""
        while not self._stop_event.is_set():
            self._do_check()

            # Sleep for interval, but wake early on trigger or stop
            self._check_now_event.clear()
            self._check_now_event.wait(timeout=self._interval)

    def _do_check(self) -> tuple[bool, str]:
        """Run a single vision obstacle check."""
        jpeg = self._camera.get_latest_jpeg()
        if jpeg is None:
            return False, ""

        try:
            # Write frame to temp file
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(jpeg)
                tmp_path = tmp.name

            result = subprocess.run(
                [
                    "claude", "--print", "--model", "haiku",
                    "--dangerously-skip-permissions",
                    f"Read the file {tmp_path} and tell me: "
                    "You are a small robot's obstacle detector camera. "
                    "Is there an obstacle within 30cm directly ahead? "
                    "Reply ONLY one word: CLEAR or LEFT or RIGHT. "
                    "LEFT/RIGHT means which way to turn to avoid it.",
                ],
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_S,
            )

            # Clean up
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            answer = result.stdout.strip().upper().split("\n")[0]
            self._checks_done += 1

            if "CLEAR" in answer:
                self._map.update_vision(blocked=False)
                return False, ""
            elif "LEFT" in answer:
                self._map.update_vision(blocked=True, direction="left")
                return True, "left"
            elif "RIGHT" in answer:
                self._map.update_vision(blocked=True, direction="right")
                return True, "right"
            else:
                logger.warning("Ambiguous vision response: %r", answer)
                self._map.update_vision(blocked=True, direction="right")
                return True, "right"

        except subprocess.TimeoutExpired:
            logger.warning("Vision check timed out")
            return False, ""
        except Exception:
            logger.debug("Vision check failed", exc_info=True)
            return False, ""
