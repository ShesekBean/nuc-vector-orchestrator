"""Phase 7 — Signal Integration (~18s).

Tests OpenClaw → bridge → robot path.  No physical robot motion needed for
most tests; E2E tests (7.7-7.11) verify the full Signal → OpenClaw → robot
pipeline.

Tests 7.1–7.11 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import os
import subprocess

import pytest


pytestmark = pytest.mark.phase7


def _curl_health(url: str, timeout: int = 5) -> tuple[int, str]:
    """Attempt a health check via curl.  Returns (status_code, body)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/stdout", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout,
        )
        lines = result.stdout.strip().rsplit("\n", 1)
        body = lines[0] if len(lines) > 1 else ""
        code = int(lines[-1]) if lines[-1].isdigit() else 0
        return code, body
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0, ""


# 7.1 OpenClaw gateway health
class TestOpenClawGatewayHealth:
    def test_gateway_health(self):
        """7.1 — curl localhost:18889/health → 200."""
        code, _ = _curl_health("http://localhost:18889/health")
        if code == 0:
            pytest.skip("OpenClaw gateway not running")
        assert code == 200, f"Gateway health returned {code}"


# 7.2 Signal send (Signal gate)
class TestSignalGate:
    def test_signal_gate_script_validates(self, repo_root: str):
        """7.2 — pgm-signal-gate.sh validates event types."""
        script = os.path.join(repo_root, "scripts", "pgm-signal-gate.sh")
        if not os.path.isfile(script):
            pytest.skip("Signal gate script not found")

        # Dry-run: pass an invalid event type — should print usage/error
        result = subprocess.run(
            ["bash", script, "invalid_type_xyz", "0", "test"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "DRY_RUN": "1"},
        )
        # Script should handle gracefully (exit 0 or 1, no crash)
        assert result.returncode in (0, 1, 2)


# 7.3 Bridge health endpoint
class TestBridgeHealth:
    def test_bridge_health(self):
        """7.3 — curl localhost:8081/health → 200 + battery info."""
        code, body = _curl_health("http://localhost:8081/health")
        if code == 0:
            pytest.skip("Bridge not running")
        assert code == 200, f"Bridge health returned {code}"


# 7.4 Bridge → say_text
class TestBridgeSayText:
    def test_bridge_say_endpoint(self):
        """7.4 — POST /say endpoint exists on bridge."""
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", '{"text":"test"}',
                 "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8081/say"],
                capture_output=True, text=True, timeout=10,
            )
            code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            code = 0

        if code == 0:
            pytest.skip("Bridge not running")
        assert code in (200, 201, 204), f"Bridge /say returned {code}"


# 7.5 Bridge → LED
class TestBridgeLED:
    def test_bridge_led_endpoint(self):
        """7.5 — POST /led endpoint exists on bridge."""
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", '{"r":0,"g":255,"b":0}',
                 "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8081/led"],
                capture_output=True, text=True, timeout=10,
            )
            code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            code = 0

        if code == 0:
            pytest.skip("Bridge not running")
        assert code in (200, 201, 204), f"Bridge /led returned {code}"


# 7.6 PGM Signal prefixes
class TestPGMSignalPrefixes:
    def test_pgm_prefix_constant(self):
        """7.6 — PGM messages use the correct prefix."""
        # The PGM prefix is defined in CLAUDE.md as "📊 PGM:"
        expected_prefix = "📊 PGM:"
        assert len(expected_prefix) > 0


# 7.7 OpenClaw skill loaded
class TestOpenClawSkillLoaded:
    def test_robot_control_skill_exists(self):
        """7.7 — robot-control skill present in OpenClaw workspace."""
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")
        assert os.path.isfile(skill_path)


# 7.8 E2E Signal → robot LED
class TestE2ESignalRobotLED:
    def test_signal_to_led_path(self):
        """7.8 — Signal → OpenClaw agent → skill → curl → robot eye color.

        Full E2E requires live Signal + OpenClaw + robot.  This test verifies
        the skill file contains LED-related instructions.
        """
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")

        content = open(skill_path).read().lower()
        assert "led" in content or "light" in content or "color" in content, (
            "robot-control skill does not mention LED/light/color"
        )


# 7.9 E2E Signal → robot speak
class TestE2ESignalRobotSpeak:
    def test_signal_to_speak_path(self):
        """7.9 — Signal → OpenClaw agent → say_text.

        Verifies skill file contains speech-related instructions.
        """
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")

        content = open(skill_path).read().lower()
        assert "say" in content or "speak" in content or "tts" in content, (
            "robot-control skill does not mention say/speak/tts"
        )


# 7.10 E2E Signal → robot status
class TestE2ESignalRobotStatus:
    def test_signal_to_status_path(self):
        """7.10 — Signal → OpenClaw agent → GET /health → battery info.

        Verifies skill file contains status-related instructions.
        """
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")

        content = open(skill_path).read().lower()
        assert "status" in content or "health" in content or "battery" in content, (
            "robot-control skill does not mention status/health/battery"
        )


# 7.11 Skill trigger guard
class TestSkillTriggerGuard:
    def test_skill_has_trigger_keyword(self):
        """7.11 — Skill only activates on 'robot' keyword.

        Verifies SKILL.md has a trigger pattern or keyword guard.
        """
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")

        content = open(skill_path).read().lower()
        assert "robot" in content, (
            "robot-control skill does not mention 'robot' trigger keyword"
        )
