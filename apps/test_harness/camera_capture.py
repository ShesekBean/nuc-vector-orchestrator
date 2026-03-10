#!/usr/bin/env python3
"""camera_capture.py — Capture frames from Ophir's iPhone via LiveKit Cloud.

Connects to a LiveKit Cloud room as the NUC Vision Oracle, subscribes to
the first available video track (Ophir's iPhone camera), and captures frames.

Usage (class-based):
    from monitoring.camera_capture import CameraCapture
    cam = CameraCapture()
    frame_path = cam.capture_and_save("before")

Usage (CLI):
    python3 monitoring/camera_capture.py --before
    python3 monitoring/camera_capture.py --after
    python3 monitoring/camera_capture.py --continuous --interval 5 --count 10
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
Image: Any = None
try:
    from PIL import Image as pil_image
    Image = pil_image
except ModuleNotFoundError:  # pragma: no cover - exercised in CI without pillow
    pass

api: Any = None
rtc: Any = None
try:
    from livekit import api as livekit_api, rtc as livekit_rtc
    api = livekit_api
    rtc = livekit_rtc
except ModuleNotFoundError:  # pragma: no cover - exercised in CI without livekit
    pass

CAPTURES_DIR = Path(__file__).parent / "captures"
ENV_FILE = Path(__file__).parent.parent / ".env.livekit"
DEFAULT_ROOM = "robot-cam"
DEFAULT_IDENTITY = "nuc-vision-oracle"
CONNECT_TIMEOUT = 15


def _require_livekit() -> None:
    if api is None or rtc is None:
        raise ModuleNotFoundError(
            "LiveKit SDK is not installed. Install package 'livekit' to use camera capture."
        )


def _require_pillow() -> None:
    if Image is None:
        raise ModuleNotFoundError(
            "Pillow is not installed. Install package 'pillow' to use camera capture."
        )


def _load_credentials() -> tuple[str, str, str]:
    """Load LiveKit credentials from .env.livekit file."""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"LiveKit credentials not found: {ENV_FILE}")
    creds = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    url = creds.get("LIVEKIT_URL", "")
    key = creds.get("LIVEKIT_API_KEY", "")
    secret = creds.get("LIVEKIT_API_SECRET", "")
    if not all([url, key, secret]):
        raise ValueError("Missing LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET in .env.livekit")
    return url, key, secret


def _generate_token(api_key: str, api_secret: str, room: str, identity: str) -> str:
    """Generate a LiveKit access token."""
    _require_livekit()
    token = api.AccessToken(api_key, api_secret)
    token.with_identity(identity)
    token.with_name("NUC Vision Oracle")
    token.with_grants(api.VideoGrants(room_join=True, room=room))
    return token.to_jwt()


async def _capture_frame_async(url: str, token: str, timeout: float = CONNECT_TIMEOUT) -> bytes:
    """Connect to LiveKit room, capture one frame, return JPEG bytes."""
    _require_livekit()
    _require_pillow()
    room = rtc.Room()
    frame_data: list[bytes] = []
    frame_captured = asyncio.Event()

    @room.on("track_subscribed")
    def on_track(track, publication, participant):
        if isinstance(track, rtc.RemoteVideoTrack):
            asyncio.ensure_future(_read_frame(track))

    async def _read_frame(track):
        stream = rtc.VideoStream(track)
        async for event in stream:
            argb_frame = event.frame.convert(rtc.VideoBufferType.RGBA)
            arr = np.frombuffer(argb_frame.data, dtype=np.uint8).reshape(
                argb_frame.height, argb_frame.width, 4
            )
            img = Image.fromarray(arr[:, :, :3])  # drop alpha
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            frame_data.append(buf.getvalue())
            frame_captured.set()
            break
        await stream.aclose()

    await room.connect(url, token)
    try:
        await asyncio.wait_for(frame_captured.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"No video frame received within {timeout}s — "
            "is Ophir in the LiveKit room with camera enabled?"
        )
    finally:
        await room.disconnect()

    return frame_data[0]


def save_frame(data: bytes, captures_dir: Path, label: str | None = None) -> Path:
    """Save frame data to captures directory with timestamp. Returns saved path."""
    captures_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{label}_{ts}.jpg" if label else f"capture_{ts}.jpg"
    path = captures_dir / filename
    path.write_bytes(data)

    if label:
        latest = captures_dir / f"{label}_latest.jpg"
        tmp = captures_dir / f".{label}_latest.tmp.jpg"
        tmp.write_bytes(data)
        tmp.rename(latest)

    return path


class CameraCapture:
    """Capture frames from LiveKit Cloud room (Ophir's iPhone camera)."""

    def __init__(
        self,
        room: str = DEFAULT_ROOM,
        captures_dir: Path | None = None,
        capture_delay_ms: int = 500,
    ):
        self.room = room
        self.captures_dir = captures_dir or CAPTURES_DIR
        self.capture_delay_ms = capture_delay_ms
        self._url, self._api_key, self._api_secret = _load_credentials()

    def capture(self) -> bytes:
        """Capture a single frame. Returns JPEG bytes."""
        token = _generate_token(self._api_key, self._api_secret, self.room, DEFAULT_IDENTITY)
        return asyncio.run(_capture_frame_async(self._url, token))

    def capture_and_save(self, label: str | None = None) -> Path:
        """Capture a frame and save to disk. Returns the saved file path."""
        data = self.capture()
        return save_frame(data, self.captures_dir, label)

    def delay(self) -> None:
        """Sleep for the configured capture delay (settle time between captures)."""
        time.sleep(self.capture_delay_ms / 1000.0)

    def generate_join_url(self) -> str:
        """Generate a URL for Ophir to join the LiveKit room from browser."""
        token = _generate_token(
            self._api_key, self._api_secret, self.room, "ophir-phone"
        )
        return (
            f"https://meet.livekit.io/custom?"
            f"liveKitUrl={self._url}&token={token}"
        )


def main():
    parser = argparse.ArgumentParser(description="Capture frames from LiveKit Cloud (iPhone camera)")
    parser.add_argument("--before", action="store_true", help="Capture a 'before' baseline frame")
    parser.add_argument("--after", action="store_true", help="Capture an 'after' comparison frame")
    parser.add_argument("--continuous", action="store_true", help="Capture frames continuously")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between continuous captures")
    parser.add_argument("--count", type=int, default=10, help="Number of continuous captures")
    parser.add_argument("--room", type=str, default=DEFAULT_ROOM, help=f"LiveKit room name (default: {DEFAULT_ROOM})")
    parser.add_argument("--output-dir", type=str, default=None, help=f"Output directory (default: {CAPTURES_DIR})")
    parser.add_argument("--join-url", action="store_true", help="Print a join URL for Ophir and exit")

    args = parser.parse_args()

    if args.before and args.after:
        print("Error: --before and --after are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    captures_dir = Path(args.output_dir) if args.output_dir else None
    cam = CameraCapture(room=args.room, captures_dir=captures_dir)

    if args.join_url:
        print(cam.generate_join_url())
        return

    if args.continuous:
        for i in range(args.count):
            try:
                path = cam.capture_and_save(f"frame_{i:04d}")
                print(f"[{i+1}/{args.count}] {path}")
            except (TimeoutError, Exception) as e:
                print(f"[{i+1}/{args.count}] Error: {e}", file=sys.stderr)
            if i < args.count - 1:
                time.sleep(args.interval)
        return

    label = "before" if args.before else ("after" if args.after else None)
    try:
        path = cam.capture_and_save(label)
        data = path.read_bytes()
        print(f"Saved: {path} ({len(data)} bytes)")
    except TimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
