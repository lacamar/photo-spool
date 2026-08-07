"""Minimal sd_notify(3) client -- just enough to send READY=1 once startup
finishes and WATCHDOG=1 on a periodic timer, so systemd (running this app
as a Type=notify user service) can tell "still alive" apart from "hung" and
restart it automatically, instead of a wedged GUI thread sitting there
forever as a false "active (running)".

Deliberately a raw AF_UNIX datagram send rather than a python3-systemd
dependency -- the whole protocol is one write() to the socket path in
$NOTIFY_SOCKET (see systemd's sd_notify(3) man page), not worth a new
package dependency for."""
from __future__ import annotations

import os
import socket


def notify(state: str) -> None:
    """No-op if NOTIFY_SOCKET isn't set (e.g. running via `just run` from a
    terminal, not under systemd) or if the send fails for any reason --
    this must never be allowed to affect the app itself either way."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass
