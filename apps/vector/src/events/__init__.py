"""Hybrid event system: Vector SDK events + NUC-side event bus."""

from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.events.event_types import (
    EmergencyStopEvent,
    FaceRecognizedEvent,
    FollowStateChangedEvent,
    LiveKitSessionEvent,
    MotorCommandEvent,
    SttResultEvent,
    CommandReceivedEvent,
    TtsPlayingEvent,
    YoloPersonDetectedEvent,
)
from apps.vector.src.events.sdk_events import SdkEventBridge

__all__ = [
    "NucEventBus",
    "SdkEventBridge",
    "CommandReceivedEvent",
    "EmergencyStopEvent",
    "FaceRecognizedEvent",
    "FollowStateChangedEvent",
    "LiveKitSessionEvent",
    "MotorCommandEvent",
    "SttResultEvent",
    "TtsPlayingEvent",
    "YoloPersonDetectedEvent",
]
