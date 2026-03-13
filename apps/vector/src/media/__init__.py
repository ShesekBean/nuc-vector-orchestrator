"""Media streaming channels for Vector.

Provides MediaService (singleton) managing channels that connect
to vector-streamer on the robot and deliver decoded media to subscribers.
"""

from apps.vector.src.media.channel import MediaChannel, ChannelSubscription
from apps.vector.src.media.mic_channel import MicChannel
from apps.vector.src.media.service import MediaService

__all__ = [
    "MediaService",
    "MediaChannel",
    "MicChannel",
    "ChannelSubscription",
]
