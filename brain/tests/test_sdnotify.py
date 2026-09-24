#
# Unit test: the brain's systemd readiness notification (teaport_brain.sdnotify).
#
# Under a Type=notify unit, `systemctl restart` waits for READY=1 and fails the start if
# it never comes, so both halves matter: the datagram must reach systemd's socket (a
# filesystem path, or an abstract one that $NOTIFY_SOCKET spells with a leading @), and
# outside systemd — no $NOTIFY_SOCKET — it must do nothing at all. A send that fails must
# not raise into a brain that is already serving.
#
# The gateway front-end is checked end to end: READY=1 arrives only once uvicorn has
# bound the port (an ASGI startup hook would fire before the bind), and never when the
# bind fails.
#
# Run: python test_sdnotify.py
#
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import sdnotify  # noqa: E402


class _Env:
    """Set (or, with None, unset) $NOTIFY_SOCKET for the duration of a block."""

    def __init__(self, value):
        self.value, self.saved = value, None

    def __enter__(self):
        self.saved = os.environ.pop("NOTIFY_SOCKET", None)
        if self.value is not None:
            os.environ["NOTIFY_SOCKET"] = self.value

    def __exit__(self, *exc):
        os.environ.pop("NOTIFY_SOCKET", None)
        if self.saved is not None:
            os.environ["NOTIFY_SOCKET"] = self.saved


def _listener(addr: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(addr)
    s.settimeout(5.0)
    return s


def test_unset_is_a_no_op():
    with _Env(None):
        assert sdnotify.ready() is False
    with _Env(""):
        assert sdnotify.ready() is False


def test_path_socket_gets_ready():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "notify")
        with _listener(path) as srv, _Env(path):
            assert sdnotify.ready() is True
            assert srv.recv(64) == b"READY=1"


def test_abstract_socket_gets_ready():
    name = f"teaport-sdnotify-test-{os.getpid()}-{time.monotonic_ns()}"
    with _listener("\0" + name) as srv, _Env("@" + name):
        assert sdnotify.ready() is True
        assert srv.recv(64) == b"READY=1"


def test_a_dead_socket_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        with _Env(os.path.join(d, "nobody-listening")):
            assert sdnotify.ready() is False


def _run_gateway(port: int):
    """Run gateway_server's server on a thread. Returns (server, thread, outcome) where
    outcome["exit"] records a SystemExit (uvicorn exits on a failed bind)."""
    from teaport_brain import gateway_server
    import uvicorn

    server = gateway_server._ReadyServer(
        uvicorn.Config(gateway_server.app, host="127.0.0.1", port=port, log_level="warning"))
    outcome = {}

    def go():
        try:
            server.run()
        except SystemExit as e:
            outcome["exit"] = e.code

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return server, t, outcome


def test_gateway_is_ready_only_once_bound():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "notify")
        with _listener(path) as srv, _Env(path):
            server, t, _ = _run_gateway(0)
            try:
                assert srv.recv(64) == b"READY=1"
                # At READY the port must already accept connections.
                port = server.servers[0].sockets[0].getsockname()[1]
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            finally:
                server.should_exit = True
                t.join(10)
            assert not t.is_alive(), "server did not stop"


def test_gateway_bind_failure_is_not_ready():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "notify")
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with _listener(path) as srv, _Env(path):
                server, t, outcome = _run_gateway(port)
                t.join(20)
                assert not t.is_alive(), "server did not give up on a port in use"
                assert "exit" in outcome and not server.started, outcome
                srv.settimeout(0.2)
                try:
                    got = srv.recv(64)
                except socket.timeout:
                    got = None
                assert got is None, f"notified {got!r} although the bind failed"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
