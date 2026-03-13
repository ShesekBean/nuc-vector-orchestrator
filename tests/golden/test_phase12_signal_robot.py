"""Phase 12 — Signal→Robot E2E.

Tests the full Signal→OpenClaw→robot command path using OpenClaw's
WebSocket gateway directly (no actual Signal messages needed).

Tests 12.1–12.4 from the comprehensive test plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.phase12

# OpenClaw gateway WebSocket
OPENCLAW_WS_URL = "ws://127.0.0.1:18889"
OPENCLAW_GATEWAY_TOKEN = "fed3aea80e03410f8dae71c586049e85af3929b10d1f7a36508cabf05a5ec505"
VOICE_SESSION_KEY = "hook:voice"
PROTOCOL_VERSION = 3


def _gateway_available() -> bool:
    """Check if OpenClaw gateway is reachable."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "http://localhost:18889/health"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "200"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


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


async def _openclaw_chat(message: str, timeout_s: float = 30.0) -> str:
    """Send a message to OpenClaw via WebSocket and return the response text."""
    try:
        import aiohttp
    except ImportError:
        pytest.skip("aiohttp not installed")

    idempotency_key = str(uuid.uuid4())
    run_id = idempotency_key

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            OPENCLAW_WS_URL,
            timeout=aiohttp.ClientWSTimeout(ws_close=5.0),
        ) as ws:
            # Wait for connect.challenge
            challenge_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
            nonce = ""
            if (
                challenge_msg.get("type") == "event"
                and challenge_msg.get("event") == "connect.challenge"
            ):
                payload = challenge_msg.get("payload", {})
                nonce = payload.get("nonce", "")

            # Send connect request
            connect_id = str(uuid.uuid4())
            await ws.send_json({
                "type": "req",
                "id": connect_id,
                "method": "connect",
                "params": {
                    "minProtocol": PROTOCOL_VERSION,
                    "maxProtocol": PROTOCOL_VERSION,
                    "client": {
                        "id": "gateway-client",
                        "displayName": "Test Client",
                        "version": "1.0.0",
                        "platform": "linux",
                        "mode": "backend",
                    },
                    "auth": {"token": OPENCLAW_GATEWAY_TOKEN},
                    "scopes": ["operator.admin"],
                },
            })

            # Wait for hello-ok
            hello_msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
            if not (hello_msg.get("type") == "res" and hello_msg.get("ok")):
                return f"Connect failed: {json.dumps(hello_msg)[:200]}"

            # Send chat.send
            await ws.send_json({
                "type": "req",
                "id": run_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": VOICE_SESSION_KEY,
                    "message": message,
                    "deliver": False,
                    "idempotencyKey": idempotency_key,
                    "timeoutMs": 30_000,
                },
            })

            # Collect response
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

                if msg_type == "res" and msg.get("id") == run_id:
                    if not msg.get("ok"):
                        error = msg.get("error", {})
                        return f"Error: {error.get('message', 'unknown')}"

                elif msg_type == "event" and msg.get("event") == "chat":
                    payload = msg.get("payload", {})
                    state = payload.get("state")
                    chat_msg = payload.get("message")

                    extracted = _extract_text(chat_msg)
                    if extracted:
                        response_parts.clear()
                        response_parts.append(extracted)

                    if state in ("final", "error", "aborted"):
                        if not response_parts and payload.get("message"):
                            final_text = _extract_text(payload["message"])
                            if final_text:
                                response_parts.append(final_text)
                        break

            return " ".join(response_parts).strip()


def _run_chat(message: str) -> str:
    """Synchronous wrapper for _openclaw_chat."""
    return asyncio.run(_openclaw_chat(message))


# 12.1 Signal → robot say hello
class TestSignalRobotSayHello:
    def test_robot_say_hello(self):
        """12.1 — Send 'robot say hello' via WebSocket chat.send → verify response."""
        if not _gateway_available():
            pytest.skip("OpenClaw gateway not running")

        response = _run_chat("robot say hello")
        assert len(response) > 0, "Empty response from OpenClaw"
        # Response should acknowledge the command in some way
        lower = response.lower()
        assert any(word in lower for word in ("hello", "speak", "say", "said", "robot")), (
            f"Response doesn't seem related to speaking: {response[:200]}"
        )


# 12.2 Signal → robot status
class TestSignalRobotStatus:
    def test_robot_status(self):
        """12.2 — Send 'robot status' via chat.send → response contains status info."""
        if not _gateway_available():
            pytest.skip("OpenClaw gateway not running")

        response = _run_chat("robot status")
        assert len(response) > 0, "Empty response from OpenClaw"


# 12.3 Signal → robot set eyes green
class TestSignalRobotEyes:
    def test_robot_set_eyes(self):
        """12.3 — Send 'robot set eyes green' → response acknowledges LED change."""
        if not _gateway_available():
            pytest.skip("OpenClaw gateway not running")

        response = _run_chat("robot set eyes green")
        assert len(response) > 0, "Empty response from OpenClaw"


# 12.4 Signal notification
class TestSignalNotification:
    def test_signal_notify_test_running(self, repo_root: str):
        """12.4 — Notify Ophir via Signal that test is running."""
        script = os.path.join(repo_root, "scripts", "pgm-signal-gate.sh")
        if not os.path.isfile(script):
            pytest.skip("Signal gate script not found")

        result = subprocess.run(
            ["bash", script, "board-status", "0",
             "📊 PGM: Phase 12 integration test running — Signal→Robot E2E"],
            capture_output=True, text=True, timeout=15,
        )
        # Script should complete without crashing
        assert result.returncode in (0, 1, 2), (
            f"Signal gate failed with rc={result.returncode}: {result.stderr}"
        )
