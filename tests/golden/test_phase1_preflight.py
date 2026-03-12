"""Phase 1 — Preflight: Robot Connectivity + Battery Gate (~12s).

Gate: If this phase fails, skip Phases 2, 5, 6.
NUC-only phases (3, 4, 7, 8, 9) still run.

Tests 1.1–1.5 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import time

import pytest


pytestmark = [pytest.mark.phase1, pytest.mark.robot]


# 1.1 Vector SDK connects
class TestVectorConnect:
    def test_sdk_connects(self, robot_connected):
        """1.1 — robot.connect() succeeds, robot.status populated."""
        robot = robot_connected
        # If we got here, connection succeeded (fixture handles connect)
        status = robot.status
        assert status is not None


# 1.2 Battery voltage
class TestBatteryVoltage:
    def test_battery_above_safe_threshold(self, robot_connected):
        """1.2 — Battery voltage > 3.6V (safe for motors)."""
        robot = robot_connected
        battery = robot.get_battery_state()
        assert battery.battery_volts > 3.6, (
            f"Battery too low: {battery.battery_volts}V (need > 3.6V)"
        )


# 1.3 gRPC round-trip latency
class TestGRPCLatency:
    def test_latency_under_500ms(self, robot_connected):
        """1.3 — Timed get_battery_state() < 500ms."""
        robot = robot_connected
        start = time.monotonic()
        robot.get_battery_state()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"gRPC latency too high: {elapsed_ms:.0f}ms"


# 1.4 Request control
class TestRequestControl:
    def test_control_granted(self, robot_connected):
        """1.4 — request_control() grants control."""
        robot = robot_connected
        robot.conn.request_control()
        # No exception means control was granted


# 1.5 Release control
class TestReleaseControl:
    def test_clean_handoff(self, robot_connected):
        """1.5 — Release + re-request → clean handoff, no errors."""
        robot = robot_connected
        robot.conn.release_control()
        robot.conn.request_control()
        # No exception means clean handoff
