"""Shared fixtures and phase gating for the Vector golden test suite.

Phase 1 (preflight) determines robot connectivity.  If Phase 1 fails,
Phases 2, 5, and 6 are skipped but NUC-only phases continue.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _stub_anki_vector() -> None:
    """Create an anki_vector stub module for CI (no SDK installed)."""
    if "anki_vector" in sys.modules:
        return

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

    color_mod = types.ModuleType("anki_vector.color")
    color_mod.Color = MagicMock
    anki_vector_mod.color = color_mod

    sys.modules["anki_vector"] = anki_vector_mod
    sys.modules["anki_vector.events"] = events_mod
    sys.modules["anki_vector.util"] = util_mod
    sys.modules["anki_vector.screen"] = screen_mod
    sys.modules["anki_vector.messaging"] = messaging_mod
    sys.modules["anki_vector.messaging.protocol"] = protocol_mod
    sys.modules["anki_vector.color"] = color_mod


# ---------------------------------------------------------------------------
# Pytest configuration
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers and restore mocked modules."""
    config.addinivalue_line("markers", "phase0: NUC unit tests")
    config.addinivalue_line("markers", "phase1: Preflight robot connectivity")
    config.addinivalue_line("markers", "phase2: Stationary hardware tests")
    config.addinivalue_line("markers", "phase3: NUC inference pipeline")
    config.addinivalue_line("markers", "phase4: Voice pipeline")
    config.addinivalue_line("markers", "phase5: Movement + safety")
    config.addinivalue_line("markers", "phase6: Person following integration")
    config.addinivalue_line("markers", "phase7: Signal integration")
    config.addinivalue_line("markers", "phase8: Agent loop")
    config.addinivalue_line("markers", "phase9: Event bus integration")
    config.addinivalue_line("markers", "robot: Requires live Vector robot")

    # Restore real numpy/PIL/cv2 if test_evaluator mocked them
    mock_names = [
        name for name, mod in sys.modules.items()
        if type(mod).__name__ == "MagicMock"
        and name in ("numpy", "PIL", "PIL.Image", "cv2")
    ]
    for name in mock_names:
        del sys.modules[name]

    numpy_mocks = [
        k for k in sys.modules
        if k.startswith("numpy.") and type(sys.modules[k]).__name__ == "MagicMock"
    ]
    for name in numpy_mocks:
        del sys.modules[name]

    for name in ("numpy", "PIL", "cv2"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass

    # Only stub anki_vector if the real SDK is not installed.
    # On the NUC, the real SDK is available and we want real robot tests.
    try:
        importlib.import_module("anki_vector")
    except ImportError:
        _stub_anki_vector()


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def robot_available() -> bool:
    """Attempt to connect to Vector; return True if reachable."""
    try:
        import anki_vector
        robot = anki_vector.Robot(serial="0dd1cdcf", default_logging=False)
        robot.connect()
        robot.get_battery_state()
        robot.disconnect()
        return True
    except Exception:
        return False


@pytest.fixture(scope="function")
def robot_connected(robot_available: bool):
    """Provide a fresh connected robot for each test.

    Per-test connections prevent gRPC socket drops from cascading
    across tests and causing Vector error 915 (cloud disconnect).
    """
    import time

    if not robot_available:
        pytest.skip("Robot not available — skipping robot-dependent test")

    import anki_vector
    # OVERRIDE_BEHAVIORS_PRIORITY suppresses Vector's autonomous movement
    # so he stays still between test commands
    robot = anki_vector.Robot(
        serial="0dd1cdcf",
        default_logging=False,
        behavior_control_level=anki_vector.connection.ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
    )
    robot.connect()
    yield robot
    try:
        robot.disconnect()
    except Exception:
        pass
    # Small delay between tests to let Vector's gRPC server stabilize
    time.sleep(0.5)


@pytest.fixture(scope="session")
def repo_root() -> str:
    """Return the repository root directory."""
    # Walk up from this file to find the repo root
    d = os.path.dirname(os.path.abspath(__file__))
    while d != "/":
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    # Fallback to cwd
    return os.getcwd()


@pytest.fixture(scope="session")
def event_bus():
    """Provide a fresh NucEventBus instance."""
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    return NucEventBus()


# ---------------------------------------------------------------------------
# Phase skip logic
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply skip logic: phases 2/5/6 require robot (Phase 1 gate)."""
    # We can't know robot status at collection time, so we rely on the
    # robot_available fixture.  Tests in phases 2/5/6 use the
    # robot_connected fixture which calls pytest.skip if unavailable.
    pass
