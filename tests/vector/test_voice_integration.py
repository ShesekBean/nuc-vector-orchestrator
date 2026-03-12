"""Integration tests for voice command routing.

Tests the full voice→OpenClaw→bridge→action flow and the dual-path
routing (wire-pod intents + OpenClaw agent).
"""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    STT_RESULT,
    TTS_PLAYING,
    USER_INTENT,
    UserIntentEvent,
    WakeWordDetectedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.openclaw_voice_bridge import (
    OpenClawVoiceBridge,
)
from apps.vector.src.voice.voice_command_router import VoiceCommandRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm(n_samples: int = 1600, amplitude: int = 1000) -> bytes:
    """Generate PCM audio with a specific amplitude."""
    import math
    samples = [
        int(amplitude * math.sin(2 * math.pi * 440 * i / 16000))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def _mock_audio_client():
    """Create a mock AudioClient."""
    client = MagicMock()
    type(client).chunk_count = property(lambda self: 0)
    client.get_latest_chunk.return_value = None
    return client


def _mock_robot():
    """Create a mock robot with say_text()."""
    robot = MagicMock()
    robot.behavior.say_text = MagicMock()
    return robot


def _mock_speech():
    """Create a mock SpeechOutput."""
    speech = MagicMock()
    speech.volume = "medium"
    return speech


class _BridgeHandler(BaseHTTPRequestHandler):
    """Records requests and returns 200 OK."""

    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        _BridgeHandler.requests.append({
            "method": "POST",
            "path": self.path,
            "body": json.loads(body) if body else None,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture()
def bridge_server():
    """Start a local HTTP server to simulate the bridge."""
    _BridgeHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# Integration: both paths emit COMMAND_RECEIVED on same bus
# ---------------------------------------------------------------------------


class TestDualPathRouting:
    """Both wire-pod intents and OpenClaw voice bridge share the same NUC bus."""

    def test_both_paths_emit_command_received(self, bridge_server: str):
        """Wire-pod intent AND OpenClaw voice bridge both emit COMMAND_RECEIVED."""
        bus = NucEventBus()
        speech = _mock_speech()
        audio = _mock_audio_client()
        robot = _mock_robot()

        # Set up both routing paths on the same bus.
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )
        bridge.start()

        received: list = []
        bus.on(COMMAND_RECEIVED, lambda e: received.append(e))

        # Path 1: Wire-pod intent → COMMAND_RECEIVED(source="sdk_intent")
        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        assert len(received) == 1
        assert received[0].source == "sdk_intent"
        assert received[0].command == "intent_imperative_stop"

        # Path 2: OpenClaw voice bridge → COMMAND_RECEIVED(source="voice")
        # Mock the internal pipeline for the bridge.
        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(return_value="follow me")
        bridge._query_openclaw = MagicMock(return_value="Following you now")

        bridge._on_wake_word(WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=1.0, source="sdk"
        ))
        time.sleep(0.5)

        assert len(received) == 2
        assert received[1].source == "voice"
        assert received[1].command == "follow me"

        bridge.stop()
        router.stop()

    def test_wire_pod_stop_hits_bridge_endpoint(self, bridge_server: str):
        """Wire-pod 'stop' intent sends POST /stop to bridge."""
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        assert len(_BridgeHandler.requests) == 1
        assert _BridgeHandler.requests[0]["path"] == "/stop"
        assert _BridgeHandler.requests[0]["method"] == "POST"
        router.stop()

    def test_wire_pod_forward_hits_move_endpoint(self, bridge_server: str):
        """Wire-pod 'forward' intent sends POST /move to bridge."""
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_forward"
        ))

        req = _BridgeHandler.requests[0]
        assert req["path"] == "/move"
        assert req["body"]["type"] == "straight"
        assert req["body"]["distance_mm"] > 0
        router.stop()


# ---------------------------------------------------------------------------
# Integration: OpenClaw voice pipeline (mocked end-to-end)
# ---------------------------------------------------------------------------


class TestOpenClawVoicePipeline:
    """Tests the full OpenClaw voice pipeline: wake→record→STT→agent→TTS."""

    def test_robot_command_via_openclaw(self):
        """Voice command 'follow me' routes through OpenClaw and speaks confirmation."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )
        bridge.start()

        # Track events
        stt_events: list = []
        cmd_events: list = []
        tts_events: list = []
        bus.on(STT_RESULT, lambda e: stt_events.append(e))
        bus.on(COMMAND_RECEIVED, lambda e: cmd_events.append(e))
        bus.on(TTS_PLAYING, lambda e: tts_events.append(e))

        # Mock internals
        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(return_value="follow me")
        bridge._query_openclaw = MagicMock(return_value="Following you now")

        bridge._on_wake_word(WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=1.0, source="sdk"
        ))
        time.sleep(0.5)

        # STT result emitted
        assert len(stt_events) == 1
        assert stt_events[0].text == "follow me"

        # COMMAND_RECEIVED emitted with source="voice"
        assert len(cmd_events) == 1
        assert cmd_events[0].command == "follow me"
        assert cmd_events[0].source == "voice"

        # TTS events (start + stop)
        assert len(tts_events) == 2
        assert tts_events[0].playing is True
        assert tts_events[1].playing is False

        # say_text called with agent response
        robot.behavior.say_text.assert_called_once_with("Following you now")

        bridge.stop()

    def test_financial_query_via_openclaw(self):
        """Monarch Money query routes through OpenClaw and speaks response."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )

        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(
            return_value="how much did I spend this month"
        )
        bridge._query_openclaw = MagicMock(
            return_value="You spent $2,450 this month"
        )

        bridge._on_wake_word(WakeWordDetectedEvent(
            model="hey_jarvis", confidence=0.9, source="openwakeword"
        ))
        time.sleep(0.5)

        robot.behavior.say_text.assert_called_once_with(
            "You spent $2,450 this month"
        )
        assert bridge.total_errors == 0

        bridge.stop()

    def test_natural_language_variations(self):
        """Different phrasings of the same command all route through OpenClaw."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        commands = [
            ("stop", "Stopping now"),
            ("halt", "Stopping now"),
            ("freeze", "Okay, I stopped"),
        ]

        for text, response in commands:
            bridge = OpenClawVoiceBridge(
                bus, audio, robot,
                openai_api_key="sk-test",
                hooks_token="tok",
            )

            bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
            bridge._transcribe = MagicMock(return_value=text)
            bridge._query_openclaw = MagicMock(return_value=response)

            bridge._on_wake_word(WakeWordDetectedEvent(
                model="hey_vector_sdk", confidence=1.0, source="sdk"
            ))
            time.sleep(0.3)

            assert bridge.total_errors == 0
            bridge.stop()

    def test_unknown_command_handled_gracefully(self):
        """Unknown command gets a graceful response from OpenClaw agent."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )

        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(return_value="fly to the moon")
        bridge._query_openclaw = MagicMock(
            return_value="I can't do that, but I can help with other things"
        )

        bridge._on_wake_word(WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=1.0, source="sdk"
        ))
        time.sleep(0.5)

        robot.behavior.say_text.assert_called_once_with(
            "I can't do that, but I can help with other things"
        )
        assert bridge.total_errors == 0
        bridge.stop()


# ---------------------------------------------------------------------------
# Integration: event bus coordination
# ---------------------------------------------------------------------------


class TestEventBusCoordination:
    """Tests that events are properly shared across components."""

    def test_command_received_triggers_expression_engine(self):
        """COMMAND_RECEIVED event is visible to all bus subscribers."""
        bus = NucEventBus()
        speech = _mock_speech()

        # Simulate expression engine subscriber
        expression_events: list = []
        bus.on(COMMAND_RECEIVED, lambda e: expression_events.append(e))

        # Wire-pod path
        router = VoiceCommandRouter(
            bus, speech, bridge_url="http://127.0.0.1:19999"  # will fail
        )
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_greeting_hello"
        ))

        # Expression engine would see the COMMAND_RECEIVED event
        assert len(expression_events) == 1
        assert expression_events[0].source == "sdk_intent"

        router.stop()

    def test_tts_events_suppress_wake_word(self):
        """TTS_PLAYING events from voice bridge are visible to wake word detector."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        tts_events: list = []
        bus.on(TTS_PLAYING, lambda e: tts_events.append(e))

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )

        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(return_value="hello")
        bridge._query_openclaw = MagicMock(return_value="Hi there!")

        bridge._on_wake_word(WakeWordDetectedEvent(
            model="test", confidence=1.0, source="sdk"
        ))
        time.sleep(0.5)

        # TTS start and stop events are emitted
        assert len(tts_events) == 2
        assert tts_events[0].playing is True
        assert tts_events[1].playing is False
        bridge.stop()
