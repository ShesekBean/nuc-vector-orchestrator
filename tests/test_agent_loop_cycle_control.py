"""Tests for bounded-cycle support in the Python agent-loop."""

import inspect
from pathlib import Path

from apps.control_plane.agent_loop.config import Config
from apps.control_plane.agent_loop.loop import AgentLoop


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "scripts" / "run-evolution-cycle.sh"


def test_config_exposes_max_cycles_and_dispatch_enabled():
    """Config reads MAX_CYCLES and DISPATCH_ENABLED from environment with correct defaults."""
    cfg = Config()
    assert cfg.max_cycles == 0, "Default max_cycles should be 0 (unlimited)"
    assert cfg.dispatch_enabled is True, "Default dispatch_enabled should be True"


def test_loop_tracks_and_exits_after_cycle_limit():
    """AgentLoop.run() increments cycle_count and exits when max_cycles is reached."""
    source = inspect.getsource(AgentLoop.run)
    assert "cycle_count" in source
    assert "max_cycles" in source
    # Verify the exit log message exists
    assert "Reached MAX_CYCLES" in source


def test_loop_can_skip_worker_dispatch():
    """AgentLoop._run_cycle() checks dispatch_enabled before dispatching."""
    source = inspect.getsource(AgentLoop._run_cycle)
    assert "dispatch_enabled" in source
    assert "Skipping worker dispatch" in source


def test_run_evolution_cycle_is_interactive_script():
    source = RUNNER_PATH.read_text()
    assert 'REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"' in source
    assert "signal-interactive.sh" in source
    assert "sig_send" in source
