"""Async WebSocket client for OpenClaw gateway chat.send.

Extracted from ``scripts/openclaw-voice-proxy.py`` for reuse by the
companion dispatcher.  Each call opens a fresh WebSocket, authenticates,
sends a chat message on a given session key, and collects the streamed
response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import aiohttp

logger = logging.getLogger(__name__)

OPENCLAW_WS_URL = "ws://127.0.0.1:18889"
OPENCLAW_GATEWAY_TOKEN = "fed3aea80e03410f8dae71c586049e85af3929b10d1f7a36508cabf05a5ec505"
PROTOCOL_VERSION = 3
AGENT_TIMEOUT_MS = 90_000


def _extract_text(msg: object) -> str:
    """Extract plain text from an OpenClaw chat message."""
    if isinstance(msg, str):
        return msg.strip()
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    texts.append(t)
        return "\n".join(texts).strip()
    if isinstance(content, str):
        return content.strip()
    for key in ("text", "body"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


async def openclaw_chat(
    message: str,
    session_key: str = "hook:companion",
    timeout_s: float = 90.0,
    display_name: str = "Vector Companion",
) -> str:
    """Send *message* to OpenClaw and return the agent's text response.

    Parameters
    ----------
    message:
        The full text to send (including any context prefix).
    session_key:
        OpenClaw session key.  Different keys maintain separate threads.
    timeout_s:
        Maximum time to wait for a complete response.
    display_name:
        Client display name shown in OpenClaw logs.
    """
    idempotency_key = str(uuid.uuid4())
    run_id = idempotency_key

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                OPENCLAW_WS_URL,
                timeout=aiohttp.ClientWSTimeout(ws_close=5.0),
            ) as ws:
                # 1. connect.challenge
                challenge_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                if not (
                    challenge_msg.get("type") == "event"
                    and challenge_msg.get("event") == "connect.challenge"
                ):
                    logger.warning("Expected connect.challenge, got: %s", challenge_msg)

                # 2. connect
                await ws.send_json({
                    "type": "req",
                    "id": str(uuid.uuid4()),
                    "method": "connect",
                    "params": {
                        "minProtocol": PROTOCOL_VERSION,
                        "maxProtocol": PROTOCOL_VERSION,
                        "client": {
                            "id": "gateway-client",
                            "displayName": display_name,
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": "backend",
                        },
                        "auth": {"token": OPENCLAW_GATEWAY_TOKEN},
                        "scopes": ["operator.admin"],
                    },
                })

                # 3. hello-ok
                hello_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                if not (hello_msg.get("type") == "res" and hello_msg.get("ok")):
                    logger.warning("Connect response: %s", json.dumps(hello_msg)[:200])

                # 4. chat.send
                await ws.send_json({
                    "type": "req",
                    "id": run_id,
                    "method": "chat.send",
                    "params": {
                        "sessionKey": session_key,
                        "message": message,
                        "deliver": False,
                        "idempotencyKey": idempotency_key,
                        "timeoutMs": AGENT_TIMEOUT_MS,
                    },
                })
                logger.info("Sent companion signal (%d chars)", len(message))

                # 5. Collect response
                response_parts: list[str] = []
                deadline = time.monotonic() + timeout_s

                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive_json(), timeout=min(remaining, 5.0),
                        )
                    except asyncio.TimeoutError:
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "res" and msg.get("id") == run_id:
                        if not msg.get("ok"):
                            error = msg.get("error", {})
                            return f"Error: {error.get('message', 'unknown')}"

                    elif msg_type == "event" and msg.get("event") == "chat":
                        payload = msg.get("payload", {})
                        state = payload.get("state")
                        extracted = _extract_text(payload.get("message"))
                        if extracted:
                            response_parts.clear()
                            response_parts.append(extracted)
                        if state in ("final", "error", "aborted"):
                            if not response_parts and payload.get("message"):
                                final = _extract_text(payload["message"])
                                if final:
                                    response_parts.append(final)
                            break

                return " ".join(response_parts).strip() or "OK"

    except asyncio.TimeoutError:
        logger.error("Timeout waiting for OpenClaw companion response")
        return "timeout"
    except Exception:
        logger.exception("OpenClaw companion WebSocket error")
        return "error"
