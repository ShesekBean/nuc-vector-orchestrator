#!/usr/bin/env python3
"""Hold Vector in quiet mode — still and silent, but wake word still works.

Connects to Vector via SDK and holds behavior control indefinitely at
OVERRIDE_BEHAVIORS_PRIORITY. Periodically re-requests control in case
vic-engine took it for a wake word interaction.

Usage:
    python3 scripts/vector-quiet-mode.py

Stop with Ctrl-C to release control (Vector resumes autonomous behavior).
"""

from __future__ import annotations

import logging
import os
import signal
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("vector-quiet-mode")

SERIAL = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")

# How often to re-assert control (seconds)
CONTROL_POLL_INTERVAL = 2.0


def main():
    import anki_vector
    from anki_vector.connection import ControlPriorityLevel
    from anki_vector.util import degrees

    logger.info("Connecting to Vector (serial=%s)...", SERIAL)
    robot = anki_vector.Robot(
        serial=SERIAL,
        default_logging=False,
        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    )
    robot.connect()
    logger.info("Connected with OVERRIDE_BEHAVIORS_PRIORITY.")

    # Set neutral pose
    def set_neutral():
        try:
            robot.behavior.set_head_angle(degrees(0))
            robot.behavior.set_lift_height(0.0)
            robot.motors.set_wheel_motors(0, 0)
        except Exception as e:
            logger.debug("Neutral pose: %s", e)

    set_neutral()

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

    # Main loop: periodically re-request control to reclaim after wake word
    last_control_request = time.monotonic()
    while not stop:
        try:
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_control_request >= CONTROL_POLL_INTERVAL:
                try:
                    robot.conn.request_control(
                        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                    )
                    set_neutral()
                except Exception as e:
                    logger.warning("Control re-request failed: %s", e)
                last_control_request = now
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
