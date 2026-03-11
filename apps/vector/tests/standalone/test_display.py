#!/usr/bin/env python3
"""Standalone display test — draw patterns on Vector's 184x96 OLED.

Run: python3 apps/vector/tests/standalone/test_display.py
"""

import sys
import time

import anki_vector
from anki_vector.screen import convert_image_to_screen_data

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("FAIL — PIL/Pillow not installed (pip install Pillow)")
    sys.exit(1)

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1

SCREEN_W = 184
SCREEN_H = 96


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


def display_image(robot, img, duration=1.5):
    """Convert PIL image to screen data and display it."""
    screen_data = convert_image_to_screen_data(img)
    robot.screen.set_screen_with_image_data(screen_data, duration_sec=duration)
    time.sleep(duration)


def main():
    print("=" * 60)
    print("STANDALONE TEST: Display (184x96 OLED)")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print(f"Connected to {robot.name}\n")

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
            cell = 12
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
            draw.text((10, 20), "VECTOR", fill=(0, 255, 0))
            draw.text((10, 50), "TEST OK", fill=(255, 255, 255))
            display_image(robot, img, duration=2.0)

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
