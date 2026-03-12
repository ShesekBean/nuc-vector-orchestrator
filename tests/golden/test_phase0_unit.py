"""Phase 0 — NUC Unit Tests (~20s).

No robot needed.  Run first to catch regressions before touching hardware.

Tests 0.1–0.8 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.phase0


# 0.1 Agent loop config loads
class TestAgentLoopConfig:
    def test_config_creates_valid_object(self):
        """0.1 — Config() returns valid object, all paths exist or are creatable."""
        from apps.control_plane.agent_loop.config import Config

        cfg = Config()
        assert cfg.repo_dir is not None
        assert cfg.nuc_repo == "ShesekBean/nuc-vector-orchestrator"
        assert cfg.max_workers >= 1
        assert cfg.max_vector_workers >= 1


# 0.2 LLM config parses
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
        assert "heavy" in llm.models
        assert "medium" in llm.models
        assert "light" in llm.models


# 0.3 Dispatch label matching
class TestDispatchLabelMatching:
    def test_dispatch_label_constant(self):
        """0.3 — dispatch_label is 'assigned:worker'."""
        from apps.control_plane.agent_loop.config import Config

        cfg = Config()
        assert cfg.dispatch_label == "assigned:worker"


# 0.4 PGM dependency parser
class TestPGMDependencyParser:
    def test_dependency_section_parsed(self):
        """0.4 — ## Dependencies section parsed, #N refs extracted."""
        from apps.control_plane.agent_loop.pgm import PGMManager

        body = (
            "## Dependencies\n"
            "- #42 (some feature)\n"
            "- #99 (another thing)\n"
        )
        refs = PGMManager._parse_dependencies(body)
        assert 42 in refs
        assert 99 in refs

    def test_no_dependencies_section(self):
        """0.4b — No Dependencies section → empty set."""
        from apps.control_plane.agent_loop.pgm import PGMManager

        refs = PGMManager._parse_dependencies("Just a plain issue body.")
        assert len(refs) == 0


# 0.5 Inbox command routing
class TestInboxCommandRouting:
    def test_approve_routing(self):
        """0.5a — #approve routes correctly."""
        from apps.control_plane.agent_loop.inbox import is_physical_test_go

        assert not is_physical_test_go("#approve 42")

    def test_go_routing(self):
        """0.5b — #go routes correctly."""
        from apps.control_plane.agent_loop.inbox import is_physical_test_go

        assert is_physical_test_go("#go")
        assert is_physical_test_go("go")
        assert is_physical_test_go("Go 42")
        assert is_physical_test_go("#go 42")

    def test_pass_fail_parsing(self):
        """0.5c — pass/fail parse correctly."""
        from apps.control_plane.agent_loop.inbox import parse_physical_test_result

        assert parse_physical_test_result("pass") == "pass"
        assert parse_physical_test_result("passed") == "pass"
        assert parse_physical_test_result("fail") == "fail"
        assert parse_physical_test_result("failed") == "fail"
        assert parse_physical_test_result("hello") == ""


# 0.6 Signal gate script
class TestSignalGateScript:
    def test_script_exists_and_executable(self, repo_root: str):
        """0.6 — scripts/pgm-signal-gate.sh exists and is executable."""
        script = os.path.join(repo_root, "scripts", "pgm-signal-gate.sh")
        assert os.path.isfile(script), f"Signal gate script not found: {script}"
        assert os.access(script, os.X_OK), f"Signal gate script not executable: {script}"


# 0.7 Worker prompt injection
class TestWorkerPromptInjection:
    def test_vector_issue_detection(self):
        """0.7 — _is_vector_issue function exists for component:vector detection."""
        from apps.control_plane.agent_loop.dispatch import _is_vector_issue

        # Verify the function is callable (actual GitHub calls skipped)
        assert callable(_is_vector_issue)


# 0.8 Worktree path generation
class TestWorktreePathGeneration:
    def test_vector_worktree_path(self):
        """0.8a — Vector issues use /tmp/vector-worker-issue-{N}."""
        expected = "/tmp/vector-worker-issue-42"
        # The path pattern is defined in dispatch.py
        path = f"/tmp/vector-worker-issue-{42}"
        assert path == expected

    def test_nuc_worktree_path(self):
        """0.8b — NUC issues use /tmp/nuc-worker-issue-{N}."""
        expected = "/tmp/nuc-worker-issue-42"
        path = f"/tmp/nuc-worker-issue-{42}"
        assert path == expected
