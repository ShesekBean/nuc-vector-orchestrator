"""Phase 0 — NUC Unit Tests.

No robot needed.  Run first to catch regressions before touching hardware.
"""

from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.phase0


class TestAgentLoopConfig:
    def test_config_and_dispatch(self):
        """0.1 — Config() returns valid object with correct dispatch settings."""
        from apps.control_plane.agent_loop.config import Config

        cfg = Config()
        assert cfg.repo_dir is not None
        assert cfg.nuc_repo == "ShesekBean/nuc-vector-orchestrator"
        assert cfg.max_workers >= 1
        assert cfg.max_vector_workers >= 1
        assert cfg.dispatch_label == "assigned:worker"


class TestLLMConfig:
    def test_parse_llm_config(self, repo_root: str):
        """0.2 — parse_llm_config() returns correct provider/models."""
        from pathlib import Path
        from apps.control_plane.agent_loop.config import parse_llm_config

        config_path = Path(repo_root) / "config" / "llm-provider.yaml"
        if not config_path.exists():
            pytest.skip("llm-provider.yaml not found")

        llm = parse_llm_config(config_path)
        assert llm.provider == "claude"
        assert all(k in llm.models for k in ("heavy", "medium", "light"))


class TestPGMDependencyParser:
    def test_dependency_parsing(self):
        """0.3 — Dependency section parsed correctly, empty body returns empty set."""
        from apps.control_plane.agent_loop.pgm import PGMManager

        body = "## Dependencies\n- #42 (some feature)\n- #99 (another thing)\n"
        refs = PGMManager._parse_dependencies(body)
        assert 42 in refs and 99 in refs

        refs_empty = PGMManager._parse_dependencies("Just a plain issue body.")
        assert len(refs_empty) == 0


class TestInboxCommandRouting:
    def test_command_routing(self):
        """0.4 — #go, #approve, pass/fail all route correctly."""
        from apps.control_plane.agent_loop.inbox import (
            is_physical_test_go,
            parse_physical_test_result,
        )

        assert not is_physical_test_go("#approve 42")
        assert is_physical_test_go("#go")
        assert is_physical_test_go("go")
        assert is_physical_test_go("Go 42")

        assert parse_physical_test_result("pass") == "pass"
        assert parse_physical_test_result("failed") == "fail"
        assert parse_physical_test_result("hello") == ""


class TestSignalGateScript:
    def test_script_exists_and_executable(self, repo_root: str):
        """0.5 — scripts/pgm-signal-gate.sh exists and is executable."""
        script = os.path.join(repo_root, "scripts", "pgm-signal-gate.sh")
        assert os.path.isfile(script), f"Signal gate script not found: {script}"
        assert os.access(script, os.X_OK), f"Signal gate script not executable: {script}"
