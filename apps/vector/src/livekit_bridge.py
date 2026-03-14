"""LiveKit WebRTC bridge for Vector camera/mic/speaker.

Publishes Vector's camera feed and mic audio as LiveKit tracks so Ophir
can see and hear through the robot in real-time via a browser or phone
LiveKit client.  Subscribes to remote audio tracks and plays them
through Vector's speaker, enabling bidirectional communication.

Two-way streaming architecture::

    Camera OUT: Vector camera -> CameraClient -> JPEG -> RGBA VideoFrame -> LiveKit
    Mic OUT:    vector-streamer DGRAM proxy -> Opus -> TCP:5555 -> MicChannel -> PCM -> LiveKit
    Audio IN:   LiveKit remote audio -> PCM -> Vector stream_wav_file()
    Video IN:   LiveKit remote video -> downscale 160x80 -> DisplayFaceImageRGB -> Vector OLED

The mic audio pipeline uses vector-streamer (native binary on Vector) which
acts as a DGRAM proxy on mic_sock_cp_mic, intercepts audio from vic-anim
during voice sessions, Opus-encodes it, and streams via TCP to the NUC
where MicChannel decodes it back to PCM for LiveKit publishing.

Usage::

    bridge = LiveKitBridge(
        camera_client=cam,
        audio_client=mic,
        robot=robot,
        event_bus=bus,
        media_service=media,
    )
    await bridge.start(room="robot-cam")
    ...
    await bridge.stop()
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
import tempfile
import wave
from typing import TYPE_CHECKING, Any

from livekit import api as lk_api
from livekit import rtc

from apps.vector.src.events.event_types import LIVEKIT_SESSION, LiveKitSessionEvent

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.media.service import MediaService
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
EMPTY_ROOM_TIMEOUT_INITIAL = 30  # seconds — wait for first participant to join
EMPTY_ROOM_TIMEOUT_REJOIN = 3   # seconds — after last participant leaves

# Flag file — voice proxy checks this to suppress LLM during calls
_CALL_ACTIVE_FLAG = Path("/tmp/livekit-call-active")

# Video-in display settings (remote video -> Vector OLED)
DISPLAY_WIDTH = 160
DISPLAY_HEIGHT = 80
SDK_WIDTH = 184
SDK_HEIGHT = 96
VIDEO_IN_FPS = 10  # Max display update rate


class LiveKitBridge:
    """Bidirectional LiveKit WebRTC session for Vector.

    Args:
        camera_client: CameraClient for video frames.
        audio_client: AudioClient for mic PCM chunks.
        robot: Connected ``anki_vector.Robot`` for speaker playback.
        event_bus: NUC event bus for ``LIVEKIT_SESSION`` events.
        media_service: MediaService providing mic audio channel.
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
        media_service: MediaService | None = None,
        *,
        livekit_url: str = DEFAULT_LIVEKIT_URL,
        api_key: str | None = None,
        api_secret: str | None = None,
        enable_video_in: bool = True,
    ) -> None:
        self._camera = camera_client
        self._audio = audio_client
        self._robot = robot
        self._bus = event_bus
        self._media_service = media_service
        self._livekit_url = livekit_url

        import os
        self._api_key = api_key or os.environ.get("LIVEKIT_API_KEY")
        self._api_secret = api_secret or os.environ.get("LIVEKIT_API_SECRET")

        self._room: rtc.Room | None = None
        self._video_source: rtc.VideoSource | None = None
        self._audio_source: rtc.AudioSource | None = None
        self._apm: rtc.AudioProcessingModule | None = None  # echo cancellation

        self._video_task: asyncio.Task | None = None
        self._audio_pub_task: asyncio.Task | None = None
        self._audio_sub_task: asyncio.Task | None = None

        self._active = False
        self._room_name = ""
        self._should_reconnect = False
        self._playing_audio = False  # guard against overlapping playback
        self._empty_room_timer: asyncio.TimerHandle | None = None
        self._had_participant = False  # True after first participant joins

        # Mic audio subscription (from MediaService mic channel)
        self._mic_sub = None
        self._mic_task: asyncio.Task | None = None

        # Video-in: remote video -> Vector OLED display
        self._enable_video_in = enable_video_in
        self._video_in_task: asyncio.Task | None = None
        self._video_in_active = False

        # Vector-streamer TCP command constants (match protocol.h)
        self._FRAME_TYPE_CMD = 0x20
        self._CMD_MIC_STREAM_START = 0x01
        self._CMD_MIC_STREAM_STOP = 0x02
        self._CMD_MIC_MUTE_CLOUD = 0x03
        self._CMD_MIC_UNMUTE_CLOUD = 0x04

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
        mic_status = {}
        if self._media_service:
            mic_status = self._media_service.mic.get_status()
        return {
            "active": self._active,
            "room": self._room_name,
            "livekit_url": self._livekit_url,
            "mic_channel": mic_status,
            "mic_subscribed": self._mic_sub is not None and not self._mic_sub.closed,
            "video_in_active": self._video_in_active,
        }

    # ------------------------------------------------------------------
    # Token generation
    # ------------------------------------------------------------------

    def _generate_token(self, room: str) -> str:
        """Generate a LiveKit access token for the given room."""
        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "LiveKit api_key and api_secret must be set — either pass them "
                "explicitly or set LIVEKIT_API_KEY / LIVEKIT_API_SECRET env vars"
            )
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

        # Set active before connecting so track_subscribed callbacks
        # (which fire during connect for existing participants) don't
        # see _active=False and exit their loops immediately.
        self._active = True
        _CALL_ACTIVE_FLAG.touch()

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

        # Create echo cancellation + noise suppression processor
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=True,
        )
        logger.info("Audio processing module initialized (AEC + NS + AGC)")

        # Create and publish audio track for mic (fed by MediaService mic channel)
        self._audio_source = rtc.AudioSource(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        audio_track = rtc.LocalAudioTrack.create_audio_track(
            "vector-mic", self._audio_source
        )
        audio_opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self._room.local_participant.publish_track(audio_track, audio_opts)
        logger.info("Published audio track (vector-mic) — fed by MediaService mic channel")

        # Start publishing loops
        self._video_task = asyncio.create_task(self._video_publish_loop())

        # Start mic audio from MediaService channel
        if self._media_service and self._media_service.mic.is_running:
            self._mic_sub = self._media_service.mic.subscribe()
            self._mic_task = asyncio.create_task(self._mic_publish_loop())
            logger.info("Mic audio publishing via MediaService mic channel")
        else:
            logger.warning("MediaService mic channel not available -- no mic audio")

        self._emit_session_event(active=True, room=room)

        # Request continuous mic streaming and mute mic→vic-cloud
        # so wire-pod doesn't process speaker echo during the call
        self._start_mic_streaming()
        self._mute_cloud()

        # Start empty-room timer if nobody else is in the room yet
        if not self._room.remote_participants:
            self._start_empty_room_timer()

    async def _cleanup(self) -> None:
        """Cancel tasks, disconnect from room, emit session end event."""
        self._cancel_empty_room_timer()
        was_active = self._active
        self._active = False
        self._had_participant = False
        _CALL_ACTIVE_FLAG.unlink(missing_ok=True)
        self._video_in_active = False

        # Cancel all tasks
        all_tasks = (
            self._video_task, self._audio_pub_task, self._audio_sub_task,
            self._mic_task, self._video_in_task,
        )
        for task in all_tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._video_task = None
        self._audio_pub_task = None
        self._audio_sub_task = None
        self._mic_task = None
        self._video_in_task = None
        self._apm = None  # release echo cancellation resources

        # Stop continuous mic streaming and unmute mic→vic-cloud
        self._stop_mic_streaming()
        self._unmute_cloud()

        # Close mic subscription
        if self._mic_sub:
            self._mic_sub.close()
            self._mic_sub = None

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
    # Mic streaming control (via vector-streamer TCP commands)
    # ------------------------------------------------------------------

    def _send_streamer_cmd(self, cmd_id: int) -> None:
        """Send a command to vector-streamer via the MediaService TCP socket.

        Uses the same TCP connection that MicChannel uses. The command is
        sent as a framed message: [type=0x20][length=1 LE][cmd_id].
        """
        if not self._media_service:
            logger.warning("No MediaService — cannot send streamer command")
            return

        mic = self._media_service.mic
        sock = getattr(mic, "_sock", None)
        if sock is None:
            logger.warning("MicChannel not connected — cannot send streamer command")
            return

        import struct
        # Frame: [type:1][length:4 LE][payload:1]
        frame = struct.pack("<BI", self._FRAME_TYPE_CMD, 1) + bytes([cmd_id])
        try:
            sock.sendall(frame)
            logger.info("Sent streamer command 0x%02x", cmd_id)
        except Exception:
            logger.warning("Failed to send streamer command 0x%02x", cmd_id, exc_info=True)

    def _start_mic_streaming(self) -> None:
        """Tell vector-streamer to inject StartWakeWordlessStreaming."""
        logger.info("Requesting continuous mic streaming from vector-streamer")
        self._send_streamer_cmd(self._CMD_MIC_STREAM_START)

    def _stop_mic_streaming(self) -> None:
        """Tell vector-streamer to stop injecting StartWakeWordlessStreaming."""
        logger.info("Stopping continuous mic streaming")
        self._send_streamer_cmd(self._CMD_MIC_STREAM_STOP)

    def _mute_cloud(self) -> None:
        """Mute mic→vic-cloud so wire-pod gets no audio during calls."""
        logger.info("Muting mic→vic-cloud (wire-pod silenced)")
        self._send_streamer_cmd(self._CMD_MIC_MUTE_CLOUD)

    def _unmute_cloud(self) -> None:
        """Unmute mic→vic-cloud to restore normal voice pipeline."""
        logger.info("Unmuting mic→vic-cloud")
        self._send_streamer_cmd(self._CMD_MIC_UNMUTE_CLOUD)

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
        """Handle incoming remote tracks — audio for speaker, video for OLED."""
        # Skip our own published tracks (identity starts with "vector-robot")
        if participant.identity.startswith("vector-robot"):
            logger.debug(
                "Ignoring own track from %s", participant.identity
            )
            return

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info(
                "Subscribed to audio track from %s", participant.identity
            )
            if self._audio_sub_task and not self._audio_sub_task.done():
                self._audio_sub_task.cancel()

            self._audio_sub_task = asyncio.create_task(
                self._remote_audio_loop(track, participant.identity)
            )

        elif track.kind == rtc.TrackKind.KIND_VIDEO and self._enable_video_in:
            logger.info(
                "Subscribed to video track from %s — displaying on OLED",
                participant.identity,
            )
            if self._video_in_task and not self._video_in_task.done():
                self._video_in_task.cancel()

            self._video_in_task = asyncio.create_task(
                self._video_in_display_loop(track, participant.identity)
            )
        else:
            logger.debug(
                "Ignoring track kind=%s from %s", track.kind, participant.identity
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

                # Feed remote audio as reference for echo cancellation
                if self._apm is not None:
                    try:
                        self._apm.process_reverse_stream(frame)
                    except Exception:
                        pass  # don't block audio on AEC errors

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
        """A remote participant joined — resume mic streaming and cancel empty-room timer."""
        logger.info("Participant joined: %s", participant.identity)
        self._had_participant = True
        self._cancel_empty_room_timer()
        self._start_mic_streaming()

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        """A remote participant left — stop mic streaming and start empty-room timer."""
        logger.info("Participant left: %s", participant.identity)
        if self._room and not self._room.remote_participants:
            # Stop mic streaming immediately to prevent wire-pod feedback loop
            # (StartWakeWordlessStreaming → silence intents → vic-engine loop)
            self._stop_mic_streaming()
            self._start_empty_room_timer()

    def _start_empty_room_timer(self) -> None:
        """Start a timer that auto-disconnects when room is empty.

        Uses a longer timeout (30s) before anyone has joined (waiting for
        user to open the URL), and a short timeout (3s) after participants
        have already been in the room and left.
        """
        self._cancel_empty_room_timer()
        timeout = EMPTY_ROOM_TIMEOUT_REJOIN if self._had_participant else EMPTY_ROOM_TIMEOUT_INITIAL
        loop = asyncio.get_event_loop()
        logger.info(
            "Room empty — will auto-disconnect in %ds", timeout
        )
        self._empty_room_timer = loop.call_later(
            timeout, lambda: asyncio.ensure_future(self._empty_room_disconnect())
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
        logger.info("No participants — auto-disconnecting")
        await self.stop()
        self._notify_signal_closed()

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
    # Mic audio from MediaService (vector-streamer -> MicChannel -> PCM)
    # ------------------------------------------------------------------

    async def _mic_publish_loop(self) -> None:
        """Read PCM from MediaService mic channel and publish to LiveKit.

        The mic channel receives Opus-encoded audio from vector-streamer
        on Vector, decodes to 16kHz S16LE mono PCM, and delivers via
        a threading.Queue subscription.
        """
        import queue as _queue

        if not self._mic_sub:
            logger.warning("No mic subscription -- mic publish loop exiting")
            return

        logger.info("Mic publish loop started (MediaService mic channel)")
        published_count = 0
        q = self._mic_sub.queue

        try:
            while self._active and not self._mic_sub.closed:
                # Read PCM chunk from subscriber queue (non-blocking)
                chunk: bytes | None = None
                try:
                    chunk = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(AUDIO_PUBLISH_INTERVAL)
                    continue

                if not chunk or len(chunk) < 2:
                    continue

                samples_per_channel = len(chunk) // 2
                try:
                    frame = rtc.AudioFrame(
                        data=chunk,
                        sample_rate=AUDIO_SAMPLE_RATE,
                        num_channels=AUDIO_CHANNELS,
                        samples_per_channel=samples_per_channel,
                    )
                    # Run echo cancellation / noise suppression on mic audio
                    if self._apm is not None:
                        self._apm.process_stream(frame)
                    await self._audio_source.capture_frame(frame)
                    published_count += 1
                    if published_count <= 3 or published_count % 500 == 0:
                        logger.info(
                            "Mic frame #%d (%d bytes PCM, %d samples)",
                            published_count, len(chunk), samples_per_channel,
                        )
                except Exception:
                    logger.debug("Failed to publish mic audio", exc_info=True)

        except asyncio.CancelledError:
            logger.debug("Mic publish loop cancelled")
        except Exception:
            logger.exception("Mic publish loop error")
        finally:
            logger.info("Mic publish loop ended (published %d frames)", published_count)

    # ------------------------------------------------------------------
    # Video-in: remote video → Vector OLED display
    # ------------------------------------------------------------------

    async def _video_in_display_loop(
        self, track: rtc.RemoteTrack, participant_id: str
    ) -> None:
        """Receive remote video frames and display on Vector's OLED.

        Downscales to 160x80, embeds in 184x96 SDK frame, and sends
        via set_screen_with_image_data().
        """
        logger.info("Video-in display loop started for %s", participant_id)
        self._video_in_active = True
        video_stream = rtc.VideoStream(track, format=rtc.VideoBufferType.RGBA)
        frame_count = 0
        min_interval = 1.0 / VIDEO_IN_FPS
        last_display_time = 0.0

        try:
            from PIL import Image as PILImage
            from anki_vector.screen import convert_image_to_screen_data

            async for event in video_stream:
                if not self._active:
                    break

                frame_count += 1
                import time as _time
                now = _time.monotonic()

                # Rate-limit display updates
                if now - last_display_time < min_interval:
                    continue

                try:
                    video_frame: rtc.VideoFrame = event.frame

                    if frame_count <= 3:
                        logger.info(
                            "Video-in frame #%d: %dx%d type=%s",
                            frame_count, video_frame.width, video_frame.height,
                            video_frame.type,
                        )

                    # Convert to PIL Image
                    rgba_data = bytes(video_frame.data)
                    img = PILImage.frombytes(
                        "RGBA",
                        (video_frame.width, video_frame.height),
                        rgba_data,
                    ).convert("RGB")

                    # Resize to fit 160x80 preserving aspect ratio
                    img_w, img_h = img.size
                    scale = min(DISPLAY_WIDTH / img_w, DISPLAY_HEIGHT / img_h)
                    new_w = int(img_w * scale)
                    new_h = int(img_h * scale)
                    resized = img.resize((new_w, new_h), PILImage.LANCZOS)

                    # Create 160x80 canvas centered
                    display = PILImage.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (0, 0, 0))
                    offset_x = (DISPLAY_WIDTH - new_w) // 2
                    offset_y = (DISPLAY_HEIGHT - new_h) // 2
                    display.paste(resized, (offset_x, offset_y))

                    # Embed into 184x96 SDK frame
                    sdk_frame = PILImage.new("RGB", (SDK_WIDTH, SDK_HEIGHT), (0, 0, 0))
                    sdk_frame.paste(display, (0, 0))

                    # Send to Vector's OLED (in executor to avoid blocking)
                    screen_data = convert_image_to_screen_data(sdk_frame)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        self._robot.screen.set_screen_with_image_data,
                        screen_data,
                        0.2,  # duration_sec — short, will be refreshed
                    )

                    last_display_time = now

                    if frame_count <= 3 or frame_count % 100 == 0:
                        logger.info("Displayed video-in frame #%d on OLED", frame_count)

                except Exception:
                    logger.debug("Video-in frame display failed", exc_info=True)

        except asyncio.CancelledError:
            logger.debug("Video-in display loop cancelled")
        except Exception:
            logger.exception("Video-in display loop error")
        finally:
            self._video_in_active = False
            await video_stream.aclose()

            # Restore face animation after video-in ends
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._restore_face)
            except Exception:
                logger.debug("Failed to restore face after video-in", exc_info=True)

            logger.info("Video-in display loop ended (%d frames)", frame_count)

    def _restore_face(self) -> None:
        """Restore face animation after displaying video on OLED.

        DisplayFaceImage permanently disables KeepFaceAlive in vic-anim.
        Playing an animation re-enables KeepFaceAlive and restores the eyes.
        We release control briefly so vic-engine can process the animation,
        then re-acquire OVERRIDE_BEHAVIORS to keep Vector in sit mode.
        """
        import time as _time
        from anki_vector.connection import ControlPriorityLevel
        try:
            logger.info("Restoring face: releasing control + playing animation...")
            self._robot.conn.release_control()
            _time.sleep(0.5)
            self._robot.anim.play_animation("anim_neutral_eyes_01")
            _time.sleep(2.0)
            self._robot.conn.request_control(
                behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
            )
            _time.sleep(0.5)
            logger.info("Face animation restored after video-in")
        except Exception:
            logger.exception("Face restore failed")

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

    @staticmethod
    def _notify_signal_closed() -> None:
        """Send a Signal notification that the video call was closed."""
        import subprocess

        try:
            subprocess.Popen(
                ["bash", "scripts/pgm-signal-gate.sh", "board-status", "0",
                 "📹 Video call ended — no participants."],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.debug("Failed to send Signal notification", exc_info=True)

    def _emit_session_event(self, *, active: bool, room: str) -> None:
        """Emit a LiveKitSessionEvent on the NUC event bus."""
        if self._bus is None:
            return

        event = LiveKitSessionEvent(active=active, room=room)
        self._bus.emit(LIVEKIT_SESSION, event)
        logger.debug("Emitted LiveKitSessionEvent(active=%s, room=%r)", active, room)
