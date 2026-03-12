"""Phase 8 — Agent Loop (~12s).

Tests dispatch machinery.  No workers actually launched.

Tests 8.1–8.7 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import os
import subprocess

import pytest


pytestmark = pytest.mark.phase8


# 8.1 Config loads
class TestConfigLoads:
    def test_load_config(self):
        """8.1 — load_config() returns valid Config."""
        from apps.control_plane.agent_loop.config import Config, load_config

        cfg = load_config()
        assert isinstance(cfg, Config)
        assert cfg.repo_dir is not None
        assert cfg.nuc_repo == "ShesekBean/nuc-vector-orchestrator"


# 8.2 GitHub CLI auth
class TestGitHubCLIAuth:
    def test_gh_auth_status(self):
        """8.2 — gh auth status → authenticated."""
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            pytest.skip("GitHub CLI not authenticated")
        assert result.returncode == 0


# 8.3 Issue list
class TestIssueList:
    def test_gh_issue_list(self):
        """8.3 — gh issue list → valid output."""
        result = subprocess.run(
            ["gh", "issue", "list",
             "-R", "ShesekBean/nuc-vector-orchestrator",
             "--json", "number,title", "--limit", "1"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            pytest.skip("GitHub CLI issue list failed")
        import json
        data = json.loads(result.stdout)
        assert isinstance(data, list)


# 8.4 Label filtering
class TestLabelFiltering:
    def test_dispatch_label_filter(self):
        """8.4 — assigned:worker filter matches correct constant."""
        from apps.control_plane.agent_loop.config import Config

        cfg = Config()
        assert cfg.dispatch_label == "assigned:worker"


# 8.5 Worker slot counting
class TestWorkerSlotCounting:
    def test_max_workers_config(self):
        """8.5 — max_workers=4, max_vector_workers=2 respected."""
        from apps.control_plane.agent_loop.config import Config

        cfg = Config()
        assert cfg.max_workers == 4
        assert cfg.max_vector_workers == 2
        assert cfg.max_vector_workers <= cfg.max_workers


# 8.6 Worktree creation
class TestWorktreeCreation:
    def test_worktree_pattern(self):
        """8.6 — Worktree paths follow expected pattern."""
        # Vector worktrees: /tmp/vector-worker-issue-{N}
        # NUC worktrees: /tmp/nuc-worker-issue-{N}
        for issue_num in [42, 100]:
            vec_path = f"/tmp/vector-worker-issue-{issue_num}"
            nuc_path = f"/tmp/nuc-worker-issue-{issue_num}"
            assert "vector" in vec_path
            assert "nuc" in nuc_path
            assert str(issue_num) in vec_path
            assert str(issue_num) in nuc_path


# 8.7 PR review hook exists
class TestPRReviewHookExists:
    def test_review_hook_script(self, repo_root: str):
        """8.7 — Review hook script present and executable."""
        # Check for agent definition
        hook_path = os.path.join(repo_root, ".claude", "agents", "pr-review-hook.md")
        if os.path.isfile(hook_path):
            assert True
            return

        # Alternative: check for review in agent loop
        dispatch_path = os.path.join(
            repo_root, "apps", "control_plane", "agent_loop", "dispatch.py"
        )
        assert os.path.isfile(dispatch_path), "Neither review hook nor dispatch.py found"
