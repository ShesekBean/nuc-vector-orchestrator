"""Systemd watchdog integration via sd_notify protocol.

Uses raw socket write to $NOTIFY_SOCKET — no external packages needed.
All functions are silent no-ops when $NOTIFY_SOCKET is not set (dev/test).
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("agent-loop")


def _sd_notify(state: str) -> bool:
    """Send a notification to systemd via NOTIFY_SOCKET.

    Returns True if the message was sent, False if no socket is available.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False

    # Abstract socket (starts with @) or file path
    if addr.startswith("@"):
        addr = "\0" + addr[1:]

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode(), addr)
        return True
    except OSError:
        log.debug("sd_notify failed for state=%s", state)
        return False


def notify_ready() -> None:
    """Notify systemd that the service is ready (Type=notify)."""
    if _sd_notify("READY=1"):
        log.debug("sd_notify: READY=1")


def ping() -> None:
    """Send watchdog keepalive ping to systemd."""
    _sd_notify("WATCHDOG=1")
