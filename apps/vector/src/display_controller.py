"""Face display controller for Vector's 160×80 OLED screen.

Renders procedural face expressions and status overlays at native 160×80,
embeds into a 184×96 SDK frame, then pushes to Vector via
``convert_image_to_screen_data`` + ``set_screen_with_image_data``.
vic-engine converts stride 184→160 automatically for Xray hardware.

Usage::

    from apps.vector.src.display_controller import DisplayController

    ctrl = DisplayController(robot, event_bus=bus)
    ctrl.start()           # background animation thread
    ctrl.set_expression("happy")
    ctrl.show_status(battery_pct=80, wifi_strength=3, detection_text="Ophir")
    ctrl.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Actual Vector 2.0 (Xray) screen resolution
SCREEN_W = 160
SCREEN_H = 80
# SDK requires 184x96 images; vic-engine converts stride 184→160 for Xray
SDK_W = 184
SDK_H = 96


# ---------------------------------------------------------------------------
# Expression definitions — eye/mouth geometry for each face
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EyeShape:
    """Defines one eye as an ellipse (cx, cy, rx, ry)."""
    cx: int
    cy: int
    rx: int
    ry: int


@dataclass(frozen=True)
class MouthShape:
    """Defines mouth as an arc or line: (x0, y0, x1, y1, start_angle, end_angle).

    For a straight line mouth, start==end==0. For a smile, start=0 end=180.
    For a frown, start=180 end=360.
    """
    x0: int
    y0: int
    x1: int
    y1: int
    start_angle: int
    end_angle: int


@dataclass(frozen=True)
class FaceDef:
    """Full face definition — two eyes + mouth."""
    left_eye: EyeShape
    right_eye: EyeShape
    mouth: MouthShape


# Eye positions (centred on 160×80 canvas)
_LEFT_EYE_CX = 54
_RIGHT_EYE_CX = 106
_EYE_CY = 30

EXPRESSIONS: dict[str, FaceDef] = {
    "idle": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 12, 13),
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 12, 13),
        mouth=MouthShape(63, 58, 97, 68, 0, 180),
    ),
    "happy": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 14, 15),
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 14, 15),
        mouth=MouthShape(54, 53, 106, 72, 0, 180),
    ),
    "sad": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY + 3, 10, 8),
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY + 3, 10, 8),
        mouth=MouthShape(63, 60, 97, 72, 180, 360),
    ),
    "thinking": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 12, 7),   # squinted
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 12, 13),
        mouth=MouthShape(70, 63, 90, 68, 0, 0),  # flat line
    ),
    "listening": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 14, 17),  # wide
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 14, 17),
        mouth=MouthShape(71, 60, 89, 67, 0, 180),  # small 'o'
    ),
    "speaking": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 12, 13),
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 12, 13),
        mouth=MouthShape(63, 55, 97, 72, 0, 180),  # open wide
    ),
    "curious": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY - 2, 12, 17),  # raised
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY + 2, 10, 12),  # normal
        mouth=MouthShape(70, 62, 90, 68, 0, 180),  # small smile
    ),
    "excited": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY, 16, 18),  # extra wide
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY, 16, 18),
        mouth=MouthShape(50, 52, 110, 75, 0, 180),  # big open smile
    ),
    "sleepy": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY + 3, 12, 5),  # half closed
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY + 3, 12, 5),
        mouth=MouthShape(70, 63, 90, 68, 0, 0),  # flat line
    ),
    "startled": FaceDef(
        left_eye=EyeShape(_LEFT_EYE_CX, _EYE_CY - 3, 16, 20),  # very wide
        right_eye=EyeShape(_RIGHT_EYE_CX, _EYE_CY - 3, 16, 20),
        mouth=MouthShape(71, 58, 89, 70, 0, 180),  # small O
    ),
}


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _draw_eye(draw: Any, eye: EyeShape, color: tuple[int, int, int] = (0, 200, 255)) -> None:
    """Draw a filled ellipse for one eye."""
    bbox = [eye.cx - eye.rx, eye.cy - eye.ry, eye.cx + eye.rx, eye.cy + eye.ry]
    draw.ellipse(bbox, fill=color)


def _draw_mouth(draw: Any, mouth: MouthShape, color: tuple[int, int, int] = (0, 200, 255)) -> None:
    """Draw the mouth — arc for smile/frown, line for neutral."""
    bbox = [mouth.x0, mouth.y0, mouth.x1, mouth.y1]
    if mouth.start_angle == 0 and mouth.end_angle == 0:
        # Flat line
        mid_y = (mouth.y0 + mouth.y1) // 2
        draw.line([(mouth.x0, mid_y), (mouth.x1, mid_y)], fill=color, width=2)
    else:
        draw.arc(bbox, start=mouth.start_angle, end=mouth.end_angle, fill=color, width=2)


def render_face(expression: str, frame_num: int = 0) -> Any:
    """Render a face expression to a PIL Image (160×80 RGB).

    *frame_num* is used for animation effects (blink, speaking mouth cycle).
    Returns a PIL.Image.Image at native 160×80 resolution.
    """
    # Lazy import — PIL may not be available in all environments
    from PIL import Image, ImageDraw  # noqa: F811

    face = EXPRESSIONS.get(expression, EXPRESSIONS["idle"])
    img = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blink animation: every 60th frame, squish eyes for 2 frames
    blink = (frame_num % 60) in (0, 1)

    left_eye = face.left_eye
    right_eye = face.right_eye

    if blink and expression in ("idle", "happy", "listening", "curious", "excited"):
        left_eye = EyeShape(left_eye.cx, left_eye.cy, left_eye.rx, 3)
        right_eye = EyeShape(right_eye.cx, right_eye.cy, right_eye.rx, 3)

    _draw_eye(draw, left_eye)
    _draw_eye(draw, right_eye)

    mouth = face.mouth
    # Speaking animation: oscillate mouth height
    if expression == "speaking":
        cycle = frame_num % 8
        stretch = [0, 4, 8, 10, 8, 4, 0, -2][cycle]
        mouth = MouthShape(mouth.x0, mouth.y0 - stretch // 2,
                           mouth.x1, mouth.y1 + stretch // 2,
                           mouth.start_angle, mouth.end_angle)

    # Thinking animation: bouncing dots instead of normal mouth
    if expression == "thinking":
        _draw_mouth(draw, mouth)
        dot_y = 65
        for i in range(3):
            offset = (frame_num + i * 3) % 9
            bounce = abs(offset - 4)
            dx = 68 + i * 12
            draw.ellipse([dx - 2, dot_y - bounce - 2, dx + 2, dot_y - bounce + 2],
                         fill=(0, 200, 255))
    else:
        _draw_mouth(draw, mouth)

    return img


def render_status(battery_pct: int = -1,
                  wifi_strength: int = -1,
                  detection_text: str = "") -> Any:
    """Render a status overlay screen (160×80 RGB PIL Image).

    Parameters
    ----------
    battery_pct : int
        Battery percentage 0-100, or -1 to hide.
    wifi_strength : int
        WiFi signal bars 0-4, or -1 to hide.
    detection_text : str
        Detection status text to display (e.g. person name).
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = 4

    # Battery bar
    if battery_pct >= 0:
        clamped = max(0, min(100, battery_pct))
        bar_w = 60
        fill_w = int(bar_w * clamped / 100)
        color = (0, 255, 0) if clamped > 30 else (255, 165, 0) if clamped > 10 else (255, 0, 0)
        draw.rectangle([4, y, 4 + bar_w, y + 10], outline=(100, 100, 100))
        draw.rectangle([4, y, 4 + fill_w, y + 10], fill=color)
        draw.text((bar_w + 10, y), f"{clamped}%", fill=(200, 200, 200))
        y += 18

    # WiFi bars
    if wifi_strength >= 0:
        bars = max(0, min(4, wifi_strength))
        bx = 4
        for i in range(4):
            h = 4 + i * 3
            bar_color = (0, 200, 255) if i < bars else (60, 60, 60)
            draw.rectangle([bx + i * 8, y + (16 - h), bx + i * 8 + 5, y + 16],
                           fill=bar_color)
        draw.text((42, y + 2), "WiFi", fill=(200, 200, 200))
        y += 24

    # Detection text
    if detection_text:
        draw.text((4, y), detection_text, fill=(0, 255, 0))

    return img


# ---------------------------------------------------------------------------
# DisplayController — manages OLED rendering + animation loop
# ---------------------------------------------------------------------------

class DisplayController:
    """Manages Vector's 160×80 OLED face display.

    Parameters
    ----------
    robot : anki_vector.Robot
        Connected Vector robot instance.
    event_bus : NucEventBus | None
        If provided, auto-subscribes to events that drive expression changes.
    fps : int
        Target animation frame rate (default 10 — smooth enough, low CPU).
    """

    def __init__(self, robot: Any, event_bus: Any = None, fps: int = 10) -> None:
        self._robot = robot
        self._bus = event_bus
        self._fps = max(1, min(30, fps))
        self._frame_interval = 1.0 / self._fps

        self._expression = "idle"
        self._mode = "face"  # "face", "status", or "image"
        self._status_args: dict[str, Any] = {}
        self._held_image: Any = None  # PIL Image held in "image" mode
        self._image_duration: float = 0.0  # how long to hold image (0 = until changed)
        self._image_start: float = 0.0
        self._frame_num = 0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Event bus subscriptions
        self._subscriptions: list[tuple[str, Any]] = []
        if event_bus is not None:
            self._subscribe_events(event_bus)

    # --- Public API -----------------------------------------------------------

    def set_expression(self, name: str) -> None:
        """Switch to a named face expression.

        Valid names: idle, happy, sad, thinking, listening, speaking.
        Unknown names fall back to 'idle'.
        """
        if name not in EXPRESSIONS:
            logger.warning("Unknown expression %r, falling back to idle", name)
            name = "idle"
        with self._lock:
            self._expression = name
            self._mode = "face"
            self._held_image = None
            self._frame_num = 0

    def show_status(self, battery_pct: int = -1, wifi_strength: int = -1,
                    detection_text: str = "") -> None:
        """Switch to status overlay mode."""
        with self._lock:
            self._mode = "status"
            self._held_image = None
            self._status_args = {
                "battery_pct": battery_pct,
                "wifi_strength": wifi_strength,
                "detection_text": detection_text,
            }

    def show_image(self, pil_image: Any, duration: float = 0.0) -> None:
        """Display an arbitrary PIL Image on Vector's OLED.

        Switches to "image" mode which suppresses the eye animation loop.
        The image is resized to 160×80 (native) and embedded into a 184×96 SDK frame.

        Parameters
        ----------
        pil_image : PIL.Image.Image
            Image to display.
        duration : float
            How long to hold the image in seconds. 0 means hold until
            another mode is set (e.g. set_expression or show_status).
        """
        with self._lock:
            self._mode = "image"
            self._held_image = pil_image
            self._image_duration = duration
            self._image_start = time.monotonic()
        self._send_to_screen(pil_image, duration_sec=max(duration, 10.0))

    @property
    def expression(self) -> str:
        """Current expression name."""
        with self._lock:
            return self._expression

    @property
    def mode(self) -> str:
        """Current display mode ('face' or 'status')."""
        with self._lock:
            return self._mode

    def start(self) -> None:
        """Start the background animation thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Animation thread already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._animation_loop,
                                        name="display-anim", daemon=True)
        self._thread.start()
        logger.info("Display animation started at %d fps", self._fps)

    def stop(self) -> None:
        """Stop the animation thread and unsubscribe from events."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        # Unsubscribe from event bus
        if self._bus is not None:
            for event_name, cb in self._subscriptions:
                self._bus.off(event_name, cb)
            self._subscriptions.clear()

        logger.info("Display animation stopped")

    # --- Internal -------------------------------------------------------------

    def _animation_loop(self) -> None:
        """Background loop: render current expression/status and push to OLED."""
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                with self._lock:
                    mode = self._mode
                    expr = self._expression
                    frame = self._frame_num
                    status_args = self._status_args.copy()
                    held_image = self._held_image
                    image_duration = self._image_duration
                    image_start = self._image_start
                    self._frame_num += 1

                if mode == "image":
                    # Check if timed image has expired
                    if image_duration > 0 and (t0 - image_start) >= image_duration:
                        with self._lock:
                            self._mode = "face"
                            self._held_image = None
                        img = render_face(expr, frame)
                    elif held_image is not None:
                        # Re-send held image to keep it on screen
                        img = held_image
                    else:
                        img = render_face(expr, frame)
                elif mode == "face":
                    img = render_face(expr, frame)
                else:
                    img = render_status(**status_args)

                self._send_to_screen(img)
            except Exception:
                logger.exception("Display render error")

            elapsed = time.monotonic() - t0
            sleep_time = self._frame_interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def _send_to_screen(self, pil_image: Any, duration_sec: float = 0.0) -> None:
        """Convert PIL image and push to Vector's OLED.

        Parameters
        ----------
        duration_sec : float
            Duration hint for vic-anim's EnableKeepFaceAlive. If 0, uses
            frame_interval + 0.5s (suitable for animation). For held images,
            pass a longer duration to suppress eye animations.
        """
        try:
            from anki_vector.screen import convert_image_to_screen_data
        except ImportError:
            logger.error("anki_vector.screen not available — cannot display")
            return

        from PIL import Image as PILImage

        # Resize content to native 160×80 if needed
        if pil_image.size != (SCREEN_W, SCREEN_H):
            pil_image = pil_image.resize((SCREEN_W, SCREEN_H), PILImage.LANCZOS)

        # Ensure RGB mode
        if pil_image.mode != "RGB":
            pil_image = pil_image.convert("RGB")

        # Embed 160×80 content into 184×96 SDK frame (top-left aligned).
        # vic-engine reads 160 pixels from each 184-pixel row for Xray hardware.
        sdk_frame = PILImage.new("RGB", (SDK_W, SDK_H), (0, 0, 0))
        sdk_frame.paste(pil_image, (0, 0))
        pil_image = sdk_frame

        if duration_sec <= 0:
            duration_sec = self._frame_interval + 0.5

        screen_data = convert_image_to_screen_data(pil_image)
        self._robot.screen.set_screen_with_image_data(
            screen_data, duration_sec=duration_sec
        )

    def _subscribe_events(self, bus: Any) -> None:
        """Subscribe to NucEventBus events that drive expression changes."""
        from apps.vector.src.events.event_types import (
            FACE_RECOGNIZED,
            FOLLOW_STATE_CHANGED,
            TOUCH_DETECTED,
            TTS_PLAYING,
            COMMAND_RECEIVED,
            STT_RESULT,
        )

        handlers = [
            (FACE_RECOGNIZED, self._on_face_recognized),
            (FOLLOW_STATE_CHANGED, self._on_follow_state),
            (TOUCH_DETECTED, self._on_touch),
            (TTS_PLAYING, self._on_tts),
            (COMMAND_RECEIVED, self._on_command),
            (STT_RESULT, self._on_stt),
        ]
        for event_name, handler in handlers:
            bus.on(event_name, handler)
            self._subscriptions.append((event_name, handler))

    # --- Event handlers -------------------------------------------------------

    def _on_face_recognized(self, event: Any) -> None:
        self.set_expression("happy")

    def _on_follow_state(self, event: Any) -> None:
        state = getattr(event, "state", "idle")
        mapping = {
            "idle": "idle",
            "searching": "thinking",
            "tracking": "listening",
            "following": "happy",
        }
        self.set_expression(mapping.get(state, "idle"))

    def _on_touch(self, event: Any) -> None:
        is_pressed = getattr(event, "is_pressed", True)
        if is_pressed:
            self.set_expression("happy")

    def _on_tts(self, event: Any) -> None:
        playing = getattr(event, "playing", False)
        self.set_expression("speaking" if playing else "idle")

    def _on_command(self, event: Any) -> None:
        self.set_expression("thinking")

    def _on_stt(self, event: Any) -> None:
        self.set_expression("listening")
