#!/usr/bin/env python3
"""Hold Vector in quiet mode — still and silent, but wake word still works.

Connects to Vector via SDK and holds behavior control indefinitely at
OVERRIDE_BEHAVIORS_PRIORITY. When vic-engine takes control for a wake
word interaction, this script re-requests control as soon as it's
released, keeping Vector quiet between interactions.

Usage:
    python3 scripts/vector-quiet-mode.py

Stop with Ctrl-C to release control (Vector resumes autonomous behavior).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("vector-quiet-mode")

SERIAL = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")


def main():
    import anki_vector
    from anki_vector.connection import ControlPriorityLevel
    from anki_vector.util import degrees

    logger.info("Connecting to Vector (serial=%s)...", SERIAL)
    robot = anki_vector.Robot(
        serial=SERIAL,
        default_logging=False,
        behavior_control_level=None,  # manage control manually
    )
    robot.connect()
    logger.info("Connected. Requesting OVERRIDE_BEHAVIORS_PRIORITY control...")

    # Track control state
    has_control = threading.Event()

    def _on_control_granted(event_type, event):
        has_control.set()
        logger.info("Behavior control granted — Vector is quiet.")

    def _on_control_lost(event_type, event):
        has_control.clear()
        logger.info("Behavior control lost (wake word or other). Will re-request...")

    # Subscribe to control events
    robot.events.subscribe(_on_control_granted, anki_vector.events.Events.control_granted_response)
    robot.events.subscribe(_on_control_lost, anki_vector.events.Events.control_lost_response)

    # Initial control request
    robot.conn.request_control(
        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    )
    has_control.wait(timeout=5.0)

    # Set neutral pose
    try:
        robot.behavior.set_head_angle(degrees(0))
        robot.behavior.set_lift_height(0.0)
        robot.motors.set_wheel_motors(0, 0)
    except Exception as e:
        logger.warning("Could not set neutral pose: %s", e)

    logger.info(
        "Vector is still and quiet. Wake word still works.\n"
        "  Press Ctrl-C to release control and exit."
    )

    # Handle graceful shutdown
    stop = False

    def _signal_handler(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Main loop: hold control, re-request if lost
    while not stop:
        try:
            time.sleep(0.5)
            if not has_control.is_set():
                logger.info("Re-requesting behavior control...")
                robot.conn.request_control(
                    behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                )
                has_control.wait(timeout=5.0)
                if has_control.is_set():
                    # Reset neutral pose after regaining control
                    try:
                        robot.behavior.set_head_angle(degrees(0))
                        robot.behavior.set_lift_height(0.0)
                        robot.motors.set_wheel_motors(0, 0)
                    except Exception:
                        pass
        except KeyboardInterrupt:
            break

    logger.info("Releasing behavior control...")
    try:
        robot.conn.release_control()
    except Exception:
        pass
    robot.disconnect()
    logger.info("Disconnected. Vector will resume autonomous behavior.")


if __name__ == "__main__":
    main()
