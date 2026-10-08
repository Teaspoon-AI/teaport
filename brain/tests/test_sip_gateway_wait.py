#
# Unit test: the SIP brain waits for the gateway's socket instead of failing on the
# first connect.
#
# Under systemd the brain unit is After= the gateway unit, but the gateway is
# Type=simple: systemd calls it started the moment it is forked, a second or more
# before PJSUA2 is up and the UDS is listening. run() used to connect exactly once, so
# every boot and every `teaport sip restart` raised FileNotFoundError, failed the
# unit, and left it to Restart= to come back 5 s later with a traceback in the journal
# for a socket that was about to appear (#41). _connect_when_listening retries the two
# "not there yet" errors until the socket accepts, bounded by _GATEWAY_WAIT_S so a
# gateway that is genuinely absent still fails the unit and hands the pacing back to
# Restart=. The bound is a real deadline, and it must not swallow a PermissionError:
# that is a different gateway (wrong peer uid), not a slow one.
#
# Run: python test_sip_gateway_wait.py
#
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from teaport_brain import sip_server  # noqa: E402


def _bind_after(path: str, delay_s: float) -> threading.Thread:
    """Bind + listen a SEQPACKET socket at `path` after `delay_s`, on a thread, the way
    a gateway that is still initializing would."""
    def go():
        time.sleep(delay_s)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        srv.bind(path)
        srv.listen(1)
        srv.settimeout(5.0)
        try:
            conn, _ = srv.accept()
            conn.close()
        except socket.timeout:
            pass
        finally:
            srv.close()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


async def test_connects_once_the_gateway_binds_late():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gw.sock")
        t = _bind_after(path, 0.4)
        t0 = time.monotonic()
        sock = await sip_server._connect_when_listening(path)
        waited = time.monotonic() - t0
        sock.close()
        t.join(5)
    assert waited >= 0.3, f"connected before the socket existed?! waited={waited:.2f}s"
    assert waited < sip_server._GATEWAY_WAIT_S, f"waited {waited:.2f}s — did it hit the bound?"


async def test_gives_up_at_the_deadline():
    saved = sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S
    sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S = 0.3, 0.05
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "never.sock")
            t0 = time.monotonic()
            try:
                await sip_server._connect_when_listening(path)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("connected to a socket that was never bound")
            waited = time.monotonic() - t0
        assert 0.25 <= waited < 2.0, f"deadline not honoured: waited {waited:.2f}s for a 0.3 s bound"
    finally:
        sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S = saved


async def test_a_wrong_peer_is_not_retried():
    """PermissionError (the peer-uid check) is raised at once — it is a different
    process on our socket path, and waiting would only hand it more time."""
    from teaport_brain import sip_transport

    saved = sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S, sip_transport._verify_peer_uid
    sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S = 5.0, 0.05

    def reject(sock, path):
        raise PermissionError(f"refusing {path}: peer uid is not ours")
    sip_transport._verify_peer_uid = reject
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "squat.sock")
            t = _bind_after(path, 0.0)
            time.sleep(0.2)
            t0 = time.monotonic()
            try:
                await sip_server._connect_when_listening(path)
            except PermissionError:
                pass
            else:
                raise AssertionError("a wrong-uid peer was accepted")
            waited = time.monotonic() - t0
            t.join(5)
        assert waited < 1.0, f"PermissionError was retried for {waited:.2f}s"
    finally:
        sip_server._GATEWAY_WAIT_S, sip_server._GATEWAY_POLL_S, sip_transport._verify_peer_uid = saved


# --- inside teaport-brain: serve() never gives up, and never takes Talk down ----------

async def _serving(path, connections, fail=False):
    """serve() on `path`, its connections counted by a stub run_connection."""
    async def run_connection(sock, on_ready=None):
        connections.append(sock)
        if fail:
            raise RuntimeError("a bug in the SIP front-end")
        loop = asyncio.get_running_loop()
        while await loop.sock_recv(sock, 64):           # until the gateway hangs up
            pass
        sock.close()
    saved = (sip_server.run_connection, sip_server._SERVE_POLL_S)
    sip_server.run_connection, sip_server._SERVE_POLL_S = run_connection, 0.05
    return saved, asyncio.create_task(sip_server.serve(path))


async def test_serve_waits_for_the_gateway_and_comes_back_after_it_restarts():
    """No socket: it looks, quietly, for as long as it takes. A gateway that goes away
    (restarted on its own: the units no longer tie it to the brain) is connected to
    again once it is back -- teaport-sip#3 replays a call in progress to it."""
    connections = []
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gw.sock")
        saved, task = await _serving(path, connections)
        try:
            await asyncio.sleep(0.3)
            assert connections == [] and not task.done()          # telephony off: dormant
            assert sip_server.status() == "waiting"               # what /health reports
            for n in (1, 2):                                       # up, down, up again
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                srv.bind(path)
                srv.listen(1)
                srv.setblocking(False)
                loop = asyncio.get_running_loop()
                peer, _ = await asyncio.wait_for(loop.sock_accept(srv), 3)
                for _ in range(100):
                    if len(connections) == n:
                        break
                    await asyncio.sleep(0.02)
                assert len(connections) == n and sip_server.status() == "connected"
                peer.close()                                       # the gateway stops
                srv.close()
                os.unlink(path)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sip_server.run_connection, sip_server._SERVE_POLL_S = saved
        assert sip_server.status() == "off"


async def test_a_failing_sip_front_end_is_contained_and_starts_over():
    connections = []
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gw.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        srv.bind(path)
        srv.listen(8)
        saved, task = await _serving(path, connections, fail=True)
        try:
            for _ in range(100):
                if len(connections) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert len(connections) >= 2 and not task.done()       # it raised, and went on
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sip_server.run_connection, sip_server._SERVE_POLL_S = saved
            srv.close()


async def test_an_empty_socket_setting_turns_the_sip_front_end_off():
    await asyncio.wait_for(sip_server.serve(""), 1)


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
