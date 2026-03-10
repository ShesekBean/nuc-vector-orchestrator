"""Tests for systemd watchdog integration."""

from __future__ import annotations

import os
import socket
import threading
from unittest import mock

from apps.control_plane.agent_loop.watchdog import _sd_notify, notify_ready, ping


class TestSdNotify:
    """Test sd_notify protocol."""

    def test_noop_without_socket(self) -> None:
        """No-op when NOTIFY_SOCKET is not set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NOTIFY_SOCKET", None)
            assert _sd_notify("READY=1") is False

    def test_sends_to_unix_socket(self, tmp_path) -> None:
        """Sends datagram to a real Unix socket."""
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)

        received: list[bytes] = []

        def recv() -> None:
            data = server.recv(256)
            received.append(data)

        t = threading.Thread(target=recv)
        t.start()

        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": sock_path}):
            result = _sd_notify("WATCHDOG=1")

        t.join(timeout=2)
        server.close()

        assert result is True
        assert received == [b"WATCHDOG=1"]

    def test_abstract_socket_conversion(self) -> None:
        """Abstract sockets (starting with @) are converted to null prefix."""
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "@/test/socket"}):
            with mock.patch("socket.socket") as mock_sock:
                mock_instance = mock.MagicMock()
                mock_sock.return_value.__enter__ = mock.MagicMock(return_value=mock_instance)
                mock_sock.return_value.__exit__ = mock.MagicMock(return_value=False)
                _sd_notify("READY=1")
                mock_instance.sendto.assert_called_once_with(
                    b"READY=1", "\0/test/socket"
                )

    def test_handles_socket_error(self) -> None:
        """Returns False on socket errors."""
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "/nonexistent/path"}):
            assert _sd_notify("READY=1") is False


class TestHighLevelFunctions:
    """Test notify_ready and ping wrappers."""

    def test_notify_ready_calls_sd_notify(self) -> None:
        with mock.patch("apps.control_plane.agent_loop.watchdog._sd_notify") as m:
            m.return_value = True
            notify_ready()
            m.assert_called_once_with("READY=1")

    def test_ping_calls_sd_notify(self) -> None:
        with mock.patch("apps.control_plane.agent_loop.watchdog._sd_notify") as m:
            ping()
            m.assert_called_once_with("WATCHDOG=1")
