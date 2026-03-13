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

# Auto-disconnect when no remote participants for this long (seconds)
EMPTY_ROOM_TIMEOUT = 3 * 60  # 3 minutes


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
        self._audio_source: rtc.AudioSource | None = None

        self._video_task: asyncio.Task | None = None
        self._audio_pub_task: asyncio.Task | None = None
        self._audio_sub_task: asyncio.Task | None = None

        self._active = False
        self._room_name = ""
        self._should_reconnect = False
        self._playing_audio = False  # guard against overlapping playback
        self._empty_room_timer: asyncio.TimerHandle | None = None

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
        import time as _time
        token.with_identity(f"vector-robot-{int(_time.time())}")
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
        self._room.on("participant_connected", self._on_participant_connected)
        self._room.on("participant_disconnected", self._on_participant_disconnected)

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

        # Start publishing loops (video only)
        self._video_task = asyncio.create_task(self._video_publish_loop())

        self._active = True
        self._emit_session_event(active=True, room=room)

        # Start empty-room timer if nobody else is in the room yet
        if not self._room.remote_participants:
            self._start_empty_room_timer()

    async def _cleanup(self) -> None:
        """Cancel tasks, disconnect from room, emit session end event."""
        self._cancel_empty_room_timer()
        was_active = self._active
        self._active = False

        # Cancel publishing tasks
        for task in (self._video_task, self._audio_pub_task, self._audio_sub_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._video_task = None
        self._audio_pub_task = None
        self._audio_sub_task = None

        # Disconnect from room
        if self._room:
            await self._room.disconnect()
            self._room = None

        self._video_source = None
        self._audio_source = None

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

    async def _audio_publish_loop(self) -> None:
        """Read mic PCM from AudioClient subscriber queue and publish."""
        import queue as _queue

        logger.info("Audio publish loop started")
        q: _queue.Queue[bytes] = _queue.Queue(maxsize=200)
        self._audio.subscribe_queue(q)
        published_count = 0
        try:
            while self._active:
                # Drain all available chunks without blocking the event loop
                chunk: bytes | None = None
                try:
                    chunk = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(AUDIO_PUBLISH_INTERVAL)
                    continue

                if chunk and len(chunk) >= 2:
                    try:
                        frame = self._pcm_to_audio_frame(chunk)
                        if frame and self._audio_source:
                            await self._audio_source.capture_frame(frame)
                            published_count += 1
                            if published_count <= 3 or published_count % 500 == 0:
                                logger.info(
                                    "Published audio frame #%d (%d bytes PCM)",
                                    published_count, len(chunk),
                                )
                    except Exception:
                        logger.warning("Failed to publish audio frame", exc_info=True)
        except asyncio.CancelledError:
            logger.debug("Audio publish loop cancelled")
        except Exception:
            logger.exception("Audio publish loop error")
        finally:
            self._audio.unsubscribe_queue()
            logger.info("Audio publish loop stopped (published %d frames)", published_count)

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

        # Skip our own published tracks (identity starts with "vector-robot")
        if participant.identity.startswith("vector-robot"):
            logger.debug(
                "Ignoring own audio track from %s", participant.identity
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
        """Receive remote audio frames and play on Vector speaker.

        Skips silent chunks to avoid blocking the SDK event loop (which
        starves the camera feed of behavior-control time).
        """
        logger.info("Remote audio loop started for %s", participant_id)
        audio_stream = rtc.AudioStream(track)
        pcm_buffer = bytearray()
        remote_sample_rate = 0
        frame_count = 0

        # Silence detection threshold — ignore chunks below this amplitude
        SILENCE_THRESHOLD = 30  # skip near-silence to avoid blocking camera feed

        try:
            async for event in audio_stream:
                if not self._active:
                    break

                frame: rtc.AudioFrame = event.frame
                frame_count += 1
                if frame_count <= 3:
                    logger.info(
                        "Remote audio frame #%d: sample_rate=%d, channels=%d, "
                        "samples_per_channel=%d, data_len=%d",
                        frame_count, frame.sample_rate, frame.num_channels,
                        frame.samples_per_channel, len(bytes(frame.data)),
                    )
                if remote_sample_rate == 0:
                    remote_sample_rate = frame.sample_rate
                pcm_buffer.extend(bytes(frame.data))

                # Accumulate ~2 seconds of audio before playing
                flush_bytes = remote_sample_rate * 2 * 2  # 2 sec of 16-bit mono
                if len(pcm_buffer) >= flush_bytes:
                    # Check if buffer contains actual audio (not silence)
                    if self._pcm_has_signal(pcm_buffer, SILENCE_THRESHOLD) and not self._playing_audio:
                        # Fire-and-forget: don't await so we don't block
                        # the audio receive loop. Playback runs in background.
                        asyncio.ensure_future(
                            self._play_pcm_on_vector(bytes(pcm_buffer), sample_rate=remote_sample_rate)
                        )
                    pcm_buffer.clear()

            # Flush remaining audio
            if pcm_buffer and remote_sample_rate:
                if self._pcm_has_signal(pcm_buffer, SILENCE_THRESHOLD) and not self._playing_audio:
                    asyncio.ensure_future(
                        self._play_pcm_on_vector(bytes(pcm_buffer), sample_rate=remote_sample_rate)
                    )
        except asyncio.CancelledError:
            logger.debug("Remote audio loop cancelled")
        except Exception:
            logger.exception("Remote audio loop error")
        finally:
            await audio_stream.aclose()

    async def _play_pcm_on_vector(self, pcm_data: bytes, sample_rate: int = AUDIO_SAMPLE_RATE) -> None:
        """Write PCM to a temp WAV file and stream to Vector speaker.

        Vector only supports 8000-16025 Hz.  If *sample_rate* is higher,
        downsample to 16 000 Hz via linear interpolation before playing.
        """
        if not pcm_data:
            return

        if self._playing_audio:
            logger.debug("Skipping playback — already playing audio")
            return

        self._playing_audio = True
        loop = asyncio.get_running_loop()

        def _write_and_play() -> None:
            import struct as _struct

            play_rate = sample_rate
            frames = pcm_data

            # Downsample to 16 kHz if needed (Vector max is 16025 Hz)
            if sample_rate > 16025:
                n_src = len(pcm_data) // 2
                if n_src == 0:
                    return
                src = _struct.unpack(f"<{n_src}h", pcm_data)
                ratio = sample_rate / AUDIO_SAMPLE_RATE
                n_dst = int(n_src / ratio)
                dst = []
                for i in range(n_dst):
                    pos = i * ratio
                    idx = int(pos)
                    frac = pos - idx
                    if idx + 1 < n_src:
                        sample = src[idx] + frac * (src[idx + 1] - src[idx])
                    else:
                        sample = src[idx]
                    dst.append(max(-32768, min(32767, int(round(sample)))))
                frames = _struct.pack(f"<{len(dst)}h", *dst)
                play_rate = AUDIO_SAMPLE_RATE

            # Amplify quiet mic signal — LiveKit audio is typically very low
            # amplitude (~100 out of 32767).  Apply gain to make it audible.
            _GAIN = 20
            n_samples = len(frames) // 2
            if n_samples > 0:
                samples = _struct.unpack(f"<{n_samples}h", frames)
                amplified = [max(-32768, min(32767, s * _GAIN)) for s in samples]
                frames = _struct.pack(f"<{n_samples}h", *amplified)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp, "wb") as wf:
                    wf.setnchannels(AUDIO_CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(play_rate)
                    wf.writeframes(frames)

            try:
                self._robot.audio.stream_wav_file(tmp_path, volume=100)
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
        finally:
            self._playing_audio = False

    # ------------------------------------------------------------------
    # Room event handlers
    # ------------------------------------------------------------------

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        """A remote participant joined — cancel empty-room timer."""
        logger.info("Participant joined: %s", participant.identity)
        self._cancel_empty_room_timer()

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        """A remote participant left — start empty-room timer if room is empty."""
        logger.info("Participant left: %s", participant.identity)
        if self._room and not self._room.remote_participants:
            self._start_empty_room_timer()

    def _start_empty_room_timer(self) -> None:
        """Start a timer that auto-disconnects after EMPTY_ROOM_TIMEOUT."""
        self._cancel_empty_room_timer()
        loop = asyncio.get_event_loop()
        logger.info(
            "Room empty — will auto-disconnect in %ds", EMPTY_ROOM_TIMEOUT
        )
        self._empty_room_timer = loop.call_later(
            EMPTY_ROOM_TIMEOUT, lambda: asyncio.ensure_future(self._empty_room_disconnect())
        )

    def _cancel_empty_room_timer(self) -> None:
        """Cancel the empty-room auto-disconnect timer."""
        if self._empty_room_timer is not None:
            self._empty_room_timer.cancel()
            self._empty_room_timer = None

    async def _empty_room_disconnect(self) -> None:
        """Auto-disconnect after the room has been empty for too long."""
        self._empty_room_timer = None
        # Double-check room is still empty
        if self._room and self._room.remote_participants:
            logger.info("Empty-room timer fired but room has participants — ignoring")
            return
        logger.info("No participants for %ds — auto-disconnecting", EMPTY_ROOM_TIMEOUT)
        await self.stop()

    def _on_disconnected(self, reason: str | None = None) -> None:
        """Handle LiveKit room disconnection."""
        logger.warning("LiveKit room disconnected: %s", reason)
        self._cancel_empty_room_timer()
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

    @staticmethod
    def _pcm_has_signal(pcm_data: bytes | bytearray, threshold: int = 200) -> bool:
        """Return True if PCM buffer contains audio above *threshold*."""
        import struct as _struct

        n = len(pcm_data) // 2
        if n == 0:
            return False
        # Sample every 50th sample for speed
        step = max(1, n // 200)
        for i in range(0, n, step):
            sample = _struct.unpack_from("<h", pcm_data, i * 2)[0]
            if abs(sample) > threshold:
                return True
        return False

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
