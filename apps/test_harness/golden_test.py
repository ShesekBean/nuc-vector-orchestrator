#!/usr/bin/env python3
"""Golden sanity test — cumulative hardware regression covering every subsystem.

Covers: SSH, container, battery, LED, servo, motor, YOLO detection,
following, LiDAR, IMU, voice command, Signal→Robot e2e, emergency stop,
plus Sprint 11 (multi-object, scene, photo, face, named-follow) and
Sprint 12 (battery status, robot dashboard, live camera, activity log,
TTS, voice chat, visual odometry, navigation, patrol, call session,
charging mode, welcome home).

Uses LiveKit for vision verification (phone camera) and voice injection.

Flow:
  0. Send LiveKit join URL to Ophir via Signal
  1. Auto-poll every 30s until user joins LiveKit room
  2. Validate camera view (sees robot + TV)
  3. Run all tests with before/after vision where applicable
  4. Send report via Signal

Tests:
  1. Preflight — SSH, container, battery (3 tests)
  2. LED — blue only (1 vision-verified test)
  3. Servo — pan left only (1 vision-verified test)
  4. Motor — forward only (1 vision-verified test)
  5. Detection — YOLO detects target class (1 test)
  6. Following — robot drives toward target 20s (1 vision-verified test)
  7. Sensors — LiDAR + IMU topic checks (2 tests)
  8. Voice — 'hey jarvis light blue' via LiveKit (1 test)
  9. Signal→Robot — webhook → OpenClaw → skill → bridge → LED (1 test)
 10. Safety — emergency stop (1 test)

Usage:
    python3 golden_test.py --phases stationary tracking voice signal sprint11 sprint12 safety
"""

from __future__ import annotations

import argparse
import asyncio
import os
import json
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.camera_capture import CameraCapture, _load_credentials, _generate_token
from monitoring.action_evaluator import evaluate_action

try:
    from livekit import rtc
except ImportError:
    print("ERROR: livekit SDK not installed")
    sys.exit(1)

_t0 = time.time()


def tlog(msg: str):
    """Timestamped log."""
    elapsed = time.time() - _t0
    print(f"  [{elapsed:6.1f}s] {msg}")


JETSON_IP = "192.168.1.71"
BRIDGE = f"http://{JETSON_IP}:8081"
CAPTURES_DIR = Path(__file__).parent / "captures" / "golden"
VOICE_DIR = Path(__file__).parent / "voice_commands"
SIGNAL_GROUP = "BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
SIGNAL_NUMBER = "+14084758230"
HOOKS_TOKEN_PATH = Path.home() / ".openclaw" / "hooks-token"


# ── Data classes ──────────────────────────────────────────────

@dataclass
class TestResult:
    phase: str
    name: str
    passed: bool
    method: str  # "api" | "vision" | "topic" | "voice"
    details: str = ""
    confidence: float = 1.0
    before_img: str = ""
    after_img: str = ""


@dataclass
class TestReport:
    results: list[TestResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    def add(self, result: TestResult):
        self.results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.phase}: {result.name} ({result.method}) — {result.details}")

    def summary(self) -> str:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        duration = self.end_time - self.start_time
        lines = [
            f"\n{'='*60}",
            "GOLDEN AUTOMATED COMPREHENSIVE TEST REPORT",
            f"{'='*60}",
            f"Total: {total} | Passed: {passed} | Failed: {total - passed} | Duration: {duration:.0f}s",
            "",
        ]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            conf = f" ({r.confidence:.0%})" if r.method == "vision" else ""
            lines.append(f"  [{status}] {r.phase}: {r.name}{conf}")
            if not r.passed:
                lines.append(f"         {r.details}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)

    def signal_report(self) -> str:
        """Compact report for Signal message."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        duration = self.end_time - self.start_time
        lines = [
            "🤖 GOLDEN TEST REPORT",
            f"✅ {passed}/{total} passed | ⏱ {duration:.0f}s",
            "",
        ]
        for r in self.results:
            icon = "✅" if r.passed else "❌"
            conf = f" ({r.confidence:.0%})" if r.method == "vision" else ""
            lines.append(f"{icon} {r.name}{conf}")
            if not r.passed:
                lines.append(f"   → {r.details[:100]}")
        return "\n".join(lines)


# ── Signal helpers ────────────────────────────────────────────

def signal_send(msg: str):
    """Send message to Signal group."""
    try:
        import shlex
        escaped = shlex.quote(msg)
        result = subprocess.run(
            ["bash", "-c",
             f'source scripts/signal-interactive.sh && sig_send {escaped}'],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"  [!] Signal send failed (rc={result.returncode}): {result.stderr[:200]}")
        else:
            print(f"  [Signal] Sent: {msg[:80]}...")
    except Exception as e:
        print(f"  [!] Signal send failed: {e}")


def robot_say(text: str, blocking: bool = False):
    """Speak text through Vector's say_text() via the NUC bridge /say endpoint.

    Non-blocking by default — fires and forgets so TTS doesn't hold up the test.
    """
    try:
        payload = json.dumps({"text": text})
        cmd = ["curl", "-sf", "-X", "POST", "http://localhost:8081/say",
               "-H", "Content-Type: application/json",
               "-d", payload]
        if blocking:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                print(f"  [TTS] Said: {text[:80]}")
            else:
                print(f"  [!] TTS failed (rc={result.returncode}): {result.stderr[:100]}")
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  [TTS] Queued: {text[:80]}")
    except Exception as e:
        print(f"  [!] TTS failed: {e}")


def announce(msg: str, say: str | None = None):
    """Send Signal message AND speak through robot speaker.

    Args:
        msg: Full message for Signal (can include emojis/formatting).
        say: Plain text for TTS. If None, strips emojis from msg.
    """
    signal_send(msg)
    # Strip emoji prefixes for TTS
    tts_text = say if say is not None else msg.replace("🤖 ", "").replace("✅ ", "pass. ").replace("❌ ", "fail. ")
    robot_say(tts_text)


# ── LiveKit URL cache ────────────────────────────────────────

_LIVEKIT_URL_CACHE = Path(__file__).resolve().parent.parent / ".claude" / "state" / "livekit-join-url.json"


def _get_or_create_join_url(cam: CameraCapture) -> str:
    """Return a cached LiveKit join URL, generating a new one only if needed.

    URLs are cached for 4 hours (JWT tokens typically last 6h).
    """
    import jwt as _jwt_mod

    if _LIVEKIT_URL_CACHE.exists():
        try:
            cached = json.loads(_LIVEKIT_URL_CACHE.read_text())
            url = cached.get("url", "")
            # Extract token from URL and check expiry
            token = url.split("token=")[-1] if "token=" in url else ""
            if token:
                payload = _jwt_mod.decode(token, options={"verify_signature": False})
                exp = payload.get("exp", 0)
                if time.time() < exp - 300:  # 5 min margin
                    print(f"Reusing cached LiveKit URL (expires in {int(exp - time.time())}s)")
                    return url
        except Exception as e:
            print(f"Cache read failed ({e}), generating new URL")

    # Generate fresh URL
    url = cam.generate_join_url()
    try:
        _LIVEKIT_URL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _LIVEKIT_URL_CACHE.write_text(json.dumps({"url": url, "created": time.time()}))
    except OSError:
        pass
    return url


# ── Infra helpers ─────────────────────────────────────────────

def curl(endpoint: str, method: str = "GET", data: dict | None = None,
         timeout: int = 10) -> dict | None:
    url = f"{BRIDGE}{endpoint}"
    cmd = ["curl", "-sf", "-X", method, url]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def ssh_cmd(cmd: str, timeout: int = 15) -> str | None:
    try:
        result = subprocess.run(["ssh", "jetson", cmd],
                                capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else None
    except subprocess.TimeoutExpired:
        return None


def capture_robot_frame(label: str) -> str | None:
    """Capture a frame from the robot's own camera via the detector's capture mechanism.

    Returns local path to the saved JPEG, or None on failure.
    """
    cap_dir = "/tmp/golden_robot_cap"
    # Ensure detector is enabled (needed for camera access), clear old frames
    # Keep planner zeroed so detection doesn't fight servo positions
    ssh_cmd(f"docker exec muscle rm -rf {cap_dir} && docker exec muscle mkdir -p {cap_dir}")
    _ros2_param_batch([
        ("/planner", "max_speed", "0.0"),
        ("/planner", "max_turn", "0.0"),
        ("/planner", "max_strafe", "0.0"),
        ("/planner", "kp_heading", "0.0"),
        ("/person_detector", "enable", "true"),
        ("/person_detector", "capture_dir", cap_dir),
    ])
    ros2_param_set("/person_detector", "capture_interval_s", "0.5")
    time.sleep(1.5)
    ros2_param_set("/person_detector", "capture_interval_s", "0.0")

    # Find the latest frame
    frame_name = ssh_cmd(f"docker exec muscle ls -1 {cap_dir}/ | tail -1")
    if not frame_name:
        print(f"  [!] Robot camera capture failed for '{label}'")
        return None

    remote_path = f"{cap_dir}/{frame_name}"
    local_path = str(CAPTURES_DIR / f"robot_{label}.jpg")
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Copy from container to Jetson host, then to NUC
    ssh_cmd(f"docker cp muscle:{remote_path} /tmp/robot_frame.jpg")
    result = subprocess.run(
        ["scp", "jetson:/tmp/robot_frame.jpg", local_path],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        print(f"  [!] SCP failed for robot frame '{label}'")
        return None

    print(f"  [Robot cam] Captured: {label}")
    return local_path


def ros2_param_set(node: str, param: str, value: str) -> bool:
    cmd = (
        f'docker exec muscle bash -c "'
        f'export PATH=/opt/ros/humble/install/bin:$PATH && '
        f'source /opt/ros/humble/install/setup.bash && '
        f'ros2 param set {node} {param} {value}"'
    )
    result = ssh_cmd(cmd)
    return result is not None and "successful" in (result or "").lower()


def ros2_topic_echo(topic: str, timeout: int = 5) -> str | None:
    cmd = (
        f'docker exec muscle timeout {timeout} bash -c "'
        f'export PATH=/opt/ros/humble/install/bin:$PATH && '
        f'source /opt/ros/humble/install/setup.bash && '
        f'ros2 topic echo {topic} --once"'
    )
    return ssh_cmd(cmd, timeout=timeout + 5)


def ros2_node_list() -> list[str]:
    cmd = (
        'docker exec muscle bash -c "'
        'export PATH=/opt/ros/humble/install/bin:$PATH && '
        'source /opt/ros/humble/install/setup.bash && '
        'ros2 node list"'
    )
    result = ssh_cmd(cmd)
    return result.splitlines() if result else []


_PLANNER_DEFAULTS = {
    "max_speed": "0.08", "max_turn": "0.02", "max_strafe": "0.03",
    "kp_heading": "0.002",
}


def _ros2_param_batch(params: list[tuple[str, str, str]]):
    """Set multiple ros2 params in a single SSH call."""
    cmds = " && ".join(
        f"ros2 param set {node} {param} {value}" for node, param, value in params
    )
    ssh_cmd(
        f'docker exec muscle bash -c "export PATH=/opt/ros/humble/install/bin:$PATH && '
        f'source /opt/ros/humble/install/setup.bash && {cmds}"'
    )


def set_robot_mode(mode: str, target_class: int = 0):
    print(f"  [MODE] Setting robot to '{mode}' (target_class={target_class})")
    if mode == "manual":
        _ros2_param_batch([
            ("/robot_interface", "mode", "manual"),
            ("/person_detector", "enable", "false"),
            ("/planner", "max_speed", "0.0"),
            ("/planner", "max_turn", "0.0"),
            ("/planner", "max_strafe", "0.0"),
            ("/planner", "kp_heading", "0.0"),
        ])
        curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    elif mode == "track_only":
        _ros2_param_batch([
            ("/robot_interface", "mode", "track_only"),
            ("/person_detector", "target_class", str(target_class)),
            ("/person_detector", "enable", "true"),
            *[("/planner", p, v) for p, v in _PLANNER_DEFAULTS.items()],
            ("/planner", "max_speed", "0.0"),
            ("/planner", "max_strafe", "0.0"),
        ])
    elif mode == "full":
        _ros2_param_batch([
            ("/robot_interface", "mode", "full"),
            ("/person_detector", "target_class", str(target_class)),
            ("/person_detector", "enable", "true"),
            *[("/planner", p, v) for p, v in _PLANNER_DEFAULTS.items()],
        ])
    time.sleep(1)


def _capture_in_subprocess(captures_dir: str, label: str) -> str:
    """Capture a frame in a subprocess to avoid nested asyncio.run()."""
    result = subprocess.run(
        ["python3", "-c", f"""
import sys; sys.path.insert(0, '.')
from monitoring.camera_capture import CameraCapture
from pathlib import Path
cam = CameraCapture(captures_dir=Path("{captures_dir}"))
path = cam.capture_and_save("{label}")
print(path)
"""],
        capture_output=True, text=True, timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    if result.returncode != 0:
        # Extract the last meaningful error line, not the full traceback
        stderr = result.stderr.strip()
        last_line = stderr.splitlines()[-1] if stderr else "unknown error"
        if "No video frame received" in stderr:
            raise RuntimeError("Not in LiveKit room — no camera feed available")
        raise RuntimeError(f"Frame capture failed: {last_line}")
    return result.stdout.strip()


def vision_check(cam: CameraCapture, action: str, action_fn, settle: float = 2.0) -> TestResult:
    before = _capture_in_subprocess(str(cam.captures_dir), "before")
    action_fn()
    time.sleep(settle)
    after = _capture_in_subprocess(str(cam.captures_dir), "after")
    result = evaluate_action(Path(before), Path(after), action)
    return TestResult(
        phase="", name="",
        passed=result.get("success", False),
        method="vision",
        details=result.get("explanation", ""),
        confidence=result.get("confidence", 0.0),
        before_img=before,
        after_img=after,
    )


def get_audio_status() -> dict:
    try:
        result = subprocess.run(
            ["ssh", "jetson", "docker exec muscle curl -s http://localhost:8090/status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return {}


def get_recent_stt_logs(n: int = 6) -> str:
    try:
        result = subprocess.run(
            f"ssh jetson 'docker logs muscle 2>&1 | grep -E \"STT result|Command:\" | tail -{n}'",
            capture_output=True, text=True, timeout=10, shell=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ── LiveKit Audio ─────────────────────────────────────────────

def load_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, 'rb') as wf:
        frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16)
        sr = wf.getframerate()
    return samples, sr


class LiveKitAudio:
    """Manages LiveKit audio: sending commands and receiving robot responses."""

    def __init__(self):
        self.room: rtc.Room | None = None
        self.source: rtc.AudioSource | None = None
        self.sr = 24000
        self.received_audio: list[np.ndarray] = []
        self._listening = False

    async def connect(self):
        url, api_key, api_secret = _load_credentials()
        token = _generate_token(api_key, api_secret, "robot-cam", "nuc-voice-tester")
        self.room = rtc.Room()

        @self.room.on("track_subscribed")
        def on_track(track, publication, participant):
            if isinstance(track, rtc.RemoteAudioTrack):
                asyncio.ensure_future(self._listen_audio(track))

        await self.room.connect(url, token)

        self.source = rtc.AudioSource(self.sr, 1)
        track = rtc.LocalAudioTrack.create_audio_track("voice-cmd", self.source)
        await self.room.local_participant.publish_track(track)

    async def _listen_audio(self, track: rtc.RemoteAudioTrack):
        self._listening = True
        stream = rtc.AudioStream(track)
        async for event in stream:
            audio_data = np.frombuffer(event.frame.data, dtype=np.int16)
            self.received_audio.append(audio_data)

    def clear_received(self):
        self.received_audio.clear()

    def get_received_energy(self) -> float:
        if not self.received_audio:
            return 0.0
        all_audio = np.concatenate(self.received_audio)
        return float(np.sqrt(np.mean(all_audio.astype(np.float64) ** 2)))

    def has_audio_response(self, threshold: float = 100.0) -> bool:
        return self.get_received_energy() > threshold

    async def send_wav(self, wav_path: str):
        samples, sr = load_wav(wav_path)
        frame_size = int(sr * 0.02)
        for i in range(0, len(samples), frame_size):
            chunk = samples[i:i + frame_size]
            if len(chunk) < frame_size:
                chunk = np.pad(chunk, (0, frame_size - len(chunk)))
            frame = rtc.AudioFrame(
                data=chunk.tobytes(), sample_rate=sr,
                num_channels=1, samples_per_channel=frame_size,
            )
            await self.source.capture_frame(frame)
            await asyncio.sleep(0.02)

    async def send_silence(self, duration: float = 1.0):
        frame_size = int(self.sr * 0.02)
        silence = np.zeros(int(self.sr * duration), dtype=np.int16)
        for i in range(0, len(silence), frame_size):
            chunk = silence[i:i + frame_size]
            if len(chunk) < frame_size:
                chunk = np.pad(chunk, (0, frame_size - len(chunk)))
            frame = rtc.AudioFrame(
                data=chunk.tobytes(), sample_rate=self.sr,
                num_channels=1, samples_per_channel=frame_size,
            )
            await self.source.capture_frame(frame)
            await asyncio.sleep(0.02)

    async def disconnect(self):
        if self.room:
            await self.room.disconnect()


# ── Test Phases ───────────────────────────────────────────────

def phase_preflight(report: TestReport):
    print("\n── Preflight ──")

    result = ssh_cmd("echo ok")
    report.add(TestResult("pre", "SSH connectivity", result == "ok", "api", result or "failed"))

    result = ssh_cmd("docker ps --format '{{.Names}}'")
    running = "muscle" in (result or "")
    report.add(TestResult("pre", "Muscle container", running, "api", result or "not running"))

    health = curl("/health")
    if health:
        v = health.get("battery_v", 0)
        report.add(TestResult("pre", "Battery", v > 10.0, "api", f"{v:.1f}V"))
    else:
        report.add(TestResult("pre", "Battery", False, "api", "health endpoint failed"))


def phase_led(report: TestReport, cam: CameraCapture):
    print("\n── LED ──")
    announce("🤖 Testing LED blue...", "Testing LED blue")
    r = vision_check(
        cam,
        "Robot LEDs should change to blue. Look for blue light on the robot.",
        lambda: curl("/led", "POST", {"r": 0, "g": 0, "b": 255}),
        settle=1.0,
    )
    r.phase, r.name = "led", "LED blue"
    report.add(r)
    icon = "✅" if r.passed else "❌"
    announce(f"{icon} LED blue", f"LED blue {'pass' if r.passed else 'fail'}")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})


def phase_servo(report: TestReport, cam: CameraCapture):
    print("\n── Servo ──")
    announce("🤖 Testing servo pan left...", "Testing servo pan left")

    # Center first, capture before
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    time.sleep(1)
    before = capture_robot_frame("servo_center")
    # Pan left
    curl("/servo", "POST", {"channel": 3, "angle": 60})
    time.sleep(1.5)
    after = capture_robot_frame("servo_panned_left")

    if before and after:
        result = evaluate_action(
            before, after,
            "These two images are from the ROBOT's own camera. The camera servo panned LEFT. "
            "The scene should shift — objects that were centered should now appear on the right "
            "side of the frame. Any significant viewpoint shift means the servo moved."
        )
        passed = result.get("success", False)
        report.add(TestResult("servo", "Servo pan left", passed, "vision",
                              result.get("explanation", ""),
                              confidence=result.get("confidence", 0),
                              before_img=before, after_img=after))
    else:
        report.add(TestResult("servo", "Servo pan left", False, "vision", "frame capture failed"))

    # Restore center
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    icon = "✅" if (before and after and passed) else "❌"
    announce(f"{icon} Servo test done", f"Servo test {'pass' if (before and after and passed) else 'fail'}")


def phase_motors(report: TestReport, cam: CameraCapture):
    print("\n── Motor ──")
    announce("🤖 Testing motor forward...", "Testing motor forward")

    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    time.sleep(1)
    before = _capture_in_subprocess(str(cam.captures_dir), "motor_before")
    curl("/move", "POST", {"vx": 0.15, "vy": 0, "vz": 0, "duration": 4.0})
    time.sleep(5)
    after = _capture_in_subprocess(str(cam.captures_dir), "motor_after")
    curl("/stop", "POST")

    result = evaluate_action(Path(before), Path(after),
        "The robot should have moved FORWARD. Look for the robot appearing "
        "larger/closer or the background shifting. Even small movement counts.")
    r = TestResult("motor", "Motor forward 4s",
                   result.get("success", False), "vision",
                   result.get("explanation", ""),
                   confidence=result.get("confidence", 0.0),
                   before_img=before, after_img=after)
    report.add(r)
    icon = "✅" if r.passed else "❌"
    announce(f"{icon} Motor test done", f"Motor test {'pass' if r.passed else 'fail'}")


def phase_detection(report: TestReport, target_class: int):
    print(f"\n── Phase 5: Detection (class={target_class}) ──")
    class_name = "tv/monitor" if target_class == 62 else "person"
    announce(f"🤖 Testing {class_name} detection...", f"Testing {class_name} detection")

    # Center servos first so camera points at the target
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    time.sleep(1)

    _ros2_param_batch([
        ("/person_detector", "target_class", str(target_class)),
        ("/person_detector", "enable", "true"),
    ])

    # Wait longer for model switch (general detection model may need loading)
    tlog(f"Waiting for detector to switch to class {target_class}...")
    time.sleep(8)

    # Check raw_person_activity first for diagnostics
    activity = ros2_topic_echo("/raw_person_activity", timeout=5)
    tlog(f"raw_person_activity: {(activity or 'none')[:200]}")

    bbox = ros2_topic_echo("/pedestrian_bboxes", timeout=5)
    tlog(f"pedestrian_bboxes: {(bbox or 'none')[:200]}")
    has_detection = bbox is not None and "x_min" in (bbox or "")

    if not has_detection:
        # Retry — sometimes first frame after model switch is empty
        tlog("No detection on first try, retrying after 5s...")
        time.sleep(5)
        bbox = ros2_topic_echo("/pedestrian_bboxes", timeout=5)
        tlog(f"pedestrian_bboxes retry: {(bbox or 'none')[:200]}")
        has_detection = bbox is not None and "x_min" in (bbox or "")

    detail = "bbox published" if has_detection else f"no detection (activity={activity[:80] if activity else 'none'})"
    report.add(TestResult("5-detect", f"Detect {class_name}", has_detection, "topic", detail))


def phase_tracking(report: TestReport, cam: CameraCapture, target_class: int):
    print("\n── Phase 6: Servo Tracking (30s) ──")
    class_name = "tv/monitor" if target_class == 62 else "person"
    announce(f"🤖 Starting {class_name} tracking test...", f"Starting {class_name} tracking test")

    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    set_robot_mode("track_only", target_class)

    curl("/servo", "POST", {"channel": 3, "angle": 60})
    time.sleep(1)

    r = vision_check(
        cam,
        f"Camera servo should track toward the {class_name} over 30 seconds — "
        f"the servo should rotate to re-center the target in frame.",
        lambda: time.sleep(30),
        settle=1.0,
    )
    r.phase, r.name = "6-track", f"Servo tracks {class_name} (30s)"
    report.add(r)

    verdict = "pass" if r.passed else "fail"
    icon = "✅" if r.passed else "❌"
    announce(f"{icon} Tracking test done", f"Tracking test {verdict}")

    set_robot_mode("manual")


def phase_following(report: TestReport, cam: CameraCapture, target_class: int):
    tlog("Phase 7: Following (20s)")
    class_name = "tv/monitor" if target_class == 62 else "person"

    announce(f"🤖 Starting {class_name} following for 20 seconds...",
             f"Starting {class_name} following for twenty seconds")

    set_robot_mode("full", target_class)

    tlog("Capturing BEFORE frame...")
    before = _capture_in_subprocess(str(cam.captures_dir), "follow_before")
    tlog("Following for 20s...")
    time.sleep(20)
    tlog("20s done. Capturing AFTER frame...")
    after = _capture_in_subprocess(str(cam.captures_dir), "follow_after")

    # Stop FIRST, evaluate AFTER
    tlog("Stopping robot...")
    set_robot_mode("manual")
    curl("/stop", "POST")
    tlog("Robot stopped. Running vision eval...")

    result = evaluate_action(
        before, after,
        f"Compare these two images from a phone camera watching the robot. "
        f"Did the robot get closer to the {class_name}? Look for: the robot appearing "
        f"nearer to the {class_name}, the gap/distance between them shrinking, or the "
        f"robot having moved across the floor toward the {class_name}. "
        f"The robot is a small orange/black unit on the floor.",
    )
    passed = result.get("success", False)
    conf = result.get("confidence", 0.0)
    expl = result.get("explanation", "")
    tlog(f"VISION EVAL: {'PASS' if passed else 'FAIL'} (conf={conf:.0%}) — {expl[:100]}")
    report.add(TestResult(
        "7-follow", f"Follow {class_name} (20s)",
        passed, "vision", expl,
        confidence=conf,
        before_img=before, after_img=after,
    ))


def phase_sensors(report: TestReport):
    print("\n── Sensors ──")

    scan = ros2_topic_echo("/scan", timeout=5)
    has_scan = scan is not None and "ranges" in (scan or "")
    report.add(TestResult("sensor", "LiDAR /scan", has_scan, "topic",
                          "publishing" if has_scan else "no data"))

    imu = ros2_topic_echo("/imu/data", timeout=5)
    has_imu = imu is not None and "orientation" in (imu or "")
    report.add(TestResult("sensor", "IMU /imu/data", has_imu, "topic",
                          "publishing" if has_imu else "no data"))


async def phase_voice(report: TestReport, cam: CameraCapture, lk_audio: LiveKitAudio):
    """Voice command: 'hey jarvis light blue' via LiveKit with vision verification."""
    print("\n── Voice ──")
    announce("🤖 Testing voice command: light blue", "Testing voice command light blue")

    set_robot_mode("manual")
    ssh_cmd("docker exec muscle curl -s -X POST http://localhost:8081/manual/on "
            "-H 'Content-Type: application/json' -d '{\"duration\":120}'")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    time.sleep(1)

    wav_path = str(VOICE_DIR / "hey_jarvis_light_blue.wav")
    if not Path(wav_path).exists():
        report.add(TestResult("voice", "Voice: light blue", False, "voice",
                              f"WAV not found: {wav_path}"))
        return

    await lk_audio.send_silence(2.0)
    lk_audio.clear_received()

    before = _capture_in_subprocess(str(cam.captures_dir), "voice_before")
    await lk_audio.send_wav(wav_path)

    # Wait for robot to process wake word + STT + command
    has_audio = False
    for elapsed in range(1, 13):
        await asyncio.sleep(1.0)
        if lk_audio.has_audio_response(threshold=50.0):
            has_audio = True
            await asyncio.sleep(2.0)
            break

    stt_log = get_recent_stt_logs(6)
    stt_matched = any(kw.lower() in stt_log.lower() for kw in ["light", "blue"])
    cmd_handled = "handled=True" in stt_log

    after = _capture_in_subprocess(str(cam.captures_dir), "voice_after")
    vis_result = evaluate_action(before, after,
        "Robot LEDs should now be blue. Look for blue light on the robot body.")
    vision_passed = vis_result.get("success", False)

    passed = stt_matched and cmd_handled
    parts = [f"STT:{'ok' if stt_matched else 'miss'}",
             f"cmd:{'ok' if cmd_handled else 'miss'}",
             f"audio:{'ok' if has_audio else 'none'}",
             f"vision:{'ok' if vision_passed else 'fail'}"]
    if not passed:
        parts.append(f"log: {stt_log[-150:]}")

    report.add(TestResult("voice", "Voice: light blue", passed, "voice",
                          " | ".join(parts), before_img=str(before), after_img=str(after)))

    icon = "✅" if passed else "❌"
    announce(f"{icon} Voice test done", f"Voice test {'pass' if passed else 'fail'}")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})

    ssh_cmd("docker exec muscle curl -s -X POST http://localhost:8081/manual/off "
            "-H 'Content-Type: application/json' -d '{}'")


def _container_curl(endpoint: str, method: str = "GET",
                    data: dict | None = None, port: int = 8081,
                    timeout: int = 10) -> dict | None:
    """Curl an endpoint inside the muscle container on the Jetson."""
    url = f"http://localhost:{port}{endpoint}"
    cmd_parts = [f"curl -sf -X {method} {url}"]
    if data:
        cmd_parts.append(f"-H 'Content-Type: application/json' -d '{json.dumps(data)}'")
    raw = ssh_cmd(f"docker exec muscle {' '.join(cmd_parts)}", timeout=timeout)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    return None


def phase_sprint11_multiobject(report: TestReport):
    """Phase 11: Sprint 11 — Multi-object detection (3+ classes)."""
    print("\n── Phase 11: Multi-Object Detection (Sprint 11) ──")

    # Ensure detector is running
    _ros2_param_batch([
        ("/person_detector", "enable", "true"),
        ("/person_detector", "target_class", "0"),
    ])
    time.sleep(5)

    scene = curl("/scene")
    if not scene:
        report.add(TestResult("11-multiobj", "Scene endpoint", False, "api",
                              "GET /scene failed"))
        return

    objects = scene.get("objects", [])
    class_names = [o.get("class_name", "?") for o in objects]
    n_classes = len(class_names)
    stale = scene.get("stale", True)

    report.add(TestResult(
        "11-multiobj", "Scene endpoint reachable", True, "api",
        f"{n_classes} classes, stale={stale}",
    ))
    report.add(TestResult(
        "11-multiobj", "Multi-object detection (3+ classes)",
        n_classes >= 3, "api",
        f"Detected {n_classes} classes: {', '.join(class_names[:10])}",
    ))


def phase_sprint11_scene(report: TestReport):
    """Phase 12: Sprint 11 — Scene description."""
    print("\n── Phase 12: Scene Description (Sprint 11) ──")

    scene = curl("/scene")
    if not scene:
        report.add(TestResult("12-scene", "Scene endpoint", False, "api",
                              "GET /scene failed"))
        return

    objects = scene.get("objects", [])
    stale = scene.get("stale", True)

    report.add(TestResult(
        "12-scene", "Scene not stale", not stale, "api",
        f"stale={stale}, {len(objects)} objects",
    ))

    has_content = len(objects) > 0
    class_names = [o.get("class_name", "?") for o in objects]
    report.add(TestResult(
        "12-scene", "Scene has objects", has_content, "api",
        f"Objects: {', '.join(class_names)}" if has_content else "no objects detected",
    ))


def phase_sprint11_photo(report: TestReport):
    """Phase 13: Sprint 11 — Photo on demand."""
    print("\n── Phase 13: Photo on Demand (Sprint 11) ──")
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    photo_path = CAPTURES_DIR / "photo_capture.jpg"

    try:
        result = subprocess.run(
            ["curl", "-sf", f"{BRIDGE}/capture",
             "-o", str(photo_path)],
            capture_output=True, text=True, timeout=10,
        )
        captured = (result.returncode == 0 and photo_path.exists()
                    and photo_path.stat().st_size > 1000)
        size = photo_path.stat().st_size if photo_path.exists() else 0
        report.add(TestResult(
            "13-photo", "Photo capture (/capture)", captured, "api",
            f"size={size}B" if captured else f"failed (rc={result.returncode}, size={size}B)",
        ))
    except Exception as e:
        report.add(TestResult("13-photo", "Photo capture", False, "api",
                              f"error: {e}"))


def phase_sprint11_face_recognition(report: TestReport):
    """Phase 14: Sprint 11 — Face recognition (identify Ophir)."""
    print("\n── Phase 14: Face Recognition (Sprint 11) ──")

    # Check face enrollment service health
    health = _container_curl("/health", port=8085)
    models_ready = health and health.get("models_ready", False)
    report.add(TestResult(
        "14-face", "Face enrollment service health", models_ready, "api",
        str(health) if health else "service not responding on :8085",
    ))
    if not models_ready:
        return

    # Check enrolled faces
    faces = _container_curl("/faces", port=8085)
    face_names = list(faces.keys()) if faces and isinstance(faces, dict) else []
    has_ophir = any(n.lower() == "ophir" for n in face_names)
    report.add(TestResult(
        "14-face", "Ophir face enrolled", has_ophir, "api",
        f"Enrolled: {face_names}" if face_names else "no faces enrolled",
    ))

    # Try recognition (needs Ophir in camera view)
    recog = _container_curl("/recognize", port=8085)
    if recog and "identity" in recog:
        identity = recog.get("identity", "unknown")
        confidence = recog.get("confidence", 0)
        is_ophir = identity.lower() == "ophir"
        report.add(TestResult(
            "14-face", "Recognize Ophir", is_ophir, "api",
            f"identity={identity}, confidence={confidence:.3f}",
        ))
    else:
        report.add(TestResult(
            "14-face", "Face recognition", False, "api",
            f"recognize failed: {recog}",
        ))


def phase_sprint11_face_enrollment(report: TestReport):
    """Phase 15: Sprint 11 — Face enrollment (enroll/recognize/delete cycle)."""
    print("\n── Phase 15: Face Enrollment Cycle (Sprint 11) ──")

    health = _container_curl("/health", port=8085)
    if not (health and health.get("models_ready", False)):
        report.add(TestResult("15-enroll", "Face enrollment service", False, "api",
                              "service not ready"))
        return

    # Enroll a test face
    enroll_result = _container_curl("/enroll", method="POST",
                                   data={"name": "golden_test"}, port=8085)
    enrolled = enroll_result and enroll_result.get("status") == "enrolled"
    report.add(TestResult(
        "15-enroll", "Enroll test face", enrolled, "api",
        str(enroll_result) if enroll_result else "enroll failed",
    ))

    if enrolled:
        # Verify enrolled face appears in list
        faces = _container_curl("/faces", port=8085)
        in_list = faces and "golden_test" in faces
        report.add(TestResult(
            "15-enroll", "Enrolled face in list", in_list, "api",
            f"faces: {list(faces.keys()) if faces else 'none'}",
        ))

        # Recognize should return the test face (or Ophir if closer)
        recog = _container_curl("/recognize", port=8085)
        recognized = recog and recog.get("identity") != "unknown"
        report.add(TestResult(
            "15-enroll", "Recognize after enrollment", recognized, "api",
            f"identity={recog.get('identity')}, conf={recog.get('confidence', 0):.3f}"
            if recog else "recognize failed",
        ))

        # Cleanup — delete test face
        _container_curl("/faces/golden_test", method="DELETE", port=8085)

        # Verify deletion
        faces_after = _container_curl("/faces", port=8085)
        deleted = faces_after is not None and "golden_test" not in (faces_after or {})
        report.add(TestResult(
            "15-enroll", "Delete test face", deleted, "api",
            f"faces after delete: {list(faces_after.keys()) if faces_after else 'none'}",
        ))
    else:
        report.add(TestResult("15-enroll", "Enrollment cycle", False, "api",
                              "skipped — enroll failed"))


def phase_sprint11_named_following(report: TestReport, cam: CameraCapture):
    """Phase 16: Sprint 11 — Named following ('follow Ophir')."""
    print("\n── Phase 16: Named Following (Sprint 11) ──")
    announce("🤖 Testing named following — 'follow Ophir'...",
             "Testing named following. Follow Ophir.")

    # Enable detector for following
    _ros2_param_batch([
        ("/person_detector", "enable", "true"),
        ("/person_detector", "target_class", "0"),
        *[("/planner", p, v) for p, v in _PLANNER_DEFAULTS.items()],
    ])
    time.sleep(2)

    # Start named following
    follow_start = _container_curl("/follow/start", method="POST",
                                   data={"target": "ophir"}, port=8084)
    started = follow_start and follow_start.get("status") == "started"
    target = follow_start.get("target") if follow_start else None
    report.add(TestResult(
        "16-follow", "Named follow start (target=ophir)", started, "api",
        f"target={target}" if started else f"failed: {follow_start}",
    ))

    if started:
        # Check status
        time.sleep(3)
        status = _container_curl("/follow/status", port=8084)
        state = status.get("state") if status else "unknown"
        has_target = status.get("target_name") if status else None
        report.add(TestResult(
            "16-follow", "Following state active",
            state in ("following", "searching"), "api",
            f"state={state}, target={has_target}",
        ))

        # Vision verification — capture before/after 15s of following
        before = _capture_in_subprocess(str(cam.captures_dir), "named_follow_before")
        tlog("Named following for 15s...")
        time.sleep(15)
        after = _capture_in_subprocess(str(cam.captures_dir), "named_follow_after")

        # Stop following
        _container_curl("/follow/stop", method="POST", port=8084)
        curl("/stop", "POST")
        set_robot_mode("manual")

        result = evaluate_action(
            before, after,
            "Compare these two images from a phone camera watching the robot. "
            "Did the robot move to follow a person (Ophir)? Look for: the robot "
            "appearing in a different position, having turned or driven toward a "
            "person in the scene. The robot is a small orange/black wheeled unit.",
        )
        passed = result.get("success", False)
        conf = result.get("confidence", 0.0)
        report.add(TestResult(
            "16-follow", "Named follow Ophir (vision)", passed, "vision",
            result.get("explanation", ""),
            confidence=conf, before_img=before, after_img=after,
        ))
        verdict = "pass" if passed else "fail"
        icon = "✅" if passed else "❌"
        announce(f"{icon} Named following test done", f"Named following test {verdict}")
    else:
        _container_curl("/follow/stop", method="POST", port=8084)
        set_robot_mode("manual")


def _ros2_node_running(node_name: str) -> bool:
    """Check if a ROS2 node is in the active node list."""
    nodes = ros2_node_list()
    return any(node_name in n for n in nodes)


# ── Sprint 12 Test Phases ────────────────────────────────────


def phase_sprint12_battery(report: TestReport):
    """Phase 17: Sprint 12 — Battery status endpoint."""
    print("\n── Phase 17: Battery Status (Sprint 12) ──")

    battery = curl("/battery")
    if not battery:
        report.add(TestResult("17-battery", "Battery endpoint", False, "api",
                              "GET /battery failed"))
        return

    has_voltage = "voltage" in battery
    has_pct = "percentage" in battery
    has_level = "level" in battery

    report.add(TestResult(
        "17-battery", "Battery endpoint reachable", True, "api",
        f"voltage={battery.get('voltage')}, pct={battery.get('percentage')}%, "
        f"level={battery.get('level')}",
    ))
    report.add(TestResult(
        "17-battery", "Battery fields complete",
        has_voltage and has_pct and has_level, "api",
        f"voltage={'ok' if has_voltage else 'MISSING'}, "
        f"pct={'ok' if has_pct else 'MISSING'}, "
        f"level={'ok' if has_level else 'MISSING'}",
    ))

    # Sanity check voltage range (3S LiPo: 9.6V-12.6V)
    v = battery.get("voltage", 0)
    pct = battery.get("percentage", -1)
    v_ok = 9.0 < v < 13.0
    pct_ok = 0 <= pct <= 100
    report.add(TestResult(
        "17-battery", "Battery values sane",
        v_ok and pct_ok, "api",
        f"voltage={v:.1f}V (9-13V range), pct={pct}% (0-100 range)",
    ))


def phase_sprint12_status(report: TestReport):
    """Phase 18: Sprint 12 — Robot status dashboard."""
    print("\n── Phase 18: Robot Status Dashboard (Sprint 12) ──")

    status = curl("/status", timeout=15)
    if not status:
        report.add(TestResult("18-status", "Status endpoint", False, "api",
                              "GET /status failed"))
        return

    report.add(TestResult(
        "18-status", "Status endpoint reachable", True, "api",
        f"keys: {list(status.keys())}",
    ))

    # Check required sections
    required = ["battery", "nodes", "detection", "planner"]
    present = [k for k in required if k in status]
    missing = [k for k in required if k not in status]
    report.add(TestResult(
        "18-status", "Status has required sections",
        len(missing) == 0, "api",
        f"present={present}, missing={missing}" if missing else f"all present: {present}",
    ))

    # Check uptime field
    has_uptime = "uptime" in status
    report.add(TestResult(
        "18-status", "Status has uptime",
        has_uptime, "api",
        f"uptime={status.get('uptime', 'MISSING')}",
    ))


def phase_sprint12_camera(report: TestReport):
    """Phase 19: Sprint 12 — Live camera via LiveKit."""
    print("\n── Phase 19: Live Camera (Sprint 12) ──")

    camera = curl("/camera", timeout=10)
    if not camera:
        report.add(TestResult("19-camera", "Camera endpoint", False, "api",
                              "GET /camera failed"))
        return

    report.add(TestResult(
        "19-camera", "Camera endpoint reachable", True, "api",
        f"keys={list(camera.keys()) if isinstance(camera, dict) else 'non-dict'}",
    ))


def phase_sprint12_activity(report: TestReport):
    """Phase 20: Sprint 12 — Activity log."""
    print("\n── Phase 20: Activity Log (Sprint 12) ──")

    activity = curl("/activity/summary", timeout=10)
    if not activity:
        report.add(TestResult("20-activity", "Activity log endpoint", False, "api",
                              "GET /activity/summary failed"))
        return

    has_summary = "summary" in activity
    has_count = "event_count" in activity

    report.add(TestResult(
        "20-activity", "Activity log reachable", True, "api",
        f"event_count={activity.get('event_count', '?')}",
    ))
    report.add(TestResult(
        "20-activity", "Activity log has fields",
        has_summary and has_count, "api",
        f"summary={'ok' if has_summary else 'MISSING'}, "
        f"count={'ok' if has_count else 'MISSING'}",
    ))


def phase_sprint12_say(report: TestReport):
    """Phase 21: Sprint 12 — TTS via /say endpoint."""
    print("\n── Phase 21: TTS Say (Sprint 12) ──")

    result = curl("/say", method="POST",
                  data={"text": "Sprint twelve golden test"}, timeout=15)
    passed = result is not None
    report.add(TestResult(
        "21-say", "TTS /say endpoint", passed, "api",
        str(result)[:200] if result else "POST /say failed",
    ))


def phase_sprint12_voice_chat(report: TestReport):
    """Phase 22: Sprint 12 — Voice chat endpoint."""
    print("\n── Phase 22: Voice Chat (Sprint 12) ──")

    result = curl("/voice/chat", method="POST",
                  data={"text": "What do you see?"}, timeout=20)
    if not result:
        report.add(TestResult("22-chat", "Voice chat endpoint", False, "api",
                              "POST /voice/chat failed"))
        return

    has_response = "response" in result
    report.add(TestResult(
        "22-chat", "Voice chat responds", has_response, "api",
        f"response={result.get('response', '')[:150]}" if has_response
        else f"no 'response' field: {str(result)[:150]}",
    ))


def phase_sprint12_visual_odom(report: TestReport):
    """Phase 23: Sprint 12 — Visual odometry topic."""
    print("\n── Phase 23: Visual Odometry (Sprint 12) ──")

    vodom = ros2_topic_echo("/visual_odom", timeout=8)
    has_odom = vodom is not None and "position" in (vodom or "")

    report.add(TestResult(
        "23-vodom", "Visual odometry /visual_odom",
        has_odom, "topic",
        "publishing" if has_odom else f"no data (got: {(vodom or 'none')[:100]})",
    ))


def phase_sprint12_navigation(report: TestReport):
    """Phase 24-25: Sprint 12 — Navigation system health + waypoints."""
    print("\n── Phase 24: Navigation System (Sprint 12) ──")

    # Check nav manager health
    nav_status = _container_curl("/status", port=8092)
    nav_ok = nav_status is not None
    report.add(TestResult(
        "24-nav", "Navigation manager reachable (:8092)",
        nav_ok, "api",
        str(nav_status)[:200] if nav_status else "nav manager :8092 not responding",
    ))

    # Check waypoint navigator
    print("\n── Phase 25: Waypoints (Sprint 12) ──")
    waypoints = _container_curl("/waypoints", port=8093)
    wp_ok = waypoints is not None
    wp_list = list(waypoints.keys()) if isinstance(waypoints, dict) else waypoints
    report.add(TestResult(
        "25-waypoints", "Waypoint navigator reachable (:8093)",
        wp_ok, "api",
        f"waypoints={wp_list}" if wp_ok else "waypoint navigator :8093 not responding",
    ))


def phase_sprint12_patrol(report: TestReport):
    """Phase 26: Sprint 12 — Patrol API start/stop cycle."""
    print("\n── Phase 26: Patrol API (Sprint 12) ──")

    # Check initial status
    status_before = _container_curl("/patrol/status", port=8094)
    if not status_before:
        # Try via bridge proxy
        status_before = curl("/patrol/status")
    if not status_before:
        report.add(TestResult("26-patrol", "Patrol service reachable", False, "api",
                              "patrol :8094 and /patrol/status both failed"))
        return

    initial_state = status_before.get("state", "unknown")
    report.add(TestResult(
        "26-patrol", "Patrol service reachable", True, "api",
        f"initial state={initial_state}",
    ))

    # Start patrol (API-level only — patrol won't actually navigate without nav2 goals)
    start_result = _container_curl("/patrol/start", method="POST", data={}, port=8094)
    if not start_result:
        start_result = curl("/patrol/start", method="POST", data={})

    started = start_result is not None
    report.add(TestResult(
        "26-patrol", "Patrol start API", started, "api",
        str(start_result)[:200] if start_result else "patrol start failed",
    ))

    # Stop patrol immediately (safety — no actual movement)
    time.sleep(1)
    stop_result = _container_curl("/patrol/stop", method="POST", data={}, port=8094)
    if not stop_result:
        stop_result = curl("/patrol/stop", method="POST", data={})
    stopped = stop_result is not None
    report.add(TestResult(
        "26-patrol", "Patrol stop API", stopped, "api",
        str(stop_result)[:200] if stop_result else "patrol stop failed",
    ))


def phase_sprint12_call(report: TestReport):
    """Phase 27: Sprint 12 — Call session status."""
    print("\n── Phase 27: Call Session (Sprint 12) ──")

    call_status = _container_curl("/call/status", port=8095)
    if not call_status:
        call_status = curl("/call/status")
    if not call_status:
        report.add(TestResult("27-call", "Call session service", False, "api",
                              "call :8095 and /call/status both failed"))
        return

    state = call_status.get("status", "unknown")
    report.add(TestResult(
        "27-call", "Call session reachable", True, "api",
        f"status={state}",
    ))
    report.add(TestResult(
        "27-call", "Call session idle",
        state == "idle", "api",
        f"expected 'idle', got '{state}'",
    ))


def phase_sprint12_charging(report: TestReport):
    """Phase 28: Sprint 12 — Charging mode field in health."""
    print("\n── Phase 28: Charging Mode (Sprint 12) ──")

    health = curl("/health")
    if not health:
        report.add(TestResult("28-charging", "Health endpoint", False, "api",
                              "GET /health failed"))
        return

    has_charging = "charging" in health
    charging = health.get("charging", "MISSING")
    report.add(TestResult(
        "28-charging", "Health includes charging field",
        has_charging, "api",
        f"charging={charging}",
    ))


def phase_sprint12_welcome_home(report: TestReport):
    """Phase 29: Sprint 12 — Welcome home node running."""
    print("\n── Phase 29: Welcome Home Node (Sprint 12) ──")

    has_node = _ros2_node_running("welcome_home")
    report.add(TestResult(
        "29-welcome", "Welcome home node running",
        has_node, "topic",
        "node found" if has_node else "welcome_home not in node list",
    ))


def _openclaw_webhook(message: str, deliver: bool = True) -> dict | None:
    """Send a message via OpenClaw webhook, triggering the full agent skill pipeline."""
    if not HOOKS_TOKEN_PATH.exists():
        tlog(f"Hooks token not found at {HOOKS_TOKEN_PATH}")
        return None
    token = HOOKS_TOKEN_PATH.read_text().strip()
    payload = json.dumps({
        "message": message, "deliver": deliver,
        "channel": "signal", "to": SIGNAL_NUMBER, "wakeMode": "now",
    })
    cmd = [
        "sg", "docker", "-c",
        f"docker exec openclaw-gateway curl -sf -X POST "
        f"http://127.0.0.1:18789/hooks/agent "
        f"-H 'Content-Type: application/json' "
        f"-H 'Authorization: Bearer {token}' "
        f"-d '{payload}'"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            tlog(f"Webhook failed (rc={result.returncode}): {result.stderr[:200]}")
            return None
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        tlog(f"Webhook error: {e}")
        return None


def phase_signal_command(report: TestReport, cam: CameraCapture):
    """Signal → OpenClaw → robot-control skill → bridge → LED change."""
    print("\n── Signal → Robot ──")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    curl("/manual/on", "POST")
    time.sleep(1)

    announce("🤖 Testing Signal→Robot: 'robot led blue'",
             "Testing Signal to Robot. Sending robot led blue.")
    before = _capture_in_subprocess(str(cam.captures_dir), "signal_led_before")

    webhook_result = _openclaw_webhook("robot led blue")
    if webhook_result is None:
        report.add(TestResult("signal", "Signal→Robot LED", False, "api",
                              "Webhook failed — hooks not enabled or token missing"))
        return

    tlog("Waiting 15s for OpenClaw agent to process command...")
    time.sleep(15)

    after = _capture_in_subprocess(str(cam.captures_dir), "signal_led_after")
    result = evaluate_action(Path(before), Path(after),
        "The robot's LEDs should have changed to BLUE. "
        "Look for blue light/glow on the robot body.")
    r = TestResult("signal", "Signal→Robot LED", result.get("success", False),
                   "vision", result.get("explanation", ""),
                   confidence=result.get("confidence", 0.0),
                   before_img=before, after_img=after)
    report.add(r)
    icon = "✅" if r.passed else "❌"
    announce(f"{icon} Signal→Robot", f"Signal to Robot {'pass' if r.passed else 'fail'}")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})


def phase_safety(report: TestReport):
    print("\n── Safety ──")
    curl("/move", "POST", {"vx": 0.1, "vy": 0, "vz": 0, "duration": 5.0})
    time.sleep(0.5)
    result = curl("/stop", "POST")
    report.add(TestResult("safety", "Emergency stop", result is not None, "api",
                          str(result) if result else "failed"))


# ── Camera setup validation ───────────────────────────────────

def validate_camera_view(cam: CameraCapture) -> bool:
    """Capture a single frame and check if robot + TV are visible."""
    print("Validating camera view...")
    try:
        frame = _capture_in_subprocess(str(cam.captures_dir), "setup_validation")
    except Exception:
        return False, "Can't capture from phone camera. Are you in the LiveKit room with camera ON?"
    if not frame or not Path(frame).is_file():
        return False, "Failed to capture frame"
    frame_abs = str(Path(frame).resolve())
    # Single-image evaluation: tell Claude to Read the image file
    prompt = (
        f"Read this image file: {frame_abs}\n\n"
        "Look at the image. Can you see:\n"
        "1) A small robot (wheeled device) on the floor or desk, and\n"
        "2) A TV or monitor somewhere in view?\n\n"
        "Return ONLY JSON: {\"success\": true/false, \"explanation\": \"...\"}"
    )
    try:
        import tempfile as _tf
        with _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write(prompt)
            prompt_file = tf.name
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        with open(prompt_file) as pf:
            result = subprocess.run(
                ["claude", "-p", "--model", "haiku",
                 "--allowedTools", "Read", "--max-turns", "2"],
                stdin=pf, capture_output=True, text=True, timeout=60, env=env,
            )
        os.unlink(prompt_file)
        import re as _re
        m = _re.search(r'\{[^{}]*"success"\s*:\s*(true|false)[^{}]*\}', result.stdout, _re.IGNORECASE | _re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            return parsed.get("success", False), parsed.get("explanation", "")
        return False, f"Could not parse LLM response: {result.stdout[:200]}"
    except Exception as exc:
        return False, f"Vision check failed: {exc}"


# ── Main ──────────────────────────────────────────────────────

async def async_main(args):
    cam = CameraCapture(room="robot-cam", captures_dir=CAPTURES_DIR)

    phases = args.phases

    # Step 0: Auto-poll LiveKit until user joins with valid camera view
    robot_say("Golden test starting. Please set up the camera and join the LiveKit room.")

    if not args.skip_camera_check:
        POLL_INTERVAL = 30
        MAX_POLL_TIME = 300  # 5 minutes
        poll_start = time.time()
        url_sent = False
        camera_ready = False

        # Try immediately — user may already be in the room
        print("Checking if user is already in LiveKit room...")

        while time.time() - poll_start < MAX_POLL_TIME:
            elapsed = int(time.time() - poll_start)
            print(f"Polling LiveKit... ({elapsed}s elapsed)")

            # Try to capture a frame
            try:
                _capture_in_subprocess(str(cam.captures_dir), "poll-check")
            except RuntimeError as e:
                if "Not in LiveKit room" in str(e):
                    print("No one in LiveKit room yet.")
                    if not url_sent:
                        # First time we can't reach user — send the join URL
                        join_url = _get_or_create_join_url(cam)
                        signal_send(f"🤖 Golden Test starting (phases: {', '.join(phases)})!\n\nJoin LiveKit room (camera + mic ON):\n{join_url}\n\nI'll automatically detect when you're in the room and start when ready.")
                        url_sent = True
                    time.sleep(POLL_INTERVAL)
                    continue
                else:
                    print(f"Frame capture error: {e}")
                    time.sleep(POLL_INTERVAL)
                    continue

            # User is in the room — validate camera view
            print("User detected in LiveKit room! Validating camera view...")
            ok, explanation = validate_camera_view(cam)
            if ok:
                announce("🤖 Camera looks good! I can see the robot and TV. Starting test now...",
                         "Camera confirmed. I can see the robot and TV. Starting test now.")
                print(f"Camera validation PASSED: {explanation}")
                camera_ready = True
                break
            else:
                print(f"Camera validation FAILED: {explanation}")
                signal_send(f"🤖 I can see you're in the room but the camera view isn't right. {explanation}\n\nPlease adjust so I can see the robot AND the TV. I'll check again in 30s.")
                time.sleep(POLL_INTERVAL)

        if not camera_ready:
            signal_send("🤖 Test aborted — timed out after 5 minutes waiting for valid camera view.")
            sys.exit(1)
    else:
        print(f"Skipping camera check. Running phases: {', '.join(phases)}")

    report = TestReport()
    report.start_time = time.time()

    target = args.target_class

    def cleanup():
        """Always stop the robot, even on error/interrupt."""
        print("\nCleaning up — stopping robot...")
        set_robot_mode("manual")
        curl("/stop", "POST")
        curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
        if target != 0:
            ros2_param_set("/person_detector", "target_class", "0")

    # Clean start — manual mode first to prevent planner from moving robot
    tlog("Clean start...")
    curl("/manual/on", "POST")
    curl("/stop", "POST")
    _ros2_param_batch([
        ("/person_detector", "enable", "false"),
        ("/person_detector", "target_class", "0"),
        ("/planner", "max_speed", "0.0"),
    ])
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    tlog("Clean start done")

    try:
        # ── Stationary: Preflight, LED, Servo, Motors ──
        if "stationary" in phases:
            tlog("═══ STATIONARY TESTS START ═══")
            phase_preflight(report)
            phase_led(report, cam)
            phase_servo(report, cam)
            phase_motors(report, cam)
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ STATIONARY TESTS END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Stationary tests done ({p_pass}/{len(report.results)} passed).",
                     f"Stationary tests done. {p_pass} of {len(report.results)} passed.")

        if "tracking" in phases:
            tlog("═══ TRACKING TESTS START ═══")
            phase_detection(report, target)
            phase_following(report, cam, target)
            set_robot_mode("manual")
            curl("/stop", "POST")
            _ros2_param_batch([
                ("/person_detector", "enable", "false"),
                ("/person_detector", "target_class", "0"),
                ("/planner", "max_speed", "0.0"),
            ])
            time.sleep(1)
            phase_sensors(report)
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ TRACKING TESTS END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Tracking tests done ({p_pass}/{len(report.results)} passed).",
                     f"Tracking tests done. {p_pass} of {len(report.results)} passed.")

        if "voice" in phases:
            tlog("═══ VOICE TESTS START ═══")
            lk_audio = LiveKitAudio()
            await lk_audio.connect()
            tlog("LiveKit audio connected")
            await phase_voice(report, cam, lk_audio)
            await lk_audio.disconnect()
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ VOICE TESTS END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Voice tests done ({p_pass}/{len(report.results)} passed).",
                     f"Voice tests done. {p_pass} of {len(report.results)} passed.")

        if "signal" in phases:
            tlog("═══ SIGNAL→ROBOT TEST START ═══")
            set_robot_mode("manual")
            curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
            time.sleep(1)
            phase_signal_command(report, cam)
            curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ SIGNAL→ROBOT TEST END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Signal→Robot test done ({p_pass}/{len(report.results)} passed).",
                     f"Signal to Robot test done. {p_pass} of {len(report.results)} passed.")

        if "sprint11" in phases:
            tlog("═══ SPRINT 11 TESTS START ═══")
            set_robot_mode("manual")
            curl("/servo", "POST", {"channel": 3, "angle": 102})
            curl("/servo", "POST", {"channel": 4, "angle": 68})
            time.sleep(1)
            phase_sprint11_multiobject(report)
            phase_sprint11_scene(report)
            phase_sprint11_photo(report)
            phase_sprint11_face_recognition(report)
            phase_sprint11_face_enrollment(report)
            phase_sprint11_named_following(report, cam)
            set_robot_mode("manual")
            curl("/stop", "POST")
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ SPRINT 11 TESTS END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Sprint 11 tests done ({p_pass}/{len(report.results)} passed).",
                     f"Sprint 11 tests done. {p_pass} of {len(report.results)} passed.")

        if "sprint12" in phases:
            tlog("═══ SPRINT 12 TESTS START ═══")
            set_robot_mode("manual")
            curl("/servo", "POST", {"channel": 3, "angle": 102})
            curl("/servo", "POST", {"channel": 4, "angle": 68})
            time.sleep(1)
            phase_sprint12_battery(report)
            phase_sprint12_status(report)
            phase_sprint12_camera(report)
            phase_sprint12_activity(report)
            phase_sprint12_say(report)
            phase_sprint12_voice_chat(report)
            phase_sprint12_visual_odom(report)
            phase_sprint12_navigation(report)
            phase_sprint12_patrol(report)
            phase_sprint12_call(report)
            phase_sprint12_charging(report)
            phase_sprint12_welcome_home(report)
            set_robot_mode("manual")
            p_pass = sum(1 for r in report.results if r.passed)
            tlog(f"═══ SPRINT 12 TESTS END ({p_pass}/{len(report.results)} passed) ═══")
            announce(f"🤖 Sprint 12 tests done ({p_pass}/{len(report.results)} passed).",
                     f"Sprint 12 tests done. {p_pass} of {len(report.results)} passed.")

        if "safety" in phases:
            tlog("═══ SAFETY TEST START ═══")
            phase_safety(report)
            tlog("═══ SAFETY TEST END ═══")

    finally:
        cleanup()

    report.end_time = time.time()

    # Print and send report
    summary = report.summary()
    print(summary)

    signal_report = report.signal_report()
    signal_send(signal_report)

    # Speak summary
    total = len(report.results)
    passed = sum(1 for r in report.results if r.passed)
    failed = total - passed
    if failed == 0:
        robot_say(f"Golden test complete. All {total} tests passed.")
    else:
        failed_names = [r.name for r in report.results if not r.passed]
        robot_say(f"Golden test complete. {passed} of {total} passed. {failed} failed: {', '.join(failed_names[:5])}")

    # Save report
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CAPTURES_DIR / "report.json"
    with open(report_path, "w") as f:
        json.dump({
            "results": [
                {"phase": r.phase, "name": r.name, "passed": r.passed,
                 "method": r.method, "details": r.details, "confidence": r.confidence,
                 "before_img": r.before_img, "after_img": r.after_img}
                for r in report.results
            ],
            "total": len(report.results),
            "passed": sum(1 for r in report.results if r.passed),
            "duration_s": report.end_time - report.start_time,
        }, f, indent=2)
    print(f"\nReport saved: {report_path}")

    return 0 if all(r.passed for r in report.results) else 1


def main():
    parser = argparse.ArgumentParser(
        description="Golden automated comprehensive robot test",
        epilog="Examples:\n"
               "  python3 golden_test.py                    # full test\n"
               "  python3 golden_test.py --phases stationary # LED, servo, motors only\n"
               "  python3 golden_test.py --phases tracking voice  # tracking + voice\n"
               "  python3 golden_test.py --phases voice      # voice commands only\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target-class", type=int, default=62,
                        help="COCO class for tracking/following (0=person, 62=tv). Default: 62")
    parser.add_argument("--phases", nargs="+",
                        choices=["stationary", "tracking", "voice", "signal",
                                 "sprint11", "sprint12", "safety"],
                        default=None,
                        help="Run specific phases. Default: all phases. "
                             "stationary=preflight+LED+servo+motors, "
                             "tracking=detection+following+sensors, "
                             "voice=voice command via LiveKit, "
                             "signal=Signal→Robot e2e via OpenClaw webhook, "
                             "sprint11=multi-object+scene+photo+face+named-follow, "
                             "sprint12=battery+status+camera+activity+say+chat+vodom+"
                             "nav+patrol+call+charging+welcome, "
                             "safety=emergency stop test")
    parser.add_argument("--skip-following", action="store_true",
                        help="Skip following sub-phase within tracking")
    parser.add_argument("--skip-camera-check", action="store_true",
                        help="Skip initial camera validation (reuse existing LiveKit session)")
    parser.add_argument("--join-url", action="store_true",
                        help="Print LiveKit join URL and exit")
    args = parser.parse_args()
    if args.phases is None:
        args.phases = ["stationary", "tracking", "voice", "signal",
                       "sprint11", "sprint12", "safety"]

    if args.join_url:
        cam = CameraCapture(room="robot-cam")
        print(cam.generate_join_url())
        return

    sys.exit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
