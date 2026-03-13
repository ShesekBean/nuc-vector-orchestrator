#!/usr/bin/env python3
"""Hold Vector in quiet mode — still and silent, but wake word still works.

Connects to Vector via SDK and holds behavior control indefinitely at
OVERRIDE_BEHAVIORS_PRIORITY. When a touch (button tap) is detected,
releases control for TOUCH_RELEASE_DURATION seconds so vic-engine can
handle the wake word flow, then re-acquires control.

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

# How often to re-assert control (seconds) — only when not in touch-release window
CONTROL_POLL_INTERVAL = 2.0

# How long to release control after a touch event (seconds).
# Gives vic-engine time to handle wake word → STT → intent → TTS response.
TOUCH_RELEASE_DURATION = 15.0


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
        "  Touch (back tap) releases control for %ds to allow wake word flow.\n"
        "  Press Ctrl-C to release control and exit.",
        int(TOUCH_RELEASE_DURATION),
    )

    # Handle graceful shutdown
    stop = False

    def _signal_handler(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Track touch state for edge detection
    was_touched = False
    # When non-zero, we're in the touch-release window (control released)
    touch_release_until = 0.0
    has_control = True

    last_control_request = time.monotonic()
    while not stop:
        try:
            time.sleep(0.25)
            now = time.monotonic()

            # Check touch sensor for button wake word
            try:
                is_touched = robot.status.is_button_pressed
            except Exception:
                is_touched = False

            # Edge detection: touch just started
            if is_touched and not was_touched:
                logger.info("Touch detected! Releasing control for %ds for wake word flow.", int(TOUCH_RELEASE_DURATION))
                try:
                    robot.conn.release_control()
                    has_control = False
                except Exception as e:
                    logger.warning("Release control failed: %s", e)
                touch_release_until = now + TOUCH_RELEASE_DURATION

            was_touched = is_touched

            # In touch-release window — don't re-acquire control
            if touch_release_until > 0 and now < touch_release_until:
                continue

            # Touch-release window expired — re-acquire control
            if touch_release_until > 0 and now >= touch_release_until:
                logger.info("Touch-release window expired. Re-acquiring control.")
                touch_release_until = 0.0
                try:
                    robot.conn.request_control(
                        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                    )
                    has_control = True
                    set_neutral()
                except Exception as e:
                    logger.warning("Re-acquire control failed: %s", e)
                last_control_request = now
                continue

            # Normal polling: periodically re-request control
            if now - last_control_request >= CONTROL_POLL_INTERVAL:
                try:
                    robot.conn.request_control(
                        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                    )
                    has_control = True
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
