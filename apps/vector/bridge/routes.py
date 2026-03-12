"""HTTP route handlers for the Vector bridge server.

Each handler translates an HTTP request into one or more controller calls
and returns a JSON response.  All Vector SDK calls are synchronous and run
in the default executor to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import logging
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
            image = conn.robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("Camera capture returned None")
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
    """POST /follow/start — start person following (stub)."""
    return _json_error(501, "Follow planner not yet implemented", "NOT_IMPLEMENTED")


async def follow_stop(request: web.Request) -> web.Response:
    """POST /follow/stop — stop person following (stub)."""
    return _json_error(501, "Follow planner not yet implemented", "NOT_IMPLEMENTED")


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
    app.router.add_post("/audio/play", audio_play)
    app.router.add_get("/audio/status", audio_status)
    app.router.add_post("/call/start", call_start)
    app.router.add_post("/call/stop", call_stop)
    app.router.add_get("/call/join-url", call_join_url)
