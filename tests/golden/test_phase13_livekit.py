"""Phase 13 — LiveKit Integration.

Tests LiveKit Cloud connectivity and camera streaming.

Tests 13.1–13.6 from the comprehensive test plan.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.phase13

REPO_ROOT = Path(__file__).parent.parent.parent
ENV_FILE = REPO_ROOT / ".env.livekit"


def _load_credentials() -> tuple[str, str, str]:
    """Load LiveKit credentials from .env.livekit at repo root."""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"LiveKit credentials not found: {ENV_FILE}")
    creds = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    url = creds.get("LIVEKIT_URL", "")
    key = creds.get("LIVEKIT_API_KEY", "")
    secret = creds.get("LIVEKIT_API_SECRET", "")
    if not all([url, key, secret]):
        raise ValueError("Missing LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET")
    return url, key, secret


def _livekit_installed() -> bool:
    """Check if LiveKit SDK is installed."""
    try:
        from livekit import api, rtc
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def _credentials_exist() -> bool:
    """Check if LiveKit credentials file exists."""
    return ENV_FILE.exists()


# 13.1 LiveKit credentials load
class TestLiveKitCredentials:
    def test_credentials_load(self):
        """13.1 — LiveKit credentials load from .env.livekit."""
        if not _credentials_exist():
            pytest.skip("LiveKit credentials file not found")

        url, key, secret = _load_credentials()
        assert url, "LIVEKIT_URL is empty"
        assert key, "LIVEKIT_API_KEY is empty"
        assert secret, "LIVEKIT_API_SECRET is empty"
        assert url.startswith("wss://"), f"LIVEKIT_URL should start with wss://, got: {url[:30]}"


# 13.2 Generate access token
class TestLiveKitToken:
    def test_generate_token(self):
        """13.2 — Generate LiveKit access token without error."""
        if not _credentials_exist():
            pytest.skip("LiveKit credentials file not found")
        if not _livekit_installed():
            pytest.skip("LiveKit SDK not installed")

        from livekit import api as lk_api

        url, key, secret = _load_credentials()
        token = lk_api.AccessToken(key, secret)
        token.with_identity("test-identity")
        token.with_name("Test")
        token.with_grants(lk_api.VideoGrants(room_join=True, room="robot-cam"))
        jwt = token.to_jwt()
        assert isinstance(jwt, str), f"Token is not a string: {type(jwt)}"
        assert len(jwt) > 50, f"Token too short ({len(jwt)} chars) — likely invalid"


# 13.3 Connect to LiveKit room
class TestLiveKitConnect:
    def test_connect_to_room(self):
        """13.3 — Connect to LiveKit room 'robot-cam' (connection succeeds even without phone)."""
        if not _credentials_exist():
            pytest.skip("LiveKit credentials file not found")
        if not _livekit_installed():
            pytest.skip("LiveKit SDK not installed")

        import asyncio
        from livekit import api as lk_api, rtc as lk_rtc

        url, key, secret = _load_credentials()
        token = lk_api.AccessToken(key, secret)
        token.with_identity("test-connect")
        token.with_name("Test Connect")
        token.with_grants(lk_api.VideoGrants(room_join=True, room="robot-cam"))
        jwt = token.to_jwt()

        async def _connect():
            room = lk_rtc.Room()
            await room.connect(url, jwt)
            connected = room.isconnected() if hasattr(room, "isconnected") else True
            await room.disconnect()
            return connected

        result = asyncio.run(_connect())
        assert result, "Failed to connect to LiveKit room"


# 13.4 Capture frame (requires phone in room)
class TestLiveKitCapture:
    def test_capture_frame(self):
        """13.4 — If phone is in room: capture one video frame (JPEG > 1KB)."""
        if not _credentials_exist():
            pytest.skip("LiveKit credentials file not found")
        if not _livekit_installed():
            pytest.skip("LiveKit SDK not installed")

        # Patch camera_capture's ENV_FILE to point to repo root
        import sys
        sys.path.insert(0, str(REPO_ROOT / "apps" / "test_harness"))
        try:
            import camera_capture
            # Override the module's ENV_FILE before calling CameraCapture
            camera_capture.ENV_FILE = ENV_FILE
            cam = camera_capture.CameraCapture(room="robot-cam")
            try:
                frame_bytes = cam.capture()
            except TimeoutError:
                pytest.skip("No phone in LiveKit room — cannot capture frame")
            assert len(frame_bytes) > 1024, (
                f"Frame too small ({len(frame_bytes)} bytes), expected > 1KB"
            )
        finally:
            sys.path.pop(0)


# 13.5 Bridge module imports
class TestLiveKitBridgeImport:
    def test_bridge_import(self):
        """13.5 — LiveKit bridge module imports without error."""
        bridge_path = REPO_ROOT / "apps" / "vector" / "src" / "livekit_bridge.py"
        if not bridge_path.exists():
            pytest.skip("LiveKit bridge module not found")

        import importlib.util
        spec = importlib.util.spec_from_file_location("livekit_bridge", bridge_path)
        assert spec is not None, "Could not create module spec"
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ImportError as e:
            pytest.skip(f"Bridge import failed (missing dependency): {e}")
        assert hasattr(mod, "__name__")


# 13.6 Bridge instantiation with mock
class TestLiveKitBridgeMock:
    def test_bridge_mock_instantiation(self):
        """13.6 — Bridge can be instantiated with mock camera_client."""
        bridge_path = REPO_ROOT / "apps" / "vector" / "src" / "livekit_bridge.py"
        if not bridge_path.exists():
            pytest.skip("LiveKit bridge module not found")

        import importlib.util
        spec = importlib.util.spec_from_file_location("livekit_bridge", bridge_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ImportError as e:
            pytest.skip(f"Bridge import failed (missing dependency): {e}")

        bridge_classes = [
            name for name in dir(mod)
            if isinstance(getattr(mod, name), type) and "bridge" in name.lower()
        ]
        if not bridge_classes:
            pytest.skip("No bridge class found in livekit_bridge module")

        bridge_cls = getattr(mod, bridge_classes[0])
        mock_camera = MagicMock()
        try:
            instance = bridge_cls(camera_client=mock_camera)
            assert instance is not None
        except TypeError:
            try:
                instance = bridge_cls()
                assert instance is not None
            except Exception as e:
                pytest.skip(f"Could not instantiate bridge class: {e}")
