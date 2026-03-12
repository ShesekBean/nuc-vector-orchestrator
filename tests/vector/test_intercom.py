"""Tests for the Intercom module (text + photo to Signal via intercom-server)."""

from __future__ import annotations

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import pytest

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    INTERCOM_PHOTO_SENT,
    INTERCOM_TEXT_SENT,
    CommandReceivedEvent,
    IntercomPhotoSentEvent,
    IntercomTextSentEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.intercom import Intercom


# ---------------------------------------------------------------------------
# Fake intercom server for integration-style tests
# ---------------------------------------------------------------------------

class _FakeHandler(BaseHTTPRequestHandler):
    """Minimal handler that mimics intercom-server.py responses."""

    received: list[dict] = []  # noqa: RUF012 — shared across requests intentionally

    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress output

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _respond(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        payload = self._read_json()
        _FakeHandler.received.append({"path": self.path, "payload": payload})

        if self.path == "/intercom/receive":
            if not payload.get("text", "").strip():
                self._respond(400, {"error": "text required"})
            else:
                self._respond(200, {"status": "sent"})
        elif self.path == "/intercom/photo":
            self._respond(200, {"status": "sent"})
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})


@pytest.fixture()
def fake_server():
    """Start a fake intercom server on a random port, yield its URL."""
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
# send_text tests
# ---------------------------------------------------------------------------

class TestSendText:
    def test_send_text_success(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)
        assert intercom.send_text("Hello Ophir") is True
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/receive"
        assert _FakeHandler.received[0]["payload"]["text"] == "Hello Ophir"

    def test_send_text_strips_whitespace(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.send_text("  padded  ") is True
        assert _FakeHandler.received[0]["payload"]["text"] == "padded"

    def test_send_text_empty_rejected(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.send_text("") is False
        assert intercom.send_text("   ") is False
        assert len(_FakeHandler.received) == 0

    def test_send_text_emits_event(self, fake_server: str, bus: NucEventBus):
        events: list[IntercomTextSentEvent] = []
        bus.on(INTERCOM_TEXT_SENT, events.append)

        intercom = Intercom(event_bus=bus, intercom_url=fake_server)
        intercom.send_text("test msg")

        assert len(events) == 1
        assert events[0].text == "test msg"
        assert events[0].success is True

    def test_send_text_unreachable_returns_false(self, bus: NucEventBus):
        events: list[IntercomTextSentEvent] = []
        bus.on(INTERCOM_TEXT_SENT, events.append)

        intercom = Intercom(event_bus=bus, intercom_url="http://127.0.0.1:1")
        assert intercom.send_text("unreachable") is False

        assert len(events) == 1
        assert events[0].success is False


# ---------------------------------------------------------------------------
# send_photo tests
# ---------------------------------------------------------------------------

class TestSendPhoto:
    def test_send_photo_success(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)
        assert intercom.send_photo("Look at this!") is True
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/photo"
        assert _FakeHandler.received[0]["payload"]["caption"] == "Look at this!"

    def test_send_photo_default_caption(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.send_photo() is True
        assert _FakeHandler.received[0]["payload"]["caption"] == "Photo from robot"

    def test_send_photo_empty_caption_uses_default(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.send_photo("   ") is True
        assert _FakeHandler.received[0]["payload"]["caption"] == "Photo from robot"

    def test_send_photo_emits_event(self, fake_server: str, bus: NucEventBus):
        events: list[IntercomPhotoSentEvent] = []
        bus.on(INTERCOM_PHOTO_SENT, events.append)

        intercom = Intercom(event_bus=bus, intercom_url=fake_server)
        intercom.send_photo("test caption")

        assert len(events) == 1
        assert events[0].caption == "test caption"
        assert events[0].success is True

    def test_send_photo_unreachable_returns_false(self):
        intercom = Intercom(intercom_url="http://127.0.0.1:1")
        assert intercom.send_photo() is False


# ---------------------------------------------------------------------------
# health_check tests
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_check_success(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.health_check() is True

    def test_health_check_unreachable(self):
        intercom = Intercom(intercom_url="http://127.0.0.1:1")
        assert intercom.health_check() is False


# ---------------------------------------------------------------------------
# Event bus integration tests
# ---------------------------------------------------------------------------

class TestCommandHandler:
    def test_take_photo_command(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(command="take_photo", source="voice"),
        )
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/photo"

    def test_take_a_photo_command(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(command="take a photo", source="signal"),
        )
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/photo"

    def test_photo_command_with_caption(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(
                command="photo",
                source="voice",
                args={"caption": "My custom caption"},
            ),
        )
        assert _FakeHandler.received[0]["payload"]["caption"] == "My custom caption"

    def test_message_command(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(
                command="intercom",
                source="signal",
                args={"text": "Hello from signal"},
            ),
        )
        assert len(_FakeHandler.received) == 1
        assert _FakeHandler.received[0]["path"] == "/intercom/receive"
        assert _FakeHandler.received[0]["payload"]["text"] == "Hello from signal"

    def test_message_command_empty_text_ignored(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(
                command="intercom",
                source="voice",
                args={"text": ""},
            ),
        )
        assert len(_FakeHandler.received) == 0

    def test_unrelated_command_ignored(self, fake_server: str, bus: NucEventBus):
        intercom = Intercom(event_bus=bus, intercom_url=fake_server)  # noqa: F841
        bus.emit(
            COMMAND_RECEIVED,
            CommandReceivedEvent(command="move_forward", source="voice"),
        )
        assert len(_FakeHandler.received) == 0


# ---------------------------------------------------------------------------
# Constructor / config tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_no_event_bus(self, fake_server: str):
        """Intercom works without event bus (no command subscription)."""
        intercom = Intercom(intercom_url=fake_server)
        assert intercom.send_text("no bus") is True

    def test_env_var_override(self, fake_server: str, monkeypatch):
        monkeypatch.setenv("INTERCOM_URL", fake_server)
        intercom = Intercom()
        assert intercom.send_text("from env") is True

    def test_trailing_slash_stripped(self, fake_server: str):
        intercom = Intercom(intercom_url=fake_server + "/")
        assert intercom.send_text("slash") is True
