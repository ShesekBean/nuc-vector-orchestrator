"""DisplayChannel — video output to Vector's 160x80 OLED.

Push-based channel for displaying images on Vector's screen.
Handles the 184x96 SDK stride conversion automatically.

Usage::

    display = DisplayChannel(robot)
    display.start()
    display.show_image(pil_image, duration=2.0)
    display.show_text("Hello!", duration=3.0)
    display.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from apps.vector.src.media.channel import MediaChannel

logger = logging.getLogger(__name__)

# Vector 2.0 (Xray) physical display
DISPLAY_WIDTH = 160
DISPLAY_HEIGHT = 80

# SDK frame dimensions (vic-engine does stride conversion)
SDK_WIDTH = 184
SDK_HEIGHT = 96


class DisplayChannel(MediaChannel):
    """Video output channel for Vector's OLED display.

    Args:
        robot: Connected ``anki_vector.Robot`` instance.
    """

    def __init__(self, robot: Any) -> None:
        super().__init__("display", ring_size=5)
        self._robot = robot
        self._lock_display = threading.Lock()

    def start(self) -> None:
        """Mark channel as available."""
        if self._running:
            return
        super().start()
        logger.info("DisplayChannel started")

    def stop(self) -> None:
        """Mark channel as stopped."""
        super().stop()
        logger.info("DisplayChannel stopped")

    def show_image(self, image: Any, duration: float = 1.0) -> None:
        """Display a PIL Image on Vector's OLED.

        The image is resized to fit 160x80 and embedded in the 184x96
        SDK frame automatically.

        Args:
            image: PIL Image (any mode — will be converted to RGB).
            duration: How long to display (seconds).
        """
        if not self._running:
            logger.warning("DisplayChannel not started")
            return

        from PIL import Image as PILImage
        from anki_vector.screen import convert_image_to_screen_data

        with self._lock_display:
            try:
                img = image.convert("RGB")

                # Resize to fit 160x80 preserving aspect ratio
                img_w, img_h = img.size
                scale = min(DISPLAY_WIDTH / img_w, DISPLAY_HEIGHT / img_h)
                new_w = int(img_w * scale)
                new_h = int(img_h * scale)
                resized = img.resize((new_w, new_h), PILImage.LANCZOS)

                # Center on 160x80 canvas
                display = PILImage.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (0, 0, 0))
                offset_x = (DISPLAY_WIDTH - new_w) // 2
                offset_y = (DISPLAY_HEIGHT - new_h) // 2
                display.paste(resized, (offset_x, offset_y))

                # Embed into 184x96 SDK frame
                sdk_frame = PILImage.new("RGB", (SDK_WIDTH, SDK_HEIGHT), (0, 0, 0))
                sdk_frame.paste(display, (0, 0))

                screen_data = convert_image_to_screen_data(sdk_frame)
                self._robot.screen.set_screen_with_image_data(
                    screen_data, duration,
                )

                self._chunk_count += 1
                logger.debug("Displayed image on OLED (%.1fs)", duration)

            except Exception:
                logger.warning("show_image failed", exc_info=True)

    def show_text(
        self,
        text: str,
        duration: float = 2.0,
        font_size: int = 16,
    ) -> None:
        """Render text and display on Vector's OLED.

        Args:
            text: Text to display.
            duration: How long to display (seconds).
            font_size: Font size in pixels.
        """
        if not self._running:
            logger.warning("DisplayChannel not started")
            return

        from PIL import Image as PILImage, ImageDraw, ImageFont

        img = PILImage.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        # Center text
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (DISPLAY_WIDTH - tw) // 2
        y = (DISPLAY_HEIGHT - th) // 2
        draw.text((x, y), text, fill=(255, 255, 255), font=font)

        self.show_image(img, duration)

    def get_status(self) -> dict:
        status = super().get_status()
        status.update({
            "display_width": DISPLAY_WIDTH,
            "display_height": DISPLAY_HEIGHT,
        })
        return status
