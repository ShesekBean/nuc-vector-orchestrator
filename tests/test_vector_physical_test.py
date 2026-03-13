"""Tests for the Vector physical test framework (issue #34).

Tests the inbox whitelist, script structure, and Signal integration patterns.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "vector-physical-test.sh"
SIGNAL_LIB_PATH = REPO_ROOT / "scripts" / "signal-interactive.sh"


# ── Script existence and structure ────────────────────────────────────────────


class TestScriptStructure:
    def test_script_exists(self):
        assert SCRIPT_PATH.exists(), "vector-physical-test.sh must exist"

    def test_script_is_executable(self):
        assert os.access(SCRIPT_PATH, os.X_OK), "script must be executable"

    def test_script_sources_signal_interactive(self):
        content = SCRIPT_PATH.read_text()
        assert "source" in content and "signal-interactive.sh" in content

    def test_script_uses_bridge_url(self):
        content = SCRIPT_PATH.read_text()
        assert "localhost:8081" in content or "VECTOR_BRIDGE_URL" in content

    def test_script_has_all_test_categories(self):
        content = SCRIPT_PATH.read_text()
        categories = ["health", "led", "head", "lift", "tts", "camera", "display", "motor", "stop"]
        for cat in categories:
            assert f"test_{cat}" in content, f"Missing test category function: test_{cat}"

    def test_script_uses_sig_verify(self):
        content = SCRIPT_PATH.read_text()
        assert "sig_verify" in content, "Must use sig_verify for pass/fail verdicts"

    def test_script_uses_sig_send(self):
        content = SCRIPT_PATH.read_text()
        assert "sig_send" in content, "Must use sig_send for messages"

    def test_script_writes_result_file(self):
        content = SCRIPT_PATH.read_text()
        assert "RESULT_FILE" in content
        assert "physical-test-result" in content

    def test_script_checks_bridge_health(self):
        content = SCRIPT_PATH.read_text()
        assert "check_bridge_health" in content
        assert "/health" in content

    def test_script_posts_github_comment(self):
        content = SCRIPT_PATH.read_text()
        assert "gh issue comment" in content

    def test_signal_interactive_exists(self):
        assert SIGNAL_LIB_PATH.exists(), "signal-interactive.sh must exist"


# ── Non-interactive mode (SIG_INTERACTIVE=false) ─────────────────────────────


class TestNonInteractiveMode:
    def test_script_syntax_valid(self):
        """Verify the script parses without syntax errors."""
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_signal_interactive_syntax_valid(self):
        """Verify signal-interactive.sh parses without syntax errors."""
        result = subprocess.run(
            ["bash", "-n", str(SIGNAL_LIB_PATH)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"


# ── Inbox whitelist ──────────────────────────────────────────────────────────


class TestInboxWhitelist:
    def test_vector_physical_test_in_whitelist(self):
        """The inbox _handle_go must include vector-physical-test in allowed scripts."""
        inbox_path = REPO_ROOT / "apps" / "control_plane" / "agent_loop" / "inbox.py"
        content = inbox_path.read_text()
        assert "vector-physical-test" in content, \
            "vector-physical-test must be in _ALLOWED_SETUP_SCRIPTS"

    def test_whitelist_points_to_correct_script(self):
        inbox_path = REPO_ROOT / "apps" / "control_plane" / "agent_loop" / "inbox.py"
        content = inbox_path.read_text()
        assert "scripts/vector-physical-test.sh" in content


# ── Inbox go handler tests ───────────────────────────────────────────────────


class TestInboxGoHandler:
    def test_is_physical_test_go_bare(self):
        from apps.control_plane.agent_loop.inbox import is_physical_test_go
        assert is_physical_test_go("#go")
        assert is_physical_test_go("go")
        assert is_physical_test_go("#Go")
        assert is_physical_test_go("GO")

    def test_is_physical_test_go_with_issue(self):
        from apps.control_plane.agent_loop.inbox import is_physical_test_go
        assert is_physical_test_go("#go 34")
        assert is_physical_test_go("#go #34")
        assert is_physical_test_go("go 34")

    def test_is_not_go(self):
        from apps.control_plane.agent_loop.inbox import is_physical_test_go
        assert not is_physical_test_go("going")
        assert not is_physical_test_go("let's go")
        assert not is_physical_test_go("#golden")

    def test_parse_go_issue_number(self):
        from apps.control_plane.agent_loop.inbox import parse_go_issue_number
        assert parse_go_issue_number("#go 34") == "34"
        assert parse_go_issue_number("#go #34") == "34"
        assert parse_go_issue_number("go 99") == "99"
        assert parse_go_issue_number("#go") == ""

    def test_parse_physical_test_result(self):
        from apps.control_plane.agent_loop.inbox import parse_physical_test_result
        assert parse_physical_test_result("pass") == "pass"
        assert parse_physical_test_result("Pass") == "pass"
        assert parse_physical_test_result("passed") == "pass"
        assert parse_physical_test_result("fail") == "fail"
        assert parse_physical_test_result("failed") == "fail"
        assert parse_physical_test_result("looks good") == "pass"
        assert parse_physical_test_result("not working") == "fail"
        assert parse_physical_test_result("hello") == ""


# ── Physical test field parsing ──────────────────────────────────────────────


class TestPhysicalTestFieldParsing:
    def test_parse_standard_format(self):
        from apps.control_plane.agent_loop.inbox import _parse_physical_test_fields
        comments = """## Worker: Physical Test Request

**Setup command:** bash scripts/vector-physical-test.sh --issue 34
**What to observe:** Watch Vector run through LED, head, motor tests
**Pass criteria:** All steps report pass on Signal
**Fail criteria:** Any step fails or Vector doesn't respond"""

        result = _parse_physical_test_fields(comments)
        assert "vector-physical-test" in result["setup_command"]
        assert "LED" in result["observe"]
        assert "pass" in result["pass_criteria"].lower()
        assert "fails" in result["fail_criteria"].lower()

    def test_parse_heading_format(self):
        from apps.control_plane.agent_loop.inbox import _parse_physical_test_fields
        comments = """## Worker: Physical Test Request

### Setup command
```bash
bash scripts/vector-physical-test.sh --issue 34
```

### What to observe
Watch Vector's LEDs, head movement, and motor control.

### Pass criteria
All Signal verdicts are pass.

### Fail criteria
Any verdict fails."""

        result = _parse_physical_test_fields(comments)
        assert "vector-physical-test" in result["setup_command"]


# ── Bridge endpoint coverage ─────────────────────────────────────────────────


class TestBridgeEndpointCoverage:
    """Verify the test script exercises all key bridge endpoints."""

    def test_covers_led_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/led"' in content

    def test_covers_head_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/head"' in content

    def test_covers_lift_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/lift"' in content

    def test_covers_audio_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/audio/play"' in content

    def test_covers_capture_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert "/capture" in content

    def test_covers_display_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/display"' in content

    def test_covers_move_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/move"' in content

    def test_covers_stop_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/stop"' in content

    def test_covers_health_endpoint(self):
        content = SCRIPT_PATH.read_text()
        assert '"/health"' in content
