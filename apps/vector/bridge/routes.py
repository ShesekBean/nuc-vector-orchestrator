"""HTTP route handlers for the Vector bridge server.

Each handler translates an HTTP request into one or more controller calls
and returns a JSON response.  All Vector SDK calls are synchronous and run
in the default executor to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shlex
import tempfile
import threading
import time
from typing import Any, TYPE_CHECKING

import numpy as np

from aiohttp import web

if TYPE_CHECKING:
    from apps.vector.bridge.connection import ConnectionManager

logger = logging.getLogger(__name__)


def _json_error(status: int, message: str, code: str = "ERROR") -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": message, "code": code}, status=status)


def _require_connected(conn: ConnectionManager) -> web.Response | None:
    """Return a 503 error if not connected, else None."""
    if not conn.is_connected:
        return _json_error(503, "Vector is offline", "VECTOR_OFFLINE")
    return None


async def _run_sync(func, *args):
    """Run a synchronous function in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health(request: web.Request) -> web.Response:
    """GET /health — battery state + connectivity check."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        t0 = time.monotonic()
        battery = await _run_sync(conn.get_battery_state)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return web.json_response({
            "status": "healthy",
            "battery": battery,
            "latency_ms": latency_ms,
        })
    except Exception as exc:
        logger.exception("Health check failed")
        return _json_error(500, str(exc), "HEALTH_CHECK_FAILED")


async def move(request: web.Request) -> web.Response:
    """POST /move — motor control (drive_wheels, drive_straight, turn_in_place).

    Body options:
      {"type": "wheels", "left_speed": 100, "right_speed": 100}
      {"type": "straight", "distance_mm": 200, "speed_mmps": 100}
      {"type": "turn", "angle_deg": 90, "speed_dps": 100}
      {"type": "turn_then_drive", "angle_deg": 45, "distance_mm": 200}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    move_type = body.get("type", "wheels")
    mc = conn.motor_controller

    try:
        if move_type == "wheels":
            left = float(body.get("left_speed", 0))
            right = float(body.get("right_speed", 0))
            left_accel = float(body.get("left_accel", 200))
            right_accel = float(body.get("right_accel", 200))
            await _run_sync(mc.drive_wheels, left, right, left_accel, right_accel)
        elif move_type == "straight":
            dist = float(body.get("distance_mm", 0))
            speed = float(body.get("speed_mmps", 200))
            await _run_sync(mc.drive_straight, dist, speed)
        elif move_type == "turn":
            angle = float(body.get("angle_deg", 0))
            speed = float(body.get("speed_dps", 100))
            await _run_sync(mc.turn_in_place, angle, speed)
        elif move_type == "turn_then_drive":
            angle = float(body.get("angle_deg", 0))
            dist = float(body.get("distance_mm", 0))
            await _run_sync(mc.turn_then_drive, angle, dist)
        else:
            return _json_error(400, f"Unknown move type: {move_type}", "INVALID_MOVE_TYPE")

        return web.json_response({"status": "ok", "type": move_type})
    except Exception as exc:
        logger.exception("Move command failed")
        return _json_error(500, str(exc), "MOVE_FAILED")


async def stop(request: web.Request) -> web.Response:
    """POST /stop — emergency stop (zero all motors)."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body.get("clear"):
            await _run_sync(conn.motor_controller.clear_stop)
            return web.json_response({"status": "ok", "cleared": True})
        await _run_sync(conn.motor_controller.emergency_stop)
        return web.json_response({"status": "ok"})
    except Exception as exc:
        logger.exception("Emergency stop failed")
        return _json_error(500, str(exc), "STOP_FAILED")


async def head(request: web.Request) -> web.Response:
    """POST /head — set head angle.

    Body: {"angle_deg": 20, "speed_dps": 120}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    angle = float(body.get("angle_deg", 10))
    speed = body.get("speed_dps")
    speed = float(speed) if speed is not None else None

    try:
        actual = await _run_sync(conn.head_controller.set_angle, angle, speed)
        return web.json_response({"status": "ok", "angle_deg": actual})
    except Exception as exc:
        logger.exception("Head command failed")
        return _json_error(500, str(exc), "HEAD_FAILED")


async def lift(request: web.Request) -> web.Response:
    """POST /lift — set lift height.

    Body: {"height": 0.5}  or  {"preset": "carry"}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    try:
        preset = body.get("preset")
        if preset:
            ok = await _run_sync(conn.lift_controller.move_to_preset, preset)
        else:
            height = float(body.get("height", 0.0))
            ok = await _run_sync(conn.lift_controller.move_to, height)

        if ok:
            return web.json_response({"status": "ok"})
        return _json_error(500, "Lift command blocked (emergency stop?)", "LIFT_BLOCKED")
    except ValueError as exc:
        return _json_error(400, str(exc), "INVALID_PRESET")
    except Exception as exc:
        logger.exception("Lift command failed")
        return _json_error(500, str(exc), "LIFT_FAILED")


async def led(request: web.Request) -> web.Response:
    """POST /led — set LED state or override.

    Body: {"state": "person_detected"}
      or  {"hue": 0.5, "saturation": 1.0, "duration_s": 5.0}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    try:
        state = body.get("state")
        if state:
            await _run_sync(conn.led_controller.set_state, state)
            return web.json_response({"status": "ok", "state": state})

        hue = body.get("hue")
        if hue is not None:
            sat = float(body.get("saturation", 1.0))
            dur = body.get("duration_s")
            dur = float(dur) if dur is not None else None
            await _run_sync(conn.led_controller.override, float(hue), sat, dur)
            return web.json_response({"status": "ok", "mode": "override"})

        return _json_error(400, "Provide 'state' or 'hue'", "MISSING_PARAMS")
    except ValueError as exc:
        return _json_error(400, str(exc), "INVALID_LED_STATE")
    except Exception as exc:
        logger.exception("LED command failed")
        return _json_error(500, str(exc), "LED_FAILED")


async def capture(request: web.Request) -> web.Response:
    """GET /capture — capture a single camera frame.

    Returns JPEG image with Content-Type: image/jpeg.
    Query param ?format=base64 returns JSON with base64-encoded image.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        def _capture_frame() -> bytes:
            # Try camera client buffer first (works when feed is active)
            jpeg = conn.camera_client.get_latest_jpeg()
            if jpeg:
                return jpeg
            # Fallback to single capture (only works when feed is NOT active)
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera not producing frames — try again in a few seconds")
            import io
            buf = io.BytesIO()
            image.raw_image.save(buf, format="JPEG")
            return buf.getvalue()

        jpeg_bytes = await _run_sync(_capture_frame)

        fmt = request.query.get("format", "raw")
        if fmt == "base64":
            encoded = base64.b64encode(jpeg_bytes).decode("ascii")
            return web.json_response({
                "status": "ok",
                "image": encoded,
                "content_type": "image/jpeg",
                "size_bytes": len(jpeg_bytes),
            })

        return web.Response(body=jpeg_bytes, content_type="image/jpeg")
    except Exception as exc:
        logger.exception("Camera capture failed")
        return _json_error(500, str(exc), "CAPTURE_FAILED")


_EXPRESSION_ANIMS = {
    "happy": "anim_greeting_happy_03",
    "sad": "anim_feedback_meanwords_01",
    "thinking": "anim_explorer_scan_short_04",
    "listening": "anim_voice_new_wake_word_01",
    "greeting": "anim_greeting_hello_02",
    "excited": "anim_reacttoblock_happydetermined_01",
    "surprised": "anim_meetvictor_lookface_timeout_01",
    "idle": "anim_observing_look_up_01",
}


async def display(request: web.Request) -> web.Response:
    """POST /display — play face expression animation.

    Body: {"expression": "happy"}
    Uses play_animation instead of DisplayFaceImageRGB (which crashes vic-anim).
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    expression = body.get("expression")
    if not expression:
        return _json_error(400, "Missing 'expression' field", "MISSING_PARAMS")

    anim_name = _EXPRESSION_ANIMS.get(expression)
    if not anim_name:
        return _json_error(400, f"Unknown expression '{expression}'. Valid: {list(_EXPRESSION_ANIMS.keys())}", "UNKNOWN_EXPRESSION")

    try:
        def _play():
            conn.robot.anim.play_animation(anim_name, loop_count=1)

        await _run_sync(_play)
        return web.json_response({"status": "ok", "expression": expression, "animation": anim_name})
    except Exception as exc:
        logger.exception("Display command failed")
        return _json_error(500, str(exc), "DISPLAY_FAILED")


async def status(request: web.Request) -> web.Response:
    """GET /status — full robot status (battery + sensors)."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        battery = await _run_sync(conn.get_battery_state)
        sensors = await _run_sync(conn.get_robot_state)
        return web.json_response({
            "status": "ok",
            "battery": battery,
            "sensors": sensors,
        })
    except Exception as exc:
        logger.exception("Status check failed")
        return _json_error(500, str(exc), "STATUS_FAILED")


async def follow_start(request: web.Request) -> web.Response:
    """POST /follow/start — start person following.

    Starts the full pipeline: YOLO detection → person following.
    First call loads the YOLO model (may take a few seconds).
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    pipeline = conn.follow_pipeline
    if pipeline is None:
        return _json_error(503, "Follow pipeline not initialised", "PIPELINE_UNAVAILABLE")

    if pipeline.is_active:
        return web.json_response({
            "status": "ok",
            "active": True,
            "state": pipeline.state,
            "message": "Already following",
        })

    try:
        await _run_sync(pipeline.start)
        return web.json_response({
            "status": "ok",
            "active": True,
            "state": pipeline.state,
        })
    except Exception as exc:
        logger.exception("Failed to start follow pipeline")
        return _json_error(500, str(exc), "FOLLOW_START_FAILED")


async def follow_stop(request: web.Request) -> web.Response:
    """POST /follow/stop — stop person following."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    pipeline = conn.follow_pipeline
    if pipeline is None:
        return _json_error(503, "Follow pipeline not initialised", "PIPELINE_UNAVAILABLE")

    if not pipeline.is_active:
        return web.json_response({
            "status": "ok",
            "active": False,
            "message": "Not currently following",
        })

    try:
        await _run_sync(pipeline.stop)
        return web.json_response({
            "status": "ok",
            "active": False,
        })
    except Exception as exc:
        logger.exception("Failed to stop follow pipeline")
        return _json_error(500, str(exc), "FOLLOW_STOP_FAILED")


async def follow_status(request: web.Request) -> web.Response:
    """GET /follow/status — follow pipeline diagnostics."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    pipeline = conn.follow_pipeline
    if pipeline is None:
        return web.json_response({"active": False})

    try:
        status_data = await _run_sync(pipeline.get_status)
        return web.json_response(status_data)
    except Exception as exc:
        logger.exception("Follow status check failed")
        return _json_error(500, str(exc), "FOLLOW_STATUS_FAILED")


async def audio_play(request: web.Request) -> web.Response:
    """POST /audio/play — play audio on Vector speaker.

    Body: {"text": "Hello world"} — uses say_text() TTS.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    text = body.get("text")
    if not text:
        return _json_error(400, "Missing 'text' field", "MISSING_PARAMS")

    try:
        await _run_sync(conn.robot.behavior.say_text, text)
        return web.json_response({"status": "ok", "text": text})
    except Exception as exc:
        logger.exception("Audio play failed")
        return _json_error(500, str(exc), "AUDIO_FAILED")


async def audio_status(request: web.Request) -> web.Response:
    """GET /audio/status — debug audio client + LiveKit audio state."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    ac = conn.audio_client
    bridge = conn.livekit_bridge
    result = {
        "audio_client": {
            "streaming": ac.is_streaming,
            "chunk_count": ac.chunk_count,
            "chunks_per_second": round(ac.chunks_per_second, 1),
            "buffer_len": len(ac.get_audio_buffer()),
            "latest_chunk_bytes": len(ac.get_latest_chunk()) if ac.get_latest_chunk() else 0,
        },
    }
    if bridge:
        result["livekit"] = {
            "active": bridge.is_active,
            "room": bridge.room_name,
        }
    return web.json_response(result)


async def call_start(request: web.Request) -> web.Response:
    """POST /call/start — start LiveKit video call.

    Body (optional): {"room": "robot-cam"}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    bridge = conn.livekit_bridge
    if bridge is None:
        return _json_error(503, "LiveKit bridge not initialised", "BRIDGE_UNAVAILABLE")

    if bridge.is_active:
        return web.json_response({
            "status": "ok",
            "active": True,
            "room": bridge.room_name,
            "message": "Session already active",
        })

    try:
        body = await request.json()
    except Exception:
        body = {}

    room = body.get("room", "robot-cam")

    try:
        await bridge.start(room=room)
        return web.json_response({
            "status": "ok",
            "active": True,
            "room": room,
        })
    except Exception as exc:
        logger.exception("Failed to start LiveKit call")
        return _json_error(500, str(exc), "CALL_START_FAILED")


async def call_stop(request: web.Request) -> web.Response:
    """POST /call/stop — stop LiveKit video call."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    bridge = conn.livekit_bridge
    if bridge is None:
        return _json_error(503, "LiveKit bridge not initialised", "BRIDGE_UNAVAILABLE")

    if not bridge.is_active:
        return web.json_response({
            "status": "ok",
            "active": False,
            "message": "No active session",
        })

    try:
        await bridge.stop()
        return web.json_response({
            "status": "ok",
            "active": False,
        })
    except Exception as exc:
        logger.exception("Failed to stop LiveKit call")
        return _json_error(500, str(exc), "CALL_STOP_FAILED")


async def call_join_url(request: web.Request) -> web.Response:
    """GET /call/join-url — get a LiveKit viewer join URL.

    Auto-starts the call if not already active.  Returns a meet.livekit.io
    URL with a fresh viewer token that the caller can open in a browser.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    bridge = conn.livekit_bridge
    if bridge is None:
        return _json_error(503, "LiveKit bridge not initialised", "BRIDGE_UNAVAILABLE")

    # Auto-start if not active
    if not bridge.is_active:
        try:
            await bridge.start(room="robot-cam")
        except Exception as exc:
            logger.exception("Failed to auto-start LiveKit call")
            return _json_error(500, str(exc), "CALL_START_FAILED")

    # Generate a viewer token
    try:
        from livekit import api as lk_api
        import os
        import time as _time

        api_key = os.environ.get("LIVEKIT_API_KEY")
        api_secret = os.environ.get("LIVEKIT_API_SECRET")
        livekit_url = os.environ.get("LIVEKIT_URL", "wss://robot-a1hmnzgn.livekit.cloud")

        if not api_key or not api_secret:
            return _json_error(500, "LIVEKIT_API_KEY/SECRET not set", "CONFIG_ERROR")

        token = lk_api.AccessToken(api_key=api_key, api_secret=api_secret)
        token.with_identity(f"viewer-{int(_time.time())}")
        token.with_grants(lk_api.VideoGrants(
            room_join=True,
            room=bridge.room_name,
            can_publish=True,
            can_subscribe=True,
        ))
        jwt = token.to_jwt()
        join_url = f"https://meet.livekit.io/custom?liveKitUrl={livekit_url}&token={jwt}"

        return web.json_response({
            "status": "ok",
            "room": bridge.room_name,
            "join_url": join_url,
        })
    except Exception as exc:
        logger.exception("Failed to generate join URL")
        return _json_error(500, str(exc), "TOKEN_ERROR")


# ---------------------------------------------------------------------------
# Display image/text/color helpers
# ---------------------------------------------------------------------------

# Actual Vector 2.0 (Xray) screen resolution
_DISPLAY_W = 160
_DISPLAY_H = 80
# SDK requires 184x96 images; vic-engine converts stride 184→160 for Xray
_SDK_W = 184
_SDK_H = 96

# Active display-hold threads — keyed by "display_hold"
_display_hold_threads: dict[str, threading.Event] = {}
_display_hold_lock = threading.Lock()


def _prepare_for_screen(pil_image: "Any") -> "Any":
    """Resize a PIL image to fit 160x80 (letterboxed) and embed in 184x96 SDK frame.

    Returns a 184x96 RGB PIL Image ready for convert_image_to_screen_data.
    """
    from PIL import Image as PILImage

    # Ensure RGB
    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")

    # Resize to fit 160x80 preserving aspect ratio with black letterbox
    img_w, img_h = pil_image.size
    scale = min(_DISPLAY_W / img_w, _DISPLAY_H / img_h)
    new_w = int(img_w * scale)
    new_h = int(img_h * scale)
    resized = pil_image.resize((new_w, new_h), PILImage.LANCZOS)

    # Create 160x80 black canvas and paste centered
    display = PILImage.new("RGB", (_DISPLAY_W, _DISPLAY_H), (0, 0, 0))
    offset_x = (_DISPLAY_W - new_w) // 2
    offset_y = (_DISPLAY_H - new_h) // 2
    display.paste(resized, (offset_x, offset_y))

    # Embed into 184x96 SDK frame (top-left, rest is black)
    sdk_frame = PILImage.new("RGB", (_SDK_W, _SDK_H), (0, 0, 0))
    sdk_frame.paste(display, (0, 0))
    return sdk_frame


def _send_image_to_screen(robot: "Any", sdk_image: "Any", duration_sec: float) -> None:
    """Send a 184x96 PIL image to Vector's OLED via SDK."""
    from anki_vector.screen import convert_image_to_screen_data

    screen_data = convert_image_to_screen_data(sdk_image)
    robot.screen.set_screen_with_image_data(screen_data, duration_sec=duration_sec)


def _restore_face_animation(robot: "Any", _unused: "Any" = None) -> None:
    """Restore normal face animation after displaying a static image.

    DisplayFaceImage permanently disables KeepFaceAlive in vic-anim.
    Playing an animation re-enables KeepFaceAlive and restores the eyes.
    Uses global ControlManager singleton.
    """
    from apps.vector.src.control_manager import get_control_manager

    ctrl = get_control_manager()
    try:
        logger.info("Restoring face: request control + play animation...")
        if ctrl is not None:
            ctrl.acquire("face_restore")
        time.sleep(0.3)
        robot.anim.play_animation("anim_neutral_eyes_01")
        time.sleep(2.0)
        if ctrl is not None:
            ctrl.release("face_restore")
        time.sleep(0.3)
        logger.info("Face animation restored, control released")
    except Exception:
        logger.exception("Could not restore face animation")


def _hold_image_on_screen(robot: "Any", sdk_image: "Any",
                          duration: float, stop_event: threading.Event,
                          control_mgr: "Any" = None) -> None:
    """Re-send image every 0.5s for *duration* seconds to suppress eye animations.

    After the hold ends, releases and re-acquires behavior control to force
    vic-engine to re-enable KeepFaceAlive (restoring animated eyes).
    """
    end_time = time.monotonic() + duration
    interval = 0.5
    while not stop_event.is_set() and time.monotonic() < end_time:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        try:
            hold_sec = min(interval + 0.3, remaining + 0.1)
            _send_image_to_screen(robot, sdk_image, hold_sec)
        except Exception:
            logger.exception("Display hold send failed")
            break
        stop_event.wait(min(interval, remaining))

    # Restore normal face animation (eyes) after hold ends
    logger.info("Display hold ended (stopped=%s)", stop_event.is_set())
    _restore_face_animation(robot, control_mgr)


def _start_display_hold(robot: "Any", sdk_image: "Any", duration: float,
                         control_mgr: "Any" = None) -> None:
    """Start a background thread that holds an image on screen for *duration* seconds.

    Cancels any previous hold thread.
    """
    with _display_hold_lock:
        # Stop previous hold if any
        prev = _display_hold_threads.get("display_hold")
        if prev is not None:
            prev.set()

        stop_event = threading.Event()
        _display_hold_threads["display_hold"] = stop_event

    t = threading.Thread(
        target=_hold_image_on_screen,
        args=(robot, sdk_image, duration, stop_event),
        name="display-hold",
        daemon=True,
    )
    t.start()


def _render_text_image(text: str, fg_color: tuple, bg_color: tuple) -> "Any":
    """Render text centered on a 160x80 canvas."""
    from PIL import Image as PILImage, ImageDraw

    img = PILImage.new("RGB", (_DISPLAY_W, _DISPLAY_H), bg_color)
    draw = ImageDraw.Draw(img)

    # Use default font; get text bounding box for centering
    bbox = draw.textbbox((0, 0), text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Center the text
    x = (_DISPLAY_W - text_w) // 2
    y = (_DISPLAY_H - text_h) // 2
    draw.text((x, y), text, fill=fg_color)
    return img


def _parse_color(color_str: str) -> tuple:
    """Parse a color string (hex like '#FF0000' or name like 'red') to RGB tuple."""
    from PIL import ImageColor
    try:
        return ImageColor.getrgb(color_str)
    except (ValueError, AttributeError):
        raise ValueError(f"Unknown color: {color_str}")


# ---------------------------------------------------------------------------
# Display route handlers
# ---------------------------------------------------------------------------


async def display_image(request: web.Request) -> web.Response:
    """POST /display/image — display an image on Vector's OLED face.

    Accepts:
      - multipart/form-data with 'image' file field
      - JSON with 'image' field containing base64-encoded image data
    Optional: 'duration' (seconds, default 5)
    """
    conn: "ConnectionManager" = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    duration = 5.0

    try:
        from PIL import Image as PILImage

        content_type = request.content_type or ""

        if "multipart" in content_type:
            reader = await request.multipart()
            image_data = None
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "image":
                    image_data = await part.read()
                elif part.name == "duration":
                    raw = await part.text()
                    duration = float(raw)

            if not image_data:
                return _json_error(400, "Missing 'image' field in multipart", "MISSING_IMAGE")

        elif "json" in content_type:
            try:
                body = await request.json()
            except Exception:
                return _json_error(400, "Invalid JSON body", "INVALID_JSON")

            b64 = body.get("image")
            if not b64:
                return _json_error(400, "Missing 'image' field (base64)", "MISSING_IMAGE")
            duration = float(body.get("duration", duration))
            image_data = base64.b64decode(b64)

        else:
            # Try reading raw body as image data
            image_data = await request.read()
            if not image_data:
                return _json_error(400, "No image data in request body", "MISSING_IMAGE")
            # Check for duration query param
            dur_str = request.query.get("duration")
            if dur_str:
                duration = float(dur_str)

        pil_image = PILImage.open(io.BytesIO(image_data))
        sdk_frame = _prepare_for_screen(pil_image)

        # Send immediately, then hold in background
        await _run_sync(_send_image_to_screen, conn.robot, sdk_frame, min(1.0, duration))
        _start_display_hold(conn.robot, sdk_frame, duration)

        return web.json_response({
            "status": "ok",
            "duration": duration,
            "original_size": list(pil_image.size),
        })
    except ValueError as exc:
        return _json_error(400, str(exc), "INVALID_INPUT")
    except Exception as exc:
        logger.exception("Display image failed")
        return _json_error(500, str(exc), "DISPLAY_IMAGE_FAILED")


async def display_text(request: web.Request) -> web.Response:
    """POST /display/text — render and display text on Vector's OLED face.

    Body: {"text": "Hello!", "fg_color": "#00FF00", "bg_color": "#000000", "duration": 5}
    """
    conn: "ConnectionManager" = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    text = body.get("text")
    if not text:
        return _json_error(400, "Missing 'text' field", "MISSING_PARAMS")

    duration = float(body.get("duration", 5))

    try:
        fg_color = _parse_color(body.get("fg_color", "#FFFFFF"))
        bg_color = _parse_color(body.get("bg_color", "#000000"))
    except ValueError as exc:
        return _json_error(400, str(exc), "INVALID_COLOR")

    try:
        text_img = _render_text_image(text, fg_color, bg_color)
        sdk_frame = _prepare_for_screen(text_img)

        await _run_sync(_send_image_to_screen, conn.robot, sdk_frame, min(1.0, duration))
        _start_display_hold(conn.robot, sdk_frame, duration)

        return web.json_response({
            "status": "ok",
            "text": text,
            "duration": duration,
        })
    except Exception as exc:
        logger.exception("Display text failed")
        return _json_error(500, str(exc), "DISPLAY_TEXT_FAILED")


async def display_color(request: web.Request) -> web.Response:
    """POST /display/color — fill Vector's OLED face with a solid color.

    Body: {"color": "#FF0000", "duration": 10}
    Color can be hex ("#FF0000") or name ("red").
    """
    conn: "ConnectionManager" = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    color_str = body.get("color")
    if not color_str:
        return _json_error(400, "Missing 'color' field", "MISSING_PARAMS")

    duration = float(body.get("duration", 5))

    try:
        rgb = _parse_color(color_str)
    except ValueError as exc:
        return _json_error(400, str(exc), "INVALID_COLOR")

    try:
        from PIL import Image as PILImage

        fill_img = PILImage.new("RGB", (_DISPLAY_W, _DISPLAY_H), rgb)
        sdk_frame = _prepare_for_screen(fill_img)

        await _run_sync(_send_image_to_screen, conn.robot, sdk_frame, min(1.0, duration))
        _start_display_hold(conn.robot, sdk_frame, duration)

        return web.json_response({
            "status": "ok",
            "color": color_str,
            "rgb": list(rgb),
            "duration": duration,
        })
    except Exception as exc:
        logger.exception("Display color failed")
        return _json_error(500, str(exc), "DISPLAY_COLOR_FAILED")


async def call_status(request: web.Request) -> web.Response:
    """GET /call/status — get LiveKit call status including streamer connection."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    bridge = conn.livekit_bridge
    if bridge is None:
        return web.json_response({"active": False, "bridge": "not_initialized"})

    try:
        status_data = await bridge.get_status()
        return web.json_response(status_data)
    except Exception as exc:
        logger.exception("Call status check failed")
        return _json_error(500, str(exc), "CALL_STATUS_FAILED")


async def media_status(request: web.Request) -> web.Response:
    """GET /media/status — status of all media channels."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    ms = conn.media_service
    if ms is None:
        return _json_error(503, "MediaService not initialised", "MEDIA_UNAVAILABLE")
    return web.json_response(ms.get_status())


async def media_channels(request: web.Request) -> web.Response:
    """POST /media/channels — start/stop media channels on demand.

    Body (JSON)::

        {
            "action": "start" | "stop",
            "video_in": true,     // camera channel
            "audio_in": true,     // mic channel
            "audio_out": true,    // speaker channel
            "video_out": true     // display channel
        }

    Omitted fields default to false.  Specify ``"action": "start"`` to
    start or ``"action": "stop"`` to stop the listed channels.

    Returns status of all channels after the operation.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    ms = conn.media_service
    if ms is None:
        return _json_error(503, "MediaService not initialised", "MEDIA_UNAVAILABLE")

    body = await request.json()
    action = body.get("action", "start")

    # Map user-facing names to internal channel names
    channel_map = {
        "video_in": "camera",
        "audio_in": "mic",
        "audio_out": "speaker",
        "video_out": "display",
    }

    started = []
    stopped = []
    errors = []

    for key, channel_name in channel_map.items():
        if not body.get(key, False):
            continue
        try:
            if action == "start":
                ms.start_channel(channel_name)
                started.append(channel_name)
            elif action == "stop":
                ms.stop_channel(channel_name)
                stopped.append(channel_name)
            else:
                errors.append(f"unknown action: {action}")
        except (RuntimeError, ValueError) as exc:
            errors.append(f"{channel_name}: {exc}")

    result = {
        "started": started,
        "stopped": stopped,
    }
    if errors:
        result["errors"] = errors
    result["status"] = ms.get_status()
    return web.json_response(result)




# ---------------------------------------------------------------------------
# Navigation routes
# ---------------------------------------------------------------------------


async def nav_status(request: web.Request) -> web.Response:
    """GET /nav/status — navigation controller status (pose, map, waypoints)."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return web.json_response({"active": False, "message": "Navigation not initialised"})

    try:
        status_data = await _run_sync(nav.get_status)
        return web.json_response(status_data)
    except Exception as exc:
        logger.exception("Nav status check failed")
        return _json_error(500, str(exc), "NAV_STATUS_FAILED")


async def nav_start(request: web.Request) -> web.Response:
    """POST /nav/start — start navigation controller (SLAM + mapping).

    Body (optional): {"map": "home"}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    try:
        body = await request.json()
    except Exception:
        body = {}

    map_name = body.get("map", "default")

    try:
        await _run_sync(nav.start, map_name)
        return web.json_response({"status": "ok", "map": map_name})
    except Exception as exc:
        logger.exception("Failed to start navigation")
        return _json_error(500, str(exc), "NAV_START_FAILED")


async def nav_stop(request: web.Request) -> web.Response:
    """POST /nav/stop — stop navigation controller (saves map)."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    try:
        await _run_sync(nav.stop)
        return web.json_response({"status": "ok"})
    except Exception as exc:
        logger.exception("Failed to stop navigation")
        return _json_error(500, str(exc), "NAV_STOP_FAILED")


async def nav_goto(request: web.Request) -> web.Response:
    """POST /nav/goto — navigate to a named waypoint.

    Body: {"waypoint": "kitchen"} or {"x": 1500, "y": 2000}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    waypoint_name = body.get("waypoint")
    if waypoint_name:
        started = await _run_sync(nav.navigate_to_waypoint, waypoint_name)
        if not started:
            return _json_error(400, f"Cannot navigate to '{waypoint_name}' — not found or already navigating", "NAV_FAILED")
        return web.json_response({"status": "ok", "target": waypoint_name})

    x = body.get("x")
    y = body.get("y")
    if x is not None and y is not None:
        started = await _run_sync(nav.navigate_to_position, float(x), float(y))
        if not started:
            return _json_error(400, "Cannot navigate — already navigating", "NAV_FAILED")
        return web.json_response({"status": "ok", "target": f"({x}, {y})"})

    return _json_error(400, "Provide 'waypoint' name or 'x'/'y' coordinates", "MISSING_PARAMS")


async def nav_cancel(request: web.Request) -> web.Response:
    """POST /nav/cancel — cancel current navigation."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    await _run_sync(nav.cancel_navigation)
    return web.json_response({"status": "ok"})


async def nav_waypoint_save(request: web.Request) -> web.Response:
    """POST /nav/waypoint/save — save current position as a named waypoint.

    Body: {"name": "kitchen", "description": "By the fridge"}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    name = body.get("name")
    if not name:
        return _json_error(400, "Missing 'name' field", "MISSING_PARAMS")

    description = body.get("description", "")
    saved = await _run_sync(nav.save_current_position, name, description)

    if saved:
        return web.json_response({"status": "ok", "waypoint": name})
    return _json_error(500, "Failed to save waypoint", "SAVE_FAILED")


async def nav_waypoint_delete(request: web.Request) -> web.Response:
    """POST /nav/waypoint/delete — delete a named waypoint.

    Body: {"name": "kitchen"}
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "INVALID_JSON")

    name = body.get("name")
    if not name:
        return _json_error(400, "Missing 'name' field", "MISSING_PARAMS")

    # Access waypoint manager through nav controller's internal reference
    deleted = await _run_sync(nav._waypoint_mgr.delete, name)
    if deleted:
        return web.json_response({"status": "ok", "deleted": name})
    return _json_error(404, f"Waypoint '{name}' not found", "NOT_FOUND")


async def nav_waypoints(request: web.Request) -> web.Response:
    """GET /nav/waypoints — list all saved waypoints."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    waypoints = await _run_sync(nav._waypoint_mgr.list_waypoints)
    return web.json_response({
        "waypoints": [
            {
                "name": wp.name,
                "x": round(wp.x, 1),
                "y": round(wp.y, 1),
                "theta_deg": round(wp.theta * 57.2958, 1),
                "description": wp.description,
            }
            for wp in waypoints
        ],
    })


async def nav_maps(request: web.Request) -> web.Response:
    """GET /nav/maps — list all saved maps."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    maps = await _run_sync(nav._map_store.list_maps)
    return web.json_response({"maps": maps})


async def nav_mapping_start(request: web.Request) -> web.Response:
    """POST /nav/mapping/start — start passive mapping mode."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    await _run_sync(nav.start_mapping)
    return web.json_response({"status": "ok", "state": "mapping"})


async def nav_mapping_stop(request: web.Request) -> web.Response:
    """POST /nav/mapping/stop — stop mapping mode and save map."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "Navigation not initialised", "NAV_UNAVAILABLE")

    await _run_sync(nav.stop_mapping)
    return web.json_response({"status": "ok", "state": "idle"})


async def explore_start(request: web.Request) -> web.Response:
    """POST /explore/start — start autonomous room exploration."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    explorer = conn.explorer
    if explorer is None:
        return _json_error(503, "Explorer not initialised", "EXPLORER_UNAVAILABLE")

    # Start nav controller first if not running
    nav = conn.nav_controller
    if nav and nav.state.value == "idle":
        try:
            body = await request.json()
        except Exception:
            body = {}
        map_name = body.get("map", "home")
        await _run_sync(nav.start, map_name)

    try:
        await _run_sync(explorer.start)
        return web.json_response({"status": "ok", "state": "exploring"})
    except Exception as exc:
        logger.exception("Failed to start exploration")
        return _json_error(500, str(exc), "EXPLORE_START_FAILED")


async def explore_stop(request: web.Request) -> web.Response:
    """POST /explore/stop — stop autonomous exploration."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    explorer = conn.explorer
    if explorer is None:
        return _json_error(503, "Explorer not initialised", "EXPLORER_UNAVAILABLE")

    try:
        await _run_sync(explorer.stop)
        return web.json_response({"status": "ok", "state": "idle"})
    except Exception as exc:
        logger.exception("Failed to stop exploration")
        return _json_error(500, str(exc), "EXPLORE_STOP_FAILED")


async def explore_status(request: web.Request) -> web.Response:
    """GET /explore/status — exploration diagnostics."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    explorer = conn.explorer
    if explorer is None:
        return web.json_response({"active": False})

    try:
        status_data = await _run_sync(explorer.get_status)
        return web.json_response(status_data)
    except Exception as exc:
        logger.exception("Explore status check failed")
        return _json_error(500, str(exc), "EXPLORE_STATUS_FAILED")


async def charger_save(request: web.Request) -> web.Response:
    """POST /charger/save — drive off charger, turn 180, save charger waypoint.

    Must be called while Vector is ON the charger.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    nav = conn.nav_controller
    if nav is None:
        return _json_error(503, "NavController not initialised", "NAV_UNAVAILABLE")

    def _charger_maneuver():
        import anki_vector

        robot = conn.robot
        batt = robot.get_battery_state()
        if not batt.is_on_charger_platform:
            raise RuntimeError("Vector is not on the charger")

        # Acquire control via centralized manager
        ctrl = conn.control_manager
        if ctrl:
            ctrl.acquire("charger_save")
        else:
            conn.request_override_control()

        try:
            import time as _time
            robot.behavior.drive_off_charger()
            _time.sleep(3.0)
            robot.motors.set_wheel_motors(0, 0)
            _time.sleep(0.5)

            robot.behavior.turn_in_place(anki_vector.util.degrees(180))
            _time.sleep(3.0)
            robot.motors.set_wheel_motors(0, 0)
            _time.sleep(0.5)

            nav.save_current_position("charger")

            robot.behavior.turn_in_place(anki_vector.util.degrees(180))
            _time.sleep(3.0)
            robot.motors.set_wheel_motors(0, 0)
            _time.sleep(0.5)
        finally:
            if ctrl:
                ctrl.release("charger_save")
            else:
                conn.release_override_control()

    try:
        await _run_sync(_charger_maneuver)
        return web.json_response({"status": "ok", "waypoint": "charger"})
    except RuntimeError as exc:
        return _json_error(400, str(exc), "NOT_ON_CHARGER")
    except Exception as exc:
        logger.exception("Charger save maneuver failed")
        return _json_error(500, str(exc), "CHARGER_SAVE_FAILED")


async def charger_start(request: web.Request) -> web.Response:
    """POST /charger/start — start battery monitoring + auto-charge."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    charger = conn.auto_charger
    if charger is None:
        return _json_error(503, "AutoCharger not initialised", "CHARGER_UNAVAILABLE")

    # Make sure nav controller is started
    nav = conn.nav_controller
    if nav and nav.state.value == "idle":
        await _run_sync(nav.start, "home")

    try:
        await _run_sync(charger.start)
        return web.json_response({"status": "ok", "monitoring": True})
    except Exception as exc:
        logger.exception("Failed to start auto-charger")
        return _json_error(500, str(exc), "CHARGER_START_FAILED")


async def charger_stop(request: web.Request) -> web.Response:
    """POST /charger/stop — stop battery monitoring."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    charger = conn.auto_charger
    if charger is None:
        return _json_error(503, "AutoCharger not initialised", "CHARGER_UNAVAILABLE")

    try:
        await _run_sync(charger.stop)
        return web.json_response({"status": "ok", "monitoring": False})
    except Exception as exc:
        logger.exception("Failed to stop auto-charger")
        return _json_error(500, str(exc), "CHARGER_STOP_FAILED")


# ---------------------------------------------------------------------------
# Patrol / Home Guardian routes
# ---------------------------------------------------------------------------


async def patrol_start(request: web.Request) -> web.Response:
    """POST /patrol/start — start patrol. Body: {"mode": "patrol"|"sentry", "waypoints": [...]}."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return _json_error(503, "HomeGuardian not initialised", "GUARDIAN_UNAVAILABLE")

    if guardian.is_running:
        return web.json_response({
            "status": "ok",
            "message": "Guardian already running",
            **guardian.get_status(),
        })

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass  # defaults are fine

    mode = body.get("mode", "patrol")
    waypoints = body.get("waypoints")

    try:
        await _run_sync(guardian.start, mode, waypoints)
        return web.json_response({"status": "ok", **guardian.get_status()})
    except Exception as exc:
        logger.exception("Failed to start patrol")
        return _json_error(500, str(exc), "PATROL_START_FAILED")


async def patrol_stop(request: web.Request) -> web.Response:
    """POST /patrol/stop — stop patrol."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return _json_error(503, "HomeGuardian not initialised", "GUARDIAN_UNAVAILABLE")

    try:
        await _run_sync(guardian.stop)
        return web.json_response({"status": "ok", "running": False})
    except Exception as exc:
        logger.exception("Failed to stop patrol")
        return _json_error(500, str(exc), "PATROL_STOP_FAILED")


async def patrol_status(request: web.Request) -> web.Response:
    """GET /patrol/status — patrol status and recent events."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return web.json_response({"running": False, "message": "HomeGuardian not initialised"})

    return web.json_response(guardian.get_status())


async def patrol_log(request: web.Request) -> web.Response:
    """GET /patrol/log — full activity log. Query: ?limit=50."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return web.json_response({"events": []})

    limit = int(request.query.get("limit", "50"))
    return web.json_response({"events": guardian.get_activity_log(limit)})


async def patrol_pause(request: web.Request) -> web.Response:
    """POST /patrol/pause — pause patrol."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return _json_error(503, "HomeGuardian not initialised", "GUARDIAN_UNAVAILABLE")

    guardian.pause()
    return web.json_response({"status": "ok", "paused": True})


async def patrol_resume(request: web.Request) -> web.Response:
    """POST /patrol/resume — resume patrol."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    guardian = conn.home_guardian
    if guardian is None:
        return _json_error(503, "HomeGuardian not initialised", "GUARDIAN_UNAVAILABLE")

    guardian.resume()
    return web.json_response({"status": "ok", "paused": False})


async def mode_get(request: web.Request) -> web.Response:
    """GET /mode — get current behavior mode."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    return web.json_response({
        "mode": conn.mode,
        "playful_remaining_s": round(conn.playful_remaining, 1),
    })


async def mode_set(request: web.Request) -> web.Response:
    """POST /mode — set behavior mode.

    Body: {"mode": "quiet"|"playful", "duration_s": 480}
    Playful mode auto-reverts to quiet after duration_s (default 480 = 8 min, max 480).
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "BAD_REQUEST")
    mode = body.get("mode")
    if mode not in ("quiet", "playful"):
        return _json_error(400, "mode must be 'quiet' or 'playful'", "BAD_REQUEST")
    duration_s = min(float(body.get("duration_s", 480)), 480.0)
    await _run_sync(conn.set_mode, mode, duration_s)
    return web.json_response({
        "mode": conn.mode,
        "playful_remaining_s": round(conn.playful_remaining, 1),
    })


# ---------------------------------------------------------------------------
# Face enrollment routes
# ---------------------------------------------------------------------------

# Lazy-loaded face models (shared across requests)
_face_detector = None
_face_recognizer = None
_person_detector_face = None

_FACE_DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data",
)
_FACE_DB_PATH = os.path.join(os.path.abspath(_FACE_DATA_DIR), "face_database.json")
_REF_IMG_DIR = os.path.join(os.path.abspath(_FACE_DATA_DIR), "reference_images")


def _get_face_models():
    """Lazy-load face detection and recognition models."""
    global _face_detector, _face_recognizer, _person_detector_face
    if _face_detector is None:
        from apps.vector.src.face_recognition.face_detector import FaceDetector
        from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer
        _face_detector = FaceDetector()
        _face_recognizer = FaceRecognizer()
        if os.path.isfile(_FACE_DB_PATH):
            _face_recognizer.load_database(_FACE_DB_PATH)
            logger.info("Loaded face database: %s", _face_recognizer.list_enrolled())
    if _person_detector_face is None:
        from apps.vector.src.detector.person_detector import PersonDetector
        _person_detector_face = PersonDetector()
    return _face_detector, _face_recognizer, _person_detector_face


def _pil_to_bgr(pil_image):
    """Convert PIL Image to OpenCV BGR numpy array."""
    import cv2
    rgb = np.array(pil_image)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


async def face_enroll(request: web.Request) -> web.Response:
    """POST /face/enroll — enroll a face from a live camera capture.

    JSON body: {"name": "ophir"}
    Captures a frame from Vector's camera, detects face + body,
    stores face embedding and saves body crop as reference image.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "BAD_REQUEST")

    name = body.get("name", "").strip().lower()
    if not name:
        return _json_error(400, "name is required", "BAD_REQUEST")

    def _do_enroll():
        import cv2

        detector, recognizer, person_det = _get_face_models()

        # Capture frame
        jpeg = conn.camera_client.get_latest_jpeg()
        if jpeg:
            buf = np.frombuffer(jpeg, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        else:
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera not producing frames")
            frame = _pil_to_bgr(image.raw_image)

        # Detect face
        faces = detector.detect(frame)
        if not faces:
            return {"status": "no_face", "message": "No face detected — try better lighting or angle"}

        # Enroll best face
        count = recognizer.enroll(name, frame, faces[:1])

        # Save database
        os.makedirs(os.path.abspath(_FACE_DATA_DIR), exist_ok=True)
        recognizer.save_database(_FACE_DB_PATH)

        result = {
            "status": "ok",
            "name": name,
            "face_embeddings": count,
            "face_confidence": round(faces[0].confidence, 3),
        }

        # Detect body and save reference crop
        persons = person_det.detect(frame)
        if persons:
            best = persons[0]
            h, w = frame.shape[:2]
            x1 = max(0, int(best.cx - best.width / 2))
            y1 = max(0, int(best.cy - best.height / 2))
            x2 = min(w, int(best.cx + best.width / 2))
            y2 = min(h, int(best.cy + best.height / 2))
            body_crop = frame[y1:y2, x1:x2]

            if body_crop.size > 0:
                person_dir = os.path.join(_REF_IMG_DIR, name)
                os.makedirs(person_dir, exist_ok=True)
                crop_path = os.path.join(person_dir, f"body_{count}.jpg")
                cv2.imwrite(crop_path, body_crop)
                full_path = os.path.join(person_dir, f"full_{count}.jpg")
                cv2.imwrite(full_path, frame)
                result["body_saved"] = True
                result["body_confidence"] = round(best.confidence, 3)
        else:
            result["body_saved"] = False

        return result

    try:
        result = await _run_sync(_do_enroll)
        status_code = 200 if result.get("status") == "ok" else 404
        return web.json_response(result, status=status_code)
    except Exception as exc:
        logger.exception("Face enrollment failed")
        return _json_error(500, str(exc), "ENROLL_FAILED")


async def face_list(request: web.Request) -> web.Response:
    """GET /face/list — list all enrolled faces."""
    _, recognizer, _ = _get_face_models()
    enrolled = recognizer.list_enrolled()

    # Check for reference images too
    people = {}
    for name, emb_count in enrolled.items():
        person_dir = os.path.join(_REF_IMG_DIR, name)
        body_count = 0
        if os.path.isdir(person_dir):
            body_count = len([f for f in os.listdir(person_dir) if f.startswith("body_")])
        people[name] = {"face_embeddings": emb_count, "body_references": body_count}

    return web.json_response({"status": "ok", "enrolled": people})


async def face_remove(request: web.Request) -> web.Response:
    """POST /face/remove — remove an enrolled face.

    JSON body: {"name": "ophir"}
    """
    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body", "BAD_REQUEST")

    name = body.get("name", "").strip().lower()
    if not name:
        return _json_error(400, "name is required", "BAD_REQUEST")

    _, recognizer, _ = _get_face_models()
    removed = recognizer.remove(name)
    if removed:
        recognizer.save_database(_FACE_DB_PATH)
        # Also remove reference images
        person_dir = os.path.join(_REF_IMG_DIR, name)
        if os.path.isdir(person_dir):
            import shutil
            shutil.rmtree(person_dir)
        return web.json_response({"status": "ok", "removed": name})
    return _json_error(404, f"'{name}' not enrolled", "NOT_FOUND")


async def face_recognize(request: web.Request) -> web.Response:
    """GET /face/recognize — capture frame and identify who's in view.

    Returns face match results and body detection.
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    def _do_recognize():
        import cv2

        detector, recognizer, person_det = _get_face_models()

        # Capture frame
        jpeg = conn.camera_client.get_latest_jpeg()
        if jpeg:
            buf = np.frombuffer(jpeg, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        else:
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera not producing frames")
            frame = _pil_to_bgr(image.raw_image)

        result = {"status": "ok", "faces": [], "persons": []}

        # Face recognition
        faces = detector.detect(frame)
        if faces:
            matches = recognizer.recognize(frame, faces)
            for m in matches:
                result["faces"].append({
                    "name": m.name,
                    "confidence": round(m.confidence, 3),
                    "x": round(m.detection.x),
                    "y": round(m.detection.y),
                    "width": round(m.detection.width),
                    "height": round(m.detection.height),
                })

        # Person detection
        persons = person_det.detect(frame)
        for p in persons:
            result["persons"].append({
                "cx": round(p.cx),
                "cy": round(p.cy),
                "width": round(p.width),
                "height": round(p.height),
                "confidence": round(p.confidence, 3),
            })

        return result

    try:
        result = await _run_sync(_do_recognize)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("Face recognition failed")
        return _json_error(500, str(exc), "RECOGNIZE_FAILED")


# ---------------------------------------------------------------------------
# Signal intercom — send text/images to Ophir via openclaw-gateway
# ---------------------------------------------------------------------------

SIGNAL_RECIPIENT = "+14084758230"
SIGNAL_BOT_ACCOUNT = "+14086469950"
BOT_CONTAINER = "openclaw-gateway"
SIGNAL_COOLDOWN_SECONDS = 30
_signal_last_sent: float = 0.0


async def _send_signal(text: str, attachment_path: str | None = None) -> bool:
    """Send a message to Signal via openclaw-gateway JSON-RPC.

    Uses ``asyncio.create_subprocess_exec`` so the event loop is not blocked.
    Respects a 30-second cooldown between sends to avoid spam.
    """
    global _signal_last_sent
    now = time.time()
    if now - _signal_last_sent < SIGNAL_COOLDOWN_SECONDS:
        logger.info("[signal] cooldown (%.0fs remaining), skipping: %s",
                    SIGNAL_COOLDOWN_SECONDS - (now - _signal_last_sent), text)
        return False
    _signal_last_sent = now

    params: dict[str, Any] = {"recipient": SIGNAL_RECIPIENT, "message": text}
    container = BOT_CONTAINER

    # Copy attachment into container if present
    container_attachment = None
    if attachment_path and os.path.isfile(attachment_path):
        container_attachment = f"/tmp/{os.path.basename(attachment_path)}"
        proc = await asyncio.create_subprocess_exec(
            "sg", "docker", "-c",
            f"docker cp {shlex.quote(attachment_path)} {container}:{container_attachment}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning("[signal] docker cp failed: %s", stderr.decode())
            container_attachment = None

    if container_attachment:
        params["attachments"] = [container_attachment]

    payload = json.dumps({"jsonrpc": "2.0", "method": "send", "params": params, "id": 1})

    try:
        proc = await asyncio.create_subprocess_exec(
            "sg", "docker", "-c",
            f"docker exec -i {container} curl -sf -X POST "
            "http://127.0.0.1:8080/api/v1/rpc "
            "-H 'Content-Type: application/json' -d @-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=payload.encode()), timeout=15
        )
        if proc.returncode == 0:
            suffix = " [+attachment]" if container_attachment else ""
            logger.info("[signal] sent: %s%s", text, suffix)
            return True
        logger.warning("[signal] send failed (rc=%d): %s", proc.returncode, stderr.decode())
        return False
    except asyncio.TimeoutError:
        logger.warning("[signal] send timed out")
        return False
    except Exception as exc:
        logger.warning("[signal] send error: %s", exc)
        return False


async def signal_send(request: web.Request) -> web.Response:
    """POST /signal/send — send text message to Signal.

    JSON body: ``{"message": "text"}``
    """
    try:
        data = await request.json()
    except Exception:
        return _json_error(400, "invalid JSON")

    text = str(data.get("message", "")).strip()
    if not text:
        return _json_error(400, "message required")

    ok = await _send_signal(text)
    status_code = 200 if ok else 429 if not ok else 502
    # Distinguish cooldown (False from cooldown) vs send failure
    return web.json_response({"status": "sent" if ok else "cooldown_or_failed"}, status=200 if ok else 429)


async def signal_send_image(request: web.Request) -> web.Response:
    """POST /signal/send-image — send image + caption to Signal.

    JSON body: ``{"caption": "text", "path": "/path/to/image.jpg"}``
    """
    try:
        data = await request.json()
    except Exception:
        return _json_error(400, "invalid JSON")

    caption = str(data.get("caption", "")).strip() or "Image"
    image_path = str(data.get("path", "")).strip()
    if not image_path or not os.path.isfile(image_path):
        return _json_error(400, "path required and must exist on NUC filesystem")

    ok = await _send_signal(caption, attachment_path=image_path)
    return web.json_response({"status": "sent" if ok else "cooldown_or_failed"}, status=200 if ok else 429)


async def signal_send_camera(request: web.Request) -> web.Response:
    """POST /signal/send-camera — capture current camera frame and send to Signal.

    JSON body (optional): ``{"caption": "text"}``
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        data = await request.json()
    except Exception:
        data = {}

    caption = str(data.get("caption", "Camera capture")).strip()

    # Grab a camera frame
    try:
        def _capture() -> bytes:
            jpeg = conn.camera_client.get_latest_jpeg()
            if jpeg:
                return jpeg
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera not producing frames")
            buf = io.BytesIO()
            image.raw_image.save(buf, format="JPEG")
            return buf.getvalue()

        jpeg_bytes = await _run_sync(_capture)
    except Exception as exc:
        return _json_error(502, f"camera capture failed: {exc}")

    # Write to temp file and send
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", prefix="signal-cam-", delete=False)
    try:
        tmp.write(jpeg_bytes)
        tmp.close()
        ok = await _send_signal(caption, attachment_path=tmp.name)
        return web.json_response({"status": "sent" if ok else "cooldown_or_failed"}, status=200 if ok else 429)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


async def intercom_receive(request: web.Request) -> web.Response:
    """POST /intercom/receive — backward-compatible alias for signal/send.

    JSON body: ``{"text": "..."}``
    """
    try:
        data = await request.json()
    except Exception:
        return _json_error(400, "invalid JSON")

    text = str(data.get("text", "")).strip()
    if not text:
        return _json_error(400, "text required")

    ok = await _send_signal(f"\U0001f916 Robot says: {text}")
    return web.json_response({"status": "sent" if ok else "failed"}, status=200 if ok else 502)


async def intercom_photo(request: web.Request) -> web.Response:
    """POST /intercom/photo — backward-compatible alias for signal/send-camera.

    JSON body: ``{"caption": "..."}``
    """
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err

    try:
        data = await request.json()
    except Exception:
        data = {}

    caption = str(data.get("caption", "Photo from robot")).strip()

    try:
        def _capture() -> bytes:
            jpeg = conn.camera_client.get_latest_jpeg()
            if jpeg:
                return jpeg
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera not producing frames")
            buf = io.BytesIO()
            image.raw_image.save(buf, format="JPEG")
            return buf.getvalue()

        jpeg_bytes = await _run_sync(_capture)
    except Exception as exc:
        return _json_error(502, f"capture failed: {exc}")

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", prefix="robot-", delete=False)
    try:
        tmp.write(jpeg_bytes)
        tmp.close()
        ok = await _send_signal(f"\U0001f4f8 {caption}", attachment_path=tmp.name)
        return web.json_response({"status": "sent" if ok else "failed"}, status=200 if ok else 502)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def setup_routes(app: web.Application) -> None:
    """Register all bridge routes on the application."""
    app.router.add_get("/health", health)
    app.router.add_post("/move", move)
    app.router.add_post("/stop", stop)
    app.router.add_post("/head", head)
    app.router.add_post("/lift", lift)
    app.router.add_post("/led", led)
    app.router.add_get("/capture", capture)
    app.router.add_post("/display", display)
    app.router.add_get("/status", status)
    app.router.add_post("/follow/start", follow_start)
    app.router.add_post("/follow/stop", follow_stop)
    app.router.add_get("/follow/status", follow_status)
    app.router.add_post("/audio/play", audio_play)
    app.router.add_get("/audio/status", audio_status)
    app.router.add_post("/display/image", display_image)
    app.router.add_post("/display/text", display_text)
    app.router.add_post("/display/color", display_color)
    app.router.add_post("/call/start", call_start)
    app.router.add_post("/call/stop", call_stop)
    app.router.add_get("/call/join-url", call_join_url)
    app.router.add_get("/call/status", call_status)
    app.router.add_get("/media/status", media_status)
    app.router.add_post("/media/channels", media_channels)
    app.router.add_get("/mode", mode_get)
    app.router.add_post("/mode", mode_set)
    # Navigation routes
    app.router.add_get("/nav/status", nav_status)
    app.router.add_post("/nav/start", nav_start)
    app.router.add_post("/nav/stop", nav_stop)
    app.router.add_post("/nav/goto", nav_goto)
    app.router.add_post("/nav/cancel", nav_cancel)
    app.router.add_post("/nav/waypoint/save", nav_waypoint_save)
    app.router.add_post("/nav/waypoint/delete", nav_waypoint_delete)
    app.router.add_get("/nav/waypoints", nav_waypoints)
    app.router.add_get("/nav/maps", nav_maps)
    app.router.add_post("/nav/mapping/start", nav_mapping_start)
    app.router.add_post("/nav/mapping/stop", nav_mapping_stop)
    # Exploration routes
    app.router.add_post("/explore/start", explore_start)
    app.router.add_post("/explore/stop", explore_stop)
    app.router.add_get("/explore/status", explore_status)
    # Auto-charger routes
    app.router.add_post("/charger/save", charger_save)
    app.router.add_post("/charger/start", charger_start)
    app.router.add_post("/charger/stop", charger_stop)
    # Patrol / Home Guardian routes
    app.router.add_post("/patrol/start", patrol_start)
    app.router.add_post("/patrol/stop", patrol_stop)
    app.router.add_get("/patrol/status", patrol_status)
    # Signal intercom routes
    app.router.add_post("/signal/send", signal_send)
    app.router.add_post("/signal/send-image", signal_send_image)
    app.router.add_post("/signal/send-camera", signal_send_camera)
    # Backward-compatible aliases (from standalone intercom-server.py)
    app.router.add_post("/intercom/receive", intercom_receive)
    app.router.add_post("/intercom/photo", intercom_photo)
    app.router.add_get("/patrol/log", patrol_log)
    app.router.add_post("/patrol/pause", patrol_pause)
    app.router.add_post("/patrol/resume", patrol_resume)
    # Face enrollment routes
    app.router.add_post("/face/enroll", face_enroll)
    app.router.add_get("/face/list", face_list)
    app.router.add_post("/face/remove", face_remove)
    app.router.add_get("/face/recognize", face_recognize)
