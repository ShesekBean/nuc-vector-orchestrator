"""Tests for the Vector HTTP-to-gRPC bridge server.

All tests use a mocked ConnectionManager so no real Vector connection
is needed.  The aiohttp test client handles async request/response.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.vector.bridge.connection import ConnectionManager
from apps.vector.bridge.server import create_app

pytest_plugins = ["aiohttp.pytest_plugin"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_conn(connected: bool = True) -> MagicMock:
    """Create a mocked ConnectionManager with all controllers."""
    conn = MagicMock(spec=ConnectionManager)
    conn.is_connected = connected

    # Motor controller
    conn.motor_controller = MagicMock()
    conn.motor_controller.drive_wheels = MagicMock()
    conn.motor_controller.drive_straight = MagicMock()
    conn.motor_controller.turn_in_place = MagicMock()
    conn.motor_controller.turn_then_drive = MagicMock()
    conn.motor_controller.emergency_stop = MagicMock()

    # Head controller
    conn.head_controller = MagicMock()
    conn.head_controller.set_angle = MagicMock(return_value=20.0)

    # Lift controller
    conn.lift_controller = MagicMock()
    conn.lift_controller.move_to = MagicMock(return_value=True)
    conn.lift_controller.move_to_preset = MagicMock(return_value=True)

    # LED controller
    conn.led_controller = MagicMock()
    conn.led_controller.set_state = MagicMock()
    conn.led_controller.override = MagicMock()

    # Display controller
    conn.display_controller = MagicMock()

    # Camera — robot.camera.capture_single_image
    mock_image = MagicMock()
    mock_pil = MagicMock()
    mock_image.raw_image = mock_pil

    def _save_jpeg(buf, format="JPEG"):
        buf.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    mock_pil.save = _save_jpeg
    conn.robot = MagicMock()
    conn.robot.camera.capture_single_image = MagicMock(return_value=mock_image)
    conn.robot.behavior.say_text = MagicMock()

    # Battery state
    conn.get_battery_state = MagicMock(return_value={
        "voltage": 4.1,
        "level": 3,
        "is_charging": False,
        "is_on_charger": False,
    })

    # Robot state
    conn.get_robot_state = MagicMock(return_value={
        "accel": {"x": 0.0, "y": 0.0, "z": 9.8},
        "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
        "touch": False,
        "head_angle_deg": 10.0,
        "lift_height_mm": 0.0,
    })

    return conn


@pytest.fixture
def mock_conn():
    return _make_mock_conn(connected=True)


@pytest.fixture
def mock_conn_offline():
    return _make_mock_conn(connected=False)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


async def test_health_ok(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "healthy"
    assert "battery" in data
    assert "latency_ms" in data
    conn.get_battery_state.assert_called_once()


async def test_health_offline(aiohttp_client):
    conn = _make_mock_conn(connected=False)
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 503
    data = await resp.json()
    assert data["code"] == "VECTOR_OFFLINE"


# ---------------------------------------------------------------------------
# Move endpoint
# ---------------------------------------------------------------------------


async def test_move_wheels(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", json={
        "type": "wheels",
        "left_speed": 100,
        "right_speed": 100,
    })
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    conn.motor_controller.drive_wheels.assert_called_once_with(100.0, 100.0, 200.0, 200.0)


async def test_move_straight(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", json={
        "type": "straight",
        "distance_mm": 200,
        "speed_mmps": 150,
    })
    assert resp.status == 200
    conn.motor_controller.drive_straight.assert_called_once_with(200.0, 150.0)


async def test_move_turn(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", json={
        "type": "turn",
        "angle_deg": 90,
    })
    assert resp.status == 200
    conn.motor_controller.turn_in_place.assert_called_once_with(90.0, 100.0)


async def test_move_turn_then_drive(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", json={
        "type": "turn_then_drive",
        "angle_deg": 45,
        "distance_mm": 300,
    })
    assert resp.status == 200
    conn.motor_controller.turn_then_drive.assert_called_once_with(45.0, 300.0)


async def test_move_invalid_type(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", json={"type": "fly"})
    assert resp.status == 400
    data = await resp.json()
    assert data["code"] == "INVALID_MOVE_TYPE"


async def test_move_invalid_json(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/move", data=b"not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400


# ---------------------------------------------------------------------------
# Stop endpoint
# ---------------------------------------------------------------------------


async def test_stop(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/stop")
    assert resp.status == 200
    conn.motor_controller.emergency_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Head endpoint
# ---------------------------------------------------------------------------


async def test_head(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/head", json={"angle_deg": 20, "speed_dps": 60})
    assert resp.status == 200
    data = await resp.json()
    assert data["angle_deg"] == 20.0
    conn.head_controller.set_angle.assert_called_once_with(20.0, 60.0)


async def test_head_default_speed(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/head", json={"angle_deg": -10})
    assert resp.status == 200
    conn.head_controller.set_angle.assert_called_once_with(-10.0, None)


# ---------------------------------------------------------------------------
# Lift endpoint
# ---------------------------------------------------------------------------


async def test_lift_height(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/lift", json={"height": 0.6})
    assert resp.status == 200
    conn.lift_controller.move_to.assert_called_once_with(0.6)


async def test_lift_preset(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/lift", json={"preset": "carry"})
    assert resp.status == 200
    conn.lift_controller.move_to_preset.assert_called_once_with("carry")


async def test_lift_blocked(aiohttp_client):
    conn = _make_mock_conn()
    conn.lift_controller.move_to.return_value = False
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/lift", json={"height": 0.5})
    assert resp.status == 500
    data = await resp.json()
    assert data["code"] == "LIFT_BLOCKED"


# ---------------------------------------------------------------------------
# LED endpoint
# ---------------------------------------------------------------------------


async def test_led_state(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/led", json={"state": "person_detected"})
    assert resp.status == 200
    conn.led_controller.set_state.assert_called_once_with("person_detected")


async def test_led_override(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/led", json={"hue": 0.5, "saturation": 0.8, "duration_s": 3.0})
    assert resp.status == 200
    conn.led_controller.override.assert_called_once_with(0.5, 0.8, 3.0)


async def test_led_missing_params(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/led", json={})
    assert resp.status == 400
    data = await resp.json()
    assert data["code"] == "MISSING_PARAMS"


# ---------------------------------------------------------------------------
# Capture endpoint
# ---------------------------------------------------------------------------


async def test_capture_raw(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.get("/capture")
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    body = await resp.read()
    assert body[:2] == b"\xff\xd8"


async def test_capture_base64(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.get("/capture?format=base64")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "image" in data
    assert data["content_type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# Display endpoint
# ---------------------------------------------------------------------------


async def test_display(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/display", json={"expression": "happy"})
    assert resp.status == 200
    data = await resp.json()
    assert data["expression"] == "happy"


async def test_display_missing_expression(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/display", json={})
    assert resp.status == 400
    data = await resp.json()
    assert data["code"] == "MISSING_PARAMS"


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


async def test_status(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.get("/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "battery" in data
    assert "sensors" in data


# ---------------------------------------------------------------------------
# Stub endpoints (501)
# ---------------------------------------------------------------------------


async def test_follow_start_stub(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/follow/start")
    assert resp.status == 501


async def test_follow_stop_stub(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/follow/stop")
    assert resp.status == 501


async def test_call_start_stub(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/call/start")
    assert resp.status == 501


async def test_call_stop_stub(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/call/stop")
    assert resp.status == 501


# ---------------------------------------------------------------------------
# Audio play endpoint
# ---------------------------------------------------------------------------


async def test_audio_play(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/audio/play", json={"text": "Hello world"})
    assert resp.status == 200
    conn.robot.behavior.say_text.assert_called_once_with("Hello world")


async def test_audio_play_missing_text(aiohttp_client):
    conn = _make_mock_conn()
    app = create_app(conn)
    client = await aiohttp_client(app)
    resp = await client.post("/audio/play", json={})
    assert resp.status == 400
    data = await resp.json()
    assert data["code"] == "MISSING_PARAMS"


# ---------------------------------------------------------------------------
# Offline state
# ---------------------------------------------------------------------------


async def test_all_endpoints_return_503_when_offline(aiohttp_client):
    conn = _make_mock_conn(connected=False)
    app = create_app(conn)
    client = await aiohttp_client(app)

    for method, path in [
        ("GET", "/health"),
        ("POST", "/move"),
        ("POST", "/stop"),
        ("POST", "/head"),
        ("POST", "/lift"),
        ("POST", "/led"),
        ("GET", "/capture"),
        ("POST", "/display"),
        ("GET", "/status"),
        ("POST", "/audio/play"),
    ]:
        if method == "GET":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json={})
        assert resp.status == 503, f"{method} {path} should return 503 when offline"


# ---------------------------------------------------------------------------
# Connection manager unit tests
# ---------------------------------------------------------------------------


class TestConnectionManager:
    """Tests for ConnectionManager without a real robot."""

    def test_not_connected_by_default(self):
        conn = ConnectionManager()
        assert not conn.is_connected

    def test_robot_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.robot

    def test_motor_controller_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.motor_controller

    def test_head_controller_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.head_controller

    def test_lift_controller_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.lift_controller

    def test_led_controller_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.led_controller

    def test_display_controller_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.display_controller

    def test_camera_client_raises_when_not_connected(self):
        conn = ConnectionManager()
        with pytest.raises(ConnectionError):
            _ = conn.camera_client

    def test_serial_constructor_override(self):
        conn = ConnectionManager(serial="test1234")
        assert conn._serial == "test1234"

    def test_disconnect_when_not_connected(self):
        conn = ConnectionManager()
        conn.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# Server creation tests
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Tests for app factory."""

    def test_create_app_default_conn(self):
        app = create_app()
        assert "conn" in app
        assert isinstance(app["conn"], ConnectionManager)

    def test_create_app_custom_conn(self):
        conn = _make_mock_conn()
        app = create_app(conn)
        assert app["conn"] is conn

    def test_routes_registered(self):
        conn = _make_mock_conn()
        app = create_app(conn)
        routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
        expected = [
            "/health", "/move", "/stop", "/head", "/lift", "/led",
            "/capture", "/display", "/status",
            "/follow/start", "/follow/stop",
            "/audio/play",
            "/call/start", "/call/stop",
        ]
        for path in expected:
            assert path in routes, f"Route {path} not registered"
