"""Unit tests for HomeGuardian patrol system."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.map_store import MapStore
from apps.vector.src.planner.nav_controller import NavController, NavConfig
from apps.vector.src.planner.patrol import (
    HomeGuardian,
    PatrolConfig,
    PatrolEvent,
    PatrolMode,
    PatrolState,
    _format_duration,
)
from apps.vector.src.planner.visual_slam import VisualSLAM
from apps.vector.src.planner.waypoint_manager import WaypointManager


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def motor():
    mc = MagicMock()
    mc.drive_wheels = MagicMock()
    mc.turn_in_place = MagicMock()
    mc.turn_then_drive = MagicMock()
    mc.emergency_stop = MagicMock()
    return mc


@pytest.fixture()
def head():
    hc = MagicMock()
    hc.set_angle = MagicMock(return_value=10.0)
    return hc


@pytest.fixture()
def camera():
    cam = MagicMock()
    cam.get_latest_frame = MagicMock(return_value=None)
    cam.get_latest_jpeg = MagicMock(return_value=None)
    return cam


@pytest.fixture()
def intercom():
    ic = MagicMock()
    ic.send_text = MagicMock(return_value=True)
    ic.send_photo = MagicMock(return_value=True)
    return ic


@pytest.fixture()
def robot():
    r = MagicMock()
    r.behavior = MagicMock()
    r.behavior.say_text = MagicMock()
    return r


@pytest.fixture()
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def slam(bus):
    return VisualSLAM(bus, grid_size_mm=2000, cell_size_mm=50)


@pytest.fixture()
def nav(slam, motor, head, bus, tmp_dir):
    store = MapStore(map_dir=tmp_dir)
    wp_mgr = WaypointManager(store, map_name="test")
    return NavController(
        slam, motor, head, bus, store, wp_mgr,
        NavConfig(arrival_tolerance_mm=100),
    )


@pytest.fixture()
def guardian(nav, motor, head, camera, bus, intercom, robot):
    cfg = PatrolConfig(
        dwell_time_s=0.5,
        scan_hz=2.0,
        head_angles=(0.0,),
        head_hold_s=0.2,
        loop_pause_s=0.1,
        sentry_interval_s=0.2,
        voice_enabled=False,  # disable TTS in tests
        scene_description_enabled=False,
    )
    return HomeGuardian(
        nav_controller=nav,
        motor=motor,
        head=head,
        camera=camera,
        nuc_bus=bus,
        intercom=intercom,
        robot=robot,
        config=cfg,
    )


class TestHomeGuardianLifecycle:
    def test_initial_state(self, guardian):
        assert guardian.state == PatrolState.IDLE
        assert not guardian.is_running
        assert not guardian.is_paused

    def test_start_stop_patrol(self, guardian, intercom):
        # No waypoints → will stop quickly
        guardian.start(mode="patrol")
        time.sleep(1.0)
        # Should have either stopped or be running
        guardian.stop()
        assert guardian.state == PatrolState.IDLE
        assert not guardian.is_running
        # Should have sent Signal messages
        assert intercom.send_text.call_count >= 1

    def test_start_stop_sentry(self, guardian, intercom):
        guardian.start(mode="sentry")
        assert guardian.is_running
        time.sleep(0.5)
        guardian.stop()
        assert guardian.state == PatrolState.IDLE
        assert intercom.send_text.call_count >= 1

    def test_double_start_ignored(self, guardian):
        guardian.start(mode="sentry")
        guardian.start(mode="sentry")  # should log warning, not crash
        guardian.stop()

    def test_pause_resume(self, guardian):
        guardian.start(mode="sentry")
        guardian.pause()
        assert guardian.is_paused
        assert guardian.state == PatrolState.PAUSED
        guardian.resume()
        assert not guardian.is_paused
        guardian.stop()


class TestHomeGuardianStatus:
    def test_get_status(self, guardian):
        status = guardian.get_status()
        assert "state" in status
        assert "mode" in status
        assert "running" in status
        assert "patrol_count" in status
        assert "persons_detected" in status
        assert "alerts_sent" in status
        assert "recent_events" in status

    def test_get_activity_log_empty(self, guardian):
        log = guardian.get_activity_log()
        assert log == []

    def test_activity_log_after_start_stop(self, guardian):
        guardian.start(mode="sentry")
        time.sleep(0.3)
        guardian.stop()
        log = guardian.get_activity_log()
        assert len(log) >= 1
        types = [e["type"] for e in log]
        assert "patrol_start" in types

    def test_status_during_sentry(self, guardian):
        guardian.start(mode="sentry")
        time.sleep(0.3)
        status = guardian.get_status()
        assert status["running"] is True
        assert status["mode"] == "sentry"
        guardian.stop()


class TestHomeGuardianDetection:
    def test_no_crash_without_detectors(self, guardian):
        """Scanning without detectors should log all_clear, not crash."""
        guardian._running = True  # simulate running state
        guardian._scan_location("test_room")
        guardian._running = False
        log = guardian.get_activity_log()
        types = [e["type"] for e in log]
        assert "all_clear" in types

    def test_detect_persons_no_detector(self, guardian):
        """With no detector, returns empty list."""
        import numpy as np
        frame = np.zeros((600, 800, 3), dtype=np.uint8)
        result = guardian._detect_persons(frame)
        assert result == []

    def test_try_face_recognition_no_detector(self, guardian):
        """With no face detector, returns None."""
        import numpy as np
        frame = np.zeros((600, 800, 3), dtype=np.uint8)
        result = guardian._try_face_recognition(frame)
        assert result is None


class TestHomeGuardianAlerts:
    def test_alert_cooldown(self, guardian, intercom):
        """Same person+location should not re-alert within cooldown."""
        guardian._cfg.alert_cooldown_s = 60.0
        guardian._alert_cooldown["kitchen:unknown"] = time.monotonic()

        import numpy as np
        frame = np.zeros((600, 800, 3), dtype=np.uint8)
        guardian._send_alert("kitchen", frame, "unknown", 0.9)

        # Should NOT have sent alert (cooldown active)
        intercom.send_photo.assert_not_called()

    def test_alert_sent_after_cooldown(self, guardian, intercom):
        """Alert should be sent after cooldown expires."""
        guardian._cfg.alert_cooldown_s = 0.0  # no cooldown

        import numpy as np
        frame = np.zeros((600, 800, 3), dtype=np.uint8)
        guardian._send_alert("kitchen", frame, "unknown", 0.9)

        # Should have sent alert
        assert intercom.send_photo.call_count == 1
        assert guardian._alerts_sent == 1


class TestPatrolWaypoints:
    def test_get_patrol_waypoints_empty(self, guardian):
        """No saved waypoints returns empty list."""
        wps = guardian._get_patrol_waypoints(None)
        assert wps == []

    def test_get_patrol_waypoints_explicit(self, guardian):
        """Explicit waypoint list is returned as-is."""
        wps = guardian._get_patrol_waypoints(["kitchen", "bedroom"])
        assert wps == ["kitchen", "bedroom"]

    def test_charger_excluded_from_patrol(self, guardian, nav, tmp_dir):
        """The 'charger' waypoint should be excluded from default patrol."""
        wp_mgr = nav._waypoint_mgr
        wp_mgr.save("kitchen", x=100, y=200, theta=0.0)
        wp_mgr.save("charger", x=0, y=0, theta=0.0)
        wp_mgr.save("bedroom", x=300, y=400, theta=0.0)

        wps = guardian._get_patrol_waypoints(None)
        assert "charger" not in wps
        assert "kitchen" in wps
        assert "bedroom" in wps


class TestPatrolMode:
    def test_patrol_mode_enum(self):
        assert PatrolMode.PATROL.name == "PATROL"
        assert PatrolMode.SENTRY.name == "SENTRY"

    def test_patrol_state_enum(self):
        assert PatrolState.IDLE.name == "IDLE"
        assert PatrolState.SCANNING.name == "SCANNING"
        assert PatrolState.ALERT.name == "ALERT"
        assert PatrolState.NAVIGATING.name == "NAVIGATING"
        assert PatrolState.PAUSED.name == "PAUSED"


class TestPatrolEvent:
    def test_event_creation(self):
        event = PatrolEvent(
            timestamp=time.time(),
            event_type="person_detected",
            waypoint="kitchen",
            details="Unknown person in kitchen",
            person_name="unknown",
            confidence=0.85,
        )
        assert event.event_type == "person_detected"
        assert event.waypoint == "kitchen"
        assert event.person_name == "unknown"


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(30) == "30s"

    def test_minutes(self):
        assert _format_duration(150) == "2m"

    def test_hours(self):
        assert _format_duration(3700) == "1h 1m"

    def test_zero(self):
        assert _format_duration(0) == "0s"


class TestPatrolConfig:
    def test_defaults(self):
        cfg = PatrolConfig()
        assert cfg.dwell_time_s == 8.0
        assert cfg.scan_hz == 5.0
        assert cfg.alert_cooldown_s == 120.0
        assert cfg.scene_description_enabled is True
        assert cfg.alert_on_known_faces is False
        assert cfg.voice_enabled is True

    def test_custom(self):
        cfg = PatrolConfig(
            dwell_time_s=3.0,
            alert_on_known_faces=True,
        )
        assert cfg.dwell_time_s == 3.0
        assert cfg.alert_on_known_faces is True
