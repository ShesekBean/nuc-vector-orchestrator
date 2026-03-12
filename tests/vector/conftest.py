"""Test configuration for vector tests.

Handles two CI environment issues:
1. test_evaluator.py injects MagicMock into sys.modules for numpy/PIL,
   which breaks cv2 in later test collection. We restore real modules.
2. anki_vector SDK is not installed in CI — we inject a stub module
   so lazy imports in camera_client.py succeed.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock


def _install_livekit_stubs():
    """Install lightweight livekit stub modules for CI (no real livekit)."""

    # --- livekit.rtc stubs ---
    class _VideoFrame:
        """Stub for rtc.VideoFrame that stores keyword args as attrs."""

        def __init__(self, *, width=0, height=0, type=None, data=b""):
            self.width = width
            self.height = height
            self.type = type
            self.data = data

    class _AudioFrame:
        """Stub for rtc.AudioFrame that stores keyword args as attrs."""

        def __init__(self, *, data=b"", sample_rate=0, num_channels=1,
                     samples_per_channel=0):
            self.data = data
            self.sample_rate = sample_rate
            self.num_channels = num_channels
            self.samples_per_channel = samples_per_channel

    rtc_mod = types.ModuleType("livekit.rtc")
    rtc_mod.VideoFrame = _VideoFrame
    rtc_mod.AudioFrame = _AudioFrame
    rtc_mod.VideoSource = MagicMock
    rtc_mod.AudioSource = MagicMock
    rtc_mod.LocalVideoTrack = MagicMock()
    rtc_mod.LocalAudioTrack = MagicMock()
    rtc_mod.Room = MagicMock
    rtc_mod.TrackPublishOptions = MagicMock
    rtc_mod.TrackSource = MagicMock()
    rtc_mod.TrackSource.SOURCE_CAMERA = "camera"
    rtc_mod.TrackSource.SOURCE_MICROPHONE = "microphone"
    rtc_mod.VideoBufferType = MagicMock()
    rtc_mod.VideoBufferType.RGBA = "rgba"
    rtc_mod.TrackKind = MagicMock()
    rtc_mod.TrackKind.KIND_AUDIO = "audio"
    rtc_mod.RemoteTrack = MagicMock
    rtc_mod.RemoteTrackPublication = MagicMock
    rtc_mod.RemoteParticipant = MagicMock
    rtc_mod.AudioStream = MagicMock

    # --- livekit.api stubs ---
    class _AccessToken:
        """Stub for api.AccessToken — generates a fake JWT."""

        def __init__(self, api_key=None, api_secret=None):
            self._key = api_key or "stub"
            self._secret = api_secret or "stub"
            self._identity = ""
            self._grants = None

        def with_identity(self, identity):
            self._identity = identity
            return self

        def with_grants(self, grants):
            self._grants = grants
            return self

        def to_jwt(self):
            import base64
            import json
            header = base64.urlsafe_b64encode(
                json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
            ).decode().rstrip("=")
            payload = base64.urlsafe_b64encode(
                json.dumps({"sub": self._identity, "key": self._key}).encode()
            ).decode().rstrip("=")
            sig = base64.urlsafe_b64encode(b"stub-signature").decode().rstrip("=")
            return f"{header}.{payload}.{sig}"

    api_mod = types.ModuleType("livekit.api")
    api_mod.AccessToken = _AccessToken
    api_mod.VideoGrants = MagicMock

    # --- Top-level livekit module ---
    livekit_mod = types.ModuleType("livekit")
    livekit_mod.api = api_mod
    livekit_mod.rtc = rtc_mod

    sys.modules["livekit"] = livekit_mod
    sys.modules["livekit.api"] = api_mod
    sys.modules["livekit.rtc"] = rtc_mod


def pytest_configure(config):
    """Set up module stubs and restore real imports."""
    # --- Restore real numpy/PIL/cv2 (undo test_evaluator mocking) ---
    mock_names = []
    for name, mod in sys.modules.items():
        if type(mod).__name__ == "MagicMock" and name in (
            "numpy", "PIL", "PIL.Image", "cv2",
        ):
            mock_names.append(name)

    for name in mock_names:
        del sys.modules[name]

    numpy_mocks = [k for k in sys.modules if k.startswith("numpy.") and type(sys.modules[k]).__name__ == "MagicMock"]
    for name in numpy_mocks:
        del sys.modules[name]

    for name in ("numpy", "PIL", "cv2"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass

    # --- Restore real livekit or create stubs (CI has no livekit) ---
    livekit_mocks = [k for k in sys.modules if k.startswith("livekit") and type(sys.modules[k]).__name__ == "MagicMock"]
    for name in livekit_mocks:
        del sys.modules[name]

    # Try to import real livekit; if unavailable, inject stubs
    try:
        importlib.import_module("livekit")
    except ImportError:
        _install_livekit_stubs()

    for name in ("livekit", "livekit.api", "livekit.rtc"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass

    # --- Stub anki_vector if not installed (CI environment) ---
    if "anki_vector" not in sys.modules:
        anki_vector_mod = types.ModuleType("anki_vector")
        events_mod = types.ModuleType("anki_vector.events")
        events_mod.Events = MagicMock()
        anki_vector_mod.events = events_mod
        util_mod = types.ModuleType("anki_vector.util")
        util_mod.degrees = MagicMock(side_effect=lambda x: x)
        util_mod.distance_mm = MagicMock(side_effect=lambda x: x)
        util_mod.speed_mmps = MagicMock(side_effect=lambda x: x)
        anki_vector_mod.util = util_mod
        screen_mod = types.ModuleType("anki_vector.screen")
        screen_mod.convert_image_to_screen_data = MagicMock(return_value=b"\x00" * 100)
        anki_vector_mod.screen = screen_mod
        messaging_mod = types.ModuleType("anki_vector.messaging")
        protocol_mod = types.ModuleType("anki_vector.messaging.protocol")
        protocol_mod.AudioFeedRequest = MagicMock
        messaging_mod.protocol = protocol_mod
        anki_vector_mod.messaging = messaging_mod
        sys.modules["anki_vector"] = anki_vector_mod
        sys.modules["anki_vector.events"] = events_mod
        sys.modules["anki_vector.util"] = util_mod
        sys.modules["anki_vector.screen"] = screen_mod
        sys.modules["anki_vector.messaging"] = messaging_mod
        sys.modules["anki_vector.messaging.protocol"] = protocol_mod
