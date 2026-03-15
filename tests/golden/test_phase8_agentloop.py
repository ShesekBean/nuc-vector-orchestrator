"""Phase 8 — Agent Loop.

Tests dispatch machinery.  No workers actually launched.
"""

from __future__ import annotations

import os
import subprocess

import pytest


pytestmark = pytest.mark.phase8


class TestConfigLoads:
    def test_load_config(self):
        """8.1 — load_config() returns valid Config with correct worker limits."""
        from apps.control_plane.agent_loop.config import Config, load_config

        cfg = load_config()
        assert isinstance(cfg, Config)
        assert cfg.repo_dir is not None
        assert cfg.nuc_repo == "ophir-sw/nuc-vector-orchestrator"
        assert cfg.max_workers == 4
        assert cfg.max_vector_workers == 2
        assert cfg.max_vector_workers <= cfg.max_workers


class TestGitHubCLI:
    def test_gh_auth_and_issue_list(self):
        """8.2 — gh auth status authenticated, issue list returns valid JSON."""
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            pytest.skip("GitHub CLI not authenticated")

        import json
        result = subprocess.run(
            ["gh", "issue", "list",
             "-R", "ophir-sw/nuc-vector-orchestrator",
             "--json", "number,title", "--limit", "1"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"gh issue list failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)


class TestPRReviewHookExists:
    def test_review_hook_script(self, repo_root: str):
        """8.3 — Review hook agent definition or dispatch.py exists."""
        hook_path = os.path.join(repo_root, ".claude", "agents", "pr-review-hook.md")
        if os.path.isfile(hook_path):
            return

        dispatch_path = os.path.join(
            repo_root, "apps", "control_plane", "agent_loop", "dispatch.py"
        )
        assert os.path.isfile(dispatch_path), "Neither review hook nor dispatch.py found"
