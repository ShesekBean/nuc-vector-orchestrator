"""Typed payload dataclasses for NUC-side events.

Every event emitted on the NucEventBus uses one of these payloads so
subscribers get structured data instead of raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --- Event name constants ---------------------------------------------------

YOLO_PERSON_DETECTED = "yolo_person_detected"
FACE_RECOGNIZED = "face_recognized"
FOLLOW_STATE_CHANGED = "follow_state_changed"
MOTOR_COMMAND = "motor_command"
STT_RESULT = "stt_result"
COMMAND_RECEIVED = "command_received"
TTS_PLAYING = "tts_playing"
LIVEKIT_SESSION = "livekit_session"
EMERGENCY_STOP = "emergency_stop"


# --- Payload dataclasses ----------------------------------------------------

@dataclass(frozen=True)
class YoloPersonDetectedEvent:
    """Emitted by YOLO detector when a person is detected in a camera frame."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    frame_width: int = 640
    frame_height: int = 360


@dataclass(frozen=True)
class FaceRecognizedEvent:
    """Emitted by NUC face recognizer (YuNet + SFace)."""

    name: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class FollowStateChangedEvent:
    """Emitted by follow planner when tracking state changes."""

    state: str  # "idle", "searching", "tracking", "following"
    target_name: str | None = None


@dataclass(frozen=True)
class MotorCommandEvent:
    """Emitted by follow planner to drive motors."""

    left_speed_mmps: float
    right_speed_mmps: float
    duration_ms: int = 0


@dataclass(frozen=True)
class SttResultEvent:
    """Emitted when speech-to-text produces a transcription."""

    text: str
    confidence: float = 1.0
    language: str = "en"


@dataclass(frozen=True)
class CommandReceivedEvent:
    """Emitted by command router when a valid command is parsed."""

    command: str
    source: str  # "voice", "signal", "sdk_intent"
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TtsPlayingEvent:
    """Emitted when TTS audio starts or stops playing."""

    playing: bool
    text: str = ""


@dataclass(frozen=True)
class LiveKitSessionEvent:
    """Emitted when a LiveKit session starts or ends."""

    active: bool
    room: str = ""
    participant: str = ""


@dataclass(frozen=True)
class EmergencyStopEvent:
    """Emitted when an emergency stop is triggered (cliff, connection loss, etc.)."""

    source: str  # "cliff", "connection_lost", "manual"
    details: str = ""
