"""Tests for PGM auto-unstick (dependency resolution) feature."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.control_plane.agent_loop.pgm import PGMManager


# ── _parse_dependencies tests ────────────────────────────────────────────────


class TestParseDependencies:
    def test_basic_dependencies(self):
        body = (
            "## Summary\nDo something\n\n"
            "## Dependencies\n- #1\n- #3\n- #5\n\n"
            "## Notes\nSome notes"
        )
        assert PGMManager._parse_dependencies(body) == [1, 3, 5]

    def test_no_dependencies_section(self):
        body = "## Summary\nJust a plain issue with no deps section."
        assert PGMManager._parse_dependencies(body) == []

    def test_empty_dependencies_section(self):
        body = "## Dependencies\n\n## Notes\nStuff"
        assert PGMManager._parse_dependencies(body) == []

    def test_inline_references(self):
        body = "## Dependencies\nBlocked by #10 and #20.\n"
        assert PGMManager._parse_dependencies(body) == [10, 20]

    def test_multiple_refs_per_line(self):
        body = "## Dependencies\n- #1, #2, #3\n"
        assert PGMManager._parse_dependencies(body) == [1, 2, 3]

    def test_dedup_references(self):
        body = "## Dependencies\n- #5\n- #5 again\n"
        assert PGMManager._parse_dependencies(body) == [5]

    def test_stops_at_next_heading(self):
        body = (
            "## Dependencies\n- #1\n\n"
            "## Acceptance Criteria\n- #99 should not be parsed\n"
        )
        assert PGMManager._parse_dependencies(body) == [1]

    def test_case_insensitive_heading(self):
        body = "## dependencies\n- #7\n"
        assert PGMManager._parse_dependencies(body) == [7]

    def test_dependencies_with_descriptions(self):
        body = (
            "## Dependencies\n"
            "- #3 health check must be done first\n"
            "- #39 connection config\n"
        )
        assert PGMManager._parse_dependencies(body) == [3, 39]


# ── _check_stuck_dependencies tests ──────────────────────────────────────────


def _make_pgm(tmp_path: Path) -> PGMManager:
    """Create a PGMManager with a mocked Config."""
    cfg = MagicMock()
    cfg.nuc_repo = "ophir-sw/nuc-vector-orchestrator"
    cfg.repo_dir = tmp_path
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(exist_ok=True)
    # Create the gate script so _signal_gate doesn't crash
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "pgm-signal-gate.sh").write_text("#!/bin/bash\nexit 0\n")
    return PGMManager(cfg)


class TestCheckStuckDependencies:
    @patch("apps.control_plane.agent_loop.pgm.gh")
    def test_all_deps_closed_unsticks(self, mock_gh, tmp_path):
        """When all dependency issues are closed, remove stuck + add assigned:worker."""
        pgm = _make_pgm(tmp_path)

        mock_gh.issue_list.return_value = [
            {"number": 10, "title": "Blocked issue", "body": "## Dependencies\n- #1\n- #3\n"}
        ]
        # issue_view returns "CLOSED" for both deps
        mock_gh.issue_view.return_value = "CLOSED"

        pgm._check_stuck_dependencies()

        mock_gh.issue_edit_labels.assert_called_once_with(
            "ophir-sw/nuc-vector-orchestrator", 10,
            remove=["stuck"], add=["assigned:worker"],
        )

    @patch("apps.control_plane.agent_loop.pgm.gh")
    def test_partial_deps_stays_stuck(self, mock_gh, tmp_path):
        """When some deps are still open, do not unstick."""
        pgm = _make_pgm(tmp_path)

        mock_gh.issue_list.return_value = [
            {"number": 10, "title": "Blocked", "body": "## Dependencies\n- #1\n- #3\n"}
        ]
        # #1 is closed, #3 is open
        mock_gh.issue_view.side_effect = ["CLOSED", "OPEN"]

        pgm._check_stuck_dependencies()

        mock_gh.issue_edit_labels.assert_not_called()

    @patch("apps.control_plane.agent_loop.pgm.gh")
    def test_no_deps_section_skipped(self, mock_gh, tmp_path):
        """Issues without a Dependencies section are skipped."""
        pgm = _make_pgm(tmp_path)

        mock_gh.issue_list.return_value = [
            {"number": 10, "title": "No deps", "body": "## Summary\nJust stuck manually."}
        ]

        pgm._check_stuck_dependencies()

        mock_gh.issue_view.assert_not_called()
        mock_gh.issue_edit_labels.assert_not_called()

    @patch("apps.control_plane.agent_loop.pgm.gh")
    def test_empty_body_skipped(self, mock_gh, tmp_path):
        """Issues with empty body are skipped."""
        pgm = _make_pgm(tmp_path)

        mock_gh.issue_list.return_value = [
            {"number": 10, "title": "Empty", "body": ""}
        ]

        pgm._check_stuck_dependencies()

        mock_gh.issue_view.assert_not_called()

    @patch("apps.control_plane.agent_loop.pgm.gh")
    def test_signal_notification_sent(self, mock_gh, tmp_path):
        """Verify Signal notification is sent on unstick."""
        pgm = _make_pgm(tmp_path)
        pgm._signal_gate = MagicMock()

        mock_gh.issue_list.return_value = [
            {"number": 15, "title": "Follow me", "body": "## Dependencies\n- #3\n"}
        ]
        mock_gh.issue_view.return_value = "CLOSED"

        pgm._check_stuck_dependencies()

        pgm._signal_gate.assert_called_once()
        args = pgm._signal_gate.call_args
        assert args[0][0] == "general"
        assert args[0][1] == "15"
        assert "unblocked" in args[0][2]
        assert "#15" in args[0][2]
