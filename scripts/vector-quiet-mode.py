#!/usr/bin/env python3
"""Hold Vector in quiet mode — still and silent, but wake word still works.

Connects to Vector via SDK and holds behavior control indefinitely.
This suppresses all autonomous behaviors (exploring, reacting to sounds,
idle animations) while keeping wake word detection active (it runs at
a lower level in vic-engine).

When Vector hears the wake word, vic-engine temporarily takes back control
for the voice interaction, then returns control to the SDK afterward.

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


def main():
    import anki_vector
    from anki_vector.util import degrees

    from anki_vector.connection import ControlPriorityLevel

    logger.info("Connecting to Vector (serial=%s)...", SERIAL)
    robot = anki_vector.Robot(
        serial=SERIAL,
        default_logging=False,
        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    )
    robot.connect()
    logger.info("Connected with OVERRIDE_BEHAVIORS_PRIORITY. Suppressing all autonomous behaviors.")

    # Set neutral pose: head level, lift down, stop motors
    try:
        robot.behavior.set_head_angle(degrees(0))
        robot.behavior.set_lift_height(0.0)
        robot.motors.set_wheel_motors(0, 0)
    except Exception as e:
        logger.warning("Could not set neutral pose: %s", e)

    # Set volume to minimum to reduce noise
    try:
        from anki_vector.messaging import protocol
        robot.conn.grpc_interface.SetMasterVolume(
            protocol.MasterVolumeRequest(volume_level=protocol.Volume.Value("LOW"))
        )
        logger.info("Volume set to LOW")
    except Exception as e:
        logger.warning("Could not set volume: %s", e)

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

    # Hold control forever — just keep the connection alive
    while not stop:
        try:
            time.sleep(1)
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
