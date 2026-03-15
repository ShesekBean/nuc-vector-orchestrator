#!/usr/bin/env python3
"""OpenAI-compatible proxy that forwards to OpenClaw via WebSocket.

Wire-pod's IntentGraph sends transcribed text as OpenAI chat completion
requests. This proxy bridges that to OpenClaw's WebSocket gateway
(chat.send), returning the agent's response in OpenAI streaming format.

Flow:
    wire-pod STT → OpenAI chat completion request → this proxy
    → OpenClaw WebSocket chat.send → agent response
    → OpenAI streaming response → wire-pod → Vector SayText

Usage:
    python3 scripts/openclaw-voice-proxy.py [--port 8095]

Requires: aiohttp (already installed on NUC)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("openclaw-voice-proxy")

# OpenClaw gateway WebSocket
OPENCLAW_WS_URL = "ws://127.0.0.1:18889"
OPENCLAW_TOKEN_PATH = Path.home() / ".openclaw" / "hooks-token"
OPENCLAW_DEVICE_IDENTITY_PATH = Path.home() / ".openclaw" / "identity" / "device.json"

# OpenClaw gateway auth token — read from config, never hardcode
def _load_gateway_token() -> str:
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    with open(cfg_path) as f:
        return json.load(f)["gateway"]["auth"]["token"]

OPENCLAW_GATEWAY_TOKEN = _load_gateway_token()

# Session key — shared with Signal DM for unified memory
# Must match the full session key that Signal DMs resolve to
VOICE_SESSION_KEY = "agent:main:main"

# Protocol version
PROTOCOL_VERSION = 3

# Timeout for agent response
AGENT_TIMEOUT_MS = 60_000

# Lock to serialize OpenClaw requests — prevents 2nd wake word from
# aborting the 1st request's in-flight chat session.
_openclaw_lock = asyncio.Lock()
_last_response: str | None = None
_last_response_time: float = 0.0
_RESPONSE_REUSE_WINDOW = 3.0  # seconds

# Context prefix prepended to every voice message so OpenClaw knows
# who is speaking and doesn't ask clarifying questions about recipients.
VOICE_CONTEXT_PREFIX = (
    "[Voice command from Ophir via Vector's microphone. "
    "CRITICAL: Keep responses to 1-2 SHORT sentences max — this is spoken aloud "
    "by a small robot. No markdown, no bullets, no formatting. Just talk naturally. "
    "When Ophir says 'send to Ophir' or 'message Ophir' or 'tell Ophir', "
    "send a Signal message to Ophir (+14084758230) immediately.] "
)


def load_gateway_token() -> str:
    """Load the gateway auth token."""
    return OPENCLAW_GATEWAY_TOKEN


def _load_device_identity() -> dict:
    """Load device identity from ~/.openclaw/identity/device.json."""
    with open(OPENCLAW_DEVICE_IDENTITY_PATH) as f:
        return json.load(f)


def _b64url_no_pad(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _build_device_auth(nonce: str, challenge_ts: int, client_id: str = "cli", client_mode: str = "backend") -> dict:
    """Build V3 device-signed auth payload for the connect frame.

    Signs: v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily
    """
    identity = _load_device_identity()
    device_id = identity["deviceId"]
    privkey = load_pem_private_key(identity["privateKeyPem"].encode(), password=None)
    pubkey = load_pem_public_key(identity["publicKeyPem"].encode())

    role = "operator"
    scopes = "operator.admin,operator.read,operator.write"
    signed_at_ms = str(challenge_ts)
    token = OPENCLAW_GATEWAY_TOKEN
    platform = "linux"
    device_family = ""

    payload = f"v3|{device_id}|{client_id}|{client_mode}|{role}|{scopes}|{signed_at_ms}|{token}|{nonce}|{platform}|{device_family}"
    signature = privkey.sign(payload.encode())

    return {
        "token": token,
        "device": {
            "id": device_id,
            "publicKey": _b64url_no_pad(pubkey.public_bytes_raw()),
            "signature": _b64url_no_pad(signature),
            "signedAt": int(signed_at_ms),
            "nonce": nonce,
        },
    }


def _extract_text(msg: object) -> str:
    """Extract plain text from an OpenClaw chat message.

    Messages come as: {"role":"assistant","content":[{"type":"text","text":"..."}]}
    Each delta is cumulative (full text so far), so we take the last one.
    """
    if isinstance(msg, str):
        return msg.strip()
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    # Content is a list of blocks: [{"type": "text", "text": "..."}]
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    texts.append(t)
        return "\n".join(texts).strip()
    # Content might be a plain string
    if isinstance(content, str):
        return content.strip()
    # Try other common fields
    for key in ("text", "body"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


async def openclaw_chat(message: str, timeout_s: float = 60.0) -> str:
    """Send a message to OpenClaw via WebSocket and wait for the response.

    Connects to OpenClaw's WebSocket gateway, authenticates, sends a
    chat.send request, collects ChatEvent deltas until final, and returns
    the combined response text.
    """
    idempotency_key = str(uuid.uuid4())
    run_id = idempotency_key

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                OPENCLAW_WS_URL,
                timeout=aiohttp.ClientWSTimeout(ws_close=5.0),
            ) as ws:
                # Step 1: Wait for connect.challenge from server
                challenge_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                nonce = ""
                if (
                    challenge_msg.get("type") == "event"
                    and challenge_msg.get("event") == "connect.challenge"
                ):
                    payload = challenge_msg.get("payload", {})
                    nonce = payload.get("nonce", "")
                    challenge_ts = payload.get("ts", int(time.time() * 1000))
                    logger.debug("Got challenge nonce: %s", nonce[:16])
                else:
                    logger.warning("Expected connect.challenge, got: %s", challenge_msg)
                    challenge_ts = int(time.time() * 1000)

                # Step 2: Send connect request with device-signed auth (V3)
                connect_id = str(uuid.uuid4())
                device_auth = _build_device_auth(nonce, challenge_ts)
                connect_frame = {
                    "type": "req",
                    "id": connect_id,
                    "method": "connect",
                    "params": {
                        "minProtocol": PROTOCOL_VERSION,
                        "maxProtocol": PROTOCOL_VERSION,
                        "client": {
                            "id": "cli",
                            "displayName": "Vector Voice",
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": "backend",
                        },
                        "role": "operator",
                        "scopes": ["operator.admin", "operator.read", "operator.write"],
                        "auth": {"token": device_auth["token"]},
                        "device": device_auth["device"],
                    },
                }
                await ws.send_json(connect_frame)

                # Step 3: Wait for hello-ok response
                hello_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                if hello_msg.get("type") == "res" and hello_msg.get("ok"):
                    logger.info("Connected to OpenClaw gateway")
                else:
                    logger.warning("Connect response: %s", json.dumps(hello_msg)[:200])
                    # Try to continue anyway

                # Step 4: Send chat.send
                chat_frame = {
                    "type": "req",
                    "id": run_id,
                    "method": "chat.send",
                    "params": {
                        "sessionKey": VOICE_SESSION_KEY,
                        "message": VOICE_CONTEXT_PREFIX + message,
                        "deliver": False,
                        "idempotencyKey": idempotency_key,
                        "timeoutMs": AGENT_TIMEOUT_MS,
                    },
                }
                await ws.send_json(chat_frame)
                logger.info("Sent chat.send: '%s'", message[:80])

                # Step 4: Collect response
                response_parts: list[str] = []
                deadline = time.monotonic() + timeout_s

                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break

                    try:
                        msg = await asyncio.wait_for(
                            ws.receive_json(), timeout=min(remaining, 5.0)
                        )
                    except asyncio.TimeoutError:
                        continue

                    msg_type = msg.get("type")

                    # Response to our chat.send request (ack)
                    if msg_type == "res" and msg.get("id") == run_id:
                        if msg.get("ok"):
                            payload = msg.get("payload", {})
                            logger.info(
                                "chat.send ack: status=%s",
                                payload.get("status", "unknown"),
                            )
                        else:
                            error = msg.get("error", {})
                            logger.error(
                                "chat.send error: %s",
                                error.get("message", "unknown"),
                            )
                            return f"Sorry, I encountered an error: {error.get('message', 'unknown')}"

                    # ChatEvent with response content
                    elif msg_type == "event" and msg.get("event") == "chat":
                        payload = msg.get("payload", {})
                        state = payload.get("state")
                        chat_msg = payload.get("message")

                        logger.debug(
                            "Chat event: state=%s, message_type=%s, keys=%s",
                            state,
                            type(chat_msg).__name__,
                            list(payload.keys()) if isinstance(payload, dict) else "N/A",
                        )
                        if chat_msg is not None:
                            logger.info(
                                "Chat message: %s",
                                json.dumps(chat_msg)[:200] if not isinstance(chat_msg, str) else chat_msg[:200],
                            )

                        extracted = _extract_text(chat_msg)
                        if extracted:
                            # Replace accumulated text (each delta is cumulative)
                            response_parts.clear()
                            response_parts.append(extracted)

                        if state in ("final", "error", "aborted"):
                            logger.info(
                                "Chat complete: state=%s, parts=%d, payload_keys=%s",
                                state, len(response_parts),
                                list(payload.keys()),
                            )
                            # If still no parts, try extracting from final payload
                            if not response_parts and payload.get("message"):
                                final_text = _extract_text(payload["message"])
                                if final_text:
                                    response_parts.append(final_text)
                            break

                    # Tick events — ignore
                    elif msg_type == "event" and msg.get("event") == "tick":
                        continue

                    # Other events — log and continue
                    elif msg_type == "event":
                        logger.debug("Event: %s", msg.get("event"))

                response = " ".join(response_parts).strip()
                if not response:
                    response = "I processed your request."
                return response

    except asyncio.TimeoutError:
        logger.error("Timeout waiting for OpenClaw response")
        return "Sorry, I took too long to respond."
    except Exception:
        logger.exception("OpenClaw WebSocket error")
        return "Sorry, I had trouble connecting."


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Handle OpenAI-compatible /v1/chat/completions requests.

    Wire-pod sends streaming chat completion requests. We extract the
    user's message, send it to OpenClaw, and stream the response back
    in OpenAI SSE format.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Extract the last user message
    messages = body.get("messages", [])
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_message = msg.get("content", "")
            break

    if not user_message:
        return web.json_response({"error": "no user message"}, status=400)

    logger.info("Voice request: '%s'", user_message[:100])

    # Skip voice processing during active LiveKit calls to prevent echo loops.
    # Bridge writes /tmp/livekit-call-active during calls — just a stat() check.
    if Path("/tmp/livekit-call-active").exists():
        logger.info("LiveKit call active — suppressing voice request")
        return web.json_response({
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "openclaw",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": " "}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    # Check if streaming is requested (wire-pod always uses streaming)
    stream = body.get("stream", False)

    # Serialize requests: if another request is in-flight, wait for it
    # and reuse its response (wire-pod often re-triggers on the same utterance).
    global _last_response, _last_response_time

    if _openclaw_lock.locked():
        logger.info("Request already in-flight, waiting for it to finish...")
        async with _openclaw_lock:
            # Reuse the response from the request that just finished
            if _last_response and (time.monotonic() - _last_response_time) < _RESPONSE_REUSE_WINDOW:
                response_text = _last_response
                logger.info("Reusing in-flight response: '%s'", response_text[:100])
            else:
                response_text = await openclaw_chat(user_message)
                _last_response = response_text
                _last_response_time = time.monotonic()
                logger.info("OpenClaw response: '%s'", response_text[:100])
    else:
        async with _openclaw_lock:
            response_text = await openclaw_chat(user_message)
            _last_response = response_text
            _last_response_time = time.monotonic()
            logger.info("OpenClaw response: '%s'", response_text[:100])

    if stream:
        # SSE streaming response (OpenAI format)
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)

        # Send the response as a single delta chunk
        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model", "openclaw"),
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": response_text},
                    "finish_reason": None,
                }
            ],
        }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

        # Send finish chunk
        finish_chunk = {
            "id": chunk["id"],
            "object": "chat.completion.chunk",
            "created": chunk["created"],
            "model": chunk["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
        await resp.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    else:
        # Non-streaming response
        return web.json_response(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "openclaw"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_text,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )


async def handle_models(request: web.Request) -> web.Response:
    """Handle /v1/models endpoint (wire-pod may check this)."""
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": "openclaw",
                    "object": "model",
                    "owned_by": "openclaw",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app


def main():
    parser = argparse.ArgumentParser(description="OpenClaw voice proxy for wire-pod")
    parser.add_argument("--port", type=int, default=8095, help="Port to listen on")
    args = parser.parse_args()

    logger.info("Starting OpenClaw voice proxy on port %d", args.port)
    logger.info("OpenClaw WebSocket: %s", OPENCLAW_WS_URL)
    logger.info("Voice session key: %s", VOICE_SESSION_KEY)

    app = create_app()
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
