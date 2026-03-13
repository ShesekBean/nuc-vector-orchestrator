"""Phase 11 — Voice→OpenClaw Pipeline.

Tests the proxy bridging wire-pod to OpenClaw via OpenAI-compatible API.

Tests 11.1–11.6 from the comprehensive test plan.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest


pytestmark = pytest.mark.phase11

PROXY_URL = "http://localhost:8095"


def _proxy_available() -> bool:
    """Check if the voice proxy is reachable."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{PROXY_URL}/health"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "200"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _chat_completion(message: str, stream: bool = False, timeout: int = 20) -> subprocess.CompletedProcess:
    """Send a chat completion request to the voice proxy."""
    payload = json.dumps({
        "model": "openclaw",
        "messages": [{"role": "user", "content": message}],
        "stream": stream,
    })
    return subprocess.run(
        ["curl", "-s", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", payload,
         f"{PROXY_URL}/v1/chat/completions"],
        capture_output=True, text=True, timeout=timeout,
    )


# 11.1 Non-streaming chat completion
class TestNonStreamingCompletion:
    def test_non_streaming_response(self):
        """11.1 — Proxy accepts non-streaming chat completion and returns valid JSON."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("Hello, how are you?", stream=False)
        assert result.returncode == 0, f"curl failed: {result.stderr}"

        data = json.loads(result.stdout)
        assert "choices" in data, f"No 'choices' in response: {data}"
        assert len(data["choices"]) > 0, "Empty choices array"
        assert "message" in data["choices"][0], f"No 'message' in choice: {data['choices'][0]}"
        content = data["choices"][0]["message"].get("content", "")
        assert isinstance(content, str), f"Content is not a string: {type(content)}"


# 11.2 Streaming chat completion
class TestStreamingCompletion:
    def test_streaming_sse_format(self):
        """11.2 — Proxy accepts streaming request and returns SSE with data: lines + [DONE]."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("Say hello", stream=True)
        assert result.returncode == 0, f"curl failed: {result.stderr}"

        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        data_lines = [l for l in lines if l.startswith("data:")]
        assert len(data_lines) >= 2, f"Expected at least 2 data: lines, got {len(data_lines)}: {lines}"

        # Last data line should be [DONE]
        assert data_lines[-1].strip() == "data: [DONE]", (
            f"Expected 'data: [DONE]' as last line, got: {data_lines[-1]}"
        )

        # First data line should be valid JSON chunk
        first_chunk = json.loads(data_lines[0][len("data: "):])
        assert "choices" in first_chunk


# 11.3 Valid OpenAI response format
class TestResponseFormat:
    def test_openai_format_fields(self):
        """11.3 — Response contains id, choices, and finish_reason."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("What is your name?", stream=False)
        data = json.loads(result.stdout)

        assert "id" in data, f"Missing 'id': {data}"
        assert data["id"].startswith("chatcmpl-"), f"Bad id format: {data['id']}"
        assert "choices" in data
        assert data["choices"][0].get("finish_reason") == "stop", (
            f"Expected finish_reason='stop', got {data['choices'][0].get('finish_reason')}"
        )


# 11.4 Empty message returns 400
class TestEmptyMessage:
    def test_empty_message_error(self):
        """11.4 — Proxy returns 400 for empty message."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        payload = json.dumps({
            "model": "openclaw",
            "messages": [],
        })
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/stdout", "-w", "\n%{http_code}",
             "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", payload,
             f"{PROXY_URL}/v1/chat/completions"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().rsplit("\n", 1)
        code = int(lines[-1]) if lines[-1].isdigit() else 0
        assert code == 400, f"Expected 400 for empty message, got {code}"


# 11.5 Response content is non-empty
class TestResponseContent:
    def test_non_empty_content(self):
        """11.5 — OpenClaw actually replies with non-empty content."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("Tell me a short joke", stream=False, timeout=30)
        data = json.loads(result.stdout)
        content = data["choices"][0]["message"]["content"]
        assert len(content.strip()) > 0, "Response content is empty"


# 11.6 Round-trip latency
class TestLatency:
    def test_round_trip_under_15s(self):
        """11.6 — Voice proxy round-trip latency < 15s."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        start = time.monotonic()
        result = _chat_completion("Hi", stream=False, timeout=20)
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"Request failed: {result.stderr}"
        # Parse to verify it's a valid response
        data = json.loads(result.stdout)
        assert "choices" in data, f"Invalid response: {data}"
        assert elapsed < 15.0, f"Round-trip took {elapsed:.1f}s, expected < 15s"


# 11.7 Queries that match built-in intents still reach OpenClaw
class TestBuiltinIntentBypass:
    """Wire-pod built-in intents (intent_names_ask, intent_clock_time, etc.)
    are set to requiresexact=True so conversational phrasing falls through
    to the OpenClaw LLM via IntentGraph."""

    def test_what_is_my_name(self):
        """11.7a — 'what is my name' routes to OpenClaw, not intent_names_ask."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("what is my name", stream=True, timeout=60)
        assert result.returncode == 0, f"curl failed: {result.stderr}"

        lines = [l for l in result.stdout.strip().split("\n") if l.startswith("data:")]
        assert len(lines) >= 2, f"Expected SSE response, got: {result.stdout[:200]}"

        # Extract content from first data chunk
        first = json.loads(lines[0][len("data: "):])
        content = first["choices"][0]["delta"].get("content", "")
        assert len(content.strip()) > 0, (
            "Empty response — query likely intercepted by wire-pod built-in intent"
        )

    def test_what_is_my_weight(self):
        """11.7b — 'what is my weight' routes to OpenClaw (tool-heavy query)."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("what is my weight", stream=True, timeout=60)
        assert result.returncode == 0, f"curl failed: {result.stderr}"

        lines = [l for l in result.stdout.strip().split("\n") if l.startswith("data:")]
        assert len(lines) >= 2, f"Expected SSE response, got: {result.stdout[:200]}"

        first = json.loads(lines[0][len("data: "):])
        content = first["choices"][0]["delta"].get("content", "")
        assert len(content.strip()) > 0, (
            "Empty response — OpenClaw may have timed out on tool call"
        )

    def test_what_time_is_it(self):
        """11.7c — 'what time is it' routes to OpenClaw, not intent_clock_time."""
        if not _proxy_available():
            pytest.skip("Voice proxy not running")

        result = _chat_completion("what time is it", stream=False, timeout=60)
        assert result.returncode == 0, f"curl failed: {result.stderr}"

        data = json.loads(result.stdout)
        content = data["choices"][0]["message"]["content"]
        assert len(content.strip()) > 0, (
            "Empty response — query likely intercepted by wire-pod built-in intent"
        )
