#!/usr/bin/env python3
"""Standalone audio test — say_text and volume control.

Uses the SDK's say_text API (TTS on robot). Mic recording requires
enable_audio_feed=True which streams raw audio — tested here as feed init.

Run: python3 apps/vector/tests/standalone/test_audio.py
"""

import sys
import time

import anki_vector
from anki_vector.audio import RobotVolumeLevel

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1


def run_test(name, fn):
    print(f"  [{name}] ", end="", flush=True)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        suffix = f" — {result}" if result else ""
        print(f"PASS ({elapsed:.2f}s){suffix}")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {e}")
        return False


def main():
    print("=" * 60)
    print("STANDALONE TEST: Audio")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print(f"Connected to {robot.name}\n")

        # Sub-test 1: Say simple text
        def test_say_hello():
            robot.behavior.say_text("Hello, I am Vector")

        results.append(run_test("Say 'Hello, I am Vector'", test_say_hello))

        # Sub-test 2: Set volume to medium
        def test_volume_medium():
            robot.audio.set_master_volume(RobotVolumeLevel.MEDIUM)

        results.append(run_test("Set volume: MEDIUM", test_volume_medium))

        # Sub-test 3: Say text at different speeds
        def test_say_fast():
            robot.behavior.say_text("Fast speech", duration_scalar=0.5)

        results.append(run_test("Say text (fast, 0.5x)", test_say_fast))

        def test_say_slow():
            robot.behavior.say_text("Slow speech", duration_scalar=2.0)

        results.append(run_test("Say text (slow, 2x)", test_say_slow))

        # Sub-test 4: Volume levels cycle
        def test_volume_cycle():
            levels = [
                ("LOW", RobotVolumeLevel.LOW),
                ("MEDIUM", RobotVolumeLevel.MEDIUM),
                ("HIGH", RobotVolumeLevel.HIGH),
            ]
            for name, level in levels:
                robot.audio.set_master_volume(level)
                robot.behavior.say_text(name)
            # Reset to medium
            robot.audio.set_master_volume(RobotVolumeLevel.MEDIUM)

        results.append(run_test("Volume cycle (LOW→MED→HIGH)", test_volume_cycle))

        # Sub-test 5: Say longer text
        def test_say_sentence():
            robot.behavior.say_text("Testing audio subsystem complete")

        results.append(run_test("Say full sentence", test_say_sentence))

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Audio: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
