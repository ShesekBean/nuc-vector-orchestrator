"""Tests for the MessageRelay module (voice-to-Signal relay)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    MESSAGE_RELAYED,
    MessageRelayedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.intercom import Intercom
from apps.vector.src.voice.message_relay import (
    MessageRelay,
    extract_relay_message,
)


# ---------------------------------------------------------------------------
# Fake intercom server (reuses pattern from test_intercom.py)
# ---------------------------------------------------------------------------


class _FakeHandler(BaseHTTPRequestHandler):
    received: list[dict] = []  # noqa: RUF012

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode()) if length else {}
        _FakeHandler.received.append({"path": self.path, "payload": payload})
        data = json.dumps({"status": "sent"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        data = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_server():
    _FakeHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _FakeHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture()
def bus():
    return NucEventBus()


# ---------------------------------------------------------------------------
# extract_relay_message — pattern matching tests
# ---------------------------------------------------------------------------


class TestExtractRelayMessage:
    """Test the regex pattern matching for relay commands."""

    def test_tell_ophir_simple(self):
        assert extract_relay_message("tell Ophir I'll be late") == "I'll be late"

    def test_tell_ophir_case_insensitive(self):
        assert extract_relay_message("Tell OPHIR I'm heading out") == "I'm heading out"

    def test_tell_ophir_with_that(self):
        assert (
            extract_relay_message("tell Ophir that I'm on my way")
            == "I'm on my way"
        )

    def test_tell_ophir_with_wake_word_prefix(self):
        assert (
            extract_relay_message("hey vector, tell Ophir I'll be there in 5")
            == "I'll be there in 5"
        )

    def test_tell_ophir_trailing_period_stripped(self):
        assert extract_relay_message("tell Ophir I'm done.") == "I'm done"

    def test_message_ophir(self):
        assert (
            extract_relay_message("message Ophir the meeting is cancelled")
            == "the meeting is cancelled"
        )

    def test_let_ophir_know(self):
        assert (
            extract_relay_message("let Ophir know I finished the task")
            == "I finished the task"
        )

    def test_let_ophir_know_that(self):
        assert (
            extract_relay_message("let Ophir know that dinner is ready")
            == "dinner is ready"
        )

    def test_send_ophir_a_message(self):
        assert (
            extract_relay_message("send Ophir a message saying I'm running late")
            == "I'm running late"
        )

    def test_send_a_message_to_ophir(self):
        assert (
            extract_relay_message("send a message to Ophir saying hello")
            == "hello"
        )

    def test_hey_vector_let_ophir_know(self):
        assert (
            extract_relay_message("hey vector let Ophir know I left")
            == "I left"
        )

    def test_no_match_unrelated(self):
        assert extract_relay_message("what's the weather today") is None

    def test_no_match_ophir_not_relay(self):
        assert extract_relay_message("who is Ophir") is None

    def test_no_match_empty(self):
        assert extract_relay_message("") is None

    def test_no_match_whitespace(self):
        assert extract_relay_message("   ") is None

    def test_no_match_tell_someone_else(self):
        assert extract_relay_message("tell John I'll be late") is None

    def test_tell_ophir_empty_body(self):
        # "tell Ophir" with nothing after → should not match (empty body)
        assert extract_relay_message("tell Ophir") is None

    def test_accented_speech_normalized(self):
        # gpt-4o-transcribe normalizes accented speech to standard spelling
        assert (
            extract_relay_message("tell Ophir I will be there soon")
            == "I will be there soon"
        )

    def test_long_message(self):
        body = "I finished the deployment and everything looks good on staging"
        assert extract_relay_message(f"tell Ophir {body}") == body


# ---------------------------------------------------------------------------
# MessageRelay.try_relay — integration with Intercom
# ---------------------------------------------------------------------------


class TestTryRelay:
    def test_relay_sends_text(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom, nuc_bus=bus)

        result = relay.try_relay("tell Ophir I'm heading out")

        assert result is not None
        assert "sent" in result.lower()
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/receive"
        assert _FakeHandler.received[0]["payload"]["text"] == "I'm heading out"

    def test_relay_returns_none_for_non_relay(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom)

        result = relay.try_relay("what time is it")

        assert result is None
        assert len(_FakeHandler.received) == 0

    def test_relay_emits_event(self, fake_server: str, bus: NucEventBus):
        events: list[MessageRelayedEvent] = []
        bus.on(MESSAGE_RELAYED, events.append)

        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom, nuc_bus=bus)
        relay.try_relay("tell Ophir the build passed")

        assert len(events) == 1
        assert events[0].extracted_message == "the build passed"
        assert events[0].success is True
        assert "tell Ophir" in events[0].original_text

    def test_relay_failure_returns_error_message(self, bus: NucEventBus):
        events: list[MessageRelayedEvent] = []
        bus.on(MESSAGE_RELAYED, events.append)

        intercom = Intercom(intercom_url="http://127.0.0.1:1")
        relay = MessageRelay(intercom, nuc_bus=bus)
        result = relay.try_relay("tell Ophir test message")

        assert result is not None
        assert "couldn't" in result.lower() or "could not" in result.lower()
        assert len(events) == 1
        assert events[0].success is False

    def test_relay_calls_speech(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        speech = MagicMock()
        relay = MessageRelay(intercom, speech=speech)

        relay.try_relay("tell Ophir hello")

        speech.speak.assert_called_once()
        spoken = speech.speak.call_args[0][0]
        assert "sent" in spoken.lower()

    def test_relay_no_speech_when_none(self, fake_server: str):
        """Speech output is optional — no error when not provided."""
        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom, speech=None)

        result = relay.try_relay("tell Ophir hello")
        assert result is not None

    def test_relay_with_hey_vector_prefix(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom)

        result = relay.try_relay("hey vector, tell Ophir I'll be 10 minutes late")

        assert result is not None
        assert _FakeHandler.received[0]["payload"]["text"] == "I'll be 10 minutes late"


# ---------------------------------------------------------------------------
# Voice bridge integration (mock-based)
# ---------------------------------------------------------------------------


class TestVoiceBridgeIntegration:
    """Verify that OpenClawVoiceBridge skips OpenClaw when relay handles."""

    def test_relay_skips_openclaw(self, fake_server: str, bus: NucEventBus):
        """When relay matches, OpenClaw hooks should not be called."""
        from unittest.mock import patch

        from apps.vector.src.voice.openclaw_voice_bridge import (
            OpenClawVoiceBridge,
        )

        audio_client = MagicMock()
        audio_client.chunk_count = 0

        intercom = Intercom(intercom_url=fake_server)
        relay = MessageRelay(intercom, nuc_bus=bus)

        bridge = OpenClawVoiceBridge(
            nuc_bus=bus,
            audio_client=audio_client,
            robot=None,
            hooks_url="http://127.0.0.1:1",
            hooks_token="test-token",
            openai_api_key="test-key",
            message_relay=relay,
        )

        # Patch _record_speech and _transcribe to simulate voice input
        with (
            patch.object(bridge, "_record_speech", return_value=b"\x00" * 3200),
            patch.object(
                bridge, "_transcribe", return_value="tell Ophir the build passed"
            ),
            patch(
                "apps.vector.src.voice.openclaw_voice_bridge._send_to_openclaw"
            ) as mock_openclaw,
        ):
            bridge._interaction_loop()

        # OpenClaw should NOT have been called
        mock_openclaw.assert_not_called()

        # Intercom SHOULD have been called
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["payload"]["text"] == "the build passed"

    def test_non_relay_still_queries_openclaw(self, bus: NucEventBus):
        """Non-relay messages should still go through OpenClaw."""
        from unittest.mock import patch

        from apps.vector.src.voice.openclaw_voice_bridge import (
            OpenClawVoiceBridge,
        )

        audio_client = MagicMock()
        audio_client.chunk_count = 0

        relay = MessageRelay(
            Intercom(intercom_url="http://127.0.0.1:1"),
            nuc_bus=bus,
        )

        bridge = OpenClawVoiceBridge(
            nuc_bus=bus,
            audio_client=audio_client,
            robot=None,
            hooks_url="http://127.0.0.1:18889/hooks/agent",
            hooks_token="test-token",
            openai_api_key="test-key",
            message_relay=relay,
        )

        with (
            patch.object(bridge, "_record_speech", return_value=b"\x00" * 3200),
            patch.object(
                bridge, "_transcribe", return_value="what is the weather today"
            ),
            patch(
                "apps.vector.src.voice.openclaw_voice_bridge._send_to_openclaw",
                return_value="It's sunny!",
            ) as mock_openclaw,
            patch.object(bridge, "_speak"),
        ):
            bridge._interaction_loop()

        # OpenClaw SHOULD have been called for non-relay messages
        mock_openclaw.assert_called_once()
