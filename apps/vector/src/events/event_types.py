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
CLIFF_TRIGGERED = "cliff_triggered"
TOUCH_DETECTED = "touch_detected"
TRACKED_PERSON = "tracked_person"
SCENE_DESCRIPTION = "scene_description"
WAKE_WORD_DETECTED = "wake_word_detected"
LIFT_HEIGHT_CHANGED = "lift_height_changed"
LED_STATE_CHANGED = "led_state_changed"
SLAM_POSE_UPDATED = "slam_pose_updated"


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


@dataclass(frozen=True)
class CliffTriggeredEvent:
    """Emitted when a cliff sensor detects an edge."""

    cliff_flags: int  # bitmask of triggered cliff sensors
    timestamp_ms: float = 0.0


@dataclass(frozen=True)
class TouchDetectedEvent:
    """Emitted when the capacitive touch sensor on Vector's head is touched."""

    location: str = "head"  # Vector only has head touch
    is_pressed: bool = True


@dataclass(frozen=True)
class TrackedPersonEvent:
    """Emitted by Kalman tracker — smoothed detection at prediction rate."""

    track_id: int
    cx: float
    cy: float
    width: float  # frozen at last measurement
    height: float  # frozen at last measurement
    age_frames: int  # total frames since track creation
    hits: int  # number of YOLO measurements received
    confidence: float  # last YOLO confidence
    frame_width: int = 640
    frame_height: int = 360


@dataclass(frozen=True)
class SceneDescriptionEvent:
    """Emitted when a scene description is generated from camera + YOLO + LLM."""

    description: str
    detection_count: int
    detection_labels: tuple[str, ...] = ()
    timestamp: float = 0.0


@dataclass(frozen=True)
class WakeWordDetectedEvent:
    """Emitted when a wake word is detected (SDK or openwakeword)."""

    model: str  # e.g. "hey_vector_sdk", "hey_jarvis"
    confidence: float
    source: str  # "sdk" or "openwakeword"
    source_direction: int = -1  # beamforming direction (0-11), -1 if unknown


@dataclass(frozen=True)
class LiftHeightChangedEvent:
    """Emitted when the lift moves to a new height."""

    height: float  # normalised 0.0 (down) to 1.0 (full up)
    preset: str | None = None  # preset name if used, else None


@dataclass(frozen=True)
class LedStateChangedEvent:
    """Emitted when the LED controller changes the active visual state."""

    state: str  # "idle", "searching", "person_detected", "following", etc.
    previous_state: str | None = None


@dataclass(frozen=True)
class SlamPoseUpdatedEvent:
    """Emitted by VisualSLAM when pose is updated from a camera frame."""

    x: float  # world position mm
    y: float  # world position mm
    theta: float  # heading radians (CCW positive)
    feature_matches: int  # ORB feature matches this frame
    process_time_ms: float  # ORB processing time
    landmark_count: int  # total stored landmarks
    loop_closures: int  # total loop closures detected
    free_cells: int  # free cells in occupancy grid
