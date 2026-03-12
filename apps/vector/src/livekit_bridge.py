"""LiveKit WebRTC bridge for Vector camera/mic/speaker.

Publishes Vector's camera feed and mic audio as LiveKit tracks so Ophir
can see and hear through the robot in real-time via a browser or phone
LiveKit client.  Subscribes to remote audio tracks and plays them
through Vector's speaker, enabling bidirectional communication.

Data flow::

    Vector camera → CameraClient → JPEG → decode → RGBA VideoFrame → LiveKit
    Vector mic    → AudioClient  → PCM  → AudioFrame → LiveKit
    LiveKit remote audio → PCM → Vector say_text() / stream_wav_file()

Usage::

    bridge = LiveKitBridge(
        camera_client=cam,
        audio_client=mic,
        robot=robot,
        event_bus=bus,
    )
    await bridge.start(room="robot-cam")
    ...
    await bridge.stop()
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import wave
from typing import TYPE_CHECKING, Any

from livekit import api as lk_api
from livekit import rtc

from apps.vector.src.events.event_types import LIVEKIT_SESSION, LiveKitSessionEvent

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.voice.audio_client import AudioClient

logger = logging.getLogger(__name__)

# LiveKit Cloud URL (from vision_config.yaml / CLAUDE.md)
DEFAULT_LIVEKIT_URL = "wss://robot-a1hmnzgn.livekit.cloud"
DEFAULT_ROOM = "robot-cam"

# Camera frame dimensions (Vector camera)
FRAME_WIDTH = 640
FRAME_HEIGHT = 360

# Audio settings — Vector mic resampled to 16 kHz mono
AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1

# Publishing intervals
VIDEO_PUBLISH_INTERVAL = 1.0 / 15  # ~15 fps to match camera feed
AUDIO_PUBLISH_INTERVAL = 0.02  # 20ms chunks (standard WebRTC)

# Reconnect settings
MAX_RECONNECT_DELAY = 30.0
INITIAL_RECONNECT_DELAY = 1.0


class LiveKitBridge:
    """Bidirectional LiveKit WebRTC session for Vector.

    Args:
        camera_client: CameraClient for video frames.
        audio_client: AudioClient for mic PCM chunks.
        robot: Connected ``anki_vector.Robot`` for speaker playback.
        event_bus: NUC event bus for ``LIVEKIT_SESSION`` events.
        livekit_url: LiveKit Cloud server URL.
        api_key: LiveKit API key (or ``LIVEKIT_API_KEY`` env var).
        api_secret: LiveKit API secret (or ``LIVEKIT_API_SECRET`` env var).
    """

    def __init__(
        self,
        camera_client: CameraClient,
        audio_client: AudioClient,
        robot: Any,
        event_bus: NucEventBus | None = None,
        *,
        livekit_url: str = DEFAULT_LIVEKIT_URL,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._camera = camera_client
        self._audio = audio_client
        self._robot = robot
        self._bus = event_bus
        self._livekit_url = livekit_url
        self._api_key = api_key
        self._api_secret = api_secret

        self._room: rtc.Room | None = None
        self._video_source: rtc.VideoSource | None = None

        self._video_task: asyncio.Task | None = None
        self._audio_sub_task: asyncio.Task | None = None

        self._active = False
        self._room_name = ""
        self._should_reconnect = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Whether a LiveKit session is currently running."""
        return self._active

    @property
    def room_name(self) -> str:
        """Current or last room name."""
        return self._room_name

    async def start(self, room: str = DEFAULT_ROOM) -> None:
        """Connect to LiveKit Cloud and start publishing tracks.

        Args:
            room: LiveKit room name to join.
        """
        if self._active:
            logger.warning("LiveKit session already active in room %r", self._room_name)
            return

        self._room_name = room
        self._should_reconnect = True

        token = self._generate_token(room)
        await self._connect_and_publish(token, room)

    async def stop(self) -> None:
        """Disconnect from LiveKit and stop all publishing tasks."""
        self._should_reconnect = False
        await self._cleanup()

    async def get_status(self) -> dict:
        """Return current session status as a dict."""
        return {
            "active": self._active,
            "room": self._room_name,
            "livekit_url": self._livekit_url,
        }

    # ------------------------------------------------------------------
    # Token generation
    # ------------------------------------------------------------------

    def _generate_token(self, room: str) -> str:
        """Generate a LiveKit access token for the given room."""
        token = lk_api.AccessToken(
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        token.with_identity("vector-robot")
        token.with_grants(
            lk_api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
            )
        )
        return token.to_jwt()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _connect_and_publish(self, token: str, room: str) -> None:
        """Connect to the room and start publishing video + audio."""
        self._room = rtc.Room()

        # Register event handlers before connecting
        self._room.on("disconnected", self._on_disconnected)
        self._room.on("track_subscribed", self._on_track_subscribed)

        logger.info("Connecting to LiveKit room %r at %s", room, self._livekit_url)
        await self._room.connect(self._livekit_url, token)
        logger.info("Connected to LiveKit room %r", room)

        # Create and publish video track
        self._video_source = rtc.VideoSource(FRAME_WIDTH, FRAME_HEIGHT)
        video_track = rtc.LocalVideoTrack.create_video_track(
            "vector-camera", self._video_source
        )
        video_opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
        await self._room.local_participant.publish_track(video_track, video_opts)
        logger.info("Published video track (vector-camera)")

        # NOTE: Vector mic audio is NOT published to LiveKit.
        # The SDK's AudioFeed only provides signal_power (a 980Hz calibration
        # tone), not actual microphone PCM.  Raw mic capture requires
        # Qualcomm ADSP routing which is not accessible via the SDK.
        # Audio flows ONE-WAY: user speaks → LiveKit → Vector speaker.
        logger.info("Mic audio NOT published (SDK AudioFeed provides signal_power, not raw PCM)")

        # Start publishing loops (video only — mic audio not available via SDK)
        self._video_task = asyncio.create_task(self._video_publish_loop())

        self._active = True
        self._emit_session_event(active=True, room=room)

    async def _cleanup(self) -> None:
        """Cancel tasks, disconnect from room, emit session end event."""
        was_active = self._active
        self._active = False

        # Cancel publishing tasks
        for task in (self._video_task, self._audio_sub_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._video_task = None
        self._audio_sub_task = None

        # Disconnect from room
        if self._room:
            await self._room.disconnect()
            self._room = None

        self._video_source = None

        if was_active:
            self._emit_session_event(active=False, room=self._room_name)
            logger.info("LiveKit session stopped (room=%r)", self._room_name)

    # ------------------------------------------------------------------
    # Publishing loops
    # ------------------------------------------------------------------

    async def _video_publish_loop(self) -> None:
        """Continuously capture camera frames and publish as video."""
        logger.info("Video publish loop started")
        last_frame_count = self._camera.frame_count
        published_count = 0
        try:
            while self._active:
                current_count = self._camera.frame_count
                if current_count > last_frame_count:
                    last_frame_count = current_count
                    jpeg = self._camera.get_latest_jpeg()
                    if jpeg:
                        try:
                            frame = self._jpeg_to_video_frame(jpeg)
                            if frame and self._video_source:
                                self._video_source.capture_frame(frame)
                                published_count += 1
                                if published_count <= 3 or published_count % 100 == 0:
                                    logger.info("Published video frame #%d (%dx%d, %d bytes jpeg)", published_count, frame.width, frame.height, len(jpeg))
                        except Exception:
                            logger.warning("Failed to convert/publish video frame", exc_info=True)

                await asyncio.sleep(VIDEO_PUBLISH_INTERVAL)
        except asyncio.CancelledError:
            logger.debug("Video publish loop cancelled")
        except Exception:
            logger.exception("Video publish loop error")

    # ------------------------------------------------------------------
    # Remote audio subscription
    # ------------------------------------------------------------------

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        """Handle incoming remote tracks — subscribe to audio for playback."""
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            logger.debug(
                "Ignoring non-audio track from %s", participant.identity
            )
            return

        logger.info(
            "Subscribed to audio track from %s", participant.identity
        )
        if self._audio_sub_task and not self._audio_sub_task.done():
            self._audio_sub_task.cancel()

        self._audio_sub_task = asyncio.create_task(
            self._remote_audio_loop(track, participant.identity)
        )

    async def _remote_audio_loop(
        self, track: rtc.RemoteTrack, participant_id: str
    ) -> None:
        """Receive remote audio frames and play on Vector speaker."""
        logger.info("Remote audio loop started for %s", participant_id)
        audio_stream = rtc.AudioStream(track)
        pcm_buffer = bytearray()
        # Accumulate ~1 second of audio before playing to reduce overhead
        flush_bytes = AUDIO_SAMPLE_RATE * 2  # 1 sec of 16-bit mono at 16kHz

        try:
            async for event in audio_stream:
                if not self._active:
                    break

                frame: rtc.AudioFrame = event.frame
                pcm_buffer.extend(frame.data)

                if len(pcm_buffer) >= flush_bytes:
                    await self._play_pcm_on_vector(bytes(pcm_buffer))
                    pcm_buffer.clear()

            # Flush remaining audio
            if pcm_buffer:
                await self._play_pcm_on_vector(bytes(pcm_buffer))
        except asyncio.CancelledError:
            logger.debug("Remote audio loop cancelled")
        except Exception:
            logger.exception("Remote audio loop error")
        finally:
            await audio_stream.aclose()

    async def _play_pcm_on_vector(self, pcm_data: bytes) -> None:
        """Write PCM to a temp WAV file and stream to Vector speaker."""
        if not pcm_data:
            return

        loop = asyncio.get_running_loop()

        def _write_and_play() -> None:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp, "wb") as wf:
                    wf.setnchannels(AUDIO_CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(AUDIO_SAMPLE_RATE)
                    wf.writeframes(pcm_data)

            try:
                self._robot.audio.stream_wav_file(tmp_path)
            finally:
                import os
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        try:
            await loop.run_in_executor(None, _write_and_play)
        except Exception:
            logger.debug("Failed to play remote audio on Vector", exc_info=True)

    # ------------------------------------------------------------------
    # Room event handlers
    # ------------------------------------------------------------------

    def _on_disconnected(self, reason: str | None = None) -> None:
        """Handle LiveKit room disconnection."""
        logger.warning("LiveKit room disconnected: %s", reason)
        self._active = False
        self._emit_session_event(active=False, room=self._room_name)

        if self._should_reconnect:
            asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        delay = INITIAL_RECONNECT_DELAY
        while self._should_reconnect and not self._active:
            logger.info("LiveKit reconnect attempt in %.1fs...", delay)
            await asyncio.sleep(delay)
            if not self._should_reconnect:
                break

            try:
                token = self._generate_token(self._room_name)
                await self._connect_and_publish(token, self._room_name)
                logger.info("Reconnected to LiveKit room %r", self._room_name)
                return
            except Exception:
                logger.warning("LiveKit reconnect failed", exc_info=True)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    # ------------------------------------------------------------------
    # Frame conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _jpeg_to_video_frame(jpeg_bytes: bytes) -> rtc.VideoFrame | None:
        """Decode JPEG bytes to an RGBA ``VideoFrame``."""
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(jpeg_bytes))
            # Resize to match VideoSource dimensions
            if img.size != (FRAME_WIDTH, FRAME_HEIGHT):
                img = img.resize((FRAME_WIDTH, FRAME_HEIGHT))
            rgba = img.convert("RGBA")
            width, height = rgba.size
            return rtc.VideoFrame(
                width=width,
                height=height,
                type=rtc.VideoBufferType.RGBA,
                data=rgba.tobytes(),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Event bus
    # ------------------------------------------------------------------

    def _emit_session_event(self, *, active: bool, room: str) -> None:
        """Emit a LiveKitSessionEvent on the NUC event bus."""
        if self._bus is None:
            return

        event = LiveKitSessionEvent(active=active, room=room)
        self._bus.emit(LIVEKIT_SESSION, event)
        logger.debug("Emitted LiveKitSessionEvent(active=%s, room=%r)", active, room)
