#
# Unit test: the phone on the OLED face (display.CallFace, driven by sip_server, issue
# #111 item 1) and the busy lamp's ringing.
#
# The SIP front-end owns the face's call state: ringing (with who is calling) from
# call.incoming until the call is answered (#111: the brain answers, after the session
# arbiter's grant and a ring head start), active while it is up (re-sent with a ttl),
# and none at EVERY end -- hung up, refused, hung up while
# ringing, a bring-up that failed, a pipeline that ended on its own, the gateway going
# away, the brain stopping. Nothing at all goes to an avatar that does not say "call"
# (an older one logs the whole datagram, caller id and all).
#
# The calls run through sip_server's real connection loop over a real SEQPACKET socket
# (test_sip_call_lifecycle's fake gateway, the pipeline stubbed), and the face is a real
# datagram socket with a features file beside it, as the avatar keeps them.
#
# Run: python test_call_face.py
#
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

import test_sip_call_lifecycle as lc  # noqa: E402
from teaport_brain import display, sip_server  # noqa: E402
from teaport_brain import session_arbiter as arb  # noqa: E402

FROM = '"WIRELESS CALLER" <sip:13462348500@teaspoonai>'
SHOWN = "+1 346 234 8500"
FEATURES = {"screen": 1, "sleep": 1, "call": 1, "unicode": 1}


class FakeFace:
    """The avatar's socket and features file, in a directory of their own."""

    def __init__(self, features=FEATURES):
        self.dir = tempfile.mkdtemp(prefix="teaport-face-test-")
        self.path = os.path.join(self.dir, "face.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.setblocking(False)
        with open(self.path + display.FEATURES_SUFFIX, "w") as f:
            json.dump(features, f)
        self.got = []

    def calls(self):
        """Every call event received so far, in order."""
        while True:
            try:
                ev = json.loads(self.sock.recv(65536))
            except BlockingIOError:
                break
            self.got.append(ev.get("call"))
        return self.got

    def states(self):
        """The call's states as shown, a re-send of the same state counted once."""
        out = []
        for c in self.calls():
            if not out or out[-1] != c["state"]:
                out.append(c["state"])
        return out

    def close(self):
        self.sock.close()
        shutil.rmtree(self.dir, True)


class Rig:
    """A fresh arbiter, a fake face behind sip_server.CALL_FACE, the fake gateway."""

    def __init__(self, features=FEATURES):
        arb.ARBITER = arb.SessionArbiter()
        self.face = FakeFace(features)
        self.h = lc._Harness()

    async def __aenter__(self):
        await self.h.start()
        # After the harness's own (silent) face, which its stop() takes back out.
        sip_server.CALL_FACE = display.CallFace(path=self.face.path, refresh_secs=0.05)
        return self

    async def __aexit__(self, *exc):
        await self.h.stop()
        self.face.close()
        arb.ARBITER = arb.SessionArbiter()

    async def incoming(self, call_id, frm=FROM, replay=False):
        msg = {"type": "call.incoming", "call_id": call_id, "from": frm,
               "to": "<sip:100v@teaspoonai>"}
        if replay:
            msg["replay"] = True
        await self.h.send_raw(msg)

    async def ends_with_none(self):
        """The last event is none, and nothing is sent after it (the refresh stopped)."""
        ok = await lc.wait_until(lambda: self.face.states()[-1:] == ["none"])
        n = len(self.face.calls())
        await asyncio.sleep(0.2)                 # four refresh periods: nothing more
        return ok and len(self.face.calls()) == n


# --- a normal call ------------------------------------------------------------------

async def test_a_call_rings_then_is_active_then_ends():
    async with Rig() as r:
        await r.incoming("A")
        assert await lc.wait_until(lambda: r.face.states() == ["ringing"]), r.face.got
        assert r.face.got[0] == {"state": "ringing", "caller": SHOWN,
                                 "ttl": display.CALL_TTL_SECS}, r.face.got[0]
        assert sip_server.CALL_FACE.state == display.RINGING
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: r.face.states() == ["ringing", "active"]), r.face.got
        n = len(r.face.got)
        assert await lc.wait_until(lambda: len(r.face.calls()) >= n + 2), (
            "an active call is not re-sent: the avatar drops the phone after one ttl")
        assert r.face.got[-1] == {"state": "active", "ttl": display.CALL_TTL_SECS}
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await r.ends_with_none(), r.face.got
        assert r.face.states() == ["ringing", "active", "none"], r.face.states()
        assert r.face.got[-1] == {"state": "none"}


async def test_a_replayed_call_goes_straight_to_active():
    """The brain restarted under a live call: it was answered long ago, so no ringing
    (the caller id full-screen for a call already in progress)."""
    async with Rig() as r:
        await r.incoming("A", replay=True)
        await r.h.call_state("A", "confirmed", replay=True)
        assert await lc.wait_until(lambda: r.face.states() == ["active"]), r.face.got
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await r.ends_with_none()


async def test_a_stale_hangup_leaves_the_newer_call_on_the_face():
    async with Rig() as r:
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["active"])
        await r.incoming("B")                      # the gateway replaces A with B
        await r.h.call_state("B", "confirmed")
        assert await lc.wait_until(lambda: "B" in lc._FakeSession.greeted)
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        await asyncio.sleep(0.3)
        assert r.face.states()[-1] == "active", r.face.states()
        assert sip_server.CALL_FACE.call_id == "B"
        assert "none" not in r.face.states()[r.face.states().index("ringing", 1):], (
            "A's late teardown took B's phone down")


# --- every other end ends with none -------------------------------------------------

async def test_a_refused_call_ends_with_none():
    """An awake room conversation has the box: the caller hears the busy line, and the
    phone comes down before it (it is not the box's call)."""
    saved = sip_server.answer_busy
    busy = []

    async def answer_busy(connection, holder_kind, call_id):
        busy.append((holder_kind, sip_server.CALL_FACE.state))
        await connection.send_control({"type": "call.hangup"})
    sip_server.answer_busy = answer_busy
    try:
        async with Rig() as r:
            room = arb.Claim(arb.ROOM, label="room awake", asleep=lambda: False)
            assert await arb.ARBITER.acquire(room) is None
            await r.incoming("A")
            await r.h.call_state("A", "confirmed")
            assert await lc.wait_until(lambda: busy), "the call was not refused"
            assert busy == [(arb.ROOM, None)], busy       # down before the busy line
            assert await r.ends_with_none(), r.face.got
            assert r.face.states() == ["ringing", "none"], r.face.states()
            await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
            await asyncio.sleep(0.1)
            assert r.face.states() == ["ringing", "none"]  # nothing more on its hangup
            assert lc._FakeSession.built == []
            arb.ARBITER.release(room)
    finally:
        sip_server.answer_busy = saved


async def test_a_call_hung_up_while_ringing_ends_with_none():
    async with Rig() as r:
        await r.incoming("A")
        assert await lc.wait_until(lambda: r.face.states() == ["ringing"])
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await r.ends_with_none()
        assert r.face.states() == ["ringing", "none"]


async def test_a_failed_bring_up_ends_with_none():
    async with Rig() as r:
        def build(transport, **kw):
            raise RuntimeError("no models")
        sip_server.build_agent_session = build      # the harness puts it back
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await r.ends_with_none(), r.face.got
        assert r.face.states() == ["ringing", "active", "none"], r.face.states()
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})


async def test_a_pipeline_that_ends_on_its_own_ends_with_none():
    async with Rig() as r:
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: "A" in lc._FakeSession.greeted)
        await lc._FakeSession.by_id["A"].task.finish()
        assert await r.ends_with_none(), r.face.got


async def test_the_gateway_going_away_mid_call_ends_with_none():
    async with Rig() as r:
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["active"])
        r.h.peer.close()
        r.h.peer = None
        assert await r.ends_with_none(), r.face.got


async def test_the_brain_stopping_mid_call_ends_with_none():
    async with Rig() as r:
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["active"])
        r.h.run_task.cancel()                       # serve() cancelled at shutdown
        await asyncio.gather(r.h.run_task, return_exceptions=True)
        assert await r.ends_with_none(), r.face.got


async def test_a_second_call_unanswered_leaves_the_first_on_the_face():
    """A is up, B rings over it (the gateway's second INVITE) and is cancelled before it
    is answered: the face goes back to A's handset, still re-sent, not to none -- A's
    caller is still on the line. Routine once calls ring before they are answered."""
    async with Rig() as r:
        await r.incoming("A")
        await r.h.call_state("A", "confirmed")
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["active"])
        await r.incoming("B")
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["ringing"])
        await r.h.send_raw({"type": "call.state", "call_id": "B", "state": "disconnected"})
        assert await lc.wait_until(lambda: r.face.states()[-1:] == ["active"]), r.face.states()
        assert r.face.states() == ["ringing", "active", "ringing", "active"], r.face.states()
        assert sip_server.CALL_FACE.call_id == "A" and arb.ARBITER.call_live()
        n = len(r.face.calls())
        assert await lc.wait_until(lambda: len(r.face.calls()) >= n + 2), (
            "A's handset is not re-sent after B went: it drops off after one ttl")
        assert r.face.got[-1] == {"state": "active", "ttl": display.CALL_TTL_SECS}
        await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await r.ends_with_none(), r.face.got


def test_a_ringing_call_that_ends_after_the_one_it_covers_takes_the_phone_down():
    """The covered call hangs up first (nothing shows: B still rings), then B goes
    unanswered: none, not a return to the call that is gone."""
    sent = []
    face = display.CallFace(features=lambda path: {"call": 1},
                            send=lambda ev, path: sent.append(ev["call"]["state"]))
    face.active("A")
    face.ringing("B", "Bob")
    face.end("A")
    assert face.state == display.RINGING and face.call_id == "B"
    face.end("B")
    assert face.state is None and sent[-1] == "none", sent
    face.active("A")                                 # and answered, B replaces A
    face.ringing("B", "Bob")
    face.active("B")
    face.end("A")                                    # A's teardown, behind B's answer
    assert face.state == display.ACTIVE and face.call_id == "B"
    face.end("B")
    assert face.state is None and sent[-1] == "none", sent


async def test_a_long_caller_name_is_cut_to_what_the_avatar_draws():
    """The avatar reads each datagram with recvfrom(4096): a far end's long name, \\u-
    escaped, used to overflow it, and the whole ringing event was dropped."""
    # Within the gateway's 2048-byte frame (sip_transport.MAX_FRAME_BYTES) as the test
    # sends it (\\u-escaped); the gateway itself sends UTF-8, so ~680 CJK characters.
    for name in ("中" * 300, "Z" * 1500, "\U0001F600" * 150):
        async with Rig() as r:
            await r.incoming("A", frm=f'"{name}" <sip:1@x>')
            assert await lc.wait_until(lambda: r.face.calls()), "the ringing event was lost"
            r.face.sock.setblocking(True)
            assert r.face.got[0]["caller"] == name[:display.CALLER_MAX_CHARS]
            raw = json.dumps({"call": r.face.got[0]}, ensure_ascii=False).encode()
            assert len(raw) < display.DATAGRAM_MAX_BYTES, len(raw)
            r.face.sock.setblocking(False)
            await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
            assert await r.ends_with_none()


def test_a_datagram_fits_the_avatars_read_and_survives_a_lone_surrogate():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "face.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind(path)
            assert display.send({"call": {"state": "ringing", "caller": "中" * 48}}, path)
            data = s.recv(display.DATAGRAM_MAX_BYTES)
            assert json.loads(data.decode())["call"]["caller"] == "中" * 48
            assert len(data) < 200                    # UTF-8, not six-byte escapes
            assert display.send({"call": {"state": "ringing", "caller": "a\ud800b"}}, path)
            assert json.loads(s.recv(4096).decode())["call"]["caller"] == "a?b"
            # The reviewer's case: a 700-character CJK name through CallFace itself.
            face = display.CallFace(path=path, features=lambda p: {"call": 1, "unicode": 1})
            face.ringing("A", "中" * 700)
            face.end("A")
            data = s.recv(display.DATAGRAM_MAX_BYTES)
            assert json.loads(data.decode())["call"]["caller"] == "中" * display.CALLER_MAX_CHARS
            assert json.loads(s.recv(4096).decode()) == {"call": {"state": "none"}}


# --- the avatar's features ------------------------------------------------------------

async def test_nothing_is_sent_to_an_avatar_without_calls():
    for features in ({"screen": 1, "sleep": 1}, {}):
        async with Rig(features) as r:
            await r.incoming("A")
            await r.h.call_state("A", "confirmed")
            assert await lc.wait_until(lambda: "A" in lc._FakeSession.greeted)
            await asyncio.sleep(0.15)               # refreshes, had there been any
            await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
            assert await lc.wait_until(lambda: sip_server.CALL_FACE.state is None)
            await asyncio.sleep(0.1)
            assert r.face.calls() == [], r.face.got


async def test_a_caller_name_is_folded_for_an_avatar_without_fallback_fonts():
    for features, frm, want in (
            ({"call": 1}, '"Zoë Ñúñez" <sip:1@x>', "Zoe Nunez"),
            ({"call": 1, "unicode": 1}, '"Zoë Ñúñez" <sip:1@x>', "Zoë Ñúñez"),
            ({"call": 1}, '"東京" <sip:alice@x>', None)):  # nothing left: the handset alone
        async with Rig(features) as r:
            await r.incoming("A", frm=frm)
            assert await lc.wait_until(lambda: r.face.calls())
            assert r.face.got[0].get("caller") == want, (frm, r.face.got[0])
            await r.h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
            assert await r.ends_with_none()


# --- the caller id ----------------------------------------------------------------------

def test_caller_id_formatting():
    for frm, want in (
            (FROM, SHOWN),                                            # a placeholder name
            ('"Alice Smith" <sip:13462348500@teaspoonai>', "Alice Smith"),
            ('Alice Smith <sip:13462348500@teaspoonai>;tag=abc', "Alice Smith"),
            ('"UNKNOWN" <sip:+13462348500@x>', SHOWN),
            ('"Anonymous" <sip:anonymous@anonymous.invalid>', None),
            ('<sip:anonymous@anonymous.invalid>;tag=1', None),
            ('"13462348500" <sip:13462348500@x>', SHOWN),             # a name that is the number
            ('<sip:13462348500@x>', SHOWN),
            ('sip:13462348500@x;tag=1', SHOWN),
            ('<tel:+1-346-234-8500>', SHOWN),
            ('<sip:+442071234567@x>', "+442071234567"),               # not NANP: as sent
            ('<sip:3462348500@x>', "3462348500"),                     # 10 digits: as sent
            ('<sip:alice@example.com>', "alice"),
            ('"O\\"Brien" <sip:1@x>', 'O"Brien'),
            ('"  wireless   caller " <sip:13462348500@x>', SHOWN),
            ('"Unavailable" <sip:+13462348500@x>', SHOWN),           # no name: the number
            ('"Out of area" <sip:13462348500@x>', SHOWN),
            ('"Unknown" <sip:unknown@x>', None),
            ('<sip:%2B13462348500@x>', SHOWN),
            ('<sip:+13462348500;user=phone@x>', SHOWN),
            # A withheld display name is a placeholder too: the number, when there is one
            # (the box works for its owner); the handset alone only when there is none.
            ('"ANONYMOUS" <sip:+13462348500@x>', SHOWN),
            ('"Restricted" <sip:13462348500@x>', SHOWN),
            ('"private number" <sip:13462348500@x>', SHOWN),
            ('"Withheld" <sip:13462348500@x>', SHOWN),
            ('"Blocked" <sip:13462348500@x>', SHOWN),
            ('"Caller ID blocked" <sip:13462348500@x>', SHOWN),
            ('"No Caller ID" <sip:13462348500@x>', SHOWN),
            ('<sip:Restricted@x>', None),                             # nothing identifying
            ('"Anonymous" <sip:anonymous@x>', None),
            ('"Private" <sip:unavailable@x>', None),
            ('"Blocked" <sip:@x>', None),
            ('"Withheld" <sip:bob.jones@x>', "bob.jones"),             # the user identifies
            ('"WIRELESS CALLER" <sip:anonymous@anonymous.invalid>', None),
            ('"Alice" <sip:13462348500@anonymous.invalid>', "Alice"),   # a name is a name
            ('"Alice" <sip:anonymous@x>', "Alice"),
            ("", None), (None, None), (42, None)):
        got = sip_server.caller_id(frm)
        assert got == want, (frm, got, want)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
