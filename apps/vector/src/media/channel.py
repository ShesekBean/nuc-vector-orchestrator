"""Base MediaChannel with thread-safe fan-out to subscribers.

Each channel produces data (PCM chunks, video frames, etc.) and
distributes it to all active subscribers via queues.  Supports both
``threading.Queue`` and ``asyncio.Queue`` subscribers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Default ring buffer size (number of chunks)
DEFAULT_RING_SIZE = 100

# Max items in subscriber queue before dropping
DEFAULT_QUEUE_SIZE = 200


class ChannelSubscription:
    """Handle returned by :meth:`MediaChannel.subscribe`.

    Provides a queue to read data from and a ``close()`` method to
    unsubscribe.  Can also be used as a context manager::

        with channel.subscribe() as sub:
            while True:
                chunk = sub.queue.get(timeout=1)
                ...
    """

    def __init__(
        self,
        queue: Any,  # threading.Queue or asyncio.Queue
        channel: MediaChannel,
        is_async: bool = False,
    ) -> None:
        self.queue = queue
        self._channel = channel
        self._is_async = is_async
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Unsubscribe from the channel."""
        if not self._closed:
            self._closed = True
            self._channel._remove_subscriber(self)

    def __enter__(self) -> ChannelSubscription:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class MediaChannel:
    """Base class for media channels.

    Subclasses implement ``start()`` / ``stop()`` and call ``_publish()``
    to fan data out to all subscribers.
    """

    def __init__(self, name: str, ring_size: int = DEFAULT_RING_SIZE) -> None:
        self.name = name
        self._ring: deque[bytes] = deque(maxlen=ring_size)
        self._subscribers: list[ChannelSubscription] = []
        self._lock = threading.Lock()
        self._chunk_count = 0
        self._start_time = 0.0
        self._running = False

    # -- Public API ---------------------------------------------------------

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_latest(self) -> bytes | None:
        """Return the most recent chunk without blocking, or None."""
        with self._lock:
            if self._ring:
                return self._ring[-1]
        return None

    def subscribe(
        self, *, async_queue: bool = False, maxsize: int = DEFAULT_QUEUE_SIZE
    ) -> ChannelSubscription:
        """Create a new subscription.

        Args:
            async_queue: If True, use ``asyncio.Queue`` instead of
                ``threading.Queue``.
            maxsize: Maximum items in the subscriber queue.

        Returns:
            A :class:`ChannelSubscription` whose ``.queue`` attribute
            is the queue to read from.
        """
        if async_queue:
            q: Any = asyncio.Queue(maxsize=maxsize)
        else:
            import queue
            q = queue.Queue(maxsize=maxsize)

        sub = ChannelSubscription(q, self, is_async=async_queue)
        with self._lock:
            self._subscribers.append(sub)
        logger.debug("Channel %s: new subscriber (total=%d, async=%s)",
                     self.name, len(self._subscribers), async_queue)
        return sub

    def start(self) -> None:
        """Start the channel.  Override in subclass."""
        self._start_time = time.monotonic()
        self._running = True

    def stop(self) -> None:
        """Stop the channel.  Override in subclass."""
        self._running = False

    def get_status(self) -> dict:
        """Return channel status dict."""
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        return {
            "name": self.name,
            "running": self._running,
            "chunk_count": self._chunk_count,
            "subscriber_count": self.subscriber_count,
            "chunks_per_second": round(self._chunk_count / elapsed, 1) if elapsed > 1 else 0,
            "ring_size": len(self._ring),
        }

    # -- Internal -----------------------------------------------------------

    def _publish(self, data: bytes) -> None:
        """Fan out *data* to all subscribers and the ring buffer.

        Called by subclasses from their data-producing thread/task.
        """
        self._chunk_count += 1

        with self._lock:
            self._ring.append(data)
            dead: list[ChannelSubscription] = []

            for sub in self._subscribers:
                if sub.closed:
                    dead.append(sub)
                    continue
                try:
                    if sub._is_async:
                        # asyncio.Queue -- use put_nowait
                        sub.queue.put_nowait(data)
                    else:
                        # threading.Queue -- non-blocking put
                        sub.queue.put_nowait(data)
                except (asyncio.QueueFull, Exception):
                    # Drop oldest and retry
                    try:
                        sub.queue.get_nowait()
                        sub.queue.put_nowait(data)
                    except Exception:
                        pass

            for d in dead:
                self._subscribers.remove(d)

    def _remove_subscriber(self, sub: ChannelSubscription) -> None:
        with self._lock:
            try:
                self._subscribers.remove(sub)
                logger.debug("Channel %s: subscriber removed (total=%d)",
                             self.name, len(self._subscribers))
            except ValueError:
                pass
