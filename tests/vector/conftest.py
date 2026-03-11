"""Restore real numpy/PIL/cv2 before collecting vector tests.

test_evaluator.py injects MagicMock into sys.modules for numpy and PIL
to avoid import errors in CI environments without those packages. When
pytest collects tests/vector/ after test_evaluator.py, the mocked modules
break cv2 (which needs real numpy.core.multiarray). This conftest removes
mock entries and re-imports the real modules before collection.
"""

import importlib
import sys


def pytest_configure(config):
    """Remove mock module entries and restore real imports."""
    mock_names = []
    for name, mod in sys.modules.items():
        if type(mod).__name__ == "MagicMock" and name in (
            "numpy", "PIL", "PIL.Image", "cv2",
        ):
            mock_names.append(name)

    for name in mock_names:
        del sys.modules[name]

    # Also remove any numpy sub-modules that might be mocked
    numpy_mocks = [k for k in sys.modules if k.startswith("numpy.") and type(sys.modules[k]).__name__ == "MagicMock"]
    for name in numpy_mocks:
        del sys.modules[name]

    # Force re-import
    for name in ("numpy", "PIL", "cv2"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass
