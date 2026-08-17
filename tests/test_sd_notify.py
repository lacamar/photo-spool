"""Exercises sd_notify.notify() against a real local AF_UNIX datagram
socket standing in for systemd's actual $NOTIFY_SOCKET -- no real systemd
involved, just the same protocol it expects on the receiving end."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from backend import sd_notify


class SdNotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="photo-spool-test-"))
        self.sock_path = self.tmp / "notify.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.bind(str(self.sock_path))
        self.server.settimeout(2)
        self._old_notify_socket = os.environ.get("NOTIFY_SOCKET")

    def tearDown(self):
        self.server.close()
        if self._old_notify_socket is None:
            os.environ.pop("NOTIFY_SOCKET", None)
        else:
            os.environ["NOTIFY_SOCKET"] = self._old_notify_socket
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sends_the_given_state_string(self):
        os.environ["NOTIFY_SOCKET"] = str(self.sock_path)
        sd_notify.notify("READY=1")
        data, _addr = self.server.recvfrom(1024)
        self.assertEqual(data, b"READY=1")

    def test_watchdog_state(self):
        os.environ["NOTIFY_SOCKET"] = str(self.sock_path)
        sd_notify.notify("WATCHDOG=1")
        data, _addr = self.server.recvfrom(1024)
        self.assertEqual(data, b"WATCHDOG=1")

    def test_no_notify_socket_set_is_a_silent_no_op(self):
        os.environ.pop("NOTIFY_SOCKET", None)
        sd_notify.notify("READY=1")  # must not raise
        with self.assertRaises(socket.timeout):
            self.server.settimeout(0.1)
            self.server.recvfrom(1024)

    def test_unreachable_socket_is_swallowed_not_raised(self):
        os.environ["NOTIFY_SOCKET"] = str(self.tmp / "does-not-exist.sock")
        sd_notify.notify("READY=1")  # must not raise


if __name__ == "__main__":
    unittest.main()
