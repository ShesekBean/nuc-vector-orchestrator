"""HTTP route handlers for the Vector bridge server.

Each handler translates an HTTP request into one or more controller calls
and returns a JSON response.  All Vector SDK calls are synchronous and run
in the default executor to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
import time
from typing import TYPE_CHECKING

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

    Starts the full pipeline: YOLO detection → Kalman tracking → PD follow.
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


def _restore_face_animation(robot: "Any") -> None:
    """Restore normal face animation after displaying a static image.

    DisplayFaceImage permanently disables KeepFaceAlive in vic-anim.
    Releasing and re-acquiring behavior control forces a full face reset.
    """
    try:
        logger.info("Restoring face: releasing behavior control...")
        robot.conn.release_control()
        time.sleep(0.5)
        robot.conn.request_control()
        time.sleep(0.5)
        logger.info("Face animation restored via control cycle")
    except Exception:
        logger.exception("Could not restore face animation")


def _hold_image_on_screen(robot: "Any", sdk_image: "Any",
                          duration: float, stop_event: threading.Event) -> None:
    """Re-send image every 0.5s for *duration* seconds to suppress eye animations.

    After the hold ends, plays a short animation to restore the normal face
    (DisplayFaceImage permanently disables KeepFaceAlive in vic-anim).
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
    _restore_face_animation(robot)


def _start_display_hold(robot: "Any", sdk_image: "Any", duration: float) -> None:
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


async def mode_get(request: web.Request) -> web.Response:
    """GET /mode — get current behavior mode."""
    conn: ConnectionManager = request.app["conn"]
    err = _require_connected(conn)
    if err:
        return err
    return web.json_response({"mode": conn.mode})


async def mode_set(request: web.Request) -> web.Response:
    """POST /mode — set behavior mode. Body: {"mode": "quiet"|"playful"}."""
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

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, conn.set_mode, mode)
        return web.json_response({"mode": conn.mode})
    except Exception as exc:
        logger.exception("Mode switch failed")
        return _json_error(500, str(exc), "MODE_SWITCH_FAILED")


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
    app.router.add_get("/mode", mode_get)
    app.router.add_post("/mode", mode_set)
