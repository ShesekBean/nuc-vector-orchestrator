"""Tests for FollowPipeline quiet-mode pause/resume integration."""

from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.bridge.follow_pipeline import FollowPipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pipeline():
    """Create a FollowPipeline with fully mocked dependencies."""
    camera = MagicMock()
    motor = MagicMock()
    head = MagicMock()
    bus = MagicMock()
    p = FollowPipeline(camera, motor, head, bus)
    # Mock out detector/planner/obstacle so start/stop don't do real work
    p._detector = MagicMock()
    p._detector.avg_inference_ms = 5.0
    p._planner = MagicMock()
    p._obstacle = MagicMock()
    p._tracker = MagicMock()
    return p


# ---------------------------------------------------------------------------
# Tests: _send_quiet_mode_signal
# ---------------------------------------------------------------------------


class TestSendQuietModeSignal:
    """Test the static signal-sending helper."""

    def test_no_pid_file(self, tmp_path):
        """No-op when PID file doesn't exist."""
        with patch("apps.vector.bridge.follow_pipeline.QUIET_MODE_PID_FILE",
                    str(tmp_path / "nonexistent.pid")):
            # Should not raise
            FollowPipeline._send_quiet_mode_signal(signal.SIGUSR1, "test")

    def test_stale_pid(self, tmp_path):
        """No-op when PID file contains a dead process PID."""
        pid_file = tmp_path / "stale.pid"
        pid_file.write_text("999999999")  # Almost certainly not running

        with patch("apps.vector.bridge.follow_pipeline.QUIET_MODE_PID_FILE",
                    str(pid_file)):
            # Should not raise
            FollowPipeline._send_quiet_mode_signal(signal.SIGUSR1, "test")

    def test_valid_pid_sends_signal(self, tmp_path):
        """Sends signal when PID file points to a live process."""
        pid_file = tmp_path / "live.pid"
        pid = os.getpid()
        pid_file.write_text(str(pid))

        with patch("apps.vector.bridge.follow_pipeline.QUIET_MODE_PID_FILE",
                    str(pid_file)), \
             patch("os.kill") as mock_kill:
            # First os.kill(pid, 0) check — process alive
            # Second os.kill(pid, sig) — send signal
            mock_kill.return_value = None
            FollowPipeline._send_quiet_mode_signal(signal.SIGUSR1, "SIGUSR1")
            assert mock_kill.call_count == 2
            mock_kill.assert_any_call(pid, 0)
            mock_kill.assert_any_call(pid, signal.SIGUSR1)

    def test_invalid_pid_content(self, tmp_path):
        """No-op when PID file contains non-numeric content."""
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number")

        with patch("apps.vector.bridge.follow_pipeline.QUIET_MODE_PID_FILE",
                    str(pid_file)):
            FollowPipeline._send_quiet_mode_signal(signal.SIGUSR1, "test")


# ---------------------------------------------------------------------------
# Tests: start/stop integration
# ---------------------------------------------------------------------------


class TestPipelineQuietModeIntegration:
    """Test that start/stop call pause/resume."""

    def test_start_pauses_quiet_mode(self, pipeline):
        """start() should pause quiet mode before loading YOLO."""
        with patch.object(pipeline, "_pause_quiet_mode") as mock_pause:
            pipeline.start()
            mock_pause.assert_called_once()

    def test_stop_resumes_quiet_mode(self, pipeline):
        """stop() should resume quiet mode after stopping planner."""
        pipeline._running = True
        with patch.object(pipeline, "_resume_quiet_mode") as mock_resume:
            pipeline.stop()
            mock_resume.assert_called_once()

    def test_stop_when_not_running_is_noop(self, pipeline):
        """stop() on an already-stopped pipeline should not resume quiet mode."""
        pipeline._running = False
        with patch.object(pipeline, "_resume_quiet_mode") as mock_resume:
            pipeline.stop()
            mock_resume.assert_not_called()
