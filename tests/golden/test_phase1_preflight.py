"""Phase 1 — Preflight: Robot Connectivity + Battery Gate.

Gate: If this phase fails, skip Phases 2, 5, 6.
NUC-only phases (3, 4, 7, 8, 9) still run.
"""

from __future__ import annotations

import time

import pytest


pytestmark = [pytest.mark.phase1, pytest.mark.robot]


class TestVectorConnect:
    def test_sdk_connects(self, robot_connected):
        """1.1 — robot.connect() succeeds, robot.status populated."""
        assert robot_connected.status is not None


class TestBatteryVoltage:
    def test_battery_above_safe_threshold(self, robot_connected):
        """1.2 — Battery level safe for motors (>= LOW)."""
        battery = robot_connected.get_battery_state()
        assert battery.battery_level >= 1, (
            f"Battery level too low: {battery.battery_level} (need >= 1/LOW)"
        )


class TestGRPCLatency:
    def test_latency_under_500ms(self, robot_connected):
        """1.3 — Timed get_battery_state() < 500ms."""
        start = time.monotonic()
        robot_connected.get_battery_state()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"gRPC latency too high: {elapsed_ms:.0f}ms"


class TestControlHandoff:
    def test_request_release_cycle(self, robot_connected):
        """1.4 — Request, release, and re-request control cleanly."""
        robot_connected.conn.request_control()
        robot_connected.conn.release_control()
        robot_connected.conn.request_control()
