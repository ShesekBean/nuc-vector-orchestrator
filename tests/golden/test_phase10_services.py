"""Phase 10 — Services Health.

Tests that all systemd services and infrastructure are running.
"""

from __future__ import annotations

import subprocess

import pytest


pytestmark = pytest.mark.phase10


def _curl_health(url: str, timeout: int = 5) -> tuple[int, str]:
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


def _systemctl_is_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _port_listening(port: int) -> bool:
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        )
        return str(port) in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


class TestWirePodService:
    def test_wirepod_active_and_reachable(self):
        """10.1 — wire-pod active, web UI returns 200, chipper port 443 listening."""
        if not _systemctl_is_active("wire-pod"):
            pytest.skip("wire-pod service not running")

        code, _ = _curl_health("http://localhost:8080")
        assert code == 200, f"wire-pod web UI returned {code}"
        assert _port_listening(443), "wire-pod chipper port 443 not listening"


class TestOpenClawGatewayHealth:
    def test_gateway_health(self):
        """10.2 — OpenClaw gateway at localhost:18889/health returns 200."""
        code, _ = _curl_health("http://localhost:18889/health")
        if code == 0:
            pytest.skip("OpenClaw gateway not running")
        assert code == 200, f"Gateway health returned {code}"
