#!/usr/bin/env python3
"""Standalone display test — draw patterns on Vector's 160×80 OLED.

Vector 2.0 (Xray) has a 160×80 screen. The SDK requires 184×96 images;
vic-engine converts stride 184→160 automatically. We render content at
160×80 and embed into a 184×96 frame for the SDK.

Run: python3 apps/vector/tests/standalone/test_display.py
"""

import sys
import time

import anki_vector
from anki_vector.connection import ControlPriorityLevel
from anki_vector.screen import convert_image_to_screen_data

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("FAIL — PIL/Pillow not installed (pip install Pillow)")
    sys.exit(1)

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1

# Actual Vector 2.0 (Xray) screen resolution
SCREEN_W = 160
SCREEN_H = 80
# SDK requires 184x96; vic-engine handles stride conversion
SDK_W = 184
SDK_H = 96


def to_sdk_frame(content: Image.Image) -> Image.Image:
    """Embed 160×80 content into 184×96 SDK frame.

    vic-engine reads 160 pixels per row from the 184-wide data,
    so content at columns 0-159 of each row maps correctly.
    """
    if content.size != (SCREEN_W, SCREEN_H):
        content = content.resize((SCREEN_W, SCREEN_H), Image.LANCZOS)
    frame = Image.new("RGB", (SDK_W, SDK_H), (0, 0, 0))
    frame.paste(content, (0, 0))
    return frame


def run_test(name, fn):
    print(f"  [{name}] ", end="", flush=True)
    t0 = time.time()
    try:
        fn()
        elapsed = time.time() - t0
        print(f"PASS ({elapsed:.2f}s)")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {e}")
        return False


def display_image(robot, content, duration=2.0):
    """Convert 160×80 content to SDK frame and display continuously."""
    frame = to_sdk_frame(content)
    screen_data = convert_image_to_screen_data(frame)
    end = time.monotonic() + duration
    while time.monotonic() < end:
        robot.screen.set_screen_with_image_data(screen_data, duration_sec=duration)
        time.sleep(0.1)


def main():
    print("=" * 60)
    print("STANDALONE TEST: Display (160×80 OLED, Vector 2.0 Xray)")
    print("=" * 60)

    robot = anki_vector.Robot(
        serial=SERIAL,
        default_logging=False,
        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    )
    results = []

    try:
        robot.connect()
        print("Connected to Vector (behavior override)\n")

        # Sub-test 1: Solid white
        def test_solid_white():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H), (255, 255, 255))
            display_image(robot, img)

        results.append(run_test("Solid white", test_solid_white))

        # Sub-test 2: Solid red
        def test_solid_red():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H), (255, 0, 0))
            display_image(robot, img)

        results.append(run_test("Solid red", test_solid_red))

        # Sub-test 3: Color bars (vertical stripes)
        def test_color_bars():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H))
            draw = ImageDraw.Draw(img)
            colors = [
                (255, 0, 0), (0, 255, 0), (0, 0, 255),
                (255, 255, 0), (0, 255, 255), (255, 0, 255),
            ]
            bar_w = SCREEN_W // len(colors)
            for i, color in enumerate(colors):
                draw.rectangle(
                    [i * bar_w, 0, (i + 1) * bar_w, SCREEN_H],
                    fill=color,
                )
            display_image(robot, img)

        results.append(run_test("Color bars (6 stripes)", test_color_bars))

        # Sub-test 4: Checkerboard
        def test_checkerboard():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H))
            draw = ImageDraw.Draw(img)
            cell = 10
            for y in range(0, SCREEN_H, cell):
                for x in range(0, SCREEN_W, cell):
                    color = (255, 255, 255) if (x // cell + y // cell) % 2 == 0 else (0, 0, 0)
                    draw.rectangle([x, y, x + cell, y + cell], fill=color)
            display_image(robot, img)

        results.append(run_test("Checkerboard pattern", test_checkerboard))

        # Sub-test 5: Diagonal gradient
        def test_gradient():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H))
            for y in range(SCREEN_H):
                for x in range(SCREEN_W):
                    v = int(255 * (x + y) / (SCREEN_W + SCREEN_H))
                    img.putpixel((x, y), (v, v, v))
            display_image(robot, img)

        results.append(run_test("Diagonal gradient", test_gradient))

        # Sub-test 6: Text display
        def test_text():
            img = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((10, 15), "VECTOR", fill=(0, 255, 0))
            draw.text((10, 40), "TEST OK", fill=(255, 255, 255))
            display_image(robot, img, duration=3.0)

        results.append(run_test("Text display", test_text))

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Display: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
