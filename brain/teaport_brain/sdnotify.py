#
# sdnotify.py — tell systemd the brain is serving (sd_notify READY=1), stdlib only.
#
# Under a Type=notify unit, systemd sets $NOTIFY_SOCKET and holds `systemctl start` /
# `restart` until the service sends READY=1 on it, failing the start after
# TimeoutStartSec= if it never does. That turns "did the new brain come up?" into the
# exit status of the restart itself. Each front-end calls ready() at the point it can
# serve: gateway_server once uvicorn has bound its port, sip_server once it is connected
# to the gateway's socket.
#
# Everywhere else — a Type=simple unit, a shell, a test — $NOTIFY_SOCKET is unset and
# ready() does nothing. Not libsystemd or a PyPI package: the protocol is one datagram.
#
import os
import socket
import sys


def notify(state: str) -> bool:
    """Send `state` (e.g. "READY=1") to $NOTIFY_SOCKET. False when it is unset or the
    send fails; never raises, because a brain that is serving must not die over this."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):  # an abstract socket: systemd writes its leading NUL as @
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError as e:
        print(f"sdnotify: could not send {state!r} to {addr!r}: {e}", file=sys.stderr)
        return False
    return True


def ready() -> bool:
    return notify("READY=1")
