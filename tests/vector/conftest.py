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

    # --- Restore real livekit (undo test_evaluator mocking) ---
    livekit_mocks = [k for k in sys.modules if k.startswith("livekit") and type(sys.modules[k]).__name__ == "MagicMock"]
    for name in livekit_mocks:
        del sys.modules[name]

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
