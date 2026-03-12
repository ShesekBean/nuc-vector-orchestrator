"""Unit tests for VoiceCommandRouter — runs in CI without hardware."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    USER_INTENT,
    UserIntentEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.voice_command_router import (
    DEFAULT_DRIVE_DISTANCE_MM,
    DEFAULT_TURN_ANGLE_DEG,
    IntentAction,
    VoiceCommandRouter,
    _build_intent_map,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_speech() -> MagicMock:
    """Create a mock SpeechOutput."""
    speech = MagicMock()
    speech.volume = "medium"
    return speech


class _BridgeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that records requests and returns 200 OK."""

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
        pass  # suppress log output in tests


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
# _build_intent_map tests
# ---------------------------------------------------------------------------


class TestBuildIntentMap:
    def test_returns_dict(self):
        m = _build_intent_map()
        assert isinstance(m, dict)
        assert len(m) > 0

    def test_all_values_are_intent_actions(self):
        for action in _build_intent_map().values():
            assert isinstance(action, IntentAction)
            assert action.method in ("GET", "POST")
            assert action.path.startswith("/")
            assert isinstance(action.confirmation, str)

    def test_forward_intent(self):
        m = _build_intent_map()
        action = m["intent_imperative_forward"]
        assert action.method == "POST"
        assert action.path == "/move"
        assert action.body["type"] == "straight"
        assert action.body["distance_mm"] == DEFAULT_DRIVE_DISTANCE_MM

    def test_stop_intent(self):
        m = _build_intent_map()
        action = m["intent_imperative_stop"]
        assert action.path == "/stop"
        assert action.body is None

    def test_turn_intents_opposite(self):
        m = _build_intent_map()
        left = m["intent_imperative_turn_left"]
        right = m["intent_imperative_turn_right"]
        assert left.body["angle_deg"] == DEFAULT_TURN_ANGLE_DEG
        assert right.body["angle_deg"] == -DEFAULT_TURN_ANGLE_DEG


# ---------------------------------------------------------------------------
# VoiceCommandRouter construction + start/stop
# ---------------------------------------------------------------------------


class TestRouterInit:
    def test_defaults(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)
        assert router.total_handled == 0
        assert router.total_errors == 0
        assert router.total_unknown == 0

    def test_custom_bridge_url(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(
            bus, speech, bridge_url="http://custom:9999"
        )
        assert router._bridge_url == "http://custom:9999"

    def test_start_subscribes(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)
        router.start()
        assert bus.listener_count(USER_INTENT) == 1

    def test_stop_unsubscribes(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)
        router.start()
        router.stop()
        assert bus.listener_count(USER_INTENT) == 0


# ---------------------------------------------------------------------------
# Intent routing via NUC bus
# ---------------------------------------------------------------------------


class TestIntentRouting:
    def test_known_intent_calls_bridge(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        assert router.total_handled == 1
        assert len(_BridgeHandler.requests) == 1
        assert _BridgeHandler.requests[0]["path"] == "/stop"
        speech.speak.assert_called_once_with("Stopping")

    def test_forward_intent_sends_move(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_forward"
        ))

        assert router.total_handled == 1
        req = _BridgeHandler.requests[0]
        assert req["path"] == "/move"
        assert req["body"]["type"] == "straight"
        assert req["body"]["distance_mm"] == DEFAULT_DRIVE_DISTANCE_MM

    def test_turn_left_intent(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_turn_left"
        ))

        req = _BridgeHandler.requests[0]
        assert req["body"]["type"] == "turn"
        assert req["body"]["angle_deg"] == DEFAULT_TURN_ANGLE_DEG

    def test_greeting_hello(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_greeting_hello"
        ))

        assert router.total_handled == 1
        req = _BridgeHandler.requests[0]
        assert req["path"] == "/display"
        assert req["body"]["expression"] == "happy"
        speech.speak.assert_called_once_with("Hello!")

    def test_unknown_intent_increments_counter(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_unknown_thing"
        ))

        assert router.total_unknown == 1
        assert router.total_handled == 0
        assert len(_BridgeHandler.requests) == 0
        speech.speak.assert_not_called()

    def test_emits_command_received(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech, bridge_url=bridge_server)
        router.start()

        received = []
        bus.on(COMMAND_RECEIVED, lambda e: received.append(e))

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        assert len(received) == 1
        assert received[0].command == "intent_imperative_stop"
        assert received[0].source == "sdk_intent"


# ---------------------------------------------------------------------------
# Volume handling
# ---------------------------------------------------------------------------


class TestVolumeHandling:
    def test_volume_up(self):
        bus = NucEventBus()
        speech = _mock_speech()
        speech.volume = "medium"
        router = VoiceCommandRouter(bus, speech)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_volumeup"
        ))

        speech.set_volume.assert_called_once_with("medium_high")
        assert router.total_handled == 1

    def test_volume_down(self):
        bus = NucEventBus()
        speech = _mock_speech()
        speech.volume = "medium"
        router = VoiceCommandRouter(bus, speech)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_volumedown"
        ))

        speech.set_volume.assert_called_once_with("medium_low")

    def test_volume_up_at_max(self):
        bus = NucEventBus()
        speech = _mock_speech()
        speech.volume = "high"
        router = VoiceCommandRouter(bus, speech)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_volumeup"
        ))

        speech.set_volume.assert_called_once_with("high")

    def test_volume_down_at_min(self):
        bus = NucEventBus()
        speech = _mock_speech()
        speech.volume = "mute"
        router = VoiceCommandRouter(bus, speech)
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_volumedown"
        ))

        speech.set_volume.assert_called_once_with("mute")


# ---------------------------------------------------------------------------
# Bridge error handling
# ---------------------------------------------------------------------------


class TestBridgeErrors:
    def test_bridge_connection_refused(self):
        bus = NucEventBus()
        speech = _mock_speech()
        # Use a port that nothing is listening on.
        router = VoiceCommandRouter(
            bus, speech, bridge_url="http://127.0.0.1:19999"
        )
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        assert router.total_errors == 1
        speech.speak.assert_called_once_with("Command failed")


# ---------------------------------------------------------------------------
# SDK event bridging
# ---------------------------------------------------------------------------


class TestSdkEventBridging:
    def test_sdk_user_intent_emits_bus_event(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)

        received = []
        bus.on(USER_INTENT, lambda e: received.append(e))

        # Simulate SDK event
        msg = MagicMock()
        msg.intent = "intent_imperative_stop"
        msg.param = None
        msg.params = None
        msg.metadata = None

        router._on_sdk_user_intent(None, "user_intent", msg)

        assert len(received) == 1
        assert received[0].intent == "intent_imperative_stop"

    def test_sdk_intent_with_params(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)

        received = []
        bus.on(USER_INTENT, lambda e: received.append(e))

        msg = MagicMock()
        msg.intent = "intent_imperative_forward"
        msg.param = None
        msg.params = {"distance": 500}
        msg.metadata = None

        router._on_sdk_user_intent(None, "user_intent", msg)

        assert received[0].params == {"distance": 500}

    def test_sdk_intent_fallback_to_intent_type(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)

        received = []
        bus.on(USER_INTENT, lambda e: received.append(e))

        msg = MagicMock()
        msg.intent = ""
        msg.intent_type = "intent_greeting_hello"
        msg.param = None
        msg.params = None
        msg.metadata = None

        router._on_sdk_user_intent(None, "user_intent", msg)

        assert received[0].intent == "intent_greeting_hello"

    def test_start_sdk_listener_without_anki_vector(self):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(bus, speech)

        with patch.dict("sys.modules", {"anki_vector": None, "anki_vector.events": None}):
            # Should not raise — just logs a warning.
            router.start_sdk_listener(MagicMock())


# ---------------------------------------------------------------------------
# Custom intent map
# ---------------------------------------------------------------------------


class TestCustomIntentMap:
    def test_override_intent_map(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        custom_map = {
            "my_custom_intent": IntentAction(
                "POST", "/head", {"angle_deg": 45}, "Looking up",
            ),
        }
        router = VoiceCommandRouter(
            bus, speech, bridge_url=bridge_server, intent_map=custom_map
        )
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(intent="my_custom_intent"))

        assert router.total_handled == 1
        req = _BridgeHandler.requests[0]
        assert req["path"] == "/head"
        assert req["body"]["angle_deg"] == 45
        speech.speak.assert_called_once_with("Looking up")

    def test_default_intents_not_in_custom_map(self, bridge_server: str):
        bus = NucEventBus()
        speech = _mock_speech()
        router = VoiceCommandRouter(
            bus, speech, bridge_url=bridge_server, intent_map={}
        )
        router.start()

        bus.emit(USER_INTENT, UserIntentEvent(
            intent="intent_imperative_stop"
        ))

        # Should be unknown since custom map is empty.
        assert router.total_unknown == 1
