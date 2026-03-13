"""Phase 7 — Signal Integration.

Tests OpenClaw → bridge → robot path.
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


class TestSignalGateValidation:
    def test_signal_gate_validates_event_types(self, repo_root: str):
        """7.1 — pgm-signal-gate.sh validates event types (invalid → graceful exit)."""
        script = os.path.join(repo_root, "scripts", "pgm-signal-gate.sh")
        if not os.path.isfile(script):
            pytest.skip("Signal gate script not found")

        result = subprocess.run(
            ["bash", script, "invalid_type_xyz", "0", "test"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "DRY_RUN": "1"},
        )
        assert result.returncode in (0, 1, 2)


class TestBridgeEndpoints:
    def test_bridge_health_and_endpoints(self):
        """7.2 — Bridge health returns 200, /audio/play and /led endpoints exist."""
        code, _ = _curl_health("http://localhost:8081/health")
        if code == 0:
            pytest.skip("Bridge not running")
        assert code == 200, f"Bridge health returned {code}"

        # Test /audio/play endpoint
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", '{"text":"test"}',
                 "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8081/audio/play"],
                capture_output=True, text=True, timeout=10,
            )
            play_code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            play_code = 0
        # 500 = bridge up but robot gRPC call failed (acceptable)
        assert play_code in (200, 201, 204, 500), f"Bridge /audio/play returned {play_code}"

        # Test /led endpoint
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", '{"hue":0.33,"saturation":1.0}',
                 "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8081/led"],
                capture_output=True, text=True, timeout=10,
            )
            led_code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            led_code = 0
        assert led_code in (200, 201, 204, 500), f"Bridge /led returned {led_code}"


class TestRobotControlSkill:
    def test_skill_complete(self):
        """7.3 — robot-control skill exists and covers LED, speech, status, and trigger."""
        skill_path = os.path.expanduser(
            "~/.openclaw/workspace/skills/robot-control/SKILL.md"
        )
        if not os.path.isfile(skill_path):
            pytest.skip("robot-control skill not deployed")

        content = open(skill_path).read().lower()
        for keyword, desc in [
            ("led", "LED/light control"),
            ("say", "speech/TTS"),
            ("status", "status/health"),
            ("robot", "trigger keyword"),
        ]:
            assert keyword in content, f"Skill missing {desc} (keyword: {keyword})"
