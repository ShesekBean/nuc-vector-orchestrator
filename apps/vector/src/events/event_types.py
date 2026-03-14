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
EXPRESSION_CHANGED = "expression_changed"
INTERCOM_TEXT_SENT = "intercom_text_sent"
INTERCOM_PHOTO_SENT = "intercom_photo_sent"
USER_INTENT = "user_intent"
MESSAGE_RELAYED = "message_relayed"
OBSTACLE_DETECTED = "obstacle_detected"
HEAD_ANGLE_COMMAND = "head_angle_command"
IMU_UPDATE = "imu_update"
NAV_STATE_CHANGED = "nav_state_changed"
NAV_RESULT = "nav_result"
WAYPOINT_SAVED = "waypoint_saved"
PATROL_EVENT = "patrol_event"
PRESENCE_CHANGED = "presence_changed"


# --- Payload dataclasses ----------------------------------------------------

@dataclass(frozen=True)
class YoloPersonDetectedEvent:
    """Emitted by YOLO detector when a person is detected in a camera frame."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    frame_width: int = 800
    frame_height: int = 600


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
    frame_width: int = 800
    frame_height: int = 600


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


@dataclass(frozen=True)
class ExpressionChangedEvent:
    """Emitted when the expression engine transitions to a new emotion."""

    emotion: str  # "happy", "curious", "sad", "excited", "sleepy", "startled", "idle"
    previous_emotion: str | None = None
    trigger: str = ""  # event or API call that caused the change


@dataclass(frozen=True)
class IntercomTextSentEvent:
    """Emitted when a text message is sent to Ophir via Signal."""

    text: str
    success: bool


@dataclass(frozen=True)
class IntercomPhotoSentEvent:
    """Emitted when a photo is sent to Ophir via Signal."""

    caption: str
    success: bool


@dataclass(frozen=True)
class UserIntentEvent:
    """Emitted when wire-pod processes a voice command into a structured intent."""

    intent: str  # e.g. "intent_imperative_forward", "intent_greeting_hello"
    params: dict = field(default_factory=dict)  # intent-specific parameters


@dataclass(frozen=True)
class ObstacleDetectedEvent:
    """Emitted by ObstacleDetector when an obstacle is in the forward cone."""

    zone: str  # "danger", "caution", "clear"
    proximity: float  # 0.0 (far) to 1.0 (very close)
    speed_scale: float  # 0.0 (full stop) to 1.0 (full speed)
    bbox_area_ratio: float  # obstacle bbox area / frame area
    label: str = ""  # COCO class label if available
    frame_width: int = 800
    frame_height: int = 600


@dataclass(frozen=True)
class HeadAngleCommandEvent:
    """Emitted by HeadTracker when a head angle command is issued."""

    angle_deg: float
    source: str = "head_tracker"  # "head_tracker", "search", "manual"


@dataclass(frozen=True)
class ImuUpdateEvent:
    """Emitted by ImuPoller with raw IMU sensor data from Vector SDK."""

    accel_x: float  # G
    accel_y: float  # G
    accel_z: float  # G
    gyro_x: float  # deg/s
    gyro_y: float  # deg/s
    gyro_z: float  # deg/s


@dataclass(frozen=True)
class NavStateChangedEvent:
    """Emitted by NavController when navigation state changes."""

    state: str  # "idle", "planning", "navigating", "arrived", "blocked", "mapping"
    previous_state: str | None = None
    target_waypoint: str | None = None


@dataclass(frozen=True)
class NavResultEvent:
    """Emitted by NavController when navigation completes or fails."""

    success: bool
    message: str
    target_name: str = ""
    final_x: float = 0.0
    final_y: float = 0.0
    final_theta: float = 0.0


@dataclass(frozen=True)
class WaypointSavedEvent:
    """Emitted when a waypoint is saved."""

    name: str
    x: float
    y: float
    theta: float
    is_update: bool = False


@dataclass(frozen=True)
class MessageRelayedEvent:
    """Emitted when a voice message is relayed to Ophir via Signal."""

    original_text: str  # full STT transcription
    extracted_message: str  # message body sent to Ophir
    success: bool  # whether Intercom delivery succeeded


@dataclass(frozen=True)
class PatrolEventPayload:
    """Emitted by HomeGuardian during patrol for activity tracking."""

    event_type: str  # "patrol_start", "waypoint_arrived", "person_detected", etc.
    waypoint: str = ""
    details: str = ""
    person_name: str = ""


@dataclass(frozen=True)
class PresenceChangedEvent:
    """Emitted by PresenceTracker when someone arrives, departs, or triggers a check-in."""

    signal: str  # "arrival", "departure", "still_present", "touch", "goodnight"
    person_name: str  # "ophir", "smadara", "unknown"
    is_present: bool
    first_today: bool = False
    away_duration_s: float = 0.0
    session_duration_s: float = 0.0
    engagement_score: float = 0.0
    interactions_today: int = 0
